# Door Access Control System

Raspberry Pi door access control using an ACR1552U NFC reader, relay-controlled electromagnetic lock, illuminated button, and reed-switch door sensor. Integrates with Home Assistant via MQTT.

---

## Hardware Wiring

### GPIO Pin Summary (BCM numbering)

| Pin | GPIO | Direction | Connected to |
|-----|------|-----------|--------------|
| 11  | 17   | Output    | Relay HAT signal (single lock, both doors) |
| 12  | 18   | Output    | LED button (via 330Ω resistor) |
| 13  | 27   | Input     | LED button momentary switch (unlock) |
| 15  | 22   | Input     | Door sensor — **left** (NC fridge-light switch) |
| 16  | 23   | Input     | Door sensor — **right** (NC fridge-light switch) |
| USB | —    | —         | ACR1552U NFC reader |

### Wiring Diagram

```
Raspberry Pi                     Relay HAT
─────────────                    ─────────────────────────────
GPIO17 (pin 11) ────────────────► Signal IN
GND     (pin 6) ────────────────► GND
5V      (pin 2) ────────────────► VCC

                                   COM ── 12V supply (+)
                                   NO  ── Electromagnetic lock (+)
                                          Lock GND ── 12V supply (-)

LED Momentary Button
────────────────────────────────────────────────────────────────
GPIO18 (pin 12) ──[330Ω]──► LED anode
                             LED cathode ──► GND (pin 14)

GPIO27 (pin 13) ──────────► Button terminal 1
GND    (pin 14) ──────────► Button terminal 2
(internal pull-up enabled; press = LOW)

Door Sensors — one per door (NC momentary "fridge light" switches)
────────────────────────────────────────────────────────────────
GPIO22 (pin 15) ──────────► Left door switch terminal 1
GND    (pin 20) ──────────► Left door switch terminal 2

GPIO23 (pin 16) ──────────► Right door switch terminal 1
GND    (pin 14/20) ───────► Right door switch terminal 2

(internal pull-ups enabled. A normally-closed fridge-light switch is closed
when the door is OPEN (plunger out) → reads LOW = OPEN; pressed when the door
is CLOSED → reads HIGH. This matches "active_low": true per door. Verify each
switch's polarity by watching `journalctl -u door_access -f` while opening it.)

ACR1552U NFC Reader
────────────────────────────────────────────────────────────────
USB port ─────────────────► Any Pi USB port (powered + data)
```

> **Relay polarity:** The relay HAT is active-low — GPIO LOW energizes the relay coil and releases the electromagnetic lock (unlocked). GPIO HIGH de-energizes the coil and the lock engages. This is configured in `config.json` as `"active_low_relay": true`.

---

## Software Installation

### Prerequisites

Target OS is **Raspberry Pi OS / Debian 13 (trixie)** or newer with **Python ≥ 3.10**. All
Python dependencies come from apt — on Debian 12+ PEP 668 blocks `pip install` into the
system interpreter.

```bash
sudo apt-get update
sudo apt-get install -y \
  pcscd pcsc-tools libpcsclite-dev \
  python3-pyscard python3-paho-mqtt python3-rpi-lgpio
```

> **`python3-rpi-lgpio`** is the lgpio-backed drop-in for `RPi.GPIO`. The classic
> `python3-rpi.gpio` does **not** work on the 6.x kernels in current Pi OS — don't install both.

### One-liner install from GitHub

```bash
curl -fsSL https://raw.githubusercontent.com/ApexAlphaHero/electric-magnetic-lock/main/install.sh | sudo bash
```

The installer will:
1. Install all system + Python packages (from apt) and enable the `pcscd` PC/SC daemon
2. Enable CCID escape commands in `/etc/libccid_Info.plist` (needed for the reader's LED/buzzer)
3. Create a dedicated `door` system user with hardware group access
4. Install a polkit rule so the session-less `door` user can reach `pcscd`
5. Download all application files to `/opt/door_access/`
6. Install the default config to `/etc/door_access/config.json`
7. Create `/var/log/door_access.log`
8. Install and enable the `door_access` systemd service

---

## Configuration

Edit `/etc/door_access/config.json` before starting the service:

```json
{
  "mqtt": {
    "enabled": false,
    "broker": "192.168.1.10",
    "port": 1883,
    "username": "dooruser",
    "password": "secret",
    "client_id": "door_access_pi",
    "keepalive": 60,
    "tls": false,
    "tls_ca_cert": null,
    "discovery": true,
    "discovery_prefix": "homeassistant"
  },
  "gpio": {
    "relay_pin": 17,
    "led_pin": 18,
    "button_pin": 27
  },
  "doors": [
    { "name": "left",  "sensor_pin": 22, "active_low": true },
    { "name": "right", "sensor_pin": 23, "active_low": true }
  ],
  "lock": {
    "unlock_duration_seconds": 5,
    "active_low_relay": true
  },
  "door": {
    "open_alert_threshold_seconds": 30
  },
  "logging": {
    "log_file": "/var/log/door_access.log",
    "backup_count": 7,
    "level": "INFO"
  },
  "nfc": {
    "uid_debounce_seconds": 2.0
  },
  "reader_feedback": {
    "enabled": true
  },
  "authorized_uids": {
    "AABB1122": "Alice",
    "CCDD3344": "Bob"
  }
}
```

| Key | Purpose |
|-----|---------|
| `doors[]` | One entry per door: `name`, `sensor_pin` (BCM), `active_low` (LOW = open). Each becomes its own HA `binary_sensor` |
| `lock.unlock_duration_seconds` | Default seconds the lock stays released (also settable live from HA's **Unlock Duration** number entity; changes persist here) |
| `mqtt.discovery` | Publish Home Assistant MQTT discovery configs so entities auto-appear (default `true`) |
| `mqtt.discovery_prefix` | HA discovery prefix (default `homeassistant`) |
| `reader_feedback.enabled` | Beep + LED feedback on the reader for each scan (default `true`) |

### Finding a card's UID

Run `pcsc_scan` and tap the card/phone to the reader, or watch `journalctl -u door_access -f`
and tap (each scan logs `Access GRANTED/DENIED: UID=...`). Add it to `authorized_uids` in
uppercase hex with no spaces (e.g. `"AABB1122CC": "John"`).

> **Reader/tag compatibility:** the ACR1552 is a **13.56 MHz** reader (MIFARE/NTAG, ISO 14443).
> It cannot read 125 kHz (LF) fobs/cards. Phones present a *random* UID per tap (Android HCE),
> so they can't be enrolled as a stable credential.

### Reader feedback (LED + buzzer)

On every successful read the reader gives physical feedback via CCID escape commands:

| Outcome | Feedback |
|---------|----------|
| Authorized UID | short **beep** + **solid green** |
| Unauthorized UID | short **beep** + **blinking blue** |

This requires CCID escape commands to be enabled (the installer sets `ifdDriverOptions`
to `0x0001` in `/etc/libccid_Info.plist`). Note this reader's LED is **blue + green only —
there is no red**. Disable with `"reader_feedback": {"enabled": false}`.

---

## Starting the Service

```bash
sudo systemctl start door_access
sudo systemctl status door_access
```

### View live logs

```bash
journalctl -u door_access -f
```

### Restart / stop

```bash
sudo systemctl restart door_access
sudo systemctl stop door_access
```

---

## MQTT Topics

### Published by the Pi

| Topic | Retain | Payload |
|-------|--------|---------|
| `home/door/availability` | No | `online` / `offline` |
| `home/door/lock/state` | Yes | `LOCKED` / `UNLOCKED` |
| `home/door/sensor/<name>/state` | Yes | `OPEN` / `CLOSED` — one topic per door (e.g. `left`, `right`) |
| `home/door/alert` | No | Alert message string |
| `home/door/last_access` | Yes | JSON (see below) |
| `home/door/nfc/tag` | No | Raw UID of every scan (for HA's MQTT tag scanner) |
| `home/door/unlock_duration/state` | Yes | Current unlock duration (seconds) |

Discovery configs are also published (retained) under `homeassistant/.../config` when
`mqtt.discovery` is enabled — see the Home Assistant section below.

### Subscribed by the Pi

| Topic | Payload |
|-------|---------|
| `home/door/lock/set` | `LOCK` or `UNLOCK` |
| `home/door/unlock_duration/set` | Number of seconds (1–60) to set the unlock duration |

### last_access JSON format

```json
{
  "timestamp": "2026-05-30T14:23:01+00:00",
  "uid": "AABB1122",
  "name": "Alice",
  "granted": true
}
```

### Alert messages

| Alert | Trigger |
|-------|---------|
| `ACCESS_GRANTED uid=... name=...` | Authorized NFC tap |
| `UNAUTHORIZED_ACCESS uid=...` | Unrecognized NFC UID |
| `BUTTON_UNLOCK` | Momentary button pressed |
| `DOOR_OPEN_TOO_LONG elapsed=...s` | Door open past threshold |

### Home Assistant integration

With `mqtt.discovery` enabled (the default), the Pi publishes MQTT discovery configs and
**all entities appear automatically** — no YAML needed. As long as HA's MQTT integration
points at the same broker, a **"Door Access"** device shows up under
**Settings → Devices & Services** with:

| Entity | Type | Use |
|--------|------|-----|
| Lock | `lock` | **Actuate the door** — unlock = momentary release for the unlock duration, then auto-relock; lock = relock now |
| Unlock Duration | `number` | **Set how long the lock releases** (1–60 s, default 5) for NFC grants and HA unlocks; persists across restarts |
| Door Left / Door Right | `binary_sensor` (door) | Open / closed — one per door |
| Last Access | `sensor` | **Who scanned** — state = name; attributes `uid`, `granted`, `timestamp` |
| Alert | `sensor` | Latest alert string (`UNAUTHORIZED_ACCESS …`, `DOOR_OPEN_TOO_LONG …`, etc.) |
| NFC tag scanner | `tag` | Every scan fires HA's native `tag_scanned`; badges appear under **Settings → Tags** |

#### Notify on a denied badge

```yaml
automation:
  - alias: Notify on denied badge
    trigger:
      - trigger: mqtt
        topic: home/door/last_access
    condition: "{{ trigger.payload_json.granted == false }}"
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "Denied badge {{ trigger.payload_json.uid }} at the cabinet door"
```

#### Manual YAML (only if you set `mqtt.discovery: false`)

<details>
<summary>configuration.yaml example</summary>

```yaml
mqtt:
  lock:
    - name: "Cabinet Door"
      state_topic: "home/door/lock/state"
      command_topic: "home/door/lock/set"
      payload_lock: "LOCK"
      payload_unlock: "UNLOCK"
      state_locked: "LOCKED"
      state_unlocked: "UNLOCKED"
      availability_topic: "home/door/availability"

  binary_sensor:
    - name: "Cabinet Door Sensor"
      state_topic: "home/door/sensor/state"
      payload_on: "OPEN"
      payload_off: "CLOSED"
      device_class: door
      availability_topic: "home/door/availability"

  sensor:
    - name: "Cabinet Door Last Access"
      state_topic: "home/door/last_access"
      value_template: "{{ value_json.name }}"
      availability_topic: "home/door/availability"
```
</details>

---

## Troubleshooting

**NFC reader not detected**
```bash
sudo systemctl status pcscd
sudo pcsc_scan
```
The `pcscd` service must be running. If the reader still isn't found, try unplugging and re-plugging the USB cable.

**GPIO permission denied**
Ensure the `door` user is in the `gpio` group:
```bash
groups door
# should include: gpio plugdev spi
```
If not, re-run the installer or add manually:
```bash
sudo usermod -aG gpio door
sudo systemctl restart door_access
```

**MQTT not connecting**
Check broker address and credentials in `/etc/door_access/config.json`. Test manually:
```bash
mosquitto_pub -h 192.168.1.10 -u dooruser -P secret -t test -m hello
```

**Service won't start**
```bash
journalctl -u door_access -n 50 --no-pager
```

**Relay clicks but lock doesn't engage/release**
Verify COM/NO wiring on the relay. Check that the 12V supply can deliver enough current for the electromagnet (typically 500mA–1A).

---

## File Structure

```
/opt/door_access/       Application files
  main.py               Entry point and event dispatch loop
  nfc_reader.py         ACR1552U NFC reader (pyscard/PC/SC)
  lock_controller.py    Relay, LED, button (RPi.GPIO)
  door_sensor.py        Reed switch (RPi.GPIO)
  mqtt_handler.py       Home Assistant MQTT integration + discovery + tag scanner (paho-mqtt)

/etc/door_access/
  config.json           Runtime configuration (edit this file)

/var/log/
  door_access.log       Application log (rotated daily, 7 days kept)

/etc/systemd/system/
  door_access.service   systemd unit file

/etc/polkit-1/rules.d/
  50-door-pcsc.rules    Grants the 'door' service user PC/SC access

/etc/libccid_Info.plist  ifdDriverOptions=0x0001 enables reader LED/buzzer escape commands
```
