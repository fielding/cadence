"""Filesystem locations for config, state, and logs.

Follows XDG conventions, overridable via environment variables so the daemon
and CLI always agree on where things live.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "cadence"


def _xdg(env: str, default: Path) -> Path:
    raw = os.environ.get(env)
    base = Path(raw).expanduser() if raw else default
    return base / APP_NAME


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")


def config_file() -> Path:
    # Allow a single-file override for testing / alternate profiles.
    override = os.environ.get("CADENCE_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.toml"


def state_file() -> Path:
    return state_dir() / "state.json"


def log_file() -> Path:
    return state_dir() / "cadence.log"


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)
