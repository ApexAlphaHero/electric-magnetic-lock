#!/usr/bin/env python3
"""Door Access web admin.

A small Flask app, run as its own unprivileged user (`doorweb`) under its own
systemd unit, separate from the door service. It holds no GPIO and no write
access to the door's config: every change is a request over the control socket,
executed by the door service's event loop.

Authentication is PAM against local Pi accounts, then a hard requirement of
membership in the `dooradmin` group — being a valid Pi user is not enough. This
is the per-user access control Home Assistant could not provide, and the reason
unlock lives here rather than in HA.

Served over TLS by gunicorn; see door_admin.service.
"""

import functools
import grp
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time

from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)

from control_socket import DEFAULT_SOCKET_PATH, ControlClient, normalize_uid
from event_store import DEFAULT_DB_PATH, EventReader

logger = logging.getLogger(__name__)

WEB_CONFIG_PATH = "/etc/door_access/web.json"
SECRET_PATH = "/etc/door_access/web_secret"

# The listen address is NOT here — it is gunicorn's --bind, set from
# /etc/door_access/web.env, so there is exactly one source of truth for it.
DEFAULTS = {
    "admin_group": "dooradmin",
    "session_minutes": 60,
    "allow_unlock": True,
    "control_socket": DEFAULT_SOCKET_PATH,
    "event_db": DEFAULT_DB_PATH,
    "tls_ca": "/etc/door_access/tls/ca.pem",
    "allow_update": True,
    "update_status": "/var/lib/door_access/update-status.json",
    "update_log": "/var/lib/door_access/update.log",
    "page_size": 50,
    "max_login_attempts": 5,
    "lockout_minutes": 15,
    # Which units the System log page may read. A whitelist, not a filter:
    # nothing from the query string ever reaches journalctl's argv, it is
    # only matched against this list.
    "allow_logs": True,
    "log_units": [
        "door_access.service",
        "door_admin.service",
        "door_update.service",
        "door_update_check.service",
        "pcscd.service",
    ],
    "log_lines": 300,
}

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$")


# ── configuration ───────────────────────────────────────────────────────────────

def load_web_config(path: str = WEB_CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        logger.warning("%s not found — using defaults", path)
    except ValueError as e:
        logger.error("%s is not valid JSON (%s) — using defaults", path, e)
    return cfg


def load_secret(path: str = SECRET_PATH) -> bytes:
    """Read the session-signing key, creating it if absent.

    It must persist across restarts or every restart logs everyone out, and it
    must never be a hardcoded default or sessions would be forgeable by anyone
    who has read the source.
    """
    try:
        with open(path, "rb") as f:
            data = f.read().strip()
            if len(data) >= 32:
                return data
            logger.warning("%s is too short — regenerating", path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.error("Cannot read %s (%s) — using an ephemeral key; sessions will "
                     "not survive a restart", path, e)
        return secrets.token_bytes(32)

    key = secrets.token_hex(32).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        logger.info("Generated new session secret at %s", path)
    except OSError as e:
        logger.error("Cannot write %s (%s) — using an ephemeral key", path, e)
    return key


# ── authentication ──────────────────────────────────────────────────────────────

class PAMUnavailable(RuntimeError):
    pass


def pam_authenticate(username: str, password: str) -> bool:
    """Verify a local account password via PAM.

    Reading /etc/shadow requires privilege, so the service user must be in the
    `shadow` group — pam_unix then reads it directly. (The setuid helper
    /usr/sbin/unix_chkpwd is not an option: it only lets a non-root caller check
    its *own* password.)
    """
    try:
        import pam as pam_module
    except ImportError as e:
        # Report the underlying error: this fires for a missing transitive
        # dependency (python-pam 2.0.2 imports 'six' without declaring it) just
        # as often as for a genuinely missing package, and the two need
        # different fixes.
        raise PAMUnavailable(
            f"cannot import the PAM binding from the service venv: {e}"
        ) from e

    authenticator = pam_module.pam()
    return bool(authenticator.authenticate(username, password, service="login"))


def in_admin_group(username: str, group: str) -> bool:
    """Membership check covering both secondary members and users whose *primary*
    group is the admin group (those do not appear in gr_mem)."""
    try:
        entry = grp.getgrnam(group)
    except KeyError:
        logger.error("Admin group %r does not exist — refusing all logins", group)
        return False
    if username in entry.gr_mem:
        return True
    try:
        import pwd
        return pwd.getpwnam(username).pw_gid == entry.gr_gid
    except KeyError:
        return False


class LoginThrottle:
    """In-memory lockout after repeated failures.

    Deliberately keyed on the client IP alone so that guessing *different*
    usernames from one host still trips the same counter. State is per-process,
    which is why the unit runs a single gunicorn worker.
    """

    def __init__(self, max_attempts: int, lockout_seconds: float):
        self._max = max_attempts
        self._lockout = lockout_seconds
        self._failures: dict[str, list] = {}
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> float:
        with self._lock:
            count, last = self._failures.get(key, (0, 0.0))
            if count < self._max:
                return 0.0
            remaining = self._lockout - (time.monotonic() - last)
            if remaining <= 0:
                self._failures.pop(key, None)
                return 0.0
            return remaining

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, _ = self._failures.get(key, (0, 0.0))
            self._failures[key] = (count + 1, time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# ── system log ──────────────────────────────────────────────────────────────────

SYSLOG_LEVELS = {
    0: "emerg", 1: "alert", 2: "crit", 3: "err",
    4: "warning", 5: "notice", 6: "info", 7: "debug",
}


def _journal_message(raw) -> str:
    """Render MESSAGE, which journald hands back as a list of byte values when
    the line is not valid UTF-8 — a device name with a stray byte, say.
    Decoding those rather than skipping them matters: the malformed output is
    usually the part worth reading.
    """
    if isinstance(raw, list):
        return bytes(b & 0xFF for b in raw).decode("utf-8", "replace")
    return "" if raw is None else str(raw)


def read_journal(units: list[str], lines: int = 300,
                 priority: int | None = None) -> tuple[list[dict], str | None]:
    """Recent journald entries for `units`, newest first.

    Returns (entries, error) instead of raising — a log page that cannot reach
    the journal should still render and explain why. `units` becomes argv, so
    callers must pass values drawn from the configured whitelist.
    """
    if not units:
        return [], None
    cmd = ["journalctl", "--output=json", "--no-pager", f"--lines={int(lines)}"]
    for unit in units:
        cmd += ["-u", unit]
    if priority is not None:
        cmd.append(f"--priority={int(priority)}")

    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return [], "journalctl is not installed on this system."
    except subprocess.TimeoutExpired:
        return [], "journalctl did not respond within 15s."
    except OSError as e:
        return [], str(e)

    if done.returncode != 0:
        detail = (done.stderr or "").strip()[:300]
        if "permission" in detail.lower() or "not permitted" in detail.lower():
            return [], ("Not allowed to read the journal. The doorweb account must be "
                        "in the systemd-journal group — re-run install.sh, then "
                        "'sudo systemctl restart door_admin'.")
        return [], detail or "journalctl failed."

    entries = []
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        try:
            stamp = int(record.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
        except (TypeError, ValueError):
            stamp = 0
        try:
            priority_value = int(record.get("PRIORITY", 6))
        except (TypeError, ValueError):
            priority_value = 6
        entries.append({
            "time": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
                     if stamp else ""),
            "unit": record.get("_SYSTEMD_UNIT") or record.get("SYSLOG_IDENTIFIER") or "",
            "priority": priority_value,
            "level": SYSLOG_LEVELS.get(priority_value, str(priority_value)),
            "message": _journal_message(record.get("MESSAGE")),
        })

    # journalctl --lines returns oldest-first; History is newest-first and these
    # two pages should not disagree about which way time runs.
    entries.reverse()
    return entries, None


# ── app ─────────────────────────────────────────────────────────────────────────

def create_app(cfg: dict | None = None) -> Flask:
    cfg = cfg or load_web_config()
    app = Flask(__name__)
    app.secret_key = load_secret()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # The service is TLS-only; without this the cookie could be replayed if
        # anything ever terminated plain HTTP in front of it.
        SESSION_COOKIE_SECURE=True,
        PERMANENT_SESSION_LIFETIME=int(cfg["session_minutes"]) * 60,
        MAX_CONTENT_LENGTH=64 * 1024,
    )

    door = ControlClient(cfg["control_socket"])
    history = EventReader(cfg["event_db"])
    throttle = LoginThrottle(int(cfg["max_login_attempts"]),
                             float(cfg["lockout_minutes"]) * 60)

    # ── helpers ────────────────────────────────────────────────────────────────

    def current_user() -> str | None:
        return session.get("user")

    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user():
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def require_csrf() -> None:
        sent = request.form.get("csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "CSRF token missing or invalid")

    def audit(event: str, detail: str = "") -> None:
        door.call("login_event", event=event, actor=current_user() or "anonymous",
                  detail=detail)

    @app.context_processor
    def inject_globals():
        return {
            "csrf": csrf_token(),
            "user": current_user(),
            "allow_unlock": bool(cfg["allow_unlock"]),
            "allow_update": bool(cfg["allow_update"]),
            "allow_logs": bool(cfg["allow_logs"]),
        }

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        # No external assets anywhere in this app, so the policy can be strict.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    # ── auth routes ────────────────────────────────────────────────────────────

    @app.route("/ca.crt")
    def ca_certificate():
        """Serve the local CA certificate so a device can be taught to trust this site.

        Deliberately unauthenticated. You need this certificate *before* the
        connection is trustworthy, so requiring a login first would mean sending
        a password over a connection whose certificate you have not yet verified
        — the exact thing installing the CA is meant to fix.

        Publishing it grants nobody anything: a CA certificate is a public key.
        The private key that could actually sign certificates (ca-key.pem) is
        0640 root:dooradmin and is never served.

        Sent as .crt with the x509 MIME type because Android's certificate
        installer filters the file picker by extension and skips .pem.
        """
        try:
            with open(cfg["tls_ca"], "rb") as f:
                pem = f.read()
        except OSError as e:
            logger.error("Cannot read CA certificate %s: %s", cfg["tls_ca"], e)
            abort(404)
        return Response(
            pem,
            mimetype="application/x-x509-ca-cert",
            headers={"Content-Disposition": 'attachment; filename="door-access-ca.crt"'},
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("login.html")

        require_csrf()
        client = request.remote_addr or "unknown"
        wait = throttle.locked_for(client)
        if wait > 0:
            flash(f"Too many failed attempts. Try again in {int(wait / 60) + 1} minute(s).",
                  "error")
            return render_template("login.html"), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # Reject shapes that are not plausible local usernames before they ever
        # reach PAM.
        if not username or not USERNAME_RE.match(username) or len(username) > 32:
            throttle.record_failure(client)
            flash("Invalid username or password.", "error")
            return render_template("login.html"), 401

        try:
            ok = pam_authenticate(username, password)
        except PAMUnavailable as e:
            logger.error("PAM unavailable: %s", e)
            flash("Authentication is not configured on this server.", "error")
            return render_template("login.html"), 500

        if not ok:
            throttle.record_failure(client)
            logger.warning("Failed login for %r from %s", username, client)
            door.call("login_event", event="WEB_LOGIN_FAILED", actor=username,
                      detail=f"from {client}")
            flash("Invalid username or password.", "error")
            return render_template("login.html"), 401

        if not in_admin_group(username, cfg["admin_group"]):
            throttle.record_failure(client)
            logger.warning("User %r authenticated but is not in %s", username, cfg["admin_group"])
            door.call("login_event", event="WEB_LOGIN_DENIED", actor=username,
                      detail=f"not in {cfg['admin_group']}, from {client}")
            # Same message as a bad password: do not confirm which accounts exist.
            flash("Invalid username or password.", "error")
            return render_template("login.html"), 403

        throttle.reset(client)
        session.clear()          # new session id — no fixation carried over
        session["user"] = username
        session.permanent = True
        csrf_token()
        logger.info("Login: %s from %s", username, client)
        door.call("login_event", event="WEB_LOGIN", actor=username, detail=f"from {client}")

        target = request.form.get("next") or request.args.get("next") or ""
        # Only ever redirect within this app.
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("dashboard")
        return redirect(target)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        require_csrf()
        audit("WEB_LOGOUT")
        session.clear()
        return redirect(url_for("login"))

    # ── pages ──────────────────────────────────────────────────────────────────

    @app.route("/")
    @login_required
    def dashboard():
        status = door.call("status")
        return render_template(
            "dashboard.html",
            status=status.get("result") if status.get("ok") else None,
            error=None if status.get("ok") else status.get("error"),
            events=history.recent(limit=15),
            history_ok=history.available(),
        )

    @app.route("/unlock", methods=["POST"])
    @login_required
    def unlock():
        require_csrf()
        if not cfg["allow_unlock"]:
            abort(403)
        result = door.call("unlock", actor=current_user())
        flash("Door unlocked." if result.get("ok")
              else f"Unlock failed: {result.get('error')}",
              "success" if result.get("ok") else "error")
        return redirect(url_for("dashboard"))

    @app.route("/lock", methods=["POST"])
    @login_required
    def lock():
        require_csrf()
        result = door.call("lock", actor=current_user())
        flash("Door locked." if result.get("ok")
              else f"Lock failed: {result.get('error')}",
              "success" if result.get("ok") else "error")
        return redirect(url_for("dashboard"))

    @app.route("/unlock_duration", methods=["POST"])
    @login_required
    def set_unlock_duration():
        require_csrf()
        raw = request.form.get("seconds", "").strip()
        try:
            seconds = float(raw)
        except ValueError:
            flash(f"{raw!r} is not a number.", "error")
            return redirect(url_for("dashboard"))
        # The door service re-validates the range; this is only so the operator
        # gets a useful message instead of a generic failure.
        if not 1 <= seconds <= 60:
            flash("Unlock duration must be between 1 and 60 seconds.", "error")
            return redirect(url_for("dashboard"))
        result = door.call("set_unlock_duration", seconds=seconds, actor=current_user())
        flash(f"Unlock duration set to {seconds:.0f} seconds." if result.get("ok")
              else f"Could not set duration: {result.get('error')}",
              "success" if result.get("ok") else "error")
        return redirect(url_for("dashboard"))

    @app.route("/tags")
    @login_required
    def tags():
        listing = door.call("list_tags")
        return render_template(
            "tags.html",
            tags=listing.get("result") if listing.get("ok") else [],
            error=None if listing.get("ok") else listing.get("error"),
            unknown=history.unknown_uids(limit=10),
            prefill_uid=normalize_uid(request.args.get("uid", "")),
        )

    @app.route("/tags/add", methods=["POST"])
    @login_required
    def add_tag():
        require_csrf()
        uid = request.form.get("uid", "")
        name = request.form.get("name", "")
        if not normalize_uid(uid):
            flash(f"{uid!r} is not a valid tag UID — expected hex like AABB1122.", "error")
            return redirect(url_for("tags"))
        result = door.call("add_tag", uid=uid, name=name, actor=current_user())
        if result.get("ok"):
            added = result["result"]
            flash(f"Added {added['name']} ({added['uid']}).", "success")
        else:
            flash(f"Could not add tag: {result.get('error')}", "error")
        return redirect(url_for("tags"))

    # ── enrollment via the door reader ─────────────────────────────────────────
    # These two are fetch/JSON rather than form-and-redirect: the operator walks
    # to the door after arming, so the page has to report the outcome without a
    # navigation they aren't there to perform.

    @app.route("/tags/arm", methods=["POST"])
    @login_required
    def arm_enroll():
        require_csrf()
        result = door.call("arm_enroll", name=request.form.get("name", ""),
                           actor=current_user())
        return jsonify(result), 200 if result.get("ok") else 502

    @app.route("/tags/enroll_status")
    @login_required
    def enroll_status():
        return jsonify(door.call("enroll_status", actor=current_user()))

    @app.route("/tags/cancel_arm", methods=["POST"])
    @login_required
    def cancel_enroll():
        require_csrf()
        return jsonify(door.call("cancel_enroll", actor=current_user()))

    @app.route("/tags/remove", methods=["POST"])
    @login_required
    def remove_tag():
        require_csrf()
        result = door.call("remove_tag", uid=request.form.get("uid", ""),
                           actor=current_user())
        if result.get("ok"):
            flash(f"Removed {result['result']['name']}.", "success")
        else:
            flash(f"Could not remove tag: {result.get('error')}", "error")
        return redirect(url_for("tags"))

    @app.route("/tags/rename", methods=["POST"])
    @login_required
    def rename_tag():
        require_csrf()
        result = door.call("rename_tag", uid=request.form.get("uid", ""),
                           name=request.form.get("name", ""), actor=current_user())
        if result.get("ok"):
            flash(f"Renamed to {result['result']['name']}.", "success")
        else:
            flash(f"Could not rename tag: {result.get('error')}", "error")
        return redirect(url_for("tags"))

    @app.route("/history")
    @login_required
    def history_page():
        page_size = int(cfg["page_size"])
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        etype = request.args.get("type", "")
        query = request.args.get("q", "").strip()[:64]
        total = history.count(type=etype, query=query)
        return render_template(
            "history.html",
            events=history.recent(limit=page_size, offset=(page - 1) * page_size,
                                  type=etype, query=query),
            page=page,
            pages=max(1, (total + page_size - 1) // page_size),
            total=total,
            etype=etype,
            query=query,
            all_types=history.types(),
            history_ok=history.available(),
        )

    # ── updates ────────────────────────────────────────────────────────────────
    # The web app has no privilege to install anything. It may only ask systemd
    # to start one of two units (see /etc/sudoers.d/door_update); everything
    # else here is reading files those units wrote.

    def update_enabled() -> bool:
        return bool(cfg["allow_update"])

    def read_update_status() -> dict:
        try:
            with open(cfg["update_status"]) as f:
                return json.load(f)
        except FileNotFoundError:
            return {"state": "unknown", "message": "No update has been run yet.",
                    "behind": 0, "pending": []}
        except Exception as e:
            return {"state": "error", "message": f"Cannot read update status: {e}",
                    "behind": 0, "pending": []}

    def start_unit(unit: str) -> dict:
        """Ask systemd to start one of the updater units.

        No sudo: this service runs with NoNewPrivileges=yes, which blocks setuid
        binaries outright. systemctl reaches PID 1 over D-Bus and is authorized
        by /etc/polkit-1/rules.d/50-door-update.rules, which permits starting
        exactly these two units and nothing else.

        --no-block because both are Type=oneshot: without it systemctl waits for
        the whole update to finish and the request would time out.
        """
        try:
            done = subprocess.run(
                ["systemctl", "--no-block", "start", unit],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"{unit} did not start within 30s"}
        except FileNotFoundError:
            return {"ok": False, "error": "systemctl not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()[:300]
            logger.error("Could not start %s: %s", unit, detail)
            return {"ok": False, "error": detail or f"systemctl start {unit} failed"}
        return {"ok": True}

    @app.route("/updates")
    @login_required
    def updates():
        if not update_enabled():
            abort(403)
        log_tail = ""
        try:
            with open(cfg["update_log"]) as f:
                # Tail without holding the whole file: these logs accumulate
                # across every update ever run.
                log_tail = "".join(f.readlines()[-120:])
        except OSError:
            pass
        return render_template("updates.html", status=read_update_status(), log=log_tail)

    @app.route("/updates/status")
    @login_required
    def updates_status():
        if not update_enabled():
            abort(403)
        return jsonify(read_update_status())

    @app.route("/updates/check", methods=["POST"])
    @login_required
    def updates_check():
        require_csrf()
        if not update_enabled():
            abort(403)
        audit("UPDATE_CHECK")
        return jsonify(start_unit("door_update_check.service"))

    @app.route("/updates/apply", methods=["POST"])
    @login_required
    def updates_apply():
        require_csrf()
        if not update_enabled():
            abort(403)
        status = read_update_status()
        target = (status.get("pending") or [{}])[0].get("sha", "?")
        # Audited before starting: door_admin is restarted by the update, so
        # recording it afterwards is not guaranteed to happen.
        audit("UPDATE_APPLY", f"to {target}, from {status.get('current', {}).get('sha', '?')}")
        logger.warning("Update applied by %s", current_user())
        return jsonify(start_unit("door_update.service"))

    # ── system log ─────────────────────────────────────────────────────────────
    # A read-only window onto journald for this application's own units: the
    # things the History page structurally cannot show, because they happen
    # underneath it — service starts and crashes, PC/SC and reader errors,
    # MQTT reconnects, and what the updater actually did.

    def logs_enabled() -> bool:
        return bool(cfg["allow_logs"])

    def _log_query() -> dict:
        units = [str(u) for u in cfg["log_units"]]
        # Whitelist membership, not sanitising: an unrecognised unit falls back
        # to all of them rather than being passed through to journalctl.
        selected = request.args.get("unit", "")
        wanted = [selected] if selected in units else units

        try:
            priority = int(request.args.get("priority", ""))
        except ValueError:
            priority = None
        else:
            if not 0 <= priority <= 7:
                priority = None

        query = request.args.get("q", "").strip()[:64]
        entries, error = read_journal(wanted, int(cfg["log_lines"]), priority)
        if query:
            needle = query.lower()
            entries = [e for e in entries
                       if needle in e["message"].lower() or needle in e["unit"].lower()]

        return {"units": units, "unit": selected, "priority": priority,
                "query": query, "entries": entries, "error": error}

    @app.route("/logs")
    @login_required
    def logs_page():
        if not logs_enabled():
            abort(403)
        return render_template(
            "logs.html",
            # journald's Storage=auto keeps the journal in /run unless this
            # directory exists, in which case every entry here dies on reboot.
            persistent=os.path.isdir("/var/log/journal"),
            **_log_query(),
        )

    @app.route("/logs/data")
    @login_required
    def logs_data():
        if not logs_enabled():
            abort(403)
        result = _log_query()
        return jsonify({"entries": result["entries"], "error": result["error"]})

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("error.html", code=400, message=str(e)), 400

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, message="Not permitted."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, message="No such page."), 404

    return app


app = create_app()


if __name__ == "__main__":
    # Development only — production runs under gunicorn with TLS (see
    # door_admin.service). Flask's dev server is not a production server, so
    # this binds loopback only.
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=8443, debug=False)
