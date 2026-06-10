"""Tests for Captain movement execution against a fake desk.

The fake client jumps straight to commanded targets (or refuses to move, or
stops short) so every move_to outcome is exercised without hardware. Sleeps
are no-op'd: the loops in move_to count iterations internally, so tests stay
fast while still walking the real code paths.
"""

import asyncio

import pytest

from cadence import protocol, scheduler
from cadence.config import Config
from cadence.protocol import HeightReading
from cadence.scheduler import Captain, CollisionDetected, MoveFailed


class FakeClient:
    """Scripted desk: behavior controls what a goto does to the height."""

    def __init__(self, start_inches: float, behavior: str = "obey"):
        self.latest_height = HeightReading(raw=round(start_inches * 10))
        self.behavior = behavior  # "obey" | "ignore" | "stop_short"
        self.commands: list = []

    async def stop(self):
        self.commands.append("stop")

    async def goto_mm(self, mm: int):
        self.commands.append(("goto", mm))
        target_inches = mm / protocol.MM_PER_INCH
        if self.behavior == "obey":
            self.latest_height = HeightReading(raw=round(target_inches * 10))
        elif self.behavior == "stop_short":
            self.latest_height = HeightReading(raw=round((target_inches + 3.0) * 10))
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


def test_move_to_raises_collision_when_settling_short(fast_sleep):
    client = FakeClient(44.8, behavior="stop_short")
    captain = _captain(client)
    with pytest.raises(CollisionDetected):
        asyncio.run(captain.move_to(26.8))


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
