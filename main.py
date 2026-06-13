#!/usr/bin/env python3
import json
import logging
import logging.handlers
import queue
import signal
import sys
import threading

from door_sensor import DoorSensor
from lock_controller import LockController
from mqtt_handler import MQTTHandler
from nfc_reader import NFCReader

CONFIG_PATH = "/etc/door_access/config.json"


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def save_config(config: dict, path: str = CONFIG_PATH) -> None:
    """Persist config to disk (used when a setting is changed at runtime, e.g.
    the unlock duration set from Home Assistant). Writes atomically."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    import os
    os.replace(tmp, path)


def setup_logging(config: dict) -> logging.Logger:
    log_cfg = config.get("logging", {})
    log_file = log_cfg.get("log_file", "/var/log/door_access.log")
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
                config: dict, logger: logging.Logger) -> None:
    uid = event["uid"]
    authorized = config["authorized_uids"]
    # Forward every scan to Home Assistant's MQTT tag scanner (fires tag_scanned).
    mqtt.publish_tag(uid)
    if uid in authorized:
        name = authorized[uid]
        lock_ctrl.unlock()  # uses the controller's current default duration
        mqtt.publish_lock_state("UNLOCKED")
        mqtt.publish_last_access(uid=uid, name=name, granted=True)
        mqtt.publish_alert(f"ACCESS_GRANTED uid={uid} name={name}")
        logger.info("Access GRANTED: UID=%s Name=%s", uid, name)
    else:
        mqtt.publish_last_access(uid=uid, name="Unknown", granted=False)
        mqtt.publish_alert(f"UNAUTHORIZED_ACCESS uid={uid}")
        logger.warning("Access DENIED: UID=%s", uid)


def _handle_button(lock_ctrl: LockController, mqtt: MQTTHandler,
                   config: dict, logger: logging.Logger) -> None:
    lock_ctrl.unlock()  # uses the controller's current default duration
    mqtt.publish_lock_state("UNLOCKED")
    mqtt.publish_alert("BUTTON_UNLOCK")
    logger.info("Access via button press")


def _handle_mqtt_command(event: dict, lock_ctrl: LockController, mqtt: MQTTHandler,
                         logger: logging.Logger) -> None:
    cmd = event["payload"]
    if cmd == "UNLOCK":
        lock_ctrl.unlock()
        mqtt.publish_lock_state("UNLOCKED")
        logger.info("MQTT command: UNLOCK")
    elif cmd == "LOCK":
        lock_ctrl.lock()
        mqtt.publish_lock_state("LOCKED")
        logger.info("MQTT command: LOCK")


def _handle_door_state(event: dict, mqtt: MQTTHandler, logger: logging.Logger) -> None:
    door = event.get("door", "door")
    state = event["state"]
    mqtt.publish_door_state(door, state)
    logger.info("Door sensor '%s': %s", door, state)


def _handle_door_alert(event: dict, mqtt: MQTTHandler, logger: logging.Logger) -> None:
    door = event.get("door", "door")
    elapsed = event["elapsed"]
    msg = f"DOOR_OPEN_TOO_LONG door={door} elapsed={elapsed:.0f}s"
    mqtt.publish_alert(msg)
    logger.warning("Alert: %s", msg)


def _handle_set_unlock_duration(event: dict, lock_ctrl: LockController, mqtt: MQTTHandler,
                                config: dict, logger: logging.Logger) -> None:
    seconds = max(1.0, min(60.0, float(event["seconds"])))
    lock_ctrl.set_default_duration(seconds)
    config["lock"]["unlock_duration_seconds"] = seconds
    try:
        save_config(config)
    except Exception as e:
        logger.error("Failed to persist unlock duration: %s", e)
    mqtt.publish_unlock_duration(seconds)
    logger.info("Unlock duration set to %.0fs", seconds)


def dispatch_event(event: dict, lock_ctrl: LockController, mqtt: MQTTHandler,
                   config: dict, logger: logging.Logger) -> None:
    etype = event["type"]
    if etype == "NFC_UID":
        _handle_nfc(event, lock_ctrl, mqtt, config, logger)
    elif etype == "BUTTON_PRESS":
        _handle_button(lock_ctrl, mqtt, config, logger)
    elif etype == "MQTT_COMMAND":
        _handle_mqtt_command(event, lock_ctrl, mqtt, logger)
    elif etype == "DOOR_STATE":
        _handle_door_state(event, mqtt, logger)
    elif etype == "DOOR_ALERT":
        _handle_door_alert(event, mqtt, logger)
    elif etype == "SET_UNLOCK_DURATION":
        _handle_set_unlock_duration(event, lock_ctrl, mqtt, config, logger)
    elif etype == "UNLOCK_TIMER_EXPIRED":
        lock_ctrl.lock()
        mqtt.publish_lock_state("LOCKED")
        logger.info("Auto-relock: timer expired")
    else:
        logger.debug("Unknown event type: %s", etype)


def run_event_loop(event_queue: queue.Queue, shutdown_event: threading.Event,
                   lock_ctrl: LockController, mqtt: MQTTHandler,
                   config: dict, logger: logging.Logger) -> None:
    logger.info("Event loop started")
    while not shutdown_event.is_set():
        try:
            event = event_queue.get(timeout=1.0)
            dispatch_event(event, lock_ctrl, mqtt, config, logger)
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

    mqtt_handler = MQTTHandler(event_queue, config, shutdown_event)
    lock_ctrl = LockController(event_queue, config, shutdown_event)
    door_sensors = _build_door_sensors(event_queue, config, shutdown_event)
    nfc_reader = NFCReader(event_queue, config, shutdown_event)

    try:
        lock_ctrl.setup()
        for ds in door_sensors:
            ds.setup()
            ds.start()
        mqtt_handler.setup()
        mqtt_handler.connect()
        nfc_reader.start()
        run_event_loop(event_queue, shutdown_event, lock_ctrl, mqtt_handler, config, logger)
    finally:
        logger.info("Shutdown initiated")
        nfc_reader.stop()
        for ds in door_sensors:
            ds.stop()
        mqtt_handler.disconnect()
        lock_ctrl.cleanup()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
