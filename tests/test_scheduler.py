"""Tests for the pure scheduling decision and safety guards."""

from cadence import safety, scheduler
from cadence.config import Config
from cadence.state import State


def _armed_state(**kw) -> State:
    st = State(enabled=True, paused=False)
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_disabled_sleeps():
    cfg = Config()
    st = State(enabled=False)
    action = scheduler.decide(cfg, st, now=1000.0)
    assert action.kind == "sleep"


def test_paused_sleeps():
    cfg = Config()
    st = _armed_state(paused=True)
    assert scheduler.decide(cfg, st, now=1000.0).kind == "sleep"


def test_first_tick_establishes_posture():
    cfg = Config()
    st = _armed_state(posture="unknown", phase_started_at=None)
    assert scheduler.decide(cfg, st, now=1000.0).kind == "establish"


def test_phase_in_progress_sleeps_until_end():
    cfg = Config()  # sit 45m
    start = 1000.0
    st = _armed_state(posture="sit", phase_started_at=start)
    action = scheduler.decide(cfg, st, now=start + 60)  # 1 min in
    assert action.kind == "sleep"


def test_phase_elapsed_transitions_to_opposite():
    cfg = Config()
    start = 1000.0
    st = _armed_state(posture="sit", phase_started_at=start)
    elapsed = cfg.schedule.sit_minutes * 60 + 1
    action = scheduler.decide(cfg, st, now=start + elapsed)
    assert action.kind == "transition"
    assert action.target_posture == "stand"


def test_pending_next_forces_transition():
    cfg = Config()
    st = _armed_state(posture="stand", phase_started_at=1000.0, pending="next")
    action = scheduler.decide(cfg, st, now=1001.0)
    assert action.kind == "transition"
    assert action.target_posture == "sit"


def test_snooze_blocks_transition():
    cfg = Config()
    start = 1000.0
    elapsed = cfg.schedule.sit_minutes * 60 + 1
    now = start + elapsed
    st = _armed_state(posture="sit", phase_started_at=start, snooze_until=now + 120)
    action = scheduler.decide(cfg, st, now=now)
    assert action.kind == "sleep"


def test_safety_clamps_to_bounds():
    cfg = Config()
    st = State()
    d = safety.check_move(cfg, st, 99.0, height_known=True, is_moving=False)
    assert d.allowed
    assert d.target_inches == cfg.safety.max_height_inches


def test_safety_refuses_without_height():
    cfg = Config()
    st = State()
    d = safety.check_move(cfg, st, 30.0, height_known=False, is_moving=False)
    assert not d.allowed


def test_safety_refuses_while_moving():
    cfg = Config()
    st = State()
    d = safety.check_move(cfg, st, 30.0, height_known=True, is_moving=True)
    assert not d.allowed


def test_safety_respects_recent_manual_move():
    cfg = Config()
    st = State(last_manual_move_at=1000.0)
    d = safety.check_move(cfg, st, 30.0, height_known=True, is_moving=False, now=1010.0)
    assert not d.allowed


def test_interference_detection_windows():
    drops = [100.0, 200.0, 300.0]
    assert scheduler.interference_suspected(drops, now=350.0)
    # Old drops age out of the window and stop counting.
    drops = [100.0, 200.0, 300.0]
    assert not scheduler.interference_suspected(drops, now=200.0 + 601.0)
    assert drops == [300.0]
    # Two drops are not enough to call it interference.
    assert not scheduler.interference_suspected([10.0, 20.0], now=30.0)


def test_idle_user_blocks_transition():
    cfg = Config()
    start = 1000.0
    elapsed = cfg.schedule.sit_minutes * 60 + 1
    st = _armed_state(posture="sit", phase_started_at=start)
    # Phase elapsed but the user has been idle past the threshold.
    action = scheduler.decide(cfg, st, now=start + elapsed, idle_seconds=11 * 60)
    assert action.kind == "sleep"
    assert "idle" in action.reason


def test_active_user_still_transitions():
    cfg = Config()
    start = 1000.0
    elapsed = cfg.schedule.sit_minutes * 60 + 1
    st = _armed_state(posture="sit", phase_started_at=start)
    action = scheduler.decide(cfg, st, now=start + elapsed, idle_seconds=30.0)
    assert action.kind == "transition"


def test_unknown_idle_fails_open():
    cfg = Config()
    start = 1000.0
    elapsed = cfg.schedule.sit_minutes * 60 + 1
    st = _armed_state(posture="sit", phase_started_at=start)
    action = scheduler.decide(cfg, st, now=start + elapsed, idle_seconds=None)
    assert action.kind == "transition"


def test_presence_disabled_ignores_idle():
    cfg = Config()
    cfg.presence.enabled = False
    start = 1000.0
    elapsed = cfg.schedule.sit_minutes * 60 + 1
    st = _armed_state(posture="sit", phase_started_at=start)
    action = scheduler.decide(cfg, st, now=start + elapsed, idle_seconds=9999.0)
    assert action.kind == "transition"


def test_due_transition_holds_without_recent_activity():
    cfg = Config()  # active threshold 2m, away threshold 10m
    start = 1000.0
    elapsed = cfg.schedule.sit_minutes * 60 + 1
    st = _armed_state(posture="sit", phase_started_at=start)
    # 5 minutes idle: not away, but not at the desk either.
    action = scheduler.decide(cfg, st, now=start + elapsed, idle_seconds=5 * 60)
    assert action.kind == "sleep"
    assert "activity" in action.reason


def test_due_transition_fires_after_return():
    cfg = Config()
    start = 1000.0
    elapsed = cfg.schedule.sit_minutes * 60 + 1
    st = _armed_state(posture="sit", phase_started_at=start)
    action = scheduler.decide(cfg, st, now=start + elapsed, idle_seconds=10.0)
    assert action.kind == "transition"


def test_pending_next_also_waits_for_activity():
    cfg = Config()
    st = _armed_state(posture="stand", phase_started_at=1000.0, pending="next")
    action = scheduler.decide(cfg, st, now=1001.0, idle_seconds=5 * 60)
    assert action.kind == "sleep"


def test_sleep_until_woken_returns_early_on_signal():
    import asyncio
    import time as _time

    async def scenario():
        wake = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, wake.set)
        t0 = _time.monotonic()
        await scheduler._sleep_until_woken(wake, seconds=5.0)
        return _time.monotonic() - t0

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.0  # woke early, didn't sleep the full 5s
    # event is cleared for the next sleep


def test_nearest_posture_classifies_collision_limbo_as_stand():
    cfg = Config()  # sit 26.8, stand 44.9
    # A collided rise that parked at 41.6 is much closer to standing.
    assert scheduler.nearest_posture(cfg, 41.6) == "stand"
    assert scheduler.nearest_posture(cfg, 30.0) == "sit"
    assert scheduler.nearest_posture(cfg, None) == "sit"  # safe default
