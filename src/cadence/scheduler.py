"""The captain: scheduling logic and the daemon loop.

`decide` is a pure function (config + state + clock -> next action) so the
timing logic is testable without BLE. `Captain` executes actions against a live
DeskClient: warnings, safety-checked moves, and manual-move detection. `run`
is the daemon entry point.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime

from . import notify, presence, protocol, safety
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


def decide(cfg: Config, state: State, now: float, idle_seconds: float | None = None) -> Action:
    """Decide the next action. Never performs side effects.

    idle_seconds is the time since last user input (None = unknown, treated
    as present so a broken probe never silently disables the schedule).
    """
    if not state.enabled or not cfg.schedule.enabled or state.paused:
        return Action("sleep", POLL_CAP_SECONDS, reason="disabled/paused")

    if not within_working_hours(cfg, now):
        return Action("sleep", POLL_CAP_SECONDS, reason="outside working hours")

    if (
        cfg.presence.enabled
        and idle_seconds is not None
        and idle_seconds >= cfg.presence.idle_threshold_minutes * 60
    ):
        return Action("sleep", POLL_CAP_SECONDS, reason="user idle/away")

    # Below the away threshold, but still don't move the desk for an empty
    # room: a due transition fires only on recent input. Held transitions
    # fire within one poll tick of the user returning.
    recently_active = (
        not cfg.presence.enabled
        or idle_seconds is None
        or idle_seconds <= cfg.presence.active_threshold_minutes * 60
    )

    # Manual one-shot: force the next transition now.
    if state.pending == "next":
        if not recently_active:
            return Action("sleep", POLL_CAP_SECONDS, reason="due, waiting for user activity")
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

    if not recently_active:
        return Action("sleep", POLL_CAP_SECONDS, reason="due, waiting for user activity")

    return Action(
        "transition",
        target_posture=opposite(state.posture),
        reason=f"{state.posture} phase elapsed",
    )


# --- Live execution ----------------------------------------------------------

class MoveFailed(Exception):
    """A movement command was ACKed but the desk did not move.

    Observed live: after ~30+ min connected, the controller ACKs writes but
    ignores movement commands; a fresh connection fixes it. Raising tears down
    the connection so the reconnect loop can retry the transition cleanly.
    """


class CollisionDetected(Exception):
    """The desk moved but settled far from target — likely an obstruction.

    Never auto-retry into a possible obstruction: notify the user and back
    off instead.
    """


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
        back = protocol.inches_to_goto_mm(cur, offset)
        # Compute the delta in mm with an 8mm floor: the controller's goto
        # deadband swallows smaller moves (7mm confirmed ignored live).
        delta_mm = max(8, round(w.tap_delta_inches * protocol.MM_PER_INCH))
        up = back + delta_mm
        self._commanded_move = True
        try:
            await self.client.stop()  # clear any stale goto session
            for _ in range(max(1, w.tap_count)):
                await self.client.goto_mm(up)
                await asyncio.sleep(w.tap_pause_ms / 1000)
                await self.client.goto_mm(back)
                await asyncio.sleep(w.tap_pause_ms / 1000)
        finally:
            self._commanded_move = False

    async def move_to(self, target_inches: float, *, settle_timeout: float = 40.0) -> None:
        """Issue a goto, verify the desk actually moves, and hold the
        commanded-move flag until motion settles.

        Raises MoveFailed if the controller ACKs but never moves (stale
        connection) or the desk settles far from the target (collision)."""
        mm = protocol.inches_to_goto_mm(target_inches, self.cfg.calibration.offset_inches)
        start = self.current_inches()
        if start is not None and abs(start - target_inches) <= 0.3:
            return  # already there; a goto this small is deadband anyway
        self._commanded_move = True
        try:
            await self.client.stop()  # clear any stale goto session
            await self.client.goto_mm(mm)
            if not await self._motion_started(start):
                log.warning("goto %.1fin produced no movement; STOP + one retry", target_inches)
                await self.client.stop()
                await self.client.goto_mm(mm)
                if not await self._motion_started(start):
                    raise MoveFailed(f"desk ignored goto to {target_inches:.1f}in")

            # Motion confirmed; now wait for it to settle (no change for 2s).
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

            final = self.current_inches()
            if final is not None and abs(final - target_inches) > 1.0:
                # Settled well short of target: obstruction/anti-collision.
                raise CollisionDetected(
                    f"desk settled at {final:.1f}in, expected {target_inches:.1f}in "
                    "(possible obstruction)"
                )
        finally:
            self._commanded_move = False

    async def _motion_started(self, start_inches: float | None, wait: float = 4.0) -> bool:
        """True once the height stream shows real movement from start."""
        waited = 0.0
        while waited < wait:
            await asyncio.sleep(0.5)
            waited += 0.5
            cur = self.current_inches()
            if start_inches is None or (cur is not None and abs(cur - start_inches) > 0.15):
                return True
        return False

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

# Repeated rapid drops usually mean another BLE central (typically the vendor
# phone app) is fighting for the desk connection. Observed live: with the
# AiDesk-paired phone in range, the link dropped every 1-8 minutes; with
# phone Bluetooth off it held indefinitely.
INTERFERENCE_WINDOW_SECONDS = 600.0
INTERFERENCE_DROP_THRESHOLD = 3
INTERFERENCE_NOTIFY_COOLDOWN_SECONDS = 1800.0
INTERFERENCE_HINT = (
    "Desk Bluetooth keeps dropping. Another device may be competing for the "
    "connection - close desk apps (e.g. AiDesk) or turn off Bluetooth on "
    "phones/tablets that have paired with the desk."
)


def interference_suspected(drop_times: list[float], now: float) -> bool:
    """True when enough recent drops cluster inside the detection window.

    Mutates drop_times to discard entries older than the window.
    """
    drop_times[:] = [t for t in drop_times if now - t < INTERFERENCE_WINDOW_SECONDS]
    return len(drop_times) >= INTERFERENCE_DROP_THRESHOLD


async def run(cfg: Config) -> None:
    """Daemon loop. Connects to the desk and drives the schedule, reconnecting
    forever if the BLE link drops."""
    if cfg.device.address is None:
        raise SystemExit("no device configured — run `cadence scan` first")
    if not cfg.device.verified:
        raise SystemExit(
            "desk has not passed verification — run `cadence setup` first "
            "(or set device.verified = true in config if you've proven the "
            "protocol yourself)"
        )

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

    # CLI control commands (next/pause/resume/snooze) send SIGUSR1 after
    # writing state so the daemon reacts immediately instead of waiting out
    # its poll sleep.
    wake = asyncio.Event()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, wake.set)
    except (NotImplementedError, ValueError):  # non-unix or nested loop
        pass

    drop_times: list[float] = []
    last_interference_warn = 0.0
    while True:
        try:
            await _run_connected(cfg, wake)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — BLE drops, timeouts, etc.
            now = time.time()
            drop_times.append(now)
            if (
                interference_suspected(drop_times, now)
                and now - last_interference_warn > INTERFERENCE_NOTIFY_COOLDOWN_SECONDS
            ):
                last_interference_warn = now
                log.warning(
                    "%d drops in %.0f min — %s",
                    len(drop_times), INTERFERENCE_WINDOW_SECONDS / 60, INTERFERENCE_HINT,
                )
                notify.notify("cadence", INTERFERENCE_HINT)
            log.warning("desk connection lost (%s); reconnecting in %.0fs",
                        e, RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)


async def _sleep_until_woken(wake: asyncio.Event, seconds: float) -> None:
    """Sleep, but return immediately if a control signal arrives."""
    try:
        await asyncio.wait_for(wake.wait(), timeout=seconds)
        wake.clear()
    except asyncio.TimeoutError:
        pass


async def _run_connected(cfg: Config, wake: asyncio.Event) -> None:
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
        was_idle = False
        while True:
            st = load_state()
            now = time.time()

            # Keep-alive: re-poll periodically so the height stream stays
            # fresh (manual-move detection) and the link doesn't idle out.
            if now - last_poll > KEEPALIVE_POLL_SECONDS:
                await client.request_limits()
                last_poll = now

            idle = presence.hid_idle_seconds() if cfg.presence.enabled else None
            is_idle = (
                idle is not None
                and idle >= cfg.presence.idle_threshold_minutes * 60
            )
            if was_idle and not is_idle:
                log.info("user returned after idle; resetting phase timer")
                if cfg.presence.reset_timer_on_return and st.posture in ("sit", "stand"):
                    st.phase_started_at = now
                    save_state(st)
            was_idle = is_idle

            action = decide(cfg, st, now, idle_seconds=idle)

            if action.kind == "sleep":
                await _sleep_until_woken(
                    wake, max(1.0, min(action.seconds, KEEPALIVE_POLL_SECONDS))
                )
                continue

            if action.kind == "establish":
                st.posture = nearest_posture(captain.cfg, captain.current_inches())
                st.phase_started_at = now
                save_state(st)
                log.info("established posture=%s", st.posture)
                continue

            if action.kind == "transition":
                target = action.target_posture or opposite(st.posture)
                try:
                    moved = await captain.transition(st, target, action.reason)
                except CollisionDetected as e:
                    log.error("collision during transition: %s", e)
                    notify.notify("cadence", f"Desk stopped short: {e}. Snoozing 5m.")
                    st = load_state()
                    st.pending = None
                    st.snooze_until = time.time() + 300
                    save_state(st)
                    continue
                # consume one-shot + reset cycle bookkeeping
                st = load_state()
                st.pending = None
                st.snooze_until = None
                if moved:
                    st.posture = target
                    st.phase_started_at = time.time()
                    st.last_auto_move_at = time.time()
                else:
                    # Blocked by a safety guard; don't spin on it.
                    await _sleep_until_woken(wake, POLL_CAP_SECONDS)
                save_state(st)
                continue


def _getpid() -> int:
    import os

    return os.getpid()
