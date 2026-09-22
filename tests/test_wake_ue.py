import asyncio
import struct
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import le_att
from ue_speaker_power import (
    POWER_CHARACTERISTIC,
    POWER_VALUE_HANDLE,
    WakeError,
    forced_le_write,
    mac_payload,
    wake_once,
)


def test_payload_colon_case():
    assert mac_payload("44:80:EB:ED:C1:74") == bytes.fromhex("4480ebedc17401")


def test_payload_dash_and_plain_case():
    expected = bytes.fromhex("4480ebedc17401")
    assert mac_payload("44-80-eb-ed-c1-74") == expected
    assert mac_payload("4480eBeDc174") == expected


@pytest.mark.parametrize("value", ["44:80:EB:ED:C1", "44:80:EB:ED:C1:74:00", "44:80:EB:ED:C1:7Z", "44:80:EB:ED:C1:74x", ""])
def test_payload_rejects_malformed(value):
    with pytest.raises(ValueError):
        mac_payload(value)


def test_payload_order_and_single_on_byte():
    payload = mac_payload("01:02:03:04:05:06")
    assert payload == b"\x01\x02\x03\x04\x05\x06\x01"
    assert len(payload) == 7


def test_off_payload_command_byte_is_two():
    assert mac_payload("01:02:03:04:05:06", 2) == bytes.fromhex("01020304050602")


@pytest.mark.parametrize("command", [0, 3, -1, 255])
def test_payload_rejects_unknown_command_bytes(command):
    """Only the two documented power commands may ever be emitted. In particular
    this keeps the separate BLE-standby-disable command out of the codebase."""
    with pytest.raises(ValueError):
        mac_payload("01:02:03:04:05:06", command)


# --- ON path: must remain the unchanged bleak/D-Bus implementation ------------


def _fake_bleak_client():
    class Characteristic:
        uuid = POWER_CHARACTERISTIC
        properties = ["write"]

    class Service:
        characteristics = [Characteristic()]

    client = type("Client", (), {
        "is_connected": True,
        "services": [Service()],
        "__aenter__": AsyncMock(return_value=None),
        "__aexit__": AsyncMock(return_value=None),
        "write_gatt_char": AsyncMock(),
    })()
    client.__aenter__.return_value = client
    return client


def test_write_call_details(monkeypatch):
    client = _fake_bleak_client()
    monkeypatch.setattr("ue_speaker_power.find_device", AsyncMock(return_value=object()))
    monkeypatch.setattr("ue_speaker_power.BleakClient", lambda *args, **kwargs: client)
    asyncio.run(wake_once("aa:bb:cc:dd:ee:ff", "01:02:03:04:05:06", 10))
    client.write_gatt_char.assert_awaited_once()
    args, kwargs = client.write_gatt_char.await_args
    assert args[0] == POWER_CHARACTERISTIC
    assert args[1] == bytes.fromhex("01020304050601")
    assert kwargs == {"response": True}


def test_on_does_not_use_the_forced_le_path(monkeypatch):
    client = _fake_bleak_client()
    forced = AsyncMock()
    monkeypatch.setattr("ue_speaker_power.find_device", AsyncMock(return_value=object()))
    monkeypatch.setattr("ue_speaker_power.BleakClient", lambda *args, **kwargs: client)
    monkeypatch.setattr("ue_speaker_power.forced_le_write", forced)
    asyncio.run(wake_once("aa:bb:cc:dd:ee:ff", "01:02:03:04:05:06", 10))
    forced.assert_not_awaited()


# --- OFF path: forced LE ------------------------------------------------------


def test_off_uses_forced_le_write_not_bleak(monkeypatch):
    """OFF must bypass bleak/D-Bus entirely: BlueZ routes an awake dual-mode
    speaker to BR/EDR and the GATT write fails with 'Not connected'."""
    forced = AsyncMock()
    bleak_used = AsyncMock()
    monkeypatch.setattr("ue_speaker_power.forced_le_write", forced)
    monkeypatch.setattr("ue_speaker_power.find_device", bleak_used)
    asyncio.run(wake_once("aa:bb:cc:dd:ee:ff", "01:02:03:04:05:06", 10, "off"))
    forced.assert_awaited_once_with(
        "aa:bb:cc:dd:ee:ff", bytes.fromhex("01020304050602"), 10, "01:02:03:04:05:06"
    )
    bleak_used.assert_not_awaited()


def test_off_no_longer_preconnects_over_classic():
    """Regression: the old implementation shelled out to bluetoothctl to establish
    a BR/EDR link first, which is exactly what made the OFF write fail."""
    import ue_speaker_power

    assert not hasattr(ue_speaker_power, "ensure_classic_connection")
    source = (Path(__file__).parents[1] / "src" / "ue_speaker_power.py").read_text()
    assert "bluetoothctl" not in source


def test_no_module_shells_out_to_gatttool():
    """The forced-LE path is implemented with sockets, not a deprecated binary.
    gatttool may only be named in prose, never invoked."""
    for name in ("ue_speaker_power.py", "le_att.py"):
        source = (Path(__file__).parents[1] / "src" / name).read_text()
        assert "create_subprocess" not in source
        assert "subprocess" not in source


def test_forced_le_write_passes_arguments_through(monkeypatch):
    seen = {}

    def fake_write(adapter, peer, handle, value, timeout):
        seen.update(adapter=adapter, peer=peer, handle=handle, value=value, timeout=timeout)

    monkeypatch.setattr(le_att, "write_characteristic", fake_write)
    asyncio.run(forced_le_write("aa:bb:cc:dd:ee:ff", bytes.fromhex("01020304050602"), 12, "11:22:33:44:55:66"))
    assert seen["adapter"] == "11:22:33:44:55:66"
    assert seen["peer"] == "AA:BB:CC:DD:EE:FF"
    assert seen["handle"] == POWER_VALUE_HANDLE
    assert seen["value"] == bytes.fromhex("01020304050602")
    assert seen["timeout"] == 12


def test_forced_le_write_distinguishes_connection_from_write_failure(monkeypatch):
    def raise_connect(*args, **kwargs):
        raise le_att.LEConnectionError("LE connect timed out")

    monkeypatch.setattr(le_att, "write_characteristic", raise_connect)
    with pytest.raises(WakeError, match="le connection failure"):
        asyncio.run(forced_le_write("aa:bb:cc:dd:ee:ff", b"\x00" * 7, 10, "11:22:33:44:55:66"))

    def raise_write(*args, **kwargs):
        raise le_att.ATTWriteError("peer rejected the write")

    monkeypatch.setattr(le_att, "write_characteristic", raise_write)
    with pytest.raises(WakeError, match="le write failure"):
        asyncio.run(forced_le_write("aa:bb:cc:dd:ee:ff", b"\x00" * 7, 10, "11:22:33:44:55:66"))


def test_forced_le_write_rejects_a_malformed_speaker_address(monkeypatch):
    monkeypatch.setattr(le_att, "write_characteristic", lambda *a, **k: None)
    with pytest.raises(WakeError):
        asyncio.run(forced_le_write("not-a-mac", b"\x00" * 7, 10, "11:22:33:44:55:66"))


# --- le_att: the LE/ATT mechanics themselves ---------------------------------


def test_sockaddr_selects_att_cid_and_public_le_address():
    encoded = le_att._encode_sockaddr_l2("AA:BB:CC:DD:EE:FF", le_att.ATT_CID, le_att.BDADDR_LE_PUBLIC)
    family, psm = struct.unpack("<HH", encoded[:4])
    cid, bdaddr_type = struct.unpack("<HB", encoded[10:13])
    assert family == le_att.AF_BLUETOOTH
    assert psm == 0, "PSM must be 0 so the kernel uses a CID-based ATT channel"
    assert cid == le_att.ATT_CID
    assert bdaddr_type == le_att.BDADDR_LE_PUBLIC, "must not fall back to BR/EDR addressing"
    assert encoded[4:10] == bytes.fromhex("ffeeddccbbaa"), "address is little-endian"


@pytest.mark.parametrize("value", ["AA:BB:CC:DD:EE", "", "not-a-mac"])
def test_sockaddr_rejects_malformed_addresses(value):
    with pytest.raises(ValueError):
        le_att._encode_sockaddr_l2(value, le_att.ATT_CID, le_att.BDADDR_LE_PUBLIC)


def test_write_characteristic_rejects_bad_handle_and_timeout():
    with pytest.raises(ValueError):
        le_att.write_characteristic("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF", 0x0003, b"", timeout=0)
    with pytest.raises(ValueError):
        le_att.write_characteristic("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF", 0x10000, b"")


def test_interpret_accepts_only_a_real_write_response():
    le_att._interpret(bytes([le_att.ATT_OP_WRITE_RSP]))

    with pytest.raises(le_att.ATTWriteError, match="write not permitted"):
        le_att._interpret(bytes([le_att.ATT_OP_ERROR_RSP, 0x12, 0x03, 0x00, 0x03]))

    with pytest.raises(le_att.ATTWriteError, match="unexpected ATT response"):
        le_att._interpret(bytes([0x1B, 0x03, 0x00]))


def test_att_write_request_pdu_layout():
    """Opcode 0x12, little-endian handle, then the value."""
    pdu = struct.pack("<BH", le_att.ATT_OP_WRITE_REQ, 0x0003) + bytes.fromhex("01020304050602")
    assert pdu == bytes.fromhex("120300") + bytes.fromhex("01020304050602")


def test_write_characteristic_times_out_and_closes_the_socket(monkeypatch):
    closed = []

    class FakeSocket:
        def setblocking(self, flag):
            pass
        def fileno(self):
            return 7
        def close(self):
            closed.append(True)

    monkeypatch.setattr(le_att.socket, "socket", lambda *a, **k: FakeSocket())
    monkeypatch.setattr(le_att._libc, "bind", lambda *a: 0)
    monkeypatch.setattr(le_att._libc, "connect", lambda *a: -1)
    monkeypatch.setattr(le_att.ctypes, "get_errno", lambda: le_att.errno.EINPROGRESS)
    monkeypatch.setattr(le_att.select, "select", lambda *a: ([], [], []))

    with pytest.raises(le_att.LEConnectionError, match="timed out"):
        le_att.write_characteristic("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF", 0x0003, b"\x00", timeout=1)
    assert closed, "the L2CAP socket must be closed even when the connect fails"


def test_write_characteristic_reports_a_refused_connection(monkeypatch):
    class FakeSocket:
        def setblocking(self, flag):
            pass
        def fileno(self):
            return 7
        def close(self):
            pass

    monkeypatch.setattr(le_att.socket, "socket", lambda *a, **k: FakeSocket())
    monkeypatch.setattr(le_att._libc, "bind", lambda *a: 0)
    monkeypatch.setattr(le_att._libc, "connect", lambda *a: -1)
    monkeypatch.setattr(le_att.ctypes, "get_errno", lambda: le_att.errno.EHOSTDOWN)

    with pytest.raises(le_att.LEConnectionError, match="LE connect"):
        le_att.write_characteristic("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF", 0x0003, b"\x00", timeout=1)


def test_write_characteristic_reports_a_silent_peer(monkeypatch):
    """A connected link that never answers is an ATT failure, not a link failure."""
    class FakeSocket:
        def setblocking(self, flag):
            pass
        def fileno(self):
            return 7
        def send(self, data):
            return len(data)
        def close(self):
            pass

    monkeypatch.setattr(le_att.socket, "socket", lambda *a, **k: FakeSocket())
    monkeypatch.setattr(le_att._libc, "bind", lambda *a: 0)
    monkeypatch.setattr(le_att._libc, "connect", lambda *a: 0)
    monkeypatch.setattr(le_att.select, "select", lambda r, w, x, t: ([], [], []))

    with pytest.raises(le_att.ATTWriteError, match="no ATT response"):
        le_att.write_characteristic("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF", 0x0003, b"\x00", timeout=1)


def test_successful_write_sends_the_expected_pdu(monkeypatch):
    sent = []

    class FakeSocket:
        def setblocking(self, flag):
            pass
        def fileno(self):
            return 7
        def send(self, data):
            sent.append(data)
            return len(data)
        def recv(self, size):
            return bytes([le_att.ATT_OP_WRITE_RSP])
        def close(self):
            pass

    monkeypatch.setattr(le_att.socket, "socket", lambda *a, **k: FakeSocket())
    monkeypatch.setattr(le_att._libc, "bind", lambda *a: 0)
    monkeypatch.setattr(le_att._libc, "connect", lambda *a: 0)
    monkeypatch.setattr(le_att.select, "select", lambda r, w, x, t: ([object()], [object()], []))

    le_att.write_characteristic(
        "11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF", 0x0003, bytes.fromhex("01020304050602"), timeout=5
    )
    assert sent == [bytes.fromhex("120300") + bytes.fromhex("01020304050602")]
