"""Safety guardrails for physical movement.

Every automatic move must pass through `check_move`. The rules implement the
handoff's safety requirements: bounds clamping, no-move-on-error, no-move-while-
moving, manual-move grace period, and refusing absolute moves when height is
unknown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import Config
from .state import State


@dataclass
class MoveDecision:
    allowed: bool
    reason: str
    # If allowed, the target clamped to safe bounds.
    target_inches: float | None = None


def within_bounds(cfg: Config, inches: float) -> bool:
    return cfg.safety.min_height_inches <= inches <= cfg.safety.max_height_inches


def clamp(cfg: Config, inches: float) -> float:
    return max(cfg.safety.min_height_inches, min(cfg.safety.max_height_inches, inches))


def recently_moved_manually(cfg: Config, state: State, *, now: float | None = None) -> bool:
    if not cfg.safety.do_not_move_if_user_recently_moved_desk:
        return False
    if state.last_manual_move_at is None:
        return False
    now = now if now is not None else time.time()
    grace = cfg.safety.recent_manual_move_grace_seconds
    return (now - state.last_manual_move_at) < grace


def check_move(
    cfg: Config,
    state: State,
    target_inches: float,
    *,
    height_known: bool,
    is_moving: bool,
    desk_error: bool = False,
    now: float | None = None,
) -> MoveDecision:
    """Decide whether an absolute move to target_inches is permitted."""
    if desk_error:
        return MoveDecision(False, "desk reports an error")
    if is_moving:
        return MoveDecision(False, "desk is already moving")
    if cfg.safety.refuse_move_without_height and not height_known:
        return MoveDecision(False, "current height unknown; refusing absolute move")
    if recently_moved_manually(cfg, state, now=now):
        return MoveDecision(False, "user moved the desk recently (within grace period)")

    target = clamp(cfg, target_inches)
    if target != target_inches:
        # We clamp rather than refuse, but say so.
        return MoveDecision(
            True,
            f"target {target_inches:.1f}in clamped to safe bound {target:.1f}in",
            target,
        )
    return MoveDecision(True, "ok", target)
