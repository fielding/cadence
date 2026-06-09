"""User-presence detection (macOS).

Reads HID idle time: seconds since the last physical keyboard/mouse/trackpad
input, from IOHIDSystem. This is the right "am I at the computer" signal for
this machine because keep-awake apps (Caffeine, Amphetamine, caffeinate) hold
power assertions but do not synthesize input — the display never sleeps, yet
HIDIdleTime keeps climbing. Mouse-jiggler apps that fake input WOULD defeat
this check.
"""

from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger("cadence.presence")

_NS_PER_SECOND = 1_000_000_000
_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def parse_hid_idle(ioreg_output: str) -> float | None:
    """Extract idle seconds from `ioreg -c IOHIDSystem` output."""
    m = _IDLE_RE.search(ioreg_output)
    if not m:
        return None
    return int(m.group(1)) / _NS_PER_SECOND


def hid_idle_seconds() -> float | None:
    """Seconds since last user input, or None if unavailable.

    Callers should treat None as "presence unknown" and fail open (assume
    present) so a broken probe never silently disables the schedule.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception as e:  # noqa: BLE001
        log.debug("ioreg probe failed: %s", e)
        return None
    return parse_hid_idle(out)
