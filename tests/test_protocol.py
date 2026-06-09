"""Tests for the wire protocol and calibration math (no hardware needed)."""

from cadence import protocol
from cadence.protocol import Op


def test_frame_examples_match_reference():
    # From the reverse-engineered reference frames.
    assert protocol.cmd_up() == bytes([0xF1, 0xF1, 0x01, 0x00, 0x01, 0x7E])
    assert protocol.cmd_down() == bytes([0xF1, 0xF1, 0x02, 0x00, 0x02, 0x7E])
    assert protocol.cmd_stop() == bytes([0xF1, 0xF1, 0x2B, 0x00, 0x2B, 0x7E])
    assert protocol.cmd_request_limits() == bytes([0xF1, 0xF1, 0x07, 0x00, 0x07, 0x7E])


def test_goto_frame_matches_live_capture():
    # goto 683mm (26.9in) — frame accepted by the desk live on 2026-06-09:
    # f1 f1 1b 02 02 ab ca 7e
    frame = protocol.cmd_goto_mm(683)
    assert frame == bytes.fromhex("f1f11b0202abca7e")
    assert frame[:2] == b"\xf1\xf1"
    assert frame[2] == Op.GOTO_HEIGHT
    assert frame[3] == 0x02
    assert frame[4:6] == (683).to_bytes(2, "big")
    expected_chk = (Op.GOTO_HEIGHT + 2 + sum(frame[4:6])) & 0xFF
    assert frame[6] == expected_chk
    assert frame[7] == 0x7E


def test_inches_to_goto_mm():
    assert protocol.inches_to_goto_mm(26.9) == 683
    assert protocol.inches_to_goto_mm(44.9) == 1140
    # calibration offset: true = display + offset, command wants display-mm
    assert protocol.inches_to_goto_mm(26.9, offset_inches=0.5) == round(26.4 * 25.4)


def test_goto_mm_rejects_out_of_range():
    import pytest

    with pytest.raises(ValueError):
        protocol.cmd_goto_mm(0x1_0000)


def test_parse_height_notification_real_frames():
    # Captured live from the Apex Pro (L-BTMEB95) on 2026-06-09.
    standing = bytes.fromhex("f2f2010301c007cc7e")
    sitting = bytes.fromhex("f2f20103010d07197e")
    for frame, expected_raw in ((standing, 448), (sitting, 269)):
        note = protocol.parse_frame(frame)
        assert note is not None
        assert note.valid_checksum
        reading = protocol.parse_height(note)
        assert reading is not None
        assert reading.raw == expected_raw

    # raw -> inches with the confirmed 0.1in units
    assert protocol.raw_to_inches(448, 10.0, 0.0) == 44.8
    assert protocol.raw_to_inches(269, 10.0, 0.0) == 26.9


def test_parse_frame_rejects_garbage():
    assert protocol.parse_frame(b"\x00\x01") is None
    assert protocol.parse_frame(b"") is None


def test_calibration_roundtrip():
    scale, offset = 254.0, -2.0
    for inches in (22.0, 26.8, 44.9, 50.0):
        raw = protocol.inches_to_raw(inches, scale, offset)
        back = protocol.raw_to_inches(raw, scale, offset)
        assert abs(back - inches) < 0.01
