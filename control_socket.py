"""Unix-domain control socket for the web admin service.

The web app runs as a separate, less-privileged process and is deliberately
given **no** write access to config.json and no GPIO access at all. Everything
it wants to change it asks for here, and the request is serviced on the main
event loop — so config mutation and GPIO writes stay single-threaded, the same
invariant the rest of this project is built on.

Authorization is filesystem permissions: the socket is mode 0660, group
`dooradmin`. If you can open it, you are already an authorized admin process.
There is no auth inside the protocol.

Wire format is newline-delimited JSON, one request and one response per
connection:

    -> {"cmd": "add_tag", "uid": "AABB1122", "name": "Alice", "actor": "david"}
    <- {"ok": true, "result": {...}}
"""

import grp
import json
import logging
import os
import queue
import socket
import threading

logger = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = "/run/door_access/control.sock"
ADMIN_GROUP = "dooradmin"
MAX_REQUEST_BYTES = 8192
REPLY_TIMEOUT = 5.0


def normalize_uid(raw: str) -> str:
    """Canonicalize a UID to the form the reader produces: uppercase hex, no
    separators.

    This lives on the protocol boundary because both processes need the exact
    same answer. UIDs arrive in several shapes — `nfc_reader` emits `AABB1122`,
    the Web NFC API's `serialNumber` is `aa:bb:11:22`, and people hand-type
    `aa bb 11 22` or `AA-BB-11-22`. Normalizing in only one of the two processes
    would let a tag be stored in a form that no live scan ever matches, which
    fails silently at the door.

    Returns "" if the result is not valid hex, so callers can reject it.
    """
    cleaned = "".join(raw.split()).replace(":", "").replace("-", "").upper()
    if not cleaned or len(cleaned) % 2 or any(c not in "0123456789ABCDEF" for c in cleaned):
        return ""
    return cleaned


class ControlSocket:
    """Accepts admin requests and hands them to the event loop for execution."""

    def __init__(self, event_queue: queue.Queue, shutdown_event: threading.Event,
                 socket_path: str = DEFAULT_SOCKET_PATH, group: str = ADMIN_GROUP):
        self._queue = event_queue
        self._shutdown = shutdown_event
        self._path = socket_path
        self._group = group
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def setup(self) -> None:
        # A leftover socket file from an unclean shutdown would make bind() fail.
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.error("Could not clear stale control socket: %s", e)

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self._path)
        self._sock.listen(8)
        self._sock.settimeout(1.0)
        self._grant_group_access()
        logger.info("Control socket listening at %s", self._path)

    def _grant_group_access(self) -> None:
        """Hand the socket (and its directory) to the dooradmin group.

        systemd creates the RuntimeDirectory owned by the service's own group,
        so the web user cannot even traverse it until we re-group it here. The
        door user must be a member of dooradmin for the chown to be permitted.
        """
        try:
            gid = grp.getgrnam(self._group).gr_gid
        except KeyError:
            logger.warning("Group '%s' does not exist — web admin will not be able to "
                           "reach the control socket", self._group)
            return
        for path, mode in ((os.path.dirname(self._path), 0o750), (self._path, 0o660)):
            try:
                os.chown(path, -1, gid)
                os.chmod(path, mode)
            except OSError as e:
                logger.warning("Could not grant '%s' access to %s: %s", self._group, path, e)

    def start(self) -> None:
        if self._sock is None:
            return
        self._thread = threading.Thread(target=self._run, name="control-socket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed during shutdown
            # Serve inline: requests are rare, tiny, and already serialized by
            # the event loop they delegate to.
            try:
                self._serve(conn)
            except Exception as e:
                logger.error("Control socket request failed: %s", e)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        logger.info("Control socket stopped")

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(REPLY_TIMEOUT)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(1024)
            if not chunk:
                return
            buf += chunk
            if len(buf) > MAX_REQUEST_BYTES:
                self._send(conn, {"ok": False, "error": "request too large"})
                return

        try:
            request = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._send(conn, {"ok": False, "error": f"malformed request: {e}"})
            return

        reply: queue.Queue = queue.Queue(maxsize=1)
        try:
            self._queue.put_nowait({"type": "WEB_COMMAND", "request": request, "reply": reply})
        except queue.Full:
            self._send(conn, {"ok": False, "error": "system busy"})
            return

        try:
            self._send(conn, reply.get(timeout=REPLY_TIMEOUT))
        except queue.Empty:
            self._send(conn, {"ok": False, "error": "timed out waiting for door service"})

    @staticmethod
    def _send(conn: socket.socket, payload: dict) -> None:
        try:
            conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        except OSError as e:
            logger.debug("Control socket reply failed: %s", e)


class ControlClient:
    """Client side, used by the web admin service."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH, timeout: float = REPLY_TIMEOUT):
        self._path = socket_path
        self._timeout = timeout

    def call(self, cmd: str, **kwargs) -> dict:
        """Send one command and return the door service's response.

        Never raises — a down or unreachable door service comes back as
        {"ok": False, "error": ...} so the UI can render it.
        """
        request = {"cmd": cmd, **kwargs}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect(self._path)
                sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            if not buf:
                return {"ok": False, "error": "no response from door service"}
            return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except FileNotFoundError:
            return {"ok": False, "error": "door service is not running"}
        except PermissionError:
            return {"ok": False, "error": f"no permission for {self._path} "
                                          f"(is this user in the {ADMIN_GROUP} group?)"}
        except socket.timeout:
            return {"ok": False, "error": "door service did not respond"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
