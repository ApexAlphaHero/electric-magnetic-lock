import logging
import queue
import threading
import time

import RPi.GPIO as GPIO

logger = logging.getLogger(__name__)


class DoorSensor:
    def __init__(self, event_queue: queue.Queue, name: str, pin: int,
                 active_low: bool, alert_threshold: float,
                 shutdown_event: threading.Event):
        self._queue = event_queue
        self._name = name
        self._pin = pin
        self._active_low = active_low
        self._alert_threshold = alert_threshold
        self._shutdown = shutdown_event
        self._state: str = "UNKNOWN"
        self._state_lock = threading.Lock()
        self._door_open_since: float | None = None
        self._alert_sent: bool = False
        self._alert_thread: threading.Thread | None = None

    def setup(self) -> None:
        GPIO.setup(self._pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        initial = self._read_state()
        with self._state_lock:
            self._state = initial
            if initial == "OPEN":
                self._door_open_since = time.monotonic()
        logger.info("DoorSensor '%s' ready: pin=GPIO%d initial=%s", self._name, self._pin, initial)

    def start(self) -> None:
        GPIO.add_event_detect(
            self._pin,
            GPIO.BOTH,
            callback=self._edge_callback,
            bouncetime=50,
        )
        self._alert_thread = threading.Thread(
            target=self._alert_monitor_loop, name="door-alert", daemon=True
        )
        self._alert_thread.start()
        # Publish the initial state so HA gets it on startup
        try:
            self._queue.put_nowait({"type": "DOOR_STATE", "door": self._name, "state": self._state})
        except queue.Full:
            pass

    def stop(self) -> None:
        try:
            GPIO.remove_event_detect(self._pin)
        except Exception:
            pass
        if self._alert_thread:
            self._alert_thread.join(timeout=3)

    def _read_state(self) -> str:
        val = GPIO.input(self._pin)
        if self._active_low:
            return "OPEN" if val == GPIO.LOW else "CLOSED"
        return "OPEN" if val == GPIO.HIGH else "CLOSED"

    def _edge_callback(self, channel: int) -> None:
        new_state = self._read_state()
        with self._state_lock:
            if new_state == self._state:
                return
            self._state = new_state
            if new_state == "OPEN":
                self._door_open_since = time.monotonic()
                self._alert_sent = False
            else:
                self._door_open_since = None
                self._alert_sent = False
        try:
            self._queue.put_nowait({"type": "DOOR_STATE", "door": self._name, "state": new_state})
        except queue.Full:
            logger.warning("Event queue full, dropping DOOR_STATE event")

    def _alert_monitor_loop(self) -> None:
        while not self._shutdown.is_set():
            self._shutdown.wait(timeout=5.0)
            with self._state_lock:
                if (
                    self._state == "OPEN"
                    and self._door_open_since is not None
                    and not self._alert_sent
                ):
                    elapsed = time.monotonic() - self._door_open_since
                    if elapsed >= self._alert_threshold:
                        self._alert_sent = True
                        try:
                            self._queue.put_nowait(
                                {"type": "DOOR_ALERT", "door": self._name, "elapsed": elapsed}
                            )
                        except queue.Full:
                            logger.warning("Event queue full, dropping DOOR_ALERT")
