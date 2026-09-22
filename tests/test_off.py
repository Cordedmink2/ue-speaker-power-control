import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from ue_speaker_off import OFF_PAYLOAD, send_off, validate_mac


def test_master_remote_off_payload_is_exact():
    assert OFF_PAYLOAD == bytes.fromhex("0201b6")
    assert OFF_PAYLOAD != bytes.fromhex("0301b900")


def test_validate_mac():
    assert validate_mac("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"
    assert validate_mac("AA-BB-CC-DD-ee-ff") == "AA:BB:CC:DD:EE:FF"


@pytest.mark.parametrize("value", ["AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00", "not-a-mac"])
def test_validate_mac_rejects_bad_values(value):
    with pytest.raises(ValueError):
        validate_mac(value)


def test_send_off_uses_rfcomm_payload_and_channel(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def settimeout(self, value):
            self.timeout = value
        def connect(self, value):
            self.connected_to = value
        def sendall(self, value):
            self.sent = value

    fake = FakeSocket()
    monkeypatch.setattr("ue_speaker_off.socket.socket", lambda *args: fake)
    send_off("AA:BB:CC:DD:EE:FF", 1, 15)
    assert fake.connected_to == ("AA:BB:CC:DD:EE:FF", 1)
    assert fake.sent == bytes.fromhex("0201b6")
