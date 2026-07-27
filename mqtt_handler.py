import datetime
import json
import logging
import threading

logger = logging.getLogger(__name__)


class MQTTHandler:
    """Home Assistant integration — publish only.

    Nothing here is writable from Home Assistant. HA has no per-user access
    control, so any entity it exposes is available to every HA user; door state,
    lock state, alerts and access history are published for observation, and the
    handler subscribes to no topics at all. Settings that affect the door live
    behind the web admin's per-user authentication instead.
    """

    TOPIC_AVAILABILITY = "home/door/availability"
    TOPIC_LOCK_STATE   = "home/door/lock/state"
    TOPIC_DOOR_STATE   = "home/door/sensor/state"
    TOPIC_ALERT        = "home/door/alert"
    TOPIC_LAST_ACCESS  = "home/door/last_access"
    TOPIC_TAG          = "home/door/nfc/tag"

    # Retained topics from removed features (remote lock/unlock, HA-driven tag
    # enrollment and removal, the unlock-duration number). Cleared on connect so
    # the entities and their retained state disappear from HA and the broker
    # instead of lingering.
    RETIRED_TOPICS = (
        "home/door/lock/set",
        "home/door/enroll/set", "home/door/enroll/state",
        "home/door/enroll_name/set", "home/door/enroll_name/state",
        "home/door/known_tags/set", "home/door/known_tags/state",
        "home/door/remove_tag/set",
        "home/door/unlock_duration/set", "home/door/unlock_duration/state",
    )
    RETIRED_DISCOVERY = (
        "lock/door_access/lock",
        "switch/door_access/enroll",
        "text/door_access/enroll_name",
        "select/door_access/known_tags",
        "button/door_access/remove_tag",
        "number/door_access/unlock_duration",
    )

    def __init__(self, config: dict, shutdown_event: threading.Event):
        self._cfg = config["mqtt"]
        self._shutdown = shutdown_event
        self._enabled: bool = self._cfg.get("enabled", True)
        self._door_names = [d["name"] for d in config.get("doors", [])] or ["door"]
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
        self._client.will_set(self.TOPIC_AVAILABILITY, "offline", qos=1, retain=True)
        if self._cfg.get("username"):
            self._client.username_pw_set(
                self._cfg["username"], self._cfg.get("password")
            )
        if self._cfg.get("tls"):
            self._client.tls_set(ca_certs=self._cfg.get("tls_ca_cert"))
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        # No on_message handler: this client subscribes to nothing.
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
        self._safe_publish(self.TOPIC_AVAILABILITY, "offline", retain=True)
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

    def _publish_discovery(self) -> None:
        """Publish Home Assistant MQTT discovery configs (retained) so the lock
        status, door sensors, last-access sensor, alert sensor, and NFC tag
        scanner appear automatically without manual YAML.

        The lock is exposed as a read-only binary_sensor, not a `lock` entity:
        an HA lock entity requires a command topic, which would give every HA
        user the ability to unlock the door."""
        if not self._cfg.get("discovery", True):
            return
        prefix = self._cfg.get("discovery_prefix", "homeassistant")
        device = self._device_block()
        avail = self._avail_block()
        configs = {
            f"{prefix}/binary_sensor/door_access/lock/config": {
                "name": "Lock", "unique_id": "door_access_lock_state",
                "state_topic": self.TOPIC_LOCK_STATE,
                "payload_on": "UNLOCKED", "payload_off": "LOCKED",
                "device_class": "lock",
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
        self._clear_retired(prefix)
        logger.info("Published HA MQTT discovery configs (%d entities)", len(configs))

    def _clear_retired(self, prefix: str) -> None:
        """Delete retained discovery configs and state for features removed from
        Home Assistant (remote unlock, tag enrollment/removal, unlock duration).
        An empty retained payload on a discovery topic tells HA to drop the
        entity; without this the old controls stay in HA and remain clickable
        after an upgrade."""
        for suffix in self.RETIRED_DISCOVERY:
            self._safe_publish(f"{prefix}/{suffix}/config", "", retain=True)
        for topic in self.RETIRED_TOPICS:
            self._safe_publish(topic, "", retain=True)

    def _device_block(self) -> dict:
        return {
            "identifiers": ["door_access_pi"],
            "name": "Door Access",
            "manufacturer": "DIY",
            "model": "Raspberry Pi Door Controller",
        }

    def _avail_block(self) -> dict:
        return {
            "availability_topic": self.TOPIC_AVAILABILITY,
            "payload_available": "online",
            "payload_not_available": "offline",
        }

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            with self._connected_lock:
                self._connected = True
            logger.info("MQTT connected to %s", self._cfg["broker"])
            client.publish(self.TOPIC_AVAILABILITY, "online", qos=1, retain=True)
            # No subscribe() call: Home Assistant cannot change anything here.
            self._publish_discovery()
        else:
            logger.error("MQTT connect refused (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc) -> None:
        with self._connected_lock:
            self._connected = False
        if rc != 0:
            logger.warning("MQTT unexpected disconnect (rc=%d), will reconnect", rc)
        else:
            logger.info("MQTT disconnected cleanly")

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
