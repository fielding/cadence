"""Desktop notifications and sound (macOS).

Used as the default, always-safe warning channel before a move. Physical taps
are opt-in; a notification + sound is the fallback when taps are disabled or
risky. All calls are best-effort and never raise.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("cadence.notify")

# A short built-in macOS system sound.
_DEFAULT_SOUND = "/System/Library/Sounds/Submarine.aiff"


def notify(title: str, message: str, *, sound: bool = True) -> None:
    """Show a desktop notification; optionally play a sound."""
    osa = shutil.which("osascript")
    if osa:
        # Escape double quotes for AppleScript.
        t = title.replace('"', '\\"')
        m = message.replace('"', '\\"')
        script = f'display notification "{m}" with title "{t}"'
        try:
            subprocess.run([osa, "-e", script], check=False, timeout=5)
        except Exception as e:  # noqa: BLE001
            log.debug("osascript notification failed: %s", e)
    else:
        log.info("notification: %s — %s", title, message)

    if sound:
        play_sound()


def play_sound(path: str = _DEFAULT_SOUND) -> None:
    afplay = shutil.which("afplay")
    if not afplay:
        return
    try:
        subprocess.Popen([afplay, path])  # fire and forget
    except Exception as e:  # noqa: BLE001
        log.debug("afplay failed: %s", e)
