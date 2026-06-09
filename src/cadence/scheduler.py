"""The captain: scheduling logic and the daemon loop.

`decide` is a pure function (config + state + clock -> next action) so the
timing logic is testable without BLE. `Captain` executes actions against a live
DeskClient: warnings, safety-checked moves, and manual-move detection. `run`
is the daemon entry point.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from . import notify, protocol, safety
from .ble import DeskClient
from .config import Config
from .state import State, load as load_state, save as save_state

log = logging.getLogger("cadence.scheduler")

POLL_CAP_SECONDS = 30.0  # never sleep longer than this so control intents land fast

_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def opposite(posture: str) -> str:
    return "stand" if posture == "sit" else "sit"


def target_inches_for(cfg: Config, posture: str) -> float:
    return cfg.heights.stand_inches if posture == "stand" else cfg.heights.sit_inches


def phase_seconds_for(cfg: Config, posture: str) -> float:
    minutes = cfg.schedule.stand_minutes if posture == "stand" else cfg.schedule.sit_minutes
    return minutes * 60.0


def nearest_posture(cfg: Config, inches: float | None) -> str:
    if inches is None:
        return "sit"  # safe default; we won't move on establish anyway
    if abs(inches - cfg.heights.stand_inches) < abs(inches - cfg.heights.sit_inches):
        return "stand"
    return "sit"


# --- Working hours -----------------------------------------------------------

def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def within_working_hours(cfg: Config, now: float) -> bool:
    wh = cfg.working_hours
    if not wh.enabled:
        return True
    dt = datetime.fromtimestamp(now)
    day = _DAY_NAMES[dt.weekday()]
    if day not in [d.lower() for d in wh.days]:
        return False
    cur = dt.hour * 60 + dt.minute
    return _minutes(wh.start) <= cur < _minutes(wh.end)


# --- Pure decision -----------------------------------------------------------

@dataclass
class Action:
    kind: str  # "sleep" | "establish" | "transition"
    seconds: float = 0.0
    target_posture: str | None = None
    reason: str = ""


def decide(cfg: Config, state: State, now: float) -> Action:
    """Decide the next action. Never performs side effects."""
    if not state.enabled or not cfg.schedule.enabled or state.paused:
        return Action("sleep", POLL_CAP_SECONDS, reason="disabled/paused")

    if not within_working_hours(cfg, now):
        return Action("sleep", POLL_CAP_SECONDS, reason="outside working hours")

    # Manual one-shot: force the next transition now.
    if state.pending == "next":
        cur = state.posture if state.posture in ("sit", "stand") else "sit"
        return Action("transition", target_posture=opposite(cur), reason="manual next")

    if state.snooze_until and now < state.snooze_until:
        return Action("sleep", min(state.snooze_until - now, POLL_CAP_SECONDS), reason="snoozed")

    # First armed tick: adopt the current posture without moving.
    if state.posture not in ("sit", "stand") or state.phase_started_at is None:
        return Action("establish", reason="adopt current posture on start")

    elapsed = now - state.phase_started_at
    remaining = phase_seconds_for(cfg, state.posture) - elapsed
    if remaining > 0:
        return Action("sleep", min(remaining, POLL_CAP_SECONDS), reason="phase in progress")

    return Action(
        "transition",
        target_posture=opposite(state.posture),
        reason=f"{state.posture} phase elapsed",
    )


# --- Live execution ----------------------------------------------------------

class Captain:
    def __init__(self, cfg: Config, client: DeskClient):
        self.cfg = cfg
        self.client = client
        self._commanded_move = False  # suppress manual-move detection during our moves

    def _inches(self, raw: int | None) -> float | None:
        if raw is None:
            return None
        cal = self.cfg.calibration
        return protocol.raw_to_inches(raw, cal.raw_units_per_inch, cal.offset_inches)

    def current_inches(self) -> float | None:
        r = self.client.latest_height
        return self._inches(r.raw if r else None)

    async def warn(self, target_posture: str) -> None:
        w = self.cfg.warning
        msg = f"Desk moving to {target_posture} in {int(self.cfg.schedule.warning_seconds)}s"
        if w.mode in ("notify", "both"):
            notify.notify("cadence", msg, sound=w.sound)
        if w.mode in ("tap", "both") and w.enabled:
            await self._tap()

    async def _tap(self) -> None:
        """Small physical nudge: up by delta, back down, repeated. Skipped if
        height is unknown (we never issue blind relative moves)."""
        cur = self.current_inches()
        if cur is None:
            log.warning("skipping physical tap: current height unknown")
            return
        w = self.cfg.warning
        offset = self.cfg.calibration.offset_inches
        up = protocol.inches_to_goto_mm(cur + w.tap_delta_inches, offset)
        back = protocol.inches_to_goto_mm(cur, offset)
        self._commanded_move = True
        try:
            for _ in range(max(1, w.tap_count)):
                await self.client.goto_mm(up)
                await asyncio.sleep(w.tap_pause_ms / 1000)
                await self.client.goto_mm(back)
                await asyncio.sleep(w.tap_pause_ms / 1000)
        finally:
            self._commanded_move = False

    async def move_to(self, target_inches: float, *, settle_timeout: float = 40.0) -> None:
        """Issue a goto and hold the commanded-move flag until motion settles,
        so our own height stream isn't mistaken for a manual move."""
        mm = protocol.inches_to_goto_mm(target_inches, self.cfg.calibration.offset_inches)
        self._commanded_move = True
        try:
            await self.client.goto_mm(mm)
            last_raw = None
            stable = 0.0
            waited = 0.0
            while waited < settle_timeout and stable < 2.0:
                await asyncio.sleep(0.5)
                waited += 0.5
                cur = self.client.latest_height.raw if self.client.latest_height else None
                if cur == last_raw:
                    stable += 0.5
                else:
                    stable = 0.0
                    last_raw = cur
        finally:
            self._commanded_move = False

    async def transition(self, state: State, target_posture: str, reason: str) -> bool:
        """Warn, safety-check, then move. Returns True if the move was issued."""
        target = target_inches_for(self.cfg, target_posture)
        cur = self.current_inches()
        decision = safety.check_move(
            self.cfg,
            state,
            target,
            height_known=cur is not None,
            is_moving=False,
            desk_error=False,
        )
        if not decision.allowed:
            log.warning("move to %s blocked: %s", target_posture, decision.reason)
            return False

        log.info(
            "transition: warn then move %s -> %s (%.1fin -> %.1fin) [%s]",
            state.posture,
            target_posture,
            cur if cur is not None else float("nan"),
            decision.target_inches,
            reason,
        )
        await self.warn(target_posture)
        await asyncio.sleep(self.cfg.schedule.warning_seconds)
        await self.move_to(decision.target_inches or target)
        log.info(
            "MOVED from=%.2fin to=%.2fin posture=%s reason=%s",
            cur if cur is not None else float("nan"),
            decision.target_inches or target,
            target_posture,
            reason,
        )
        return True


# --- Daemon ------------------------------------------------------------------

RECONNECT_DELAY_SECONDS = 15.0
KEEPALIVE_POLL_SECONDS = 60.0


async def run(cfg: Config) -> None:
    """Daemon loop. Connects to the desk and drives the schedule, reconnecting
    forever if the BLE link drops."""
    if cfg.device.address is None:
        raise SystemExit("no device configured — run `cadence scan` first")

    # Arm-on-start policy: state.enabled gates movement. require_manual_enable
    # means the daemon starts disarmed until `cadence resume`.
    st = load_state()
    if cfg.safety.require_manual_enable_on_start:
        st.enabled = False
        log.info("started disarmed (require_manual_enable_on_start); run `cadence resume`")
    else:
        st.enabled = True
    st.paused = False
    st.daemon_pid = _getpid()
    save_state(st)

    while True:
        try:
            await _run_connected(cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — BLE drops, timeouts, etc.
            log.warning("desk connection lost (%s); reconnecting in %.0fs",
                        e, RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)


async def _run_connected(cfg: Config) -> None:
    """One connection lifetime: connect and drive the schedule until the link
    breaks (exception) — the caller handles reconnect."""

    def on_height(reading: protocol.HeightReading) -> None:
        # Detect manual moves: a height change we didn't command.
        # The desk streams ~5Hz once polled; skip the disk write when nothing
        # changed.
        s = load_state()
        prev = s.last_known_raw_height
        if prev == reading.raw:
            return
        s.last_known_raw_height = reading.raw
        cal = cfg.calibration
        s.last_known_inches = protocol.raw_to_inches(reading.raw, cal.raw_units_per_inch, cal.offset_inches)
        if (
            not captain._commanded_move
            and prev is not None
            and abs(reading.raw - prev) > cal.raw_units_per_inch * 0.3  # >0.3in jump
        ):
            s.last_manual_move_at = time.time()
            if cfg.behavior.reset_timer_on_manual_move:
                s.phase_started_at = time.time()
                s.posture = "unknown"  # re-establish from new height
            log.info("manual move detected (raw %s -> %s); timer reset", prev, reading.raw)
        save_state(s)

    client = DeskClient(cfg.device, on_height=on_height)
    captain = Captain(cfg, client)

    async with client:
        log.info("connected to desk %s", cfg.device.address)
        await client.read_height(wait=3.0)  # prime latest_height
        last_poll = time.time()
        while True:
            st = load_state()
            now = time.time()

            # Keep-alive: re-poll periodically so the height stream stays
            # fresh (manual-move detection) and the link doesn't idle out.
            if now - last_poll > KEEPALIVE_POLL_SECONDS:
                await client.request_limits()
                last_poll = now

            action = decide(cfg, st, now)

            if action.kind == "sleep":
                await asyncio.sleep(max(1.0, min(action.seconds, KEEPALIVE_POLL_SECONDS)))
                continue

            if action.kind == "establish":
                st.posture = nearest_posture(captain.cfg, captain.current_inches())
                st.phase_started_at = now
                save_state(st)
                log.info("established posture=%s", st.posture)
                continue

            if action.kind == "transition":
                target = action.target_posture or opposite(st.posture)
                moved = await captain.transition(st, target, action.reason)
                # consume one-shot + reset cycle bookkeeping
                st = load_state()
                st.pending = None
                st.snooze_until = None
                if moved:
                    st.posture = target
                    st.phase_started_at = time.time()
                    st.last_auto_move_at = time.time()
                save_state(st)
                continue


def _getpid() -> int:
    import os

    return os.getpid()
