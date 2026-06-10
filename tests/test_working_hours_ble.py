"""Working-hours windowing and BLE discovery heuristics."""

from datetime import datetime

from cadence import ble, scheduler
from cadence.ble import ScannedDevice
from cadence.config import Config


def _ts(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute).timestamp()


# --- working hours -----------------------------------------------------------
# 2026-06-08 is a Monday.

def test_disabled_working_hours_always_within():
    cfg = Config()  # disabled by default
    assert scheduler.within_working_hours(cfg, _ts(2026, 6, 7, 3, 0))  # Sunday 3am


def test_within_hours_on_weekday():
    cfg = Config()
    cfg.working_hours.enabled = True
    assert scheduler.within_working_hours(cfg, _ts(2026, 6, 8, 10, 0))
    assert not scheduler.within_working_hours(cfg, _ts(2026, 6, 8, 8, 59))
    assert not scheduler.within_working_hours(cfg, _ts(2026, 6, 8, 18, 0))  # end exclusive


def test_weekend_excluded():
    cfg = Config()
    cfg.working_hours.enabled = True
    assert not scheduler.within_working_hours(cfg, _ts(2026, 6, 13, 10, 0))  # Saturday


# --- BLE heuristics ----------------------------------------------------------

def test_looks_like_desk_by_name():
    dev = ScannedDevice(address="X", name="AiDesk Pro", rssi=-50)
    assert ble.looks_like_desk(dev)


def test_looks_like_desk_by_service_uuid():
    dev = ScannedDevice(
        address="X", name=None, rssi=-80,
        service_uuids=["0000fe60-0000-1000-8000-00805f9b34fb"],
    )
    assert ble.looks_like_desk(dev)


def test_random_device_is_not_a_desk():
    dev = ScannedDevice(address="X", name="Quest 3", rssi=-50,
                        service_uuids=["0000feb8-0000-1000-8000-00805f9b34fb"])
    assert not ble.looks_like_desk(dev)


def test_guess_characteristics_picks_write_and_notify():
    services = {
        "0000fe60-0000-1000-8000-00805f9b34fb": [
            {"uuid": "0000fe61-...", "handle": 1, "properties": ["write", "write-without-response"]},
            {"uuid": "0000fe62-...", "handle": 2, "properties": ["notify"]},
        ],
        "0000180a-0000-1000-8000-00805f9b34fb": [
            {"uuid": "00002a29-...", "handle": 3, "properties": ["read"]},
        ],
    }
    dev = ble.guess_characteristics(services)
    assert dev.service_uuid == "0000fe60-0000-1000-8000-00805f9b34fb"
    assert dev.command_char_uuid == "0000fe61-..."
    assert dev.notify_char_uuid == "0000fe62-..."


def test_guess_falls_back_to_known_defaults():
    dev = ble.guess_characteristics({})
    assert dev.command_char_uuid == "0000fe61-0000-1000-8000-00805f9b34fb"
    assert dev.notify_char_uuid == "0000fe62-0000-1000-8000-00805f9b34fb"
