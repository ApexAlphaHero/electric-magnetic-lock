# Door Access Control — Claude Context

## Project Overview

Python application for Raspberry Pi controlling a 12V electromagnetic door lock. Integrates NFC card reading, GPIO hardware control, reed-switch door sensing, and Home Assistant MQTT publishing.

## Architecture

Two systemd services. `door_access` owns all hardware and config; `door_admin` is a Flask web UI running as a different user with neither.

Event-driven with a shared `queue.Queue(maxsize=100)`. All hardware threads and GPIO ISR callbacks are producers; the main thread is the sole consumer. This keeps GPIO writes and business logic single-threaded.

```
nfc_reader.py   ──► queue ──► main.py dispatch loop ──► lock_controller.py
door_sensor.py  ──►             │                    ──► mqtt_handler.py
mqtt_handler.py ──►             ├─ logging           ──► event_store.py (SQLite)
lock_controller.py (button ISR) ──►
control_socket.py ──────────────┘   ▲
                                    │ Unix socket (request/response)
       door_admin.service ──► web_admin.py ──► event_store.EventReader (read-only)
```

Event types: `NFC_UID`, `BUTTON_PRESS`, `DOOR_STATE`, `DOOR_ALERT`, `UNLOCK_TIMER_EXPIRED`, `WEB_COMMAND`

**Where writable features are allowed to live.** Anything that changes the door's behaviour belongs in `web_admin.py`, never in `mqtt_handler.py`. The web UI authenticates each operator individually (PAM + `dooradmin` group membership) and audits them; Home Assistant cannot do either. See the two sections below.

**Home Assistant is monitoring-only — this is a security requirement, not an oversight.** HA has no per-user access control, so anything actuatable from HA is actuatable by every HA user. There is deliberately **no remote unlock** (the lock appears as a read-only `binary_sensor`, not a `lock` entity, because an HA `lock` requires a command topic), **no HA-driven tag enrollment or removal**, and **no HA-settable unlock duration**. The handler subscribes to nothing. Every writable feature lives in the web admin behind per-user auth. Do not reintroduce an HA-facing control of any kind.

`MQTTHandler.RETIRED_TOPICS` / `RETIRED_DISCOVERY` clear the retained topics from those removed features on every connect, so old entities vanish from HA on upgrade rather than lingering as clickable controls. **When you remove an HA entity, add its discovery suffix and topics to these tuples** — deleting the publish alone leaves the retained config in the broker and the entity stays in HA, still clickable.

## Web Admin (`door_admin.service`)

Flask + gunicorn (TLS), runs as `doorweb`, separate from the door service. Dependencies live in `/opt/door_access/venv` (Flask, gunicorn, python-pam) — **not** apt, because the PAM binding isn't reliably packaged and PEP 668 blocks system pip. The door service itself still uses system python3 + apt only; don't add venv deps to it.

- **Auth** — PAM (`login` service) *plus* `dooradmin` group membership. Both required. Bad password and not-in-group return the identical message so the UI never confirms which accounts exist. `doorweb` is in `shadow` so pam_unix can read `/etc/shadow` directly (the setuid `unix_chkpwd` helper only lets a non-root caller check its *own* password).
- **No shared state with the door service except the socket and the DB.** The web app cannot write `config.json` (it can't even read it — the MQTT password lives there) and has no GPIO. Every mutation is a `ControlClient.call()` over `/run/door_access/control.sock`, dispatched as `WEB_COMMAND` and executed in `_run_web_command` on the event loop.
- **Socket authorization is filesystem permissions** — mode 0660, group `dooradmin`. There is no auth inside the protocol. `ControlSocket._grant_group_access` re-groups the socket and its systemd-created runtime dir at startup, which is why `door` must also be in `dooradmin`.
- **`normalize_uid` lives in `control_socket.py`** because both processes must agree exactly. Web NFC gives `aa:bb:11:22`, the reader gives `AABB1122`; normalizing in only one process silently stores tags that no live scan matches.
- **Single gunicorn worker with threads** — the login throttle is per-process in-memory state; multiple workers would weaken it.
- **CSP is `'self'` with no inline scripts or styles**, so no `onclick` attributes — destructive buttons use `data-confirm`, wired up in `static/nfc.js`.
- **The Pi's own reader is the primary enrollment path, not the phone.** "Scan at the door reader" arms the ACR1552 via `arm_enroll`; `_capture_tag` in `main.py` adds the tag and the arm is one-shot with a 60 s window, expired by the sweep in `run_event_loop` so the reader's LED stops signalling enroll the moment it lapses. An armed scan does **not** unlock. `enroll_status` hands the result over exactly once — the operator is at the door, not the browser, so the outcome must survive until polled, but must not replay.
- **Web NFC is progressive enhancement only, and usually unavailable.** Chromium-on-Android + trusted secure context. **Firefox has no Web NFC on any platform and no iOS browser does** — this is the user's own browser, so never present phone scanning as the default or assume it will work. The button stays hidden unless `NDEFReader` exists; the fallback text distinguishes "certificate not trusted" from "wrong browser" so nobody is sent to install a CA that won't help them.
- **`/ca.crt` is intentionally unauthenticated.** You need the CA *before* the connection is trustworthy; gating it behind login would mean sending a password over an unverified connection. It serves the public CA cert only — `ca-key.pem` is never exposed. Sent as `.crt` with `application/x-x509-ca-cert` because Android's certificate installer filters the picker by extension and skips `.pem`.

## In-place updates

`/opt/door_access/repo` is a root-owned git checkout — the *source* the updater installs from, not the running code (that stays in `/opt/door_access`). Keep it root-owned and non-group-writable: write access there is equivalent to root, since `update.sh` installs from it.

The web app has **no** privilege to install anything. It runs `systemctl --no-block start door_update{,_check}.service` and otherwise only reads the status JSON and log those units write. Everything else — fetch, merge, install, restart — happens as root inside `update.sh`.

**Authorization is polkit, not sudo** — `door_admin.service` sets `NoNewPrivileges=yes`, which blocks setuid binaries, so sudo cannot run there at all. Don't "fix" a permission error by removing that hardening; extend `50-door-update.rules` instead. `--no-block` is required because both units are `Type=oneshot`: without it `systemctl start` waits for the entire update and the HTTP request times out.

- **Auto-rollback is the point.** If `door_access` isn't active after the restart, `update.sh` resets to the previous commit, reinstalls, and restarts. A broken release must never leave the door unmanaged. Preserve this if you touch the script.
- **`--ff-only` and a pinned remote.** It refuses a non-fast-forward (so hand-edits on the Pi fail loudly rather than being discarded) and refuses any origin but the configured URL (otherwise rewriting the remote turns the update button into arbitrary root execution).
- **`raw.githubusercontent.com` caches ~5 minutes**, so `curl | bash` right after a push silently installs stale files. The updater is immune (it fetches over git and installs from the checkout with `DOOR_SRC_DIR`); only the bootstrap one-liner is affected. When deploying a just-pushed change by hand, go through the checkout.
- **Status lives on disk, not in the request.** Applying an update restarts `door_admin`, killing the browser's connection; `updates.js` treats fetch failures during a run as "restarting" and reloads once the server returns.
- **`install.sh` must stay non-interactive under the updater.** `grant_web_admins` tests `${DOOR_WEB_ADMINS+x}` (set, not non-empty) so the updater can pass an empty value to skip the prompt — `read` at EOF would otherwise abort the script under `set -e`.
- **Security posture:** a `dooradmin` web session is effectively root on this box. That is documented, not accidental; `"allow_update": false` removes the page.

## Event history (`event_store.py`)

SQLite at `/var/lib/door_access/events.db`, WAL. `EventStore` writes (main thread only, best-effort — a history failure must never stop the door). `EventReader` reads from the web process.

`EventReader` opens `mode=rw` + `PRAGMA query_only=ON` rather than `mode=ro`: a WAL reader must write the `-shm` sidecar to coordinate with the live writer, so a truly read-only handle fails exactly when the door service is busy logging. The directory is setgid `dooradmin` (2770) so the DB the door service creates inherits a group the web user can reach.

`DOOR_STATE`/`DOOR_ALERT` carry a `door` key (door name) — there is one `DoorSensor` instance per configured door.

## File Map

| File | Role |
|------|------|
| `main.py` | Entry point, logging setup, signal handlers, event dispatch loop |
| `nfc_reader.py` | `NFCReader` class — pyscard PC/SC, GET_UID APDU `[0xFF,0xCA,0x00,0x00,0x00]`, daemon thread; ACR1552 LED/buzzer feedback via CCID escape (beep + green=granted / blue-blink=denied) |
| `lock_controller.py` | `LockController` class — GPIO17 relay, GPIO18 LED, GPIO27 button ISR, `threading.Timer` auto-relock |
| `door_sensor.py` | `DoorSensor` class — one instance per door (name + pin + active_low), edge detection, per-door open-too-long alert monitor thread; events tagged with `door` |
| `mqtt_handler.py` | `MQTTHandler` class — paho-mqtt, LWT, retain flags, auto-reconnect via `loop_start()`; HA MQTT discovery (`_publish_discovery`) + tag scanner (`publish_tag`); monitoring-only (see above) |
| `event_store.py` | `EventStore` (writer, door service) / `EventReader` (reader, web) — SQLite event history |
| `control_socket.py` | `ControlSocket` (server, door service) / `ControlClient` (web); `normalize_uid` shared by both |
| `web_admin.py` | Flask web admin — PAM login, `dooradmin` gating, CSRF, login throttle, tags + history + unlock |
| `templates/`, `static/` | Web admin pages; `static/nfc.js` holds the optional Web NFC scan |
| `config.json` | Door service settings (deployed to `/etc/door_access/config.json` on Pi) |
| `web.json` | Web admin settings (deployed to `/etc/door_access/web.json`) — separate file so `doorweb` never needs to read the MQTT password |
| `door_access.service` | systemd unit — runs as `door` user, auto-restart on failure |
| `door_admin.service` | systemd unit — runs as `doorweb`, gunicorn + TLS, hardened, `SupplementaryGroups=shadow dooradmin` |
| `update.sh` | Privileged updater — fetch, ff-only merge, reinstall, restart, auto-rollback. Run as root by the units below, never by the web app |
| `door_update.service` / `door_update_check.service` | Oneshot root units: apply / check. Separate so "may check" and "may apply" are distinct capabilities |
| `door-update.rules` | → `/etc/polkit-1/rules.d/50-door-update.rules`. Lets `doorweb` start exactly those two units and nothing else |
| `install.sh` | apt deps (incl. rpi-lgpio), venv, CCID escape, users/groups, polkit rule, local CA + TLS cert, dirs, installs files, enables both services. `DOOR_SRC_DIR=<path>` installs from a local checkout instead of GitHub |
| `README.md` | Wiring, setup, MQTT topic reference, HA config examples |

## Hardware

- **GPIO17** — Relay HAT signal (active-low: LOW = unlocked, HIGH = locked)
- **GPIO18** — LED button illumination (HIGH = on when unlocked)
- **GPIO27** — Button input, internal pull-up, FALLING edge = press
- **GPIO22** — Left door sensor (NC fridge-light switch), internal pull-up, LOW = open
- **GPIO23** — Right door sensor (NC fridge-light switch), internal pull-up, LOW = open
- **USB** — ACR1552U NFC reader (pyscard/PC/SC, `pcscd` daemon required)

Two doors, **one shared lock** (single relay on GPIO17). Door sensors are configured via the `doors` list in `config.json`.

## Runtime Paths on Pi

- App files: `/opt/door_access/` (incl. `templates/`, `static/`, `venv/`)
- Config: `/etc/door_access/config.json` (door-only), `web.json` + `web.env` + `web_secret` + `tls/` (web)
- Event DB: `/var/lib/door_access/events.db` (setgid `dooradmin` dir)
- Control socket: `/run/door_access/control.sock` (0660, group `dooradmin`)
- Log: `/var/log/door_access/door_access.log` (door-owned dir so rotation works; daily rotation, 7 days)
- Services: `/etc/systemd/system/door_access.service`, `door_admin.service`
- Service users: `door` (groups: gpio, plugdev, spi, dooradmin), `doorweb` (groups: shadow, dooradmin)
- Admin group: `dooradmin` — the web UI's access control; `usermod -aG dooradmin <user>` to grant

## MQTT Topics

| Topic | Direction | Retain | Payload |
|-------|-----------|--------|---------|
| `home/door/availability` | pub | Yes | `online`/`offline` (retained + LWT, so HA sees current state after a restart) |
| `home/door/lock/state` | pub | Yes | `LOCKED`/`UNLOCKED` |
| `home/door/sensor/<name>/state` | pub | Yes | `OPEN`/`CLOSED` (one per door) |
| `home/door/alert` | pub | No | string |
| `home/door/last_access` | pub | Yes | JSON |
| `home/door/nfc/tag` | pub | No | raw UID of every scan (HA MQTT tag scanner) |
| `homeassistant/<comp>/door_access/.../config` | pub | Yes | HA MQTT discovery configs (binary_sensor×(N+1), sensor×2, tag) when `mqtt.discovery` true |

**There are no `sub` rows.** `MQTTHandler` calls `subscribe()` nowhere and sets no `on_message`; the connection is publish-only. Adding a subscription re-opens the RBAC hole this design exists to close.

## Key Design Decisions

- **Timer callback posts to queue, never calls lock() directly** — keeps GPIO operations on the main thread only, avoiding race conditions with the timer thread.
- **ISR callbacks are fire-and-forget** — they only call `queue.put_nowait()`, never GPIO operations.
- **paho `loop_start()` not `loop_forever()`** — leaves main thread free for the event loop; reconnect is automatic.
- **Config never overwritten on reinstall** — `install.sh` skips config download if `/etc/door_access/config.json` already exists.

## Deployment Notes (Debian 13 / Pi OS trixie)

- **Python deps via apt, not pip** — PEP 668 blocks system pip installs. Use `python3-pyscard`, `python3-paho-mqtt`, and **`python3-rpi-lgpio`** (lgpio-backed drop-in for `RPi.GPIO`; classic `python3-rpi.gpio` fails on 6.x kernels). Don't install both.
- **systemd `WorkingDirectory`/`RuntimeDirectory`** — lgpio creates a `.lgd-nfy*` FIFO in the CWD, so the unit sets `RuntimeDirectory=door_access` + `WorkingDirectory=/run/door_access`.
- **polkit rule** `/etc/polkit-1/rules.d/50-door-pcsc.rules` — grants the session-less `door` user PC/SC access (pcsc-lite default is allow_active only).
- **CCID escape** — `/etc/libccid_Info.plist` `ifdDriverOptions=0x0001` enables the reader's LED/buzzer escape commands. Reader LED is **blue+green only (no red)**.
- `install.sh` performs all of the above.

## Development Notes

- The code uses Python 3.10+ union type syntax (`str | None`). Target Pi OS must have Python ≥ 3.10.
- `RPi.GPIO` and `pyscard` are Pi-specific. For local dev/testing, mock them with `unittest.mock.MagicMock`.
- Run `pcsc_scan` to verify the ACR1552U is recognized before starting the service.
- Check `journalctl -u door_access -f` for live logs when running under systemd.
- ACR1552 is **13.56 MHz only** (no 125 kHz LF tags); phones present a random UID per tap.
