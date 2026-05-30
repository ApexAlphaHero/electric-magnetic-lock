# Door Access Control — Claude Context

## Project Overview

Python application for Raspberry Pi controlling a 12V electromagnetic door lock. Integrates NFC card reading, GPIO hardware control, reed-switch door sensing, and Home Assistant MQTT publishing.

## Architecture

Event-driven with a shared `queue.Queue(maxsize=100)`. All hardware threads and GPIO ISR callbacks are producers; the main thread is the sole consumer. This keeps GPIO writes and business logic single-threaded.

```
nfc_reader.py  ──► queue ──► main.py dispatch loop ──► lock_controller.py
door_sensor.py ──►              │                   ──► mqtt_handler.py
mqtt_handler.py ──►             └─ logging
lock_controller.py (button ISR) ──►
```

Event types: `NFC_UID`, `BUTTON_PRESS`, `MQTT_COMMAND`, `DOOR_STATE`, `DOOR_ALERT`, `UNLOCK_TIMER_EXPIRED`

## File Map

| File | Role |
|------|------|
| `main.py` | Entry point, logging setup, signal handlers, event dispatch loop |
| `nfc_reader.py` | `NFCReader` class — pyscard PC/SC, GET_UID APDU `[0xFF,0xCA,0x00,0x00,0x00]`, daemon thread |
| `lock_controller.py` | `LockController` class — GPIO17 relay, GPIO18 LED, GPIO27 button ISR, `threading.Timer` auto-relock |
| `door_sensor.py` | `DoorSensor` class — GPIO22 reed switch, edge detection, door-open alert monitor thread |
| `mqtt_handler.py` | `MQTTHandler` class — paho-mqtt, LWT, retain flags, auto-reconnect via `loop_start()` |
| `config.json` | All runtime settings (deployed to `/etc/door_access/config.json` on Pi) |
| `door_access.service` | systemd unit — runs as `door` user, auto-restart on failure |
| `install.sh` | Downloads files from GitHub, creates user, dirs, enables service |
| `README.md` | Wiring, setup, MQTT topic reference, HA config examples |

## Hardware

- **GPIO17** — Relay HAT signal (active-low: LOW = unlocked, HIGH = locked)
- **GPIO18** — LED button illumination (HIGH = on when unlocked)
- **GPIO27** — Button input, internal pull-up, FALLING edge = press
- **GPIO22** — Reed switch, internal pull-up, LOW = door open
- **USB** — ACR1552U NFC reader (pyscard/PC/SC, `pcscd` daemon required)

## Runtime Paths on Pi

- App files: `/opt/door_access/`
- Config: `/etc/door_access/config.json`
- Log: `/var/log/door_access.log` (daily rotation, 7 days)
- Service: `/etc/systemd/system/door_access.service`
- Service user: `door` (groups: gpio, plugdev, spi)

## MQTT Topics

| Topic | Direction | Retain | Payload |
|-------|-----------|--------|---------|
| `home/door/availability` | pub | No | `online`/`offline` |
| `home/door/lock/state` | pub | Yes | `LOCKED`/`UNLOCKED` |
| `home/door/lock/set` | sub | — | `LOCK`/`UNLOCK` |
| `home/door/sensor/state` | pub | Yes | `OPEN`/`CLOSED` |
| `home/door/alert` | pub | No | string |
| `home/door/last_access` | pub | Yes | JSON |

## Key Design Decisions

- **Timer callback posts to queue, never calls lock() directly** — keeps GPIO operations on the main thread only, avoiding race conditions with the timer thread.
- **ISR callbacks are fire-and-forget** — they only call `queue.put_nowait()`, never GPIO operations.
- **paho `loop_start()` not `loop_forever()`** — leaves main thread free for the event loop; reconnect is automatic.
- **Config never overwritten on reinstall** — `install.sh` skips config download if `/etc/door_access/config.json` already exists.

## Development Notes

- The code uses Python 3.10+ union type syntax (`str | None`). Target Pi OS must have Python ≥ 3.10.
- `RPi.GPIO` and `pyscard` are Pi-specific. For local dev/testing, mock them with `unittest.mock.MagicMock`.
- Run `pcsc_scan` to verify the ACR1552U is recognized before starting the service.
- Check `journalctl -u door_access -f` for live logs when running under systemd.
