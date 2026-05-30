# Door Access Control System

Raspberry Pi door access control using an ACR1552U NFC reader, relay-controlled electromagnetic lock, illuminated button, and reed-switch door sensor. Integrates with Home Assistant via MQTT.

---

## Hardware Wiring

### GPIO Pin Summary (BCM numbering)

| Pin | GPIO | Direction | Connected to |
|-----|------|-----------|--------------|
| 11  | 17   | Output    | Relay HAT signal |
| 12  | 18   | Output    | LED button (via 330Ω resistor) |
| 13  | 27   | Input     | LED button momentary switch |
| 15  | 22   | Input     | Reed switch door sensor |
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

Reed Switch Door Sensor
────────────────────────────────────────────────────────────────
GPIO22 (pin 15) ──────────► Reed switch terminal 1
GND    (pin 20) ──────────► Reed switch terminal 2
(internal pull-up enabled; LOW = door open per wiring)

ACR1552U NFC Reader
────────────────────────────────────────────────────────────────
USB port ─────────────────► Any Pi USB port (powered + data)
```

> **Relay polarity:** The relay HAT is active-low — GPIO LOW energizes the relay coil and releases the electromagnetic lock (unlocked). GPIO HIGH de-energizes the coil and the lock engages. This is configured in `config.json` as `"active_low_relay": true`.

---

## Software Installation

### Prerequisites

```bash
sudo apt-get update
sudo apt-get install -y python3-pip pcscd pcsc-tools libpcsclite-dev python3-systemd
```

### One-liner install from GitHub

Replace `YOUR_GITHUB_USERNAME` with your GitHub username before running:

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/electric-magnetic-lock/main/install.sh | sudo bash
```

The installer will:
1. Install all system and Python packages
2. Enable the `pcscd` PC/SC daemon
3. Create a dedicated `door` system user with hardware group access
4. Download all application files to `/opt/door_access/`
5. Install the default config to `/etc/door_access/config.json`
6. Create `/var/log/door_access.log`
7. Install and enable the `door_access` systemd service

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
    "tls_ca_cert": null
  },
  "gpio": {
    "relay_pin": 17,
    "led_pin": 18,
    "button_pin": 27,
    "door_sensor_pin": 22,
    "door_sensor_active_low": true
  },
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
  "authorized_uids": {
    "AABB1122": "Alice",
    "CCDD3344": "Bob"
  }
}
```

### Finding a card's UID

Run `pcsc_scan` and tap the card/phone to the reader. The UID will appear in the output. Add it to `authorized_uids` in uppercase hex with no spaces (e.g. `"AABB1122CC": "John"`).

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
| `home/door/sensor/state` | Yes | `OPEN` / `CLOSED` |
| `home/door/alert` | No | Alert message string |
| `home/door/last_access` | Yes | JSON (see below) |

### Subscribed by the Pi

| Topic | Payload |
|-------|---------|
| `home/door/lock/set` | `LOCK` or `UNLOCK` |

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

### Home Assistant MQTT integration example

```yaml
# configuration.yaml

mqtt:
  lock:
    - name: "Front Door"
      state_topic: "home/door/lock/state"
      command_topic: "home/door/lock/set"
      payload_lock: "LOCK"
      payload_unlock: "UNLOCK"
      state_locked: "LOCKED"
      state_unlocked: "UNLOCKED"
      availability_topic: "home/door/availability"

  binary_sensor:
    - name: "Front Door Sensor"
      state_topic: "home/door/sensor/state"
      payload_on: "OPEN"
      payload_off: "CLOSED"
      device_class: door
      availability_topic: "home/door/availability"

  sensor:
    - name: "Front Door Last Access"
      state_topic: "home/door/last_access"
      value_template: "{{ value_json.name }}"
      availability_topic: "home/door/availability"
```

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
  mqtt_handler.py       Home Assistant MQTT integration (paho-mqtt)

/etc/door_access/
  config.json           Runtime configuration (edit this file)

/var/log/
  door_access.log       Application log (rotated daily, 7 days kept)

/etc/systemd/system/
  door_access.service   systemd unit file
```
