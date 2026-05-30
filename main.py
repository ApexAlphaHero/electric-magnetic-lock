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
    if uid in authorized:
        name = authorized[uid]
        duration = config["lock"]["unlock_duration_seconds"]
        lock_ctrl.unlock(duration=duration)
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
    duration = config["lock"]["unlock_duration_seconds"]
    lock_ctrl.unlock(duration=duration)
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
    state = event["state"]
    mqtt.publish_door_state(state)
    logger.info("Door sensor: %s", state)


def _handle_door_alert(event: dict, mqtt: MQTTHandler, logger: logging.Logger) -> None:
    elapsed = event["elapsed"]
    msg = f"DOOR_OPEN_TOO_LONG elapsed={elapsed:.0f}s"
    mqtt.publish_alert(msg)
    logger.warning("Alert: %s", msg)


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


def main() -> None:
    config = load_config()
    logger = setup_logging(config)
    logger.info("Door access control system starting")

    shutdown_event = threading.Event()
    event_queue: queue.Queue = queue.Queue(maxsize=100)
    setup_signal_handlers(shutdown_event)

    mqtt_handler = MQTTHandler(event_queue, config, shutdown_event)
    lock_ctrl = LockController(event_queue, config, shutdown_event)
    door_sensor = DoorSensor(event_queue, config, shutdown_event)
    nfc_reader = NFCReader(event_queue, config, shutdown_event)

    try:
        lock_ctrl.setup()
        door_sensor.setup()
        door_sensor.start()
        mqtt_handler.setup()
        mqtt_handler.connect()
        nfc_reader.start()
        run_event_loop(event_queue, shutdown_event, lock_ctrl, mqtt_handler, config, logger)
    finally:
        logger.info("Shutdown initiated")
        nfc_reader.stop()
        door_sensor.stop()
        mqtt_handler.disconnect()
        lock_ctrl.cleanup()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
