#!/usr/bin/env python3
"""Wake a compatible Ultimate Ears speaker over its standby BLE GATT service."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

import le_att

POWER_CHARACTERISTIC = "c6d6dc0d-07f5-47ef-9b59-630622b01fd3"
MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{12}$")
LOG = logging.getLogger("ue_wake")


class WakeError(Exception):
    """A classified, user-facing wake failure."""


@dataclass(frozen=True)
class WakeResult:
    ok: bool
    stage: str
    speaker: str
    controller: str
    elapsed_seconds: float
    attempts: int
    error: str | None = None


def normalize_mac(value: str) -> str:
    """Validate a six-octet Bluetooth MAC and return colon form."""
    if not isinstance(value, str) or not MAC_RE.fullmatch(value.strip()):
        raise ValueError("MAC must be exactly six hexadecimal octets")
    compact = value.strip().replace(":", "").replace("-", "").lower()
    if len(compact) != 12:
        raise ValueError("MAC must contain exactly six octets")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def mac_payload(controller_mac: str, command: int = 1) -> bytes:
    """Encode controller MAC bytes followed by the documented power byte."""
    canonical = normalize_mac(controller_mac)
    if command not in (1, 2):
        raise ValueError("power command must be 1 (on) or 2 (off)")
    return bytes.fromhex(canonical.replace(":", "")) + bytes((command,))


def _load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WakeError(f"config failure: {exc}") from exc
    if not isinstance(data, dict):
        raise WakeError("config failure: root must be an object")
    return data


async def find_device(address: str, timeout: float) -> BLEDevice:
    device = await BleakScanner.find_device_by_address(address, timeout=timeout)
    if device is None:
        raise WakeError("discovery failure: speaker was not found")
    return device


POWER_VALUE_HANDLE = 0x0003


async def forced_le_write(
    speaker_address: str, payload: bytes, timeout: float, adapter_address: str
) -> None:
    """Write the power characteristic over an explicitly forced LE (ATT) bearer.

    BlueZ's org.bluez.Device1.Connect() selects the bearer itself and prefers
    BR/EDR for this dual-mode speaker, so a GATT write issued through the generic
    D-Bus path fails with 'Not connected' once the speaker is awake. le_att opens
    an L2CAP channel on the ATT CID with an explicit LE address type instead,
    which forces LE Extended Create Connection regardless of BR/EDR availability.
    """
    try:
        await asyncio.to_thread(
            le_att.write_characteristic,
            adapter_address,
            normalize_mac(speaker_address).upper(),
            POWER_VALUE_HANDLE,
            payload,
            timeout,
        )
    except le_att.LEConnectionError as exc:
        raise WakeError(f"le connection failure: {exc}") from exc
    except le_att.ATTWriteError as exc:
        raise WakeError(f"le write failure: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise WakeError(f"le write failure: {exc}") from exc


async def wake_once(speaker_address: str, controller_address: str, timeout: float, action: str = "on") -> None:
    """Send a power command.

    ON uses the bleak/D-Bus path, which is known-good: the speaker is in LE standby
    with its BR/EDR radio down, so BlueZ has no classic bearer to prefer.

    OFF must use the forced-LE path. When the speaker is awake its BR/EDR radio is
    up, and BlueZ 5.72 then routes Device1.Connect() to BR/EDR, so the GATT write
    fails with 'Not connected'. See forced_le_write for the upstream reference.
    """
    if action == "off":
        await forced_le_write(
            speaker_address, mac_payload(controller_address, 2), timeout, controller_address
        )
        return
    device = await find_device(speaker_address, timeout)
    try:
        async with BleakClient(device, timeout=max(10.0, timeout)) as client:
            if not client.is_connected:
                raise WakeError("connection failure: client is not connected")
            characteristic = next(
                (char for service in client.services for char in service.characteristics
                 if char.uuid.lower() == POWER_CHARACTERISTIC),
                None,
            )
            if characteristic is None:
                raise WakeError("characteristic failure: power characteristic is absent")
            if "write" not in characteristic.properties:
                raise WakeError("characteristic failure: power characteristic lacks write-with-response")
            await client.write_gatt_char(
                POWER_CHARACTERISTIC,
                mac_payload(controller_address, 1 if action == "on" else 2),
                response=True,
            )
    except WakeError:
        raise
    except BleakError as exc:
        message = str(exc)
        stage = "authorization failure" if "author" in message.lower() else "write failure"
        raise WakeError(f"{stage}: {message}") from exc
    except (OSError, asyncio.TimeoutError) as exc:
        raise WakeError(f"connection failure: {exc}") from exc


async def wake(config: dict[str, Any], retries: int, timeout: float, action: str = "on") -> WakeResult:
    speaker = normalize_mac(str(config["speaker_address"]))
    controller = normalize_mac(str(config["controller_address"]))
    started = time.monotonic()
    last_error: str | None = None
    for attempt in range(1, retries + 2):
        try:
            await wake_once(speaker, controller, timeout, action)
            return WakeResult(True, "write", speaker, controller, time.monotonic() - started, attempt)
        except (WakeError, KeyError, ValueError) as exc:
            last_error = str(exc)
            LOG.warning("attempt %d/%d failed: %s", attempt, retries + 1, last_error)
            if attempt <= retries:
                await asyncio.sleep(0.5)
    stage = last_error.split(":", 1)[0] if last_error else "failure"
    return WakeResult(False, stage, speaker, controller, time.monotonic() - started, retries + 1, last_error)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--action", choices=("on", "off"), default="on")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.retries < 0 or args.retries > 3 or args.timeout <= 0:
        print("invalid retry or timeout value", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        config = _load_config(args.config)
        result = asyncio.run(wake(config, args.retries, args.timeout, args.action))
    except WakeError as exc:
        result = WakeResult(False, "config", "", "", 0.0, 0, str(exc))
    except (KeyError, ValueError) as exc:
        result = WakeResult(False, "config", "", "", 0.0, 0, str(exc))
    if args.json_output:
        print(json.dumps(asdict(result), sort_keys=True))
    else:
        print("Wake command written" if result.ok else f"Wake failed ({result.stage}): {result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
