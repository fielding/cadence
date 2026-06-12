"""Runtime state, persisted to state.json.

This is the shared blackboard between the daemon and the CLI. The CLI writes
control intents (pause/resume/next/snooze) and the daemon reads them on each
tick. Kept deliberately simple (a single JSON file with atomic writes) per the
"prefer simple first" guidance.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths

Posture = str  # "sit" | "stand" | "unknown"


@dataclass
class State:
    version: int = 1

    # Captain control
    enabled: bool = False          # has the user armed automation this run?
    paused: bool = False           # kill switch
    pending: str | None = None     # one-shot command for the daemon: "next" | "stop"
    snooze_until: float | None = None  # epoch seconds

    # Cycle tracking
    posture: Posture = "unknown"   # last posture we moved to
    phase_started_at: float | None = None  # when the current sit/stand phase began

    # Consecutive collisions without a successful move; the daemon pauses
    # automation when this reaches the configured threshold.
    collision_streak: int = 0
    # Consecutive user vetoes of the same due transition; a second veto
    # restarts the current phase instead of re-asking every snooze.
    interrupt_streak: int = 0

    # Observations
    last_known_raw_height: int | None = None
    last_known_inches: float | None = None
    last_manual_move_at: float | None = None
    last_auto_move_at: float | None = None

    # Bookkeeping
    daemon_pid: int | None = None
    updated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load(path: Path | None = None) -> State:
    path = path or paths.state_file()
    if not path.exists():
        return State()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return State()
    known = State().to_dict().keys()
    return State(**{k: v for k, v in data.items() if k in known})


def save(state: State, path: Path | None = None, *, clock: float | None = None) -> Path:
    """Atomically write state to disk (temp file + rename)."""
    path = path or paths.state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = clock if clock is not None else time.time()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state.to_dict(), fh, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def mutate(fn, path: Path | None = None) -> State:
    """Load, apply fn(state), save, return the updated state."""
    st = load(path)
    fn(st)
    save(st, path)
    return st
