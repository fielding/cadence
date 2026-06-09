"""Tests for HID idle parsing."""

from cadence.presence import parse_hid_idle


def test_parses_idle_nanoseconds():
    sample = '    | |   "HIDIdleTime" = 12500000000\n'
    assert parse_hid_idle(sample) == 12.5


def test_missing_key_returns_none():
    assert parse_hid_idle("no idle info here") is None
