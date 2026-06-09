"""BLE transport using bleak.

Two responsibilities:
  1. scan() — discover nearby devices and dump services/characteristics so the
     desk can be identified (Phase 1).
  2. DeskClient — connect to a known device, subscribe to height notifications,
     and write command frames (Phase 2+).

This module is protocol-aware only insofar as it knows which characteristics to
talk to; framing/parsing lives in protocol.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import protocol
from .config import Device

log = logging.getLogger("cadence.ble")


# --- Discovery ---------------------------------------------------------------

@dataclass
class ScannedDevice:
    address: str
    name: str | None
    rssi: int
    service_uuids: list[str] = field(default_factory=list)


async def scan(timeout: float = 8.0) -> list[ScannedDevice]:
    """Scan for BLE devices and return them sorted by signal strength."""
    found: dict[str, ScannedDevice] = {}

    def _on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
        found[device.address] = ScannedDevice(
            address=device.address,
            name=adv.local_name or device.name,
            rssi=adv.rssi,
            service_uuids=list(adv.service_uuids or []),
        )

    scanner = BleakScanner(detection_callback=_on_detect)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return sorted(found.values(), key=lambda d: d.rssi, reverse=True)


def looks_like_desk(dev: ScannedDevice) -> bool:
    """Heuristic: name hints or a known Jiecang service UUID in the advert."""
    name = (dev.name or "").lower()
    if any(hint in name for hint in ("desk", "uplift", "aidesk", "jiecang", "apex")):
        return True
    advertised = {u.lower() for u in dev.service_uuids}
    return bool(advertised & {u.lower() for u in protocol.CANDIDATE_SERVICE_UUIDS})


async def inspect(address: str, timeout: float = 20.0) -> dict[str, list[dict]]:
    """Connect and enumerate services/characteristics for protocol discovery."""
    out: dict[str, list[dict]] = {}
    async with BleakClient(address, timeout=timeout) as client:
        for service in client.services:
            chars = []
            for ch in service.characteristics:
                chars.append(
                    {
                        "uuid": ch.uuid,
                        "handle": ch.handle,
                        "properties": list(ch.properties),
                    }
                )
            out[service.uuid] = chars
    return out


def guess_characteristics(services: dict[str, list[dict]]) -> Device:
    """Pick the most likely service + command/notify characteristics."""
    dev = Device()
    candidates = {u.lower() for u in protocol.CANDIDATE_SERVICE_UUIDS}
    for svc_uuid, chars in services.items():
        if svc_uuid.lower() not in candidates:
            continue
        dev.service_uuid = svc_uuid
        for ch in chars:
            props = set(ch["properties"])
            if {"write", "write-without-response"} & props and not dev.command_char_uuid:
                dev.command_char_uuid = ch["uuid"]
            if "notify" in props and not dev.notify_char_uuid:
                dev.notify_char_uuid = ch["uuid"]
    # Fall back to the well-known Jiecang characteristics.
    dev.command_char_uuid = dev.command_char_uuid or protocol.DEFAULT_COMMAND_CHAR_UUID
    dev.notify_char_uuid = dev.notify_char_uuid or protocol.DEFAULT_NOTIFY_CHAR_UUID
    return dev


# --- Live connection ---------------------------------------------------------

HeightCallback = Callable[[protocol.HeightReading], None]


class DeskClient:
    """A connected desk. Use as an async context manager.

    Holds the latest observed raw height and invokes an optional callback on
    every height notification (used to detect manual moves).
    """

    def __init__(self, device: Device, on_height: HeightCallback | None = None):
        if not device.address:
            raise ValueError("device.address is not set — run `cadence scan` first")
        self.device = device
        self.command_uuid = device.command_char_uuid or protocol.DEFAULT_COMMAND_CHAR_UUID
        self.notify_uuid = device.notify_char_uuid or protocol.DEFAULT_NOTIFY_CHAR_UUID
        self._client = BleakClient(device.address)
        self._on_height = on_height
        self.latest_height: protocol.HeightReading | None = None

    async def __aenter__(self) -> "DeskClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def connect(self, timeout: float = 20.0) -> None:
        await self._client.connect(timeout=timeout)
        try:
            await self._client.start_notify(self.notify_uuid, self._handle_notify)
        except Exception as e:  # notify is optional on some firmwares
            log.warning("could not subscribe to notifications: %s", e)

    async def disconnect(self) -> None:
        try:
            if self._client.is_connected:
                await self._client.stop_notify(self.notify_uuid)
        except Exception:
            pass
        await self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    def _handle_notify(self, _char, data: bytearray) -> None:
        note = protocol.parse_frame(bytes(data))
        if note is None:
            log.debug("unparsed notification: %s", bytes(data).hex())
            return
        reading = protocol.parse_height(note)
        if reading is not None:
            self.latest_height = reading
            if self._on_height:
                self._on_height(reading)

    async def _write(self, frame: bytes) -> None:
        # response=True: confirmed live that a write-without-response sent just
        # before disconnect can be silently lost — fatal for STOP frames.
        log.debug("write %s", frame.hex())
        await self._client.write_gatt_char(self.command_uuid, frame, response=True)

    # High-level commands -----------------------------------------------------

    async def move_up(self) -> None:
        # NOTE: one frame = continuous movement until stop() (confirmed live).
        await self._write(protocol.cmd_up())

    async def move_down(self) -> None:
        await self._write(protocol.cmd_down())

    async def stop(self) -> None:
        await self._write(protocol.cmd_stop())
        # Hold the connection a beat so the controller acts on it before any
        # caller disconnects (a goto otherwise keeps driving the desk).
        await asyncio.sleep(0.3)

    async def goto_mm(self, mm: int) -> None:
        """Absolute move in millimeters. Callers MUST bound-check first: the
        controller clamps out-of-range targets and travels to its own limits,
        and the move continues even if we disconnect."""
        await self._write(protocol.cmd_goto_mm(mm))

    async def request_limits(self) -> None:
        await self._write(protocol.cmd_request_limits())

    async def read_height(self, wait: float = 2.0) -> protocol.HeightReading | None:
        """Poll the desk for its height and wait for the notification.

        The Apex Pro sends no passive height notifications; writing the
        REQUEST_LIMITS frame (confirmed live) wakes the height stream.
        """
        if self.latest_height is None:
            try:
                await self._write(protocol.cmd_request_limits())
            except Exception as e:  # noqa: BLE001
                log.warning("height poll write failed: %s", e)
        deadline = wait
        step = 0.1
        while self.latest_height is None and deadline > 0:
            await asyncio.sleep(step)
            deadline -= step
        return self.latest_height
