import datetime
import json
import logging
import queue
import threading

logger = logging.getLogger(__name__)


class MQTTHandler:
    TOPIC_AVAILABILITY = "home/door/availability"
    TOPIC_LOCK_STATE   = "home/door/lock/state"
    TOPIC_LOCK_SET     = "home/door/lock/set"
    TOPIC_DOOR_STATE   = "home/door/sensor/state"
    TOPIC_ALERT        = "home/door/alert"
    TOPIC_LAST_ACCESS  = "home/door/last_access"
    TOPIC_TAG          = "home/door/nfc/tag"
    TOPIC_UNLOCK_SET   = "home/door/unlock_duration/set"
    TOPIC_UNLOCK_STATE = "home/door/unlock_duration/state"

    def __init__(self, event_queue: queue.Queue, config: dict, shutdown_event: threading.Event):
        self._queue = event_queue
        self._cfg = config["mqtt"]
        self._shutdown = shutdown_event
        self._enabled: bool = self._cfg.get("enabled", True)
        self._door_names = [d["name"] for d in config.get("doors", [])] or ["door"]
        self._unlock_duration: float = config["lock"]["unlock_duration_seconds"]
        self._client = None
        self._connected = False
        self._connected_lock = threading.Lock()

        if not self._enabled:
            logger.info("MQTT disabled in config — running without broker")

    def setup(self) -> None:
        if not self._enabled:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning("paho-mqtt not installed — MQTT disabled")
            self._enabled = False
            return

        self._client = mqtt.Client(client_id=self._cfg.get("client_id", "door_access"))
        self._client.will_set(self.TOPIC_AVAILABILITY, "offline", qos=1, retain=False)
        if self._cfg.get("username"):
            self._client.username_pw_set(
                self._cfg["username"], self._cfg.get("password")
            )
        if self._cfg.get("tls"):
            self._client.tls_set(ca_certs=self._cfg.get("tls_ca_cert"))
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        logger.info("MQTT client configured for %s:%d", self._cfg["broker"], self._cfg.get("port", 1883))

    def connect(self) -> None:
        if not self._enabled:
            return
        try:
            self._client.connect(
                self._cfg["broker"],
                port=self._cfg.get("port", 1883),
                keepalive=self._cfg.get("keepalive", 60),
            )
        except Exception as e:
            logger.error("MQTT initial connect failed: %s (background retry active)", e)
        self._client.loop_start()

    def disconnect(self) -> None:
        if not self._enabled or self._client is None:
            return
        self._safe_publish(self.TOPIC_AVAILABILITY, "offline", retain=False)
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            pass
        logger.info("MQTT disconnected")

    def publish_lock_state(self, state: str) -> None:
        self._safe_publish(self.TOPIC_LOCK_STATE, state, retain=True)

    def publish_door_state(self, door: str, state: str) -> None:
        self._safe_publish(f"home/door/sensor/{door}/state", state, retain=True)

    def publish_alert(self, message: str) -> None:
        self._safe_publish(self.TOPIC_ALERT, message, retain=False)

    def publish_last_access(self, uid: str, name: str, granted: bool) -> None:
        payload = json.dumps({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "uid": uid,
            "name": name,
            "granted": granted,
        })
        self._safe_publish(self.TOPIC_LAST_ACCESS, payload, retain=True)

    def publish_tag(self, uid: str) -> None:
        """Publish a scanned UID for Home Assistant's MQTT tag scanner so each
        scan fires HA's native tag_scanned trigger. Not retained (it's an event)."""
        self._safe_publish(self.TOPIC_TAG, uid, retain=False)

    def publish_unlock_duration(self, seconds: float) -> None:
        self._unlock_duration = seconds
        self._safe_publish(self.TOPIC_UNLOCK_STATE, self._fmt_duration(seconds), retain=True)

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        # Send an int when it's whole (HA number entity displays cleanly)
        return str(int(seconds)) if float(seconds).is_integer() else str(seconds)

    def _publish_discovery(self) -> None:
        """Publish Home Assistant MQTT discovery configs (retained) so the lock,
        door sensor, last-access sensor, alert sensor, and NFC tag scanner appear
        automatically without manual YAML."""
        if not self._cfg.get("discovery", True):
            return
        prefix = self._cfg.get("discovery_prefix", "homeassistant")
        device = {
            "identifiers": ["door_access_pi"],
            "name": "Door Access",
            "manufacturer": "DIY",
            "model": "Raspberry Pi Door Controller",
        }
        avail = {
            "availability_topic": self.TOPIC_AVAILABILITY,
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        configs = {
            f"{prefix}/lock/door_access/lock/config": {
                "name": "Lock", "unique_id": "door_access_lock",
                "state_topic": self.TOPIC_LOCK_STATE, "command_topic": self.TOPIC_LOCK_SET,
                "payload_lock": "LOCK", "payload_unlock": "UNLOCK",
                "state_locked": "LOCKED", "state_unlocked": "UNLOCKED",
                **avail, "device": device,
            },
            f"{prefix}/number/door_access/unlock_duration/config": {
                "name": "Unlock Duration", "unique_id": "door_access_unlock_duration",
                "command_topic": self.TOPIC_UNLOCK_SET, "state_topic": self.TOPIC_UNLOCK_STATE,
                "min": 1, "max": 60, "step": 1, "unit_of_measurement": "s",
                "mode": "box", "icon": "mdi:timer-lock-open", "retain": True,
                **avail, "device": device,
            },
            f"{prefix}/sensor/door_access/last_access/config": {
                "name": "Last Access", "unique_id": "door_access_last_access",
                "state_topic": self.TOPIC_LAST_ACCESS,
                "value_template": "{{ value_json.name }}",
                "json_attributes_topic": self.TOPIC_LAST_ACCESS,
                "icon": "mdi:account-key",
                **avail, "device": device,
            },
            f"{prefix}/sensor/door_access/alert/config": {
                "name": "Alert", "unique_id": "door_access_alert",
                "state_topic": self.TOPIC_ALERT, "icon": "mdi:alert",
                **avail, "device": device,
            },
            f"{prefix}/tag/door_access/config": {
                "topic": self.TOPIC_TAG, "value_template": "{{ value }}",
                "device": device,
            },
        }
        # One binary_sensor per door.
        for name in self._door_names:
            configs[f"{prefix}/binary_sensor/door_access/door_{name}/config"] = {
                "name": f"Door {name.capitalize()}",
                "unique_id": f"door_access_door_{name}",
                "state_topic": f"home/door/sensor/{name}/state",
                "payload_on": "OPEN", "payload_off": "CLOSED", "device_class": "door",
                **avail, "device": device,
            }
        for topic, payload in configs.items():
            self._safe_publish(topic, json.dumps(payload), retain=True)
        # Clear stale retained topics from the earlier single-door scheme.
        self._safe_publish(f"{prefix}/binary_sensor/door_access/door/config", "", retain=True)
        self._safe_publish(self.TOPIC_DOOR_STATE, "", retain=True)
        logger.info("Published HA MQTT discovery configs (%d entities)", len(configs))

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            with self._connected_lock:
                self._connected = True
            logger.info("MQTT connected to %s", self._cfg["broker"])
            client.publish(self.TOPIC_AVAILABILITY, "online", qos=1, retain=False)
            client.subscribe(self.TOPIC_LOCK_SET, qos=1)
            client.subscribe(self.TOPIC_UNLOCK_SET, qos=1)
            self._publish_discovery()
            # Publish current unlock duration so the HA number entity shows it.
            self.publish_unlock_duration(self._unlock_duration)
        else:
            logger.error("MQTT connect refused (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc) -> None:
        with self._connected_lock:
            self._connected = False
        if rc != 0:
            logger.warning("MQTT unexpected disconnect (rc=%d), will reconnect", rc)
        else:
            logger.info("MQTT disconnected cleanly")

    def _on_message(self, client, userdata, message) -> None:
        try:
            raw = message.payload.decode("utf-8").strip()
            if message.topic == self.TOPIC_UNLOCK_SET:
                try:
                    seconds = float(raw)
                except ValueError:
                    logger.warning("Ignoring non-numeric unlock duration: %r", raw)
                    return
                self._queue.put_nowait({"type": "SET_UNLOCK_DURATION", "seconds": seconds})
                return
            payload = raw.upper()
            if payload in ("LOCK", "UNLOCK"):
                self._queue.put_nowait({"type": "MQTT_COMMAND", "payload": payload})
            else:
                logger.warning("Ignoring unknown MQTT command: %r", payload)
        except queue.Full:
            logger.warning("Event queue full, dropping MQTT message")
        except Exception as e:
            logger.error("Error processing MQTT message: %s", e)

    def _safe_publish(self, topic: str, payload: str, retain: bool = False, qos: int = 1) -> None:
        if not self._enabled:
            return
        with self._connected_lock:
            connected = self._connected
        if not connected:
            logger.debug("MQTT offline, skipping publish to %s", topic)
            return
        try:
            self._client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as e:
            logger.error("MQTT publish error on %s: %s", topic, e)
