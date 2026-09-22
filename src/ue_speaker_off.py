#!/usr/bin/env python3
"""Send the documented UE MasterRemoteOff command over RFCOMM/SPP."""
from __future__ import annotations

import argparse
import json
import socket
from dataclasses import asdict, dataclass

OFF_PAYLOAD = b"\x02\x01\xb6"
DEFAULT_RFCOMM_CHANNEL = 1


@dataclass(frozen=True)
class OffResult:
    ok: bool
    stage: str
    speaker: str
    channel: int
    payload_hex: str
    error: str | None = None


def validate_mac(value: str) -> str:
    parts = value.replace("-", ":").split(":")
    if len(parts) != 6 or any(len(part) != 2 for part in parts):
        raise ValueError("speaker address must contain six hexadecimal octets")
    try:
        bytes(int(part, 16) for part in parts)
    except ValueError as exc:
        raise ValueError("speaker address must contain hexadecimal octets") from exc
    return ":".join(part.upper() for part in parts)


def send_off(speaker: str, channel: int, timeout: float) -> None:
    address = validate_mac(speaker)
    if not 1 <= channel <= 30:
        raise ValueError("RFCOMM channel must be between 1 and 30")
    with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM) as sock:
        sock.settimeout(timeout)
        sock.connect((address, channel))
        sock.sendall(OFF_PAYLOAD)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("speaker_address")
    parser.add_argument("--channel", type=int, default=DEFAULT_RFCOMM_CHANNEL)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        speaker = validate_mac(args.speaker_address)
        send_off(speaker, args.channel, args.timeout)
        result = OffResult(True, "rfcomm_write", speaker, args.channel, OFF_PAYLOAD.hex())
    except (OSError, ValueError) as exc:
        result = OffResult(False, "rfcomm_connection_or_write", args.speaker_address, args.channel, OFF_PAYLOAD.hex(), str(exc))
    if args.json:
        print(json.dumps(asdict(result), sort_keys=True))
    else:
        print("RFCOMM off command sent" if result.ok else f"RFCOMM off failed: {result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
