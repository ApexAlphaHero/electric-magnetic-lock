import logging
import queue
import threading
import time

from smartcard.CardRequest import CardRequest
from smartcard.CardType import AnyCardType
from smartcard.Exceptions import (
    CardRequestTimeoutException,
    EstablishContextException,
    NoCardException,
    CardConnectionException,
)
from smartcard.System import readers

logger = logging.getLogger(__name__)

GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]


class NFCReader:
    def __init__(self, event_queue: queue.Queue, config: dict, shutdown_event: threading.Event):
        self._queue = event_queue
        self._debounce_seconds: float = config["nfc"]["uid_debounce_seconds"]
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
