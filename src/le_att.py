#!/usr/bin/env python3
"""Minimal ATT client that explicitly selects the Bluetooth LE bearer.

Why this module exists
----------------------
Dual-mode speakers such as the MEGABOOM 3 expose their vendor power characteristic
only over LE, but they also advertise BR/EDR profiles. Once such a speaker is awake
its BR/EDR radio is up, and BlueZ's ``org.bluez.Device1.Connect()`` chooses the
bearer on the caller's behalf, preferring BR/EDR. A GATT write issued through that
generic D-Bus path then fails with ``org.bluez.Error.Failed: Not connected``.

Opening an L2CAP socket bound to the ATT CID with an explicit LE address type makes
the bearer choice unambiguous: the kernel issues ``LE Extended Create Connection``
and the ATT PDU is carried over LE. This mirrors what BlueZ's own ``gatttool`` does
(see ``attrib/utils.c`` in the BlueZ sources: when ``psm == 0`` it calls
``bt_io_connect`` with ``BT_IO_OPT_CID = ATT_CID`` and
``BT_IO_OPT_DEST_TYPE = BDADDR_LE_PUBLIC``), but without depending on that
deprecated binary.

Only the Python standard library is used. No elevated privileges are required.
"""
from __future__ import annotations

import ctypes
import errno
import os
import select
import socket
import struct

# Values from the Linux kernel Bluetooth headers (include/net/bluetooth/bluetooth.h,
# l2cap.h). CPython's sockaddr_l2 support predates the LE fields, so the sockaddr is
# built by hand and passed to libc bind()/connect().
AF_BLUETOOTH = 31
BTPROTO_L2CAP = 0
ATT_CID = 4
BDADDR_LE_PUBLIC = 1

# Attribute protocol opcodes (Bluetooth Core Specification, Vol 3, Part F).
ATT_OP_WRITE_REQ = 0x12
ATT_OP_WRITE_RSP = 0x13
ATT_OP_ERROR_RSP = 0x01

ATT_ERRORS = {
    0x01: "invalid handle",
    0x02: "read not permitted",
    0x03: "write not permitted",
    0x05: "insufficient authentication",
    0x06: "request not supported",
    0x07: "invalid offset",
    0x08: "insufficient authorization",
    0x0D: "invalid attribute value length",
    0x0E: "unlikely error",
    0x0F: "insufficient encryption",
    0x11: "insufficient resources",
}

_libc = ctypes.CDLL("libc.so.6", use_errno=True)


class LEConnectionError(Exception):
    """The LE link to the peer could not be established."""


class ATTWriteError(Exception):
    """The LE link came up but the peer rejected or ignored the ATT write."""


def _encode_sockaddr_l2(address: str, cid: int, bdaddr_type: int) -> bytes:
    """Build a ``struct sockaddr_l2`` for an LE ATT channel.

    struct sockaddr_l2 {
        sa_family_t     l2_family;
        unsigned short  l2_psm;       /* little-endian */
        bdaddr_t        l2_bdaddr;    /* six octets, reversed */
        unsigned short  l2_cid;       /* little-endian */
        uint8_t         l2_bdaddr_type;
    };
    """
    octets = address.split(":")
    if len(octets) != 6:
        raise ValueError("address must contain six octets")
    packed = bytes(int(octet, 16) for octet in reversed(octets))
    return (
        struct.pack("<H", AF_BLUETOOTH)
        + struct.pack("<H", 0)  # PSM 0 selects a CID-based (ATT) channel
        + packed
        + struct.pack("<H", cid)
        + struct.pack("<B", bdaddr_type)
    )


def _bind(sock: socket.socket, address: str) -> None:
    sockaddr = _encode_sockaddr_l2(address, ATT_CID, BDADDR_LE_PUBLIC)
    if _libc.bind(sock.fileno(), sockaddr, len(sockaddr)) != 0:
        code = ctypes.get_errno()
        raise LEConnectionError(f"could not bind local adapter {address}: {os.strerror(code)}")


def _connect(sock: socket.socket, address: str, timeout: float) -> None:
    sockaddr = _encode_sockaddr_l2(address, ATT_CID, BDADDR_LE_PUBLIC)
    if _libc.connect(sock.fileno(), sockaddr, len(sockaddr)) == 0:
        return
    code = ctypes.get_errno()
    if code not in (errno.EINPROGRESS, errno.EAGAIN):
        raise LEConnectionError(f"LE connect to {address} failed: {os.strerror(code)}")
    if not select.select([], [sock], [], timeout)[1]:
        raise LEConnectionError(f"LE connect to {address} timed out after {timeout:.1f}s")
    pending = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if pending != 0:
        raise LEConnectionError(f"LE connect to {address} failed: {os.strerror(pending)}")


def _read_response(sock: socket.socket, timeout: float) -> bytes:
    if not select.select([sock], [], [], timeout)[0]:
        raise ATTWriteError(f"no ATT response within {timeout:.1f}s")
    response = sock.recv(64)
    if not response:
        raise ATTWriteError("peer closed the ATT channel without responding")
    return response


def _interpret(response: bytes) -> None:
    opcode = response[0]
    if opcode == ATT_OP_WRITE_RSP:
        return
    if opcode == ATT_OP_ERROR_RSP and len(response) >= 5:
        code = response[4]
        raise ATTWriteError(f"peer rejected the write: {ATT_ERRORS.get(code, 'unknown error')} (0x{code:02x})")
    raise ATTWriteError(f"unexpected ATT response 0x{opcode:02x}")


def write_characteristic(
    adapter_address: str,
    peer_address: str,
    value_handle: int,
    value: bytes,
    timeout: float = 20.0,
) -> None:
    """Write ``value`` to ``value_handle`` over an explicitly LE ATT connection.

    Raises ``LEConnectionError`` if the LE link cannot be established and
    ``ATTWriteError`` if the link is up but the write is not acknowledged. The
    distinction matters: the former usually means the speaker is out of range or
    the adapter is busy, the latter means the protocol interaction itself failed.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if not 0x0001 <= value_handle <= 0xFFFF:
        raise ValueError("attribute handle must be in the range 0x0001-0xffff")
    sock = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
    try:
        sock.setblocking(False)
        _bind(sock, adapter_address)
        _connect(sock, peer_address, timeout)
        try:
            sock.send(struct.pack("<BH", ATT_OP_WRITE_REQ, value_handle) + value)
        except OSError as exc:
            raise ATTWriteError(f"could not send the ATT write request: {exc}") from exc
        _interpret(_read_response(sock, timeout))
    finally:
        sock.close()
