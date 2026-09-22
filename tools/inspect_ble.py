#!/usr/bin/env python3
import asyncio
import json
import sys
from bleak import BleakClient, BleakScanner

ADDRESS = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:FF"
POWER = "c6d6dc0d-07f5-47ef-9b59-630622b01fd3"

async def main() -> None:
    print("Scanning...")
    device = await BleakScanner.find_device_by_address(ADDRESS, timeout=20.0)
    if device is None:
        print(json.dumps({"found": False}))
        raise SystemExit(2)
    print(json.dumps({"found": True, "address": device.address, "name": device.name, "details": str(device.details)}, default=str))
    async with BleakClient(device, timeout=20.0) as client:
        print(json.dumps({
            "connected": client.is_connected,
            "services": [
                {"uuid": service.uuid, "characteristics": [
                    {"uuid": char.uuid, "handle": char.handle, "properties": list(char.properties)}
                    for char in service.characteristics
                ]}
                for service in client.services
            ],
        }))
        matches = [char for service in client.services for char in service.characteristics if char.uuid.lower() == POWER]
        print(json.dumps({"power_matches": [{"uuid": c.uuid, "handle": c.handle, "properties": list(c.properties)} for c in matches]}))
        for uuid in ["00002a00-0000-1000-8000-00805f9b34fb", "00002a28-0000-1000-8000-00805f9b34fb", "00002a19-0000-1000-8000-00805f9b34fb", "00002a27-0000-1000-8000-00805f9b34fb"]:
            try:
                value = await client.read_gatt_char(uuid)
                print(json.dumps({"read_uuid": uuid, "hex": bytes(value).hex(), "text": bytes(value).rstrip(b"\\0").decode("utf-8", "replace")}))
            except Exception as exc:
                print(json.dumps({"read_uuid": uuid, "error": f"{type(exc).__name__}: {exc}"}))

asyncio.run(main())
