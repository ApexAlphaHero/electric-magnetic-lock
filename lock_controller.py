import logging
import queue
import threading
from typing import Callable

import RPi.GPIO as GPIO

logger = logging.getLogger(__name__)


class LockController:
    def __init__(
        self,
        event_queue: queue.Queue,
        config: dict,
        shutdown_event: threading.Event,
        state_change_callback: Callable[[str], None] | None = None,
    ):
        self._queue = event_queue
        self._relay_pin: int = config["gpio"]["relay_pin"]
        self._led_pin: int = config["gpio"]["led_pin"]
        self._button_pin: int = config["gpio"]["button_pin"]
        self._active_low: bool = config["lock"]["active_low_relay"]
        self._default_duration: float = config["lock"]["unlock_duration_seconds"]
        self._state: str = "LOCKED"
        self._state_lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._state_change_callback = state_change_callback

    def setup(self) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self._relay_pin, GPIO.OUT)
        GPIO.setup(self._led_pin, GPIO.OUT)
        GPIO.setup(self._button_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self._apply_state("LOCKED")
        GPIO.add_event_detect(
            self._button_pin,
            GPIO.FALLING,
            callback=self._button_callback,
            bouncetime=300,
        )
        logger.info(
            "LockController ready: relay=GPIO%d LED=GPIO%d button=GPIO%d",
            self._relay_pin, self._led_pin, self._button_pin,
        )

    def cleanup(self) -> None:
        with self._state_lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._apply_state("LOCKED")
        GPIO.cleanup()
        logger.info("LockController cleanup complete")

    def lock(self) -> None:
        with self._state_lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            old_state = self._state
            self._state = "LOCKED"
            self._apply_state("LOCKED")
        if old_state != "LOCKED" and self._state_change_callback:
            self._state_change_callback("LOCKED")

    def unlock(self, duration: float | None = None) -> None:
        dur = duration if duration is not None else self._default_duration
        with self._state_lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._state = "UNLOCKED"
            self._apply_state("UNLOCKED")
            self._timer = threading.Timer(dur, self._timer_callback)
            self._timer.daemon = True
            self._timer.start()
        logger.info("Door unlocked for %.1fs", dur)
        if self._state_change_callback:
            self._state_change_callback("UNLOCKED")

    def get_state(self) -> str:
        with self._state_lock:
            return self._state

    def _apply_state(self, state: str) -> None:
        if self._active_low:
            relay_val = GPIO.LOW if state == "UNLOCKED" else GPIO.HIGH
        else:
            relay_val = GPIO.HIGH if state == "UNLOCKED" else GPIO.LOW
        led_val = GPIO.HIGH if state == "UNLOCKED" else GPIO.LOW
        GPIO.output(self._relay_pin, relay_val)
        GPIO.output(self._led_pin, led_val)

    def _timer_callback(self) -> None:
        try:
            self._queue.put_nowait({"type": "UNLOCK_TIMER_EXPIRED"})
        except queue.Full:
            logger.warning("Event queue full, dropping UNLOCK_TIMER_EXPIRED")

    def _button_callback(self, channel: int) -> None:
        try:
            self._queue.put_nowait({"type": "BUTTON_PRESS"})
        except queue.Full:
            logger.warning("Event queue full, dropping BUTTON_PRESS")
