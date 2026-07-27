#!/usr/bin/env python3
import json
import logging
import logging.handlers
import os
import queue
import signal
import stat
import sys
import threading
import time

from control_socket import ControlSocket, normalize_uid
from door_sensor import DoorSensor
from event_store import EventStore
from lock_controller import LockController
from mqtt_handler import MQTTHandler
from nfc_reader import NFCReader

CONFIG_PATH = "/etc/door_access/config.json"
PRUNE_INTERVAL_SECONDS = 24 * 60 * 60
# How long the door reader stays armed to capture a tag after the web admin
# arms it. Long enough to walk to the door, short enough that a forgotten arm
# doesn't sit there swallowing a legitimate unlock.
ENROLL_WINDOW_SECONDS = 60


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def save_config(config: dict, path: str = CONFIG_PATH) -> None:
    """Persist config to disk (used when a setting is changed at runtime, e.g.
    the unlock duration set from Home Assistant). Writes atomically.

    The replacement inherits the *original* file's mode and ownership. Without
    that, os.replace() installs a fresh file created under the service umask —
    silently widening this file to 0644 on the first save. It contains the MQTT
    password, and there is a second service account on this host, so it must not
    drift world-readable.
    """
    mode, uid, gid = 0o640, -1, -1
    try:
        st = os.stat(path)
        mode, uid, gid = stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid
    except FileNotFoundError:
        pass

    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.chmod(tmp, mode)
    if uid != -1:
        try:
            os.chown(tmp, uid, gid)
        except PermissionError:
            pass  # not root; the file is already owned by us
    os.replace(tmp, path)


def setup_logging(config: dict) -> logging.Logger:
    log_cfg = config.get("logging", {})
    log_file = log_cfg.get("log_file", "/var/log/door_access/door_access.log")
    backup_count = log_cfg.get("backup_count", 7)
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    try:
        from systemd.journal import JournaldLogHandler
        journald = JournaldLogHandler()
        journald.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(journald)
    except ImportError:
        pass

    return logging.getLogger(__name__)


def setup_signal_handlers(shutdown_event: threading.Event) -> None:
    def handler(signum, frame):
        logging.getLogger(__name__).info("Signal %d received, initiating shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _handle_nfc(event: dict, lock_ctrl: LockController, mqtt: MQTTHandler,
                config: dict, events: EventStore, runtime: dict,
                logger: logging.Logger) -> None:
    uid = event["uid"]
    authorized = config["authorized_uids"]
    # Forward every scan to Home Assistant's MQTT tag scanner (fires tag_scanned).
    mqtt.publish_tag(uid)

    # Enrollment armed from the web admin: this scan captures the tag instead of
    # opening the door. One-shot — it disarms whatever the outcome, so a forgotten
    # arm can never leave the reader in capture mode.
    if runtime["enroll_event"].is_set():
        _capture_tag(uid, config, events, runtime, mqtt, logger)
        return

    if uid in authorized:
        name = authorized[uid]
        lock_ctrl.unlock()  # uses the controller's current default duration
        mqtt.publish_lock_state("UNLOCKED")
        mqtt.publish_last_access(uid=uid, name=name, granted=True)
        mqtt.publish_alert(f"ACCESS_GRANTED uid={uid} name={name}")
        events.log("ACCESS_GRANTED", uid=uid, name=name, granted=True)
        logger.info("Access GRANTED: UID=%s Name=%s", uid, name)
    else:
        mqtt.publish_last_access(uid=uid, name="Unknown", granted=False)
        mqtt.publish_alert(f"UNAUTHORIZED_ACCESS uid={uid}")
        # Recorded as ACCESS_DENIED so the web admin can offer the UID for
        # enrollment ("click a recent unknown scan").
        events.log("ACCESS_DENIED", uid=uid, name="Unknown", granted=False)
        logger.warning("Access DENIED: UID=%s", uid)


def _disarm_enroll(runtime: dict) -> None:
    runtime["enroll_event"].clear()
    runtime["enroll_deadline"] = 0.0


def _capture_tag(uid: str, config: dict, events: EventStore, runtime: dict,
                 mqtt: MQTTHandler, logger: logging.Logger) -> None:
    """Record a tag scanned at the door reader while enrollment was armed.

    Result is stashed in runtime for the web UI to collect on its next poll —
    the operator is standing at the door, not at the browser, so the outcome has
    to survive until they look."""
    authorized = config["authorized_uids"]
    actor = runtime.get("enroll_actor", "?")
    _disarm_enroll(runtime)

    if uid in authorized:
        runtime["enroll_result"] = {"status": "already", "uid": uid, "name": authorized[uid]}
        events.log("TAG_SCAN_DUPLICATE", uid=uid, name=authorized[uid], actor=actor)
        logger.info("Enroll scan: UID=%s already enrolled as %s", uid, authorized[uid])
        return

    name = (runtime.get("enroll_name") or "").strip()[:64] or f"Tag {uid[-4:]}"
    authorized[uid] = name
    if not _persist(config, logger):
        del authorized[uid]
        runtime["enroll_result"] = {"status": "error", "uid": uid,
                                    "error": "could not write config — tag not added"}
        return

    runtime["enroll_result"] = {"status": "added", "uid": uid, "name": name}
    events.log("TAG_ADDED", uid=uid, name=name, actor=actor, detail="scanned at door reader")
    mqtt.publish_alert(f"TAG_ADDED uid={uid} name={name}")
    logger.info("Tag ADDED by %s via reader: UID=%s Name=%s", actor, uid, name)


def _handle_button(lock_ctrl: LockController, mqtt: MQTTHandler,
                   config: dict, events: EventStore, logger: logging.Logger) -> None:
    lock_ctrl.unlock()  # uses the controller's current default duration
    mqtt.publish_lock_state("UNLOCKED")
    mqtt.publish_alert("BUTTON_UNLOCK")
    events.log("BUTTON_UNLOCK", granted=True)
    logger.info("Access via button press")


def _handle_door_state(event: dict, mqtt: MQTTHandler, events: EventStore,
                       logger: logging.Logger) -> None:
    door = event.get("door", "door")
    state = event["state"]
    mqtt.publish_door_state(door, state)
    events.log("DOOR_STATE", door=door, detail=state)
    logger.info("Door sensor '%s': %s", door, state)


def _handle_door_alert(event: dict, mqtt: MQTTHandler, events: EventStore,
                       logger: logging.Logger) -> None:
    door = event.get("door", "door")
    elapsed = event["elapsed"]
    msg = f"DOOR_OPEN_TOO_LONG door={door} elapsed={elapsed:.0f}s"
    mqtt.publish_alert(msg)
    events.log("DOOR_OPEN_TOO_LONG", door=door, detail=f"{elapsed:.0f}s")
    logger.warning("Alert: %s", msg)


# ── Web admin commands ──────────────────────────────────────────────────────────
# Serviced here, on the main thread, so config writes and GPIO stay single
# threaded. The web process has already authenticated the operator; `actor` is
# the username it authenticated, recorded for the audit trail.

def _handle_web_command(event: dict, lock_ctrl: LockController, mqtt: MQTTHandler,
                        config: dict, events: EventStore, runtime: dict,
                        logger: logging.Logger) -> None:
    request = event.get("request", {})
    reply: queue.Queue = event["reply"]
    cmd = request.get("cmd", "")
    actor = str(request.get("actor", "?"))[:64]

    try:
        response = _run_web_command(cmd, request, actor, lock_ctrl, mqtt, config,
                                    events, runtime, logger)
    except Exception as e:
        logger.exception("Web command %r failed", cmd)
        response = {"ok": False, "error": str(e)}

    try:
        reply.put_nowait(response)
    except queue.Full:
        logger.warning("Web command reply dropped (caller gone)")


def _run_web_command(cmd: str, request: dict, actor: str, lock_ctrl: LockController,
                     mqtt: MQTTHandler, config: dict, events: EventStore,
                     runtime: dict, logger: logging.Logger) -> dict:
    authorized = config["authorized_uids"]

    if cmd == "arm_enroll":
        runtime["enroll_name"] = str(request.get("name", ""))[:64]
        runtime["enroll_actor"] = actor
        runtime["enroll_result"] = None
        runtime["enroll_deadline"] = time.monotonic() + ENROLL_WINDOW_SECONDS
        # Setting the event last means the reader thread never sees it armed
        # with a stale name or deadline.
        runtime["enroll_event"].set()
        events.log("ENROLL_ARMED", actor=actor, name=runtime["enroll_name"] or None)
        logger.info("Reader armed for enrollment by %s (name=%r)", actor, runtime["enroll_name"])
        return {"ok": True, "result": {"armed": True, "seconds": ENROLL_WINDOW_SECONDS}}

    if cmd == "enroll_status":
        armed = runtime["enroll_event"].is_set()
        remaining = max(0, int(runtime["enroll_deadline"] - time.monotonic())) if armed else 0
        # Hand the result over exactly once; the browser acts on it and the next
        # poll must not replay a stale capture.
        result, runtime["enroll_result"] = runtime["enroll_result"], None
        return {"ok": True, "result": {"armed": armed, "remaining": remaining,
                                       "capture": result}}

    if cmd == "cancel_enroll":
        _disarm_enroll(runtime)
        runtime["enroll_result"] = None
        logger.info("Reader disarmed by %s", actor)
        return {"ok": True, "result": {"armed": False}}

    if cmd == "status":
        return {"ok": True, "result": {
            "lock": lock_ctrl.get_state(),
            "unlock_duration": config["lock"]["unlock_duration_seconds"],
            "doors": [d["name"] for d in config.get("doors", [])],
            "tag_count": len(authorized),
        }}

    if cmd == "list_tags":
        return {"ok": True, "result": [
            {"uid": uid, "name": name}
            for uid, name in sorted(authorized.items(), key=lambda kv: kv[1].lower())
        ]}

    if cmd == "unlock":
        lock_ctrl.unlock()
        mqtt.publish_lock_state("UNLOCKED")
        mqtt.publish_alert(f"WEB_UNLOCK actor={actor}")
        events.log("WEB_UNLOCK", actor=actor, granted=True)
        logger.info("Web unlock by %s", actor)
        return {"ok": True, "result": {"lock": "UNLOCKED"}}

    if cmd == "lock":
        lock_ctrl.lock()
        mqtt.publish_lock_state("LOCKED")
        events.log("WEB_LOCK", actor=actor)
        logger.info("Web lock by %s", actor)
        return {"ok": True, "result": {"lock": "LOCKED"}}

    if cmd == "set_unlock_duration":
        try:
            seconds = float(request.get("seconds"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "duration must be a number"}
        if not 1 <= seconds <= 60:
            return {"ok": False, "error": "duration must be between 1 and 60 seconds"}
        previous = config["lock"]["unlock_duration_seconds"]
        config["lock"]["unlock_duration_seconds"] = seconds
        if not _persist(config, logger):
            config["lock"]["unlock_duration_seconds"] = previous
            return {"ok": False, "error": "could not write config — duration unchanged"}
        lock_ctrl.set_default_duration(seconds)
        events.log("SET_UNLOCK_DURATION", actor=actor, detail=f"{seconds:.0f}s (was {previous:.0f}s)")
        logger.info("Unlock duration set to %.0fs by %s", seconds, actor)
        return {"ok": True, "result": {"unlock_duration": seconds}}

    if cmd == "add_tag":
        uid = normalize_uid(str(request.get("uid", "")))
        name = str(request.get("name", "")).strip()[:64]
        if not uid:
            return {"ok": False, "error": "UID must be hex (e.g. AABB1122)"}
        if not name:
            name = f"Tag {uid[-4:]}"
        if uid in authorized:
            return {"ok": False, "error": f"UID {uid} is already assigned to {authorized[uid]}"}
        authorized[uid] = name
        if not _persist(config, logger):
            del authorized[uid]
            return {"ok": False, "error": "could not write config — tag not added"}
        events.log("TAG_ADDED", uid=uid, name=name, actor=actor)
        mqtt.publish_alert(f"TAG_ADDED uid={uid} name={name}")
        logger.info("Tag ADDED by %s: UID=%s Name=%s", actor, uid, name)
        return {"ok": True, "result": {"uid": uid, "name": name}}

    if cmd == "remove_tag":
        uid = normalize_uid(str(request.get("uid", "")))
        if uid not in authorized:
            return {"ok": False, "error": "no such tag"}
        name = authorized.pop(uid)
        if not _persist(config, logger):
            authorized[uid] = name
            return {"ok": False, "error": "could not write config — tag not removed"}
        events.log("TAG_REMOVED", uid=uid, name=name, actor=actor)
        mqtt.publish_alert(f"TAG_REMOVED uid={uid} name={name}")
        logger.info("Tag REMOVED by %s: UID=%s Name=%s", actor, uid, name)
        return {"ok": True, "result": {"uid": uid, "name": name}}

    if cmd == "rename_tag":
        uid = normalize_uid(str(request.get("uid", "")))
        name = str(request.get("name", "")).strip()[:64]
        if uid not in authorized:
            return {"ok": False, "error": "no such tag"}
        if not name:
            return {"ok": False, "error": "name cannot be empty"}
        previous, authorized[uid] = authorized[uid], name
        if not _persist(config, logger):
            authorized[uid] = previous
            return {"ok": False, "error": "could not write config — tag not renamed"}
        events.log("TAG_RENAMED", uid=uid, name=name, actor=actor,
                   detail=f"was {previous}")
        logger.info("Tag RENAMED by %s: UID=%s %s -> %s", actor, uid, previous, name)
        return {"ok": True, "result": {"uid": uid, "name": name}}

    if cmd == "login_event":
        events.log(str(request.get("event", "WEB_LOGIN"))[:32], actor=actor,
                   detail=str(request.get("detail", ""))[:200])
        return {"ok": True, "result": {}}

    return {"ok": False, "error": f"unknown command {cmd!r}"}


def _persist(config: dict, logger: logging.Logger) -> bool:
    """Save config, reporting success. Callers roll their in-memory change back
    on failure so the running set never diverges from what is on disk."""
    try:
        save_config(config)
        return True
    except Exception as e:
        logger.error("Failed to persist config: %s", e)
        return False


def dispatch_event(event: dict, lock_ctrl: LockController, mqtt: MQTTHandler,
                   config: dict, events: EventStore, runtime: dict,
                   logger: logging.Logger) -> None:
    etype = event["type"]
    if etype == "NFC_UID":
        _handle_nfc(event, lock_ctrl, mqtt, config, events, runtime, logger)
    elif etype == "BUTTON_PRESS":
        _handle_button(lock_ctrl, mqtt, config, events, logger)
    elif etype == "DOOR_STATE":
        _handle_door_state(event, mqtt, events, logger)
    elif etype == "DOOR_ALERT":
        _handle_door_alert(event, mqtt, events, logger)
    elif etype == "WEB_COMMAND":
        _handle_web_command(event, lock_ctrl, mqtt, config, events, runtime, logger)
    elif etype == "UNLOCK_TIMER_EXPIRED":
        lock_ctrl.lock()
        mqtt.publish_lock_state("LOCKED")
        logger.info("Auto-relock: timer expired")
    else:
        logger.debug("Unknown event type: %s", etype)


def run_event_loop(event_queue: queue.Queue, shutdown_event: threading.Event,
                   lock_ctrl: LockController, mqtt: MQTTHandler,
                   config: dict, events: EventStore, runtime: dict,
                   logger: logging.Logger) -> None:
    logger.info("Event loop started")
    last_prune = time.monotonic()
    while not shutdown_event.is_set():
        if time.monotonic() - last_prune >= PRUNE_INTERVAL_SECONDS:
            last_prune = time.monotonic()
            events.prune()
        # Expire a forgotten arm here rather than lazily on the next scan, so the
        # reader's LED stops showing enroll feedback the moment the window closes.
        if (runtime["enroll_event"].is_set()
                and time.monotonic() > runtime["enroll_deadline"]):
            _disarm_enroll(runtime)
            runtime["enroll_result"] = {"status": "timeout"}
            logger.info("Enrollment window expired without a scan")
        try:
            event = event_queue.get(timeout=1.0)
            dispatch_event(event, lock_ctrl, mqtt, config, events, runtime, logger)
        except queue.Empty:
            continue
        except Exception:
            logger.exception("Unhandled error in dispatch loop")
    logger.info("Event loop stopped")


def _build_door_sensors(event_queue: queue.Queue, config: dict,
                        shutdown_event: threading.Event) -> list[DoorSensor]:
    threshold = config["door"]["open_alert_threshold_seconds"]
    doors_cfg = config.get("doors")
    if not doors_cfg:
        # Legacy single-sensor config (gpio.door_sensor_pin)
        gpio = config["gpio"]
        doors_cfg = [{
            "name": "door",
            "sensor_pin": gpio["door_sensor_pin"],
            "active_low": gpio.get("door_sensor_active_low", True),
        }]
    return [
        DoorSensor(event_queue, d["name"], d["sensor_pin"],
                   d.get("active_low", True), threshold, shutdown_event)
        for d in doors_cfg
    ]


def main() -> None:
    config = load_config()
    logger = setup_logging(config)
    logger.info("Door access control system starting")

    shutdown_event = threading.Event()
    event_queue: queue.Queue = queue.Queue(maxsize=100)
    setup_signal_handlers(shutdown_event)

    # Enrollment state: runtime only, never persisted. enroll_event is shared
    # with the NFC reader thread so an armed scan flashes green at the door
    # instead of the blue "denied" blink.
    runtime: dict = {
        "enroll_event": threading.Event(),
        "enroll_name": "",
        "enroll_actor": "",
        "enroll_deadline": 0.0,
        "enroll_result": None,
    }

    web_cfg = config.get("web", {})
    events = EventStore(
        web_cfg.get("event_db", "/var/lib/door_access/events.db"),
        web_cfg.get("event_retention_days", 90),
    )
    mqtt_handler = MQTTHandler(config, shutdown_event)
    lock_ctrl = LockController(event_queue, config, shutdown_event)
    door_sensors = _build_door_sensors(event_queue, config, shutdown_event)
    nfc_reader = NFCReader(event_queue, config, shutdown_event, runtime["enroll_event"])
    control = ControlSocket(
        event_queue, shutdown_event,
        web_cfg.get("control_socket", "/run/door_access/control.sock"),
    )

    try:
        events.setup()
        events.log("SERVICE_START")
        lock_ctrl.setup()
        for ds in door_sensors:
            ds.setup()
            ds.start()
        mqtt_handler.setup()
        mqtt_handler.connect()
        nfc_reader.start()
        if web_cfg.get("enabled", True):
            try:
                control.setup()
                control.start()
            except Exception as e:
                # The web admin is an add-on; the door must still work without it.
                logger.error("Control socket unavailable (%s) — web admin disabled", e)
        run_event_loop(event_queue, shutdown_event, lock_ctrl, mqtt_handler,
                       config, events, runtime, logger)
    finally:
        logger.info("Shutdown initiated")
        control.stop()
        nfc_reader.stop()
        for ds in door_sensors:
            ds.stop()
        mqtt_handler.disconnect()
        lock_ctrl.cleanup()
        events.log("SERVICE_STOP")
        events.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
