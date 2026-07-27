import logging
import queue
import threading
import time

from smartcard.CardRequest import CardRequest
from smartcard.CardType import AnyCardType
from smartcard.Exceptions import (
    CardRequestTimeoutException,
    NoCardException,
    CardConnectionException,
)
from smartcard.pcsc.PCSCExceptions import EstablishContextException
from smartcard.scard import SCARD_CTL_CODE
from smartcard.System import readers

logger = logging.getLogger(__name__)

GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]

# ACR1552 (ACS) peripheral control via CCID escape command.
# Format: E0 00 00 <ins> <len> <data...>. Requires the CCID driver option
# DRIVER_OPTION_CCID_EXCHANGE_AUTHORIZED (ifdDriverOptions 0x0001 in
# /etc/libccid_Info.plist). This reader's LED is bi-color: bit0=blue, bit1=green
# (no red). Commands are best-effort; failures are logged and ignored so the
# read loop keeps working on readers that don't support them.
ESCAPE_CTL_CODE = SCARD_CTL_CODE(1)
_BUZZER = [0xE0, 0x00, 0x00, 0x28, 0x01]  # + duration byte
_LED = [0xE0, 0x00, 0x00, 0x29, 0x01]     # + state byte
LED_OFF, LED_BLUE, LED_GREEN = 0x00, 0x01, 0x02


class NFCReader:
    def __init__(self, event_queue: queue.Queue, config: dict, shutdown_event: threading.Event):
        self._queue = event_queue
        self._debounce_seconds: float = config["nfc"]["uid_debounce_seconds"]
        self._authorized = config.get("authorized_uids", {})
        self._feedback_enabled: bool = config.get("reader_feedback", {}).get("enabled", True)
        self._shutdown = shutdown_event
        self._last_uid: str | None = None
        self._last_uid_time: float = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="nfc-reader", daemon=True)
        self._thread.start()
        logger.info("NFC reader thread started")

    def stop(self) -> None:
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                reader_list = readers()
            except EstablishContextException as e:
                logger.error("PC/SC context error: %s", e)
                self._shutdown.wait(10)
                continue

            if not reader_list:
                logger.warning("No NFC readers found, retrying in 5s")
                self._shutdown.wait(5)
                continue

            reader = reader_list[0]
            logger.debug("Using reader: %s", reader)

            try:
                cardrequest = CardRequest(timeout=1, cardType=AnyCardType(), readers=[reader])
                try:
                    cardservice = cardrequest.waitforcard()
                except CardRequestTimeoutException:
                    continue

                cardservice.connection.connect()
                uid = self._read_uid(cardservice.connection)

                if uid:
                    # Physical feedback on every successful read; LED colour
                    # reflects authorization (debounce only gates the event).
                    self._signal_read(cardservice.connection, uid in self._authorized)
                    if not self._is_debounced(uid):
                        self._last_uid = uid
                        self._last_uid_time = time.monotonic()
                        self._post_uid_event(uid)
                    else:
                        logger.debug("UID %s suppressed by debounce", uid)

                # Poll until card is removed so debounce works correctly
                while not self._shutdown.is_set():
                    try:
                        self._read_uid(cardservice.connection)
                        self._shutdown.wait(0.2)
                    except Exception:
                        break

                self._last_uid = None
                try:
                    cardservice.connection.disconnect()
                except Exception:
                    pass

            except EstablishContextException as e:
                logger.error("PC/SC context lost (reader unplugged?): %s", e)
                self._shutdown.wait(10)
            except (CardConnectionException, NoCardException) as e:
                logger.debug("Card connection error: %s", e)
                self._shutdown.wait(1)
            except Exception as e:
                logger.error("NFC reader error: %s", e)
                self._shutdown.wait(2)

    def _read_uid(self, connection) -> str | None:
        try:
            response, sw1, sw2 = connection.transmit(GET_UID)
            if sw1 == 0x90 and sw2 == 0x00 and response:
                return "".join(f"{b:02X}" for b in response)
            logger.debug("GET_UID returned SW1=%02X SW2=%02X", sw1, sw2)
        except Exception as e:
            raise e
        return None

    def _escape(self, connection, payload: list[int]) -> None:
        try:
            connection.control(ESCAPE_CTL_CODE, payload)
        except Exception as e:
            logger.debug("Reader escape command failed: %s", e)

    def _signal_read(self, connection, granted: bool) -> None:
        """Beep on a successful read, then show LED feedback: solid green when
        the UID is authorized, blinking blue when it is not. Best-effort."""
        if not self._feedback_enabled:
            return
        self._escape(connection, _BUZZER + [0x02])  # short beep
        if granted:
            self._escape(connection, _LED + [LED_GREEN])
            self._shutdown.wait(1.2)
            self._escape(connection, _LED + [LED_OFF])
        else:
            for _ in range(4):
                self._escape(connection, _LED + [LED_BLUE])
                self._shutdown.wait(0.15)
                self._escape(connection, _LED + [LED_OFF])
                self._shutdown.wait(0.15)

    def _is_debounced(self, uid: str) -> bool:
        if uid == self._last_uid:
            elapsed = time.monotonic() - self._last_uid_time
            return elapsed < self._debounce_seconds
        return False

    def _post_uid_event(self, uid: str) -> None:
        try:
            self._queue.put_nowait({"type": "NFC_UID", "uid": uid})
            logger.debug("NFC UID posted: %s", uid)
        except queue.Full:
            logger.warning("Event queue full, dropping NFC_UID event for %s", uid)
