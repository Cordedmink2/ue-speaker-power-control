# UE speaker remote power control

Turn an Ultimate Ears Bluetooth speaker on and off from a Linux host, including from Home Assistant. Tested on an **Ultimate Ears MEGABOOM 3**.

This does one thing deliberately: power control. It is not a media player and not a general-purpose UE protocol library.

## Status

- **ON** — physically verified repeatedly on the target MEGABOOM 3.
- **OFF** — physically verified repeatedly, using an explicitly forced LE connection.
- **Remote wake after OFF** — verified. The speaker stays reachable over BLE in standby.
- **Home Assistant** — example shell-command/script configuration included and running in the author's deployment.
- **Compatibility** — only physically validated on one MEGABOOM 3. Other UE models appear in the research sources below, but this project makes no compatibility claim for them.

The protocol is unofficial and reverse engineered. Use it only with hardware you own or are authorised to control.

## How it works

Both commands write seven bytes to the UE power characteristic:

```text
c6d6dc0d-07f5-47ef-9b59-630622b01fd3
```

The payload is:

```text
six bytes of a Bluetooth controller address the speaker has already been paired with
one command byte: 01 for ON, 02 for OFF
```

The write is a normal GATT write-with-response.

### Why OFF needs a forced LE connection

This is the interesting part, and it is worth understanding before you debug anything.

The MEGABOOM 3 is a dual-mode device. The power characteristic lives on BLE, but the speaker also advertises BR/EDR ("classic") profiles for audio. BlueZ's `org.bluez.Device1.Connect()` picks the bearer for you, and it prefers BR/EDR when BR/EDR is available.

That produces an asymmetry that is easy to misread:

- **Speaker asleep** — its BR/EDR radio is down, so there is no classic bearer to prefer. BlueZ connects over LE and the write works. This is why ON works through the ordinary BLE library path.
- **Speaker awake** — its BR/EDR radio is up, BlueZ connects over BR/EDR, and the GATT write then fails with `org.bluez.Error.Failed: Not connected`.

That error looks like a profile or adapter problem. It is not. It is bearer selection, and it is why OFF fails while ON succeeds on the same hardware with the same payload format.

The fix is to stop letting BlueZ choose. `src/le_att.py` opens an L2CAP socket bound to the ATT CID (`4`) with an explicit LE public destination address, which makes the kernel issue `LE Extended Create Connection` regardless of what BR/EDR is doing. It then sends a single ATT Write Request and checks for a real ATT Write Response.

This mirrors what BlueZ's own `gatttool` does — in the BlueZ source, `attrib/utils.c` calls `bt_io_connect` with `BT_IO_OPT_CID = ATT_CID` and `BT_IO_OPT_DEST_TYPE = BDADDR_LE_PUBLIC` when the PSM is 0 — but without depending on that deprecated binary.

You can confirm the bearer yourself with `btmon`. A correct OFF looks like this, with no BR/EDR `Create Connection (0x01|0x0005)` anywhere in the sequence:

```text
< HCI Command: LE Extended Create Connection (0x08|0x0043)
        Peer address type: Public (0x00)
> HCI Event: LE Meta Event — LE Enhanced Connection Complete (0x0a)
        Status: Success (0x00)
< ACL Data TX: ATT: Write Request (0x12)
> ACL Data RX: ATT: Write Response (0x13)
```

### What is deliberately not implemented

The speaker also exposes an SPP/LWACP service. The historical `02 01 B6` RFCOMM command is kept as a diagnostic reference in `src/ue_speaker_off.py`; on the tested speaker it connects and writes successfully but does **not** power the speaker off, so it is not the production path.

The UE command that disables BLE standby is intentionally not implemented. Sending it would remove the standby capability that remote wake depends on, and there is no undo over the air.

## Requirements

- Linux with BlueZ and a Bluetooth adapter that can reach the speaker
- A speaker already paired with the controller address used in the payload
- Python 3.10 or newer
- An account allowed to use BlueZ/D-Bus and to open Bluetooth sockets

No elevated privileges are needed, and no `gatttool`. The OFF path uses only the Python standard library (`socket`, plus `ctypes` for the `sockaddr_l2` fields CPython does not expose). Bleak is used for the ON path and is pinned in `requirements.txt`.

## Install and configure

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

Edit `config.json`:

```json
{
  "speaker_address": "AA:BB:CC:DD:EE:FF",
  "controller_address": "11:22:33:44:55:66"
}
```

`speaker_address` is the speaker's Bluetooth address. `controller_address` is the stable address of a controller the speaker has already been paired with — this is what the speaker uses to decide the command is authorised. It is not the same thing as the speaker's own address, and guessing a private/random BLE address will not work.

`config.json` is excluded by `.gitignore`. Keep it that way.

## Command-line use

```sh
# on
.venv/bin/python src/ue_speaker_power.py \
  --config config.json --action on --retries 1 --timeout 20 --json

# off
.venv/bin/python src/ue_speaker_power.py \
  --config config.json --action off --retries 1 --timeout 20 --json
```

The JSON result carries `ok`, `stage`, `attempts`, elapsed time, and an error message when something fails. Exit status `0` means the GATT operation completed and was acknowledged; non-zero means it did not.

A successful write is still not proof of physical state. During initial setup, look at the speaker.

Addresses may be colon-separated, dash-separated, or twelve plain hex digits; malformed input is rejected. Retries and timeouts are bounded.

## Home Assistant

`home_assistant/ue_ble_wake.example.yaml` defines:

- `shell_command.ue_speaker_wake` / `shell_command.ue_speaker_off`
- `script.ue_speaker_wake` / `script.ue_speaker_off`

Point the commands at the absolute paths of the virtual environment, the helper, and your private config on a Bluetooth-capable host. Do not put real addresses in a YAML file you intend to share.

Error handling is deliberately plain. The helper exits non-zero on failure, and Home Assistant's `shell_command` raises on a non-zero exit, so the script fails and the exact failing command appears in the log. A failed power command never looks like a success.

Two deployment notes:

- In a container or HAOS, a shell command runs inside the managed environment and does not automatically get the host's Bluetooth adapter. Either run the helper on a host with adapter access, or arrange an explicit pass-through.
- If you shell out over SSH, keep host-key checking on and use a narrowly scoped account and command rather than weakening SSH.

## Testing

```sh
.venv/bin/pytest -q
```

The suite covers payload construction and rejection of any command byte other than `01`/`02`, MAC validation, the ON path staying on Bleak, the OFF path using forced LE, the `sockaddr_l2` encoding asserting PSM 0 / ATT CID / LE-public addressing, the ATT PDU layout, connection-failure versus write-failure classification, timeouts with socket cleanup, a silent peer, and regressions against both the removed classic pre-connect and any reintroduction of a subprocess helper.

These are mocked. They do not prove hardware compatibility. For a real installation:

1. Confirm the speaker is paired and still reachable in standby.
2. Enumerate services and confirm the power characteristic is present and writable.
3. Run ON and watch the speaker power on.
4. Turn it off normally, run OFF, and watch it power off.
5. Run ON again and confirm standby wake still works.

Do not fuzz characteristics, send random payloads, remove pairings, reset the speaker, or experiment with firmware commands.

## Troubleshooting

- **`Not connected` during OFF** — this is the bearer problem described above. Confirm you are on a build that routes OFF through `src/le_att.py`.
- **`br-connection-busy` during ON** — something else is holding a pending BR/EDR connection to the speaker, often a diagnostic tool such as `l2ping`. Let it clear; the bounded retry usually handles it.
- **Speaker not found** — check range, adapter power, `rfkill`, whether Remote Power/standby is enabled on the speaker, and whether another host has it connected. Do not change payload bytes to fix discovery.
- **Authorization error** — verify `controller_address` really is a controller the speaker has been paired with. An authorization error alone does not prove the address is wrong.
- **Characteristic missing** — stop and compare your model and firmware against the evidence. Do not substitute a similar-looking proprietary characteristic.
- **Write succeeds but the speaker stays on** — check physical state, pairing identity, and model/firmware differences. RFCOMM is not a fallback here.
- **Is it actually off?** — `l2ping <speaker>` is a useful discriminator. Powered off gives `Can't connect: Host is down` while BLE reads still succeed, which is exactly the standby signature you want.
- **Home Assistant cannot run it** — check paths and permissions inside the HA execution environment and confirm Bluetooth/D-Bus access. Do not install packages globally into HAOS.

## Known limitations

- Validated on exactly one MEGABOOM 3, one adapter, and one BlueZ version.
- The handle used for the power characteristic write is the one observed on that speaker. A different firmware could move it.
- The forced-LE path assumes a public peer address; a device using a random/resolvable address would need the LE-random address type.
- `sockaddr_l2` is built by hand because CPython's socket module does not expose the LE fields. This is stable kernel ABI, but it is Linux-specific by construction.
- Reverse-engineered and unofficial. Firmware updates can break it without notice.

## Research and attribution

The protocol is not ours. It comes from public reverse-engineering work, and these are technical evidence rather than official UE specifications:

- [Reversing-UE-Boom](https://github.com/skilo-sh/Reversing-UE-Boom) and [Skilo's write-up](https://skilo.sh/posts/reversing-ue-boom/) — the power characteristic and the `MAC + command` payload
- [BoomSwitch](https://github.com/Shingyx/BoomSwitch) and [the state-machine fix](https://github.com/Shingyx/BoomSwitch/pull/2)
- [CountableSet UE Boom reverse engineering](https://github.com/countableSet/ue-boom-re) and [its write-up](https://blog.countableset.com/2022/02/22/ue-boom-reverse-engineering/)
- [eni23/ueboom](https://github.com/eni23/ueboom)
- [cstan11/ue-boom-macos](https://github.com/cstan11/ue-boom-macos)
- [Marcus T.'s UE Boom gist](https://gist.github.com/marcust/af93ff47899583f5a52f)
- [Logitech support: Getting Started - MEGABOOM 3](https://support.logi.com/hc/en-us/articles/18601350410775-Getting-Started-MEGABOOM-3) — official product documentation, including app-level Remote Power ON/OFF

What this project contributes on top of that prior work is narrow: identifying BlueZ bearer selection as the reason the OFF write fails on a dual-mode speaker, and implementing a dependency-free forced-LE ATT write to avoid it.

Technical references used for that part:

- [BlueZ](http://www.bluez.org/) — `attrib/utils.c` and `src/adapter.c` in the 5.72 release tarball
- [Bleak client API](https://bleak.readthedocs.io/en/latest/api/client.html)
- [Home Assistant Bluetooth developer documentation](https://developers.home-assistant.io/docs/bluetooth)

The tested device's addresses, host identity, deployment paths, and local validation records are intentionally not published here.

## License

No license is currently granted for this repository. It is published for reference; please ask before redistributing or incorporating the code. The research sources above remain under their own licenses and ownership.
