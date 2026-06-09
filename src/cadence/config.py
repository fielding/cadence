"""Configuration loading and saving (TOML).

The config is the single source of truth for user preferences: schedule,
heights, warning behavior, safety limits, working hours, and the discovered
BLE device. Defaults match the project handoff so the app runs before the
user has written a config file.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from . import paths


@dataclass
class Schedule:
    sit_minutes: float = 45.0
    stand_minutes: float = 15.0
    warning_seconds: float = 15.0
    enabled: bool = True


@dataclass
class Heights:
    sit_inches: float = 26.8
    stand_inches: float = 44.9


@dataclass
class Warning:
    enabled: bool = True
    # "tap" = small physical nudge, "notify" = desktop notification + sound,
    # "both" = notify then tap. Default to the safest option.
    mode: str = "notify"
    tap_count: int = 2
    tap_delta_inches: float = 0.2
    tap_pause_ms: int = 500
    sound: bool = True


@dataclass
class Safety:
    min_height_inches: float = 22.0
    max_height_inches: float = 50.0
    require_manual_enable_on_start: bool = True
    do_not_move_if_user_recently_moved_desk: bool = True
    recent_manual_move_grace_seconds: float = 300.0
    # If the desk height cannot be read, refuse absolute moves entirely.
    refuse_move_without_height: bool = True


@dataclass
class WorkingHours:
    enabled: bool = False
    days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    start: str = "09:00"
    end: str = "18:00"


@dataclass
class Behavior:
    snooze_minutes: float = 15.0
    reset_timer_on_manual_move: bool = True


@dataclass
class Calibration:
    """Maps raw device height units to inches.

    The desk display was historically miscalibrated by ~2 inches, so we never
    trust either the display or a hardcoded conversion. The relationship is
    assumed linear:  inches = raw / raw_units_per_inch + offset_inches.

    CONFIRMED on the Apex Pro (live probe 2026-06-09): firmware reports height
    in 0.1 inch units (raw 448 = 44.8in, raw 269 = 26.9in, matching display).
    Use `cadence calibrate` with a tape measure if the display itself drifts.
    """

    raw_units_per_inch: float = 10.0  # 0.1 inch units (confirmed live)
    offset_inches: float = 0.0


@dataclass
class Device:
    """Discovered BLE device metadata (filled in by `cadence scan`)."""

    address: str | None = None
    name: str | None = None
    service_uuid: str | None = None
    command_char_uuid: str | None = None
    notify_char_uuid: str | None = None


@dataclass
class Config:
    schedule: Schedule = field(default_factory=Schedule)
    heights: Heights = field(default_factory=Heights)
    warning: Warning = field(default_factory=Warning)
    safety: Safety = field(default_factory=Safety)
    working_hours: WorkingHours = field(default_factory=WorkingHours)
    behavior: Behavior = field(default_factory=Behavior)
    calibration: Calibration = field(default_factory=Calibration)
    device: Device = field(default_factory=Device)


# --- (de)serialization -------------------------------------------------------

_SECTION_TYPES: dict[str, type] = {
    "schedule": Schedule,
    "heights": Heights,
    "warning": Warning,
    "safety": Safety,
    "working_hours": WorkingHours,
    "behavior": Behavior,
    "calibration": Calibration,
    "device": Device,
}


def _build_section(cls: type, data: dict[str, Any]) -> Any:
    known = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    return cls(**kwargs)


def from_dict(data: dict[str, Any]) -> Config:
    sections: dict[str, Any] = {}
    for key, cls in _SECTION_TYPES.items():
        sections[key] = _build_section(cls, data.get(key, {}) or {})
    return Config(**sections)


def to_dict(cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _SECTION_TYPES:
        section = getattr(cfg, key)
        d = dataclasses.asdict(section)
        # tomli_w cannot serialize None; drop unset device fields.
        out[key] = {k: v for k, v in d.items() if v is not None}
    return out


def load(path: Path | None = None) -> Config:
    """Load config from disk, falling back to defaults for missing values."""
    path = path or paths.config_file()
    if not path.exists():
        return Config()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return from_dict(data)


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(to_dict(cfg), fh)
    return path
