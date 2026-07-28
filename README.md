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
  python3-pyscard python3-paho-mqtt python3-rpi-lgpio \
  python3-venv libpam0g-dev openssl
```

> **`python3-rpi-lgpio`** is the lgpio-backed drop-in for `RPi.GPIO`. The classic
> `python3-rpi.gpio` does **not** work on the 6.x kernels in current Pi OS — don't install both.

The door service itself uses only these apt packages. The [Web Admin](#web-admin) needs
Flask, gunicorn and python-pam, which the installer puts in a venv at
`/opt/door_access/venv` — `python-pam` isn't reliably packaged across Debian releases, and
PEP 668 blocks pip into the system interpreter.

Those venv packages are pinned in [`requirements.txt`](requirements.txt), transitives
included, and installed with `--upgrade`. Dependabot watches that file, so a security
advisory becomes a PR; merging it and pressing **Update** in the web admin is what actually
moves the version on the Pi. Without the pin the venv would keep whatever pip resolved on
the day of first install — reinstalling leaves an already-satisfied package alone.

### One-liner install from GitHub

```bash
curl -fsSL https://raw.githubusercontent.com/ApexAlphaHero/electric-magnetic-lock/master/install.sh | sudo bash
```

The installer will:
1. Install all system + Python packages (from apt) and enable the `pcscd` PC/SC daemon
2. Create `/opt/door_access/venv` with the web admin's dependencies, at the versions pinned in `requirements.txt`
3. Enable CCID escape commands in `/etc/libccid_Info.plist` (needed for the reader's LED/buzzer)
4. Create the `door` and `doorweb` system users and the `dooradmin` admin group
5. Install a polkit rule so the session-less `door` user can reach `pcscd`
6. Generate a local CA and TLS certificate in `/etc/door_access/tls/`
7. Download all application files to `/opt/door_access/`
8. Install the default configs to `/etc/door_access/`
9. Create the log directory `/var/log/door_access/` and the event database directory `/var/lib/door_access/`
10. Install and enable the `door_access` and `door_admin` systemd services
11. Prompt for the Pi usernames that should get web admin access

> Set `DOOR_SRC_DIR=<path>` to install from a local checkout instead of GitHub — useful for
> deploying unreleased changes or installing without internet access.

> **`raw.githubusercontent.com` caches for a few minutes.** Running the one-liner
> immediately after pushing can install stale files, with no error to tell you. If you've
> just pushed, either wait ~5 minutes or install from the git checkout instead, which reads
> the commit directly and is never cached:
>
> ```bash
> sudo git -C /opt/door_access/repo pull --ff-only
> sudo DOOR_SRC_DIR=/opt/door_access/repo DOOR_WEB_ADMINS= bash /opt/door_access/repo/install.sh
> ```
>
> The web **Updates** page is not affected — it fetches over git, then installs from the
> checkout.

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
    "log_file": "/var/log/door_access/door_access.log",
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
| `lock.unlock_duration_seconds` | Seconds the lock stays released after an authorized tag, button press, or web unlock (also settable live from the [Web Admin](#unlock-duration) status page; changes persist here) |
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

## Web Admin

A separate service (`door_admin.service`) serves a small HTTPS site on the Pi for
managing tags, viewing event history, and — unlike Home Assistant — unlocking the door.

```
https://<pi-ip>:8443
```

**Why unlock lives here and not in Home Assistant.** HA has no per-user access control:
every HA user shares one set of entity permissions, so an HA unlock button is an unlock
button for everyone in the house. This service authenticates each operator individually
against their own Pi account and additionally requires membership in the `dooradmin`
group, and it records who did what.

### Access control

| Layer | Check |
|-------|-------|
| Password | PAM against local Pi accounts (`login` service) |
| Authorization | Must be a member of `dooradmin` — a valid Pi password alone is not enough |
| Lockout | 5 failed attempts from one IP locks that IP out for 15 minutes |
| Audit | Logins, failures, unlocks and tag changes are all written to the event history |

Grant and revoke access with normal Unix group membership:

```bash
sudo usermod -aG dooradmin alice     # grant
sudo gpasswd -d alice dooradmin      # revoke
```

Revoking takes effect at their next login; to cut an active session immediately, restart
the service (`sudo systemctl restart door_admin`), which invalidates all sessions.

To take the unlock button away entirely and leave a monitoring-and-tags UI, set
`"allow_unlock": false` in `/etc/door_access/web.json` and restart `door_admin`.

### Unlock duration

Set on the **Status** page (1–60 seconds). It applies to every unlock — NFC tag, physical
button, and the web unlock button — takes effect on the next unlock, and persists to
`config.json`, so it survives a restart. Changes are recorded in the event history against
your username. An unlock already counting down keeps its original timer.

### Adding a tag

All on the **Tags** page. Type a name first — a blank name becomes `Tag <last 4>`.

1. **Scan at the door reader** — *the default, works everywhere.* Press the button, walk to
   the door, hold the tag to the reader. It's captured and added under the name you typed;
   the page updates itself. Uses the Pi's own ACR1552, so it needs nothing from the
   browser — no NFC hardware, no certificate trust, works on iPhone and Firefox alike.
2. **Scan with this phone** — *only appears where it can actually work.* See the note below.
3. **From a recent unknown scan** — a tag presented at the door and denied is logged; click
   **Use this UID** under *Recent unknown scans*.
4. **By hand** — type the UID. Any separator style works; `aa:bb:11:22` and `AABB1122` are
   the same tag.

Changes take effect immediately — no restart.

While the reader is armed it will **not** open the door: the scan is treated as an admin
capture, and the reader flashes green to confirm. The arm is one-shot and expires after 60
seconds, so a forgotten arm can't sit there swallowing legitimate unlocks. There's a
**Cancel** button, and the countdown is shown while it's waiting.

> **Phone scanning is optional and often unavailable.** Web NFC exists only in Chromium
> browsers on Android (Chrome, Edge, Samsung Internet) *and* only in a trusted secure
> context. **Firefox does not implement it on any platform, and no iOS browser has it** —
> Mozilla and Apple have both declined to ship it. If the certificate is the only thing
> missing, the page says so and links the CA download; if the browser is the problem, it
> says that instead. Either way the door-reader button above is unaffected.

### Trusting the site's certificate

The installer generates a local CA and signs the web admin's certificate with it, so
browsers warn until that CA is installed. Grab it from the **Certificate** link in the nav
(or `https://<pi-ip>:8443/ca.crt`) and install it:

- **Android** — Settings → Security → Encryption & credentials → Install a certificate → CA certificate
- **Windows** — double-click → Install Certificate → Local Machine → Trusted Root Certification Authorities
- **iOS** — Settings → General → VPN & Device Management → install, then Settings → General → About → Certificate Trust Settings → enable it

The download is deliberately **unauthenticated**: you need the certificate *before* the
connection is trustworthy, and requiring a login first would mean sending your password
over a connection you haven't verified. A CA certificate is a public key — publishing it
grants nobody anything. The signing key (`ca-key.pem`) is `0640 root:dooradmin` and is
never served.

### Updating

The **Updates** page shows the installed commit and anything newer on `master`. **Check for
updates** fetches; **Update now** fast-forwards the checkout at `/opt/door_access/repo`,
re-runs the installer from it, and restarts both services. The page survives its own server
restarting and reports the result when it comes back.

**If the door service doesn't come back within a few seconds, the update is rolled back
automatically** and the previous version reinstalled — a bad release can't leave the lock
unmanaged. If both the update *and* the rollback fail, the page says so explicitly; recover
with `journalctl -u door_access -n 50`.

It refuses to run if the checkout has local edits (it's `--ff-only`), and refuses any git
remote other than the configured one.

> **This is remote code execution as root, gated on a `dooradmin` session.** Anyone who can
> sign in to the web admin can cause code from the git remote to run as root on the Pi. The
> web app itself holds no such privilege — it may only ask systemd to start two specific
> units, via `/etc/sudoers.d/door_update` — but the end result is the same, so treat
> `dooradmin` membership as equivalent to root on this machine. Set
> `"allow_update": false` in `/etc/door_access/web.json` to remove the page entirely and
> update by hand instead:
>
> ```bash
> sudo git -C /opt/door_access/repo pull
> sudo DOOR_SRC_DIR=/opt/door_access/repo DOOR_WEB_ADMINS= bash /opt/door_access/repo/install.sh
> sudo systemctl restart door_access door_admin
> ```

### Event history

Every scan (granted and denied), button press, web unlock, door open/close, alert, tag
change and login attempt is written to `/var/lib/door_access/events.db`, filterable and
paginated under **History**. Retention defaults to 90 days
(`web.event_retention_days` in `config.json`).

The history starts from the moment you install this version — earlier activity only
exists in `door_access.log` and is not backfilled.

### How the two services are separated

The web app runs as its own user (`doorweb`) and holds **no** GPIO access and **no** write
access to `config.json`. Everything it changes it requests over a Unix socket
(`/run/door_access/control.sock`, mode 0660, group `dooradmin`), and the door service
executes it on its single event-loop thread — so config writes and GPIO stay
single-threaded, as everywhere else in this project.

`doorweb` is in the `shadow` group so PAM can verify passwords. That is a real privilege —
it can read password hashes — so the account has no shell, no home directory, and the unit
is locked down with `ProtectSystem=strict` and friends.

### Troubleshooting

| Symptom | Cause |
|---------|-------|
| "door service is not running" | `door_access.service` is down — the socket only exists while it runs |
| "no permission for /run/door_access/control.sock" | `doorweb` is not in `dooradmin`, or `door` isn't either (it needs to be, to hand over the socket) |
| Correct password rejected | The account is not in `dooradmin`; the message is deliberately identical to a wrong password |
| "Authentication is not configured" | `python-pam` missing from `/opt/door_access/venv` |
| Browser warns about the certificate | Expected until `tls/ca.pem` is installed on the device |
| Scan button missing on Android | Not HTTPS-trusted yet — install `ca.pem`; Web NFC is hidden in insecure contexts |

```bash
journalctl -u door_admin -f
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

Discovery configs are also published (retained) under `homeassistant/.../config` when
`mqtt.discovery` is enabled — see the Home Assistant section below.

### Subscribed by the Pi

**Nothing.** The Pi subscribes to no MQTT topics at all — the connection is publish-only.

> Home Assistant has no per-user access control, so anything writable over MQTT is
> writable by every HA user, and by anyone who can publish to the broker. The door
> opens only from an authorized NFC tag, the physical button, or the
> [Web Admin](#web-admin), which authenticates each operator individually.

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

Home Assistant is **monitoring only** — every entity is read-only. HA has no per-user
access control (all HA users share one set of entity permissions), so nothing exposed
here can open the door, change who is authorized, or change any setting.

With `mqtt.discovery` enabled (the default), the Pi publishes MQTT discovery configs and
**all entities appear automatically** — no YAML needed. As long as HA's MQTT integration
points at the same broker, a **"Door Access"** device shows up under
**Settings → Devices & Services** with:

| Entity | Type | Use |
|--------|------|-----|
| Lock | `binary_sensor` (lock) | Lock status — on = unlocked, off = locked |
| Door Left / Door Right | `binary_sensor` (door) | Open / closed — one per door |
| Last Access | `sensor` | **Who scanned** — state = name; attributes `uid`, `granted`, `timestamp` |
| Alert | `sensor` | Latest alert string (`UNAUTHORIZED_ACCESS …`, `DOOR_OPEN_TOO_LONG …`, etc.) |
| NFC tag scanner | `tag` | Every scan fires HA's native `tag_scanned`; badges appear under **Settings → Tags** |

Upgrading from a version that had the Lock control, the Unlock Duration number, or the
enrollment entities? The Pi clears their retained discovery configs on connect, so they
disappear from HA by themselves — no manual cleanup in the broker.

#### Managing tags

Not from Home Assistant — use the [Web Admin](#web-admin), which authenticates each
operator individually. Editing `authorized_uids` in `/etc/door_access/config.json` by hand
and restarting the service also works.

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
  binary_sensor:
    # Read-only lock status. Do NOT define an `mqtt: lock:` entity here — it needs a
    # command_topic, and the Pi no longer subscribes to one.
    - name: "Cabinet Door Lock"
      state_topic: "home/door/lock/state"
      payload_on: "UNLOCKED"
      payload_off: "LOCKED"
      device_class: lock
      availability_topic: "home/door/availability"

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
  event_store.py        SQLite event history (writer for the door service, reader for the web UI)
  control_socket.py     Unix-socket protocol between the web UI and the door service
  web_admin.py          Flask web admin (runs as 'doorweb' under door_admin.service)
  templates/  static/   Web admin pages and assets
  requirements.txt      Pinned web admin dependencies (the venv is installed from this)
  venv/                 Web admin dependencies (Flask, gunicorn, python-pam)

/etc/door_access/
  config.json           Door service configuration (door-owned; holds the MQTT password)
  web.json              Web admin settings (admin group, session length, unlock toggle)
  web.env               Web admin listen address (gunicorn --bind)
  web_secret            Session signing key (doorweb-owned, 0600)
  tls/                  ca.pem, cert.pem, key.pem for the web admin

/var/lib/door_access/
  events.db             Event history (SQLite, WAL; setgid dir so the web UI can read it)

/var/log/door_access/
  door_access.log       Application log (rotated daily, 7 days kept; door-owned dir so rotation works)

/etc/systemd/system/
  door_access.service   Door service unit
  door_admin.service    Web admin unit

/etc/polkit-1/rules.d/
  50-door-pcsc.rules    Grants the 'door' service user PC/SC access

/etc/libccid_Info.plist  ifdDriverOptions=0x0001 enables reader LED/buzzer escape commands
```
