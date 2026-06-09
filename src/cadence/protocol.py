"""Jiecang / Uplift BLE wire protocol.

This is the device-facing layer: it knows how to build command frames, parse
notification frames, and translate raw device height units. It does NOT talk
BLE itself (see ble.py) and it does NOT know about schedules or safety.

Protocol facts below are reverse-engineered from the Uplift/AiDesk Android app
and community projects (librick/uplift-ble and the BLE reverse-engineering
docs). The Deskhaus Apex Pro is believed to use the same Jiecang controller,
but every value marked UNCONFIRMED must be validated against the real desk via
`cadence scan` and a notification capture before trusting absolute moves.

Discovery on the actual desk (2026-06-09)
-----------------------------------------
Device "BLE Device 12D8A4", model L-BTMEB95-07014-03, sw v1.01 (Jul 2023).
CONFIRMED present:
  - service 0000fe60  (matches the Jiecang candidate)
  - char fe61: write / write-without-response  -> command channel
  - char fe62: notify                          -> notification channel
  - char fe63, fe64: write + notify            -> purpose unknown (untested)
CONFIRMED by live probing (supervised):
  - Writing REQUEST_LIMITS (F1 F1 07 00 07 7E) to fe61 wakes the height
    stream on fe62 — this is the poll command. No passive notifications
    otherwise.
  - Height notification: F2 F2 01 03 <hi> <lo> <status> <chk> 7E.
    Height = payload bytes 0-1 big-endian in 0.1 INCH units (NOT 0.1 mm as
    the Uplift docs claim): observed 0x01C0=448 -> 44.8in (display 44.8) and
    0x010D=269 -> 26.9in (display ~26.9). Third payload byte (0x07) appears
    to be a status flag.
CONFIRMED by supervised movement tests (2026-06-09):
  - MOVE_UP (0x01) / STOP (0x2B): one UP frame moves CONTINUOUSLY until a
    STOP is sent — this firmware is not step-per-command.
  - GOTO_HEIGHT (0x1B): 2-byte big-endian payload in MILLIMETERS (not the
    0.1in display units used by notifications!). 683mm = 26.9in verified.
  - Out-of-range goto targets are clamped to the controller's own limits and
    the desk TRAVELS there (observed: 6832mm target drove it to its 47.8in
    max). Never rely on the controller to refuse; bound targets ourselves.
  - A goto persists after BLE disconnect — the controller keeps driving to
    target on its own. STOP must be delivered (use write-with-response) and
    the connection held briefly afterward to be sure it took effect.
  - Height notifications stream continuously during movement (~3 Hz).
  - The desk ECHOES accepted commands on fe62 with the F2F2 header (e.g.
    goto 680mm -> echo F2 F2 1B 02 02 A8 C7 7E) — usable as a delivery ACK.
  - The 3rd height-payload byte ("status") reads 0x07 during all normal
    operation; treat a different value as a potential error indicator.
  - GOTO has a deadband: a 5mm (0.2in) move is silently ignored; 8mm
    (0.3in) executes reliably. Warning taps must use >= 0.3in deltas.
  - STALE CONNECTIONS: after ~30+ min connected, the controller continued
    to ACK writes and serve height notifications but IGNORED movement
    commands. A STOP on a fresh connection restored movement. Defense:
    send STOP before every goto, verify motion starts within ~4s, and
    reconnect if a goto is ignored (scheduler.MoveFailed).
  - ANTI-COLLISION (observed live): if the desk meets resistance while
    descending it stops and backs UP ~1.8in, then settles. From the host
    side this looks like a commanded move whose height stream reverses
    direction. Treat any direction reversal during a goto as a collision:
    abort the cycle and notify the user; never blindly re-send the goto.
STILL UNCONFIRMED:
  - chars fe63/fe64 (write+notify) purpose.

Frame format
------------
    command (host -> desk):   F1 F1 <op> <len> <payload...> <chk> 7E
    notify  (desk -> host):   F2 F2 <op> <len> <payload...> <chk> 7E
    chk = (op + len + sum(payload)) & 0xFF

Height is reported in 0.1 mm increments, big-endian (UNCONFIRMED units).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

# --- GATT identifiers --------------------------------------------------------
# Jiecang/Uplift desks ship in several hardware revisions. Discovery picks the
# right one; these are the known candidates and 16-bit shorthands.
CANDIDATE_SERVICE_UUIDS: tuple[str, ...] = (
    "0000ff00-0000-1000-8000-00805f9b34fb",
    "0000fe60-0000-1000-8000-00805f9b34fb",
    "0000ff12-0000-1000-8000-00805f9b34fb",
    "3e135142-654f-9090-134a-a6ff5bb77046",  # seen on some Jiecang units
)

# Within the chosen service:
DEFAULT_COMMAND_CHAR_UUID = "0000fe61-0000-1000-8000-00805f9b34fb"  # write
DEFAULT_NOTIFY_CHAR_UUID = "0000fe62-0000-1000-8000-00805f9b34fb"  # notify

CMD_HEADER = b"\xf1\xf1"
NOTIFY_HEADER = b"\xf2\xf2"
FRAME_END = 0x7E


class Op(IntEnum):
    """Command/notification opcodes (UNCONFIRMED for Apex Pro)."""

    MOVE_UP = 0x01
    MOVE_DOWN = 0x02
    PRESET_1 = 0x05
    PRESET_2 = 0x06
    REQUEST_LIMITS = 0x07
    GOTO_HEIGHT = 0x1B  # 2-byte big-endian raw-height payload
    SET_MAX_LIMIT_HERE = 0x21
    SET_MIN_LIMIT_HERE = 0x22
    STOP = 0x2B

    # Notification opcodes
    NOTIFY_HEIGHT = 0x01  # payload: [unknown, hi, lo] -> raw height (0.1mm)
    NOTIFY_LIMITS = 0x07  # payload: [max_hi, max_lo, min_hi, min_lo] in mm


def checksum(opcode: int, payload: bytes) -> int:
    return (opcode + len(payload) + sum(payload)) & 0xFF


def build_frame(opcode: int, payload: bytes = b"") -> bytes:
    """Build a host->desk command frame."""
    body = bytes([opcode, len(payload)]) + payload
    return CMD_HEADER + body + bytes([checksum(opcode, payload), FRAME_END])


# --- Convenience command builders -------------------------------------------

def cmd_up() -> bytes:
    return build_frame(Op.MOVE_UP)


def cmd_down() -> bytes:
    return build_frame(Op.MOVE_DOWN)


def cmd_stop() -> bytes:
    return build_frame(Op.STOP)


def cmd_request_limits() -> bytes:
    return build_frame(Op.REQUEST_LIMITS)


MM_PER_INCH = 25.4


def cmd_goto_mm(mm: int) -> bytes:
    """Move to an absolute height in MILLIMETERS (big-endian uint16).

    CONFIRMED live: goto takes mm even though notifications report 0.1in.
    The controller clamps out-of-range targets and travels there, so callers
    MUST bound-check before sending.
    """
    if not 0 <= mm <= 0xFFFF:
        raise ValueError(f"mm {mm} out of uint16 range")
    payload = mm.to_bytes(2, "big")
    return build_frame(Op.GOTO_HEIGHT, payload)


def inches_to_goto_mm(inches: float, offset_inches: float = 0.0) -> int:
    """Convert a true height in inches to the goto command's mm payload.

    `offset_inches` is the calibration offset (true = display + offset), so we
    subtract it to get back to the desk's internal/display height.
    """
    return round((inches - offset_inches) * MM_PER_INCH)


# --- Notification parsing ----------------------------------------------------

@dataclass
class Notification:
    opcode: int
    payload: bytes
    valid_checksum: bool


@dataclass
class HeightReading:
    raw: int  # device units (assumed 0.1 mm)


def parse_frame(data: bytes) -> Notification | None:
    """Parse one desk->host notification frame. Returns None if not a frame.

    Tolerant of the header being either F2F2 (notify) or F1F1 (some firmwares
    echo commands). Returns None for buffers too short to contain a frame.
    """
    if len(data) < 5:
        return None
    if data[:2] not in (NOTIFY_HEADER, CMD_HEADER):
        return None
    opcode = data[2]
    length = data[3]
    end = 4 + length
    if len(data) < end + 2:
        return None
    payload = data[4:end]
    chk = data[end]
    return Notification(
        opcode=opcode,
        payload=payload,
        valid_checksum=(chk == checksum(opcode, payload)),
    )


def parse_height(note: Notification) -> HeightReading | None:
    """Extract a raw height from a height notification, if this is one.

    CONFIRMED on the Apex Pro (L-BTMEB95): payload is [hi, lo, status] with
    height in bytes 0-1 big-endian, 0.1 inch units.
    """
    if note.opcode != Op.NOTIFY_HEIGHT:
        return None
    p = note.payload
    if len(p) < 2:
        return None
    return HeightReading(raw=int.from_bytes(p[0:2], "big"))


# --- Calibration (raw <-> inches) -------------------------------------------
# Kept here so the rest of the app speaks inches and never touches raw units.

def raw_to_inches(raw: int, raw_units_per_inch: float, offset_inches: float) -> float:
    return raw / raw_units_per_inch + offset_inches


def inches_to_raw(inches: float, raw_units_per_inch: float, offset_inches: float) -> int:
    return round((inches - offset_inches) * raw_units_per_inch)
