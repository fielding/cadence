"""Tests for Captain movement execution against a fake desk.

The fake client plays back a height trajectory (one step per read) so every
move_to outcome is exercised without hardware: clean completion, ignored
commands, a handset stop (no reversal), and the anti-collision backoff
(reversal past the threshold). Sleeps are no-op'd: move_to's loops count
iterations internally, so tests stay fast while walking the real code paths.
"""

import asyncio

import pytest

from cadence import protocol, scheduler
from cadence.config import Config
from cadence.protocol import HeightReading
from cadence.scheduler import Captain, CollisionDetected, MoveFailed, MoveInterrupted


class FakeClient:
    """Scripted desk: behavior controls the trajectory a goto produces."""

    def __init__(self, start_inches: float, behavior: str = "obey"):
        self._seq: list[int] | None = [round(start_inches * 10)]
        self._i = 0
        self.behavior = behavior  # "obey" | "ignore" | "handset_stop" | "collide"
        self.commands: list = []

    @property
    def latest_height(self):
        if self._seq is None:
            return None
        raw = self._seq[min(self._i, len(self._seq) - 1)]
        if self._i < len(self._seq) - 1:
            self._i += 1
        return HeightReading(raw=raw)

    @latest_height.setter
    def latest_height(self, value):
        self._seq = None if value is None else [value.raw]
        self._i = 0

    def _play(self, raws: list[int]) -> None:
        cur = self.latest_height
        self._seq = [cur.raw if cur else raws[0], *raws]
        self._i = 0

    async def stop(self):
        self.commands.append("stop")

    async def goto_mm(self, mm: int):
        self.commands.append(("goto", mm))
        target_raw = round(mm / protocol.MM_PER_INCH * 10)
        cur = self.latest_height.raw if self.latest_height else target_raw
        toward = 1 if target_raw > cur else -1
        if self.behavior == "obey":
            self._play([target_raw])
        elif self.behavior == "handset_stop":
            # User pressed a handset button: stops 3in short, holds there.
            self._play([target_raw - toward * 30])
        elif self.behavior == "collide":
            # Anti-collision: contact 3in short, then back off 1.8in.
            contact = target_raw - toward * 30
            self._play([contact, contact - toward * 18])
        elif self.behavior == "drive_home":
            # User holds the opposite button until the desk is back at start.
            partway = cur + toward * 43
            self._play([partway, cur])
        # "ignore": height never changes


@pytest.fixture
def fast_sleep(monkeypatch):
    async def _instant(_seconds):
        pass

    monkeypatch.setattr(scheduler.asyncio, "sleep", _instant)


def _captain(client: FakeClient) -> Captain:
    return Captain(Config(), client)


def test_move_to_verifies_and_completes(fast_sleep):
    client = FakeClient(26.8)
    captain = _captain(client)
    asyncio.run(captain.move_to(44.9))
    # STOP precedes the goto (clears stale sessions), then the goto in mm.
    assert client.commands[0] == "stop"
    assert ("goto", protocol.inches_to_goto_mm(44.9)) in client.commands
    assert client.latest_height.raw == 449


def test_move_to_raises_when_desk_ignores_commands(fast_sleep):
    client = FakeClient(26.8, behavior="ignore")
    captain = _captain(client)
    with pytest.raises(MoveFailed):
        asyncio.run(captain.move_to(44.9))
    # Retried once: two stops, two gotos.
    gotos = [c for c in client.commands if c != "stop"]
    assert len(gotos) == 2


def test_handset_stop_classified_as_interrupt(fast_sleep):
    client = FakeClient(26.8, behavior="handset_stop")
    captain = _captain(client)
    with pytest.raises(MoveInterrupted):
        asyncio.run(captain.move_to(44.9))


def test_backoff_classified_as_collision(fast_sleep):
    client = FakeClient(44.8, behavior="collide")
    captain = _captain(client)
    with pytest.raises(CollisionDetected):
        asyncio.run(captain.move_to(26.8))


def test_collision_detected_on_rises_too(fast_sleep):
    client = FakeClient(26.8, behavior="collide")
    captain = _captain(client)
    with pytest.raises(CollisionDetected):
        asyncio.run(captain.move_to(44.9))


def test_move_to_skips_when_already_at_target(fast_sleep):
    client = FakeClient(26.8)
    captain = _captain(client)
    asyncio.run(captain.move_to(26.9))  # within the 0.3in deadband
    assert client.commands == []


def test_tap_delta_never_below_controller_deadband(fast_sleep):
    client = FakeClient(26.8)
    captain = _captain(client)
    captain.cfg.warning.tap_delta_inches = 0.2  # rounds to 5mm, below deadband
    asyncio.run(captain._tap())
    gotos = [mm for c, mm in (c for c in client.commands if c != "stop")]
    assert max(gotos) - min(gotos) >= 8  # floored to 8mm


def test_tap_skipped_when_height_unknown(fast_sleep):
    client = FakeClient(26.8)
    client.latest_height = None
    captain = _captain(client)
    asyncio.run(captain._tap())
    assert client.commands == []


def test_drive_home_veto_is_interrupt_despite_big_reversal(fast_sleep):
    """Holding DOWN until the desk returns to start is a veto, not a crash."""
    client = FakeClient(26.9, behavior="drive_home")
    captain = _captain(client)
    with pytest.raises(MoveInterrupted):
        asyncio.run(captain.move_to(44.9))
