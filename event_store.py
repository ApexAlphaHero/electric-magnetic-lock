"""Persistent event history in SQLite.

Written exclusively by the main thread (the event-loop consumer), read by the
web admin service in a separate process. WAL journalling is enabled so a reader
never blocks the writer and vice versa — that is why the *directory* must be
group-writable, not just the database file: SQLite creates `-wal` and `-shm`
sidecar files alongside it and readers need to map the shm.
"""

import datetime
import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/var/lib/door_access/events.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT    NOT NULL,
    type     TEXT    NOT NULL,
    uid      TEXT,
    name     TEXT,
    granted  INTEGER,
    door     TEXT,
    actor    TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);
CREATE INDEX IF NOT EXISTS idx_events_uid  ON events (uid);
"""


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class EventStore:
    """Append-only event log.

    All writes are best-effort: a failure to record history must never stop the
    door from working, so every method swallows and logs its exceptions.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, retention_days: int = 90):
        self._path = db_path
        self._retention_days = retention_days
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def setup(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            # The web service reads this file as a different user. Its group is
            # inherited from the setgid parent directory (dooradmin, created by
            # install.sh); all that is needed here is the group bit.
            try:
                os.chmod(self._path, 0o660)
            except OSError:
                pass
            logger.info("Event store ready at %s (retention %dd)", self._path, self._retention_days)
            self.prune()
        except Exception as e:
            logger.error("Event store unavailable (%s) — history will not be recorded", e)
            self._conn = None

    def close(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def log(self, type: str, uid: str | None = None, name: str | None = None,
            granted: bool | None = None, door: str | None = None,
            actor: str | None = None, detail: str | None = None) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events (ts, type, uid, name, granted, door, actor, detail)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (utcnow(), type, uid, name,
                     None if granted is None else int(granted), door, actor, detail),
                )
                self._conn.commit()
            except Exception as e:
                logger.error("Failed to record event %s: %s", type, e)

    def prune(self) -> None:
        """Drop events past the retention window. Called at startup and daily."""
        if self._conn is None or not self._retention_days:
            return
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=self._retention_days)).isoformat(timespec="seconds")
        with self._lock:
            try:
                cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                self._conn.commit()
                if cur.rowcount > 0:
                    logger.info("Pruned %d event(s) older than %dd", cur.rowcount, self._retention_days)
            except Exception as e:
                logger.error("Event prune failed: %s", e)


class EventReader:
    """Read-only view of the event log, used by the web admin service.

    Read-only is enforced with `PRAGMA query_only` rather than SQLite's
    `mode=ro` URI. That looks like the weaker choice but is the working one: a
    WAL reader has to write the `-shm` sidecar to coordinate with the live
    writer, so a genuinely read-only handle fails as soon as the door service is
    actively logging — exactly when history matters most.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._path}?mode=rw", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def available(self) -> bool:
        try:
            with self._connect():
                return True
        except Exception:
            return False

    def recent(self, limit: int = 50, offset: int = 0, type: str = "",
               query: str = "") -> list[dict]:
        sql = "SELECT * FROM events"
        where, params = [], []
        if type:
            where.append("type = ?")
            params.append(type)
        if query:
            where.append("(uid LIKE ? OR name LIKE ? OR actor LIKE ? OR detail LIKE ?)")
            params += [f"%{query}%"] * 4
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(sql, params)]
        except Exception as e:
            logger.error("Event query failed: %s", e)
            return []

    def count(self, type: str = "", query: str = "") -> int:
        sql = "SELECT COUNT(*) FROM events"
        where, params = [], []
        if type:
            where.append("type = ?")
            params.append(type)
        if query:
            where.append("(uid LIKE ? OR name LIKE ? OR actor LIKE ? OR detail LIKE ?)")
            params += [f"%{query}%"] * 4
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            with self._connect() as conn:
                return conn.execute(sql, params).fetchone()[0]
        except Exception:
            return 0

    def types(self) -> list[str]:
        try:
            with self._connect() as conn:
                return [r[0] for r in conn.execute(
                    "SELECT DISTINCT type FROM events ORDER BY type")]
        except Exception:
            return []

    def unknown_uids(self, limit: int = 10) -> list[dict]:
        """Distinct UIDs that were denied, most recent first — the source list
        for the 'click a recent unknown scan to enroll it' flow."""
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(
                    "SELECT uid, MAX(ts) AS ts, COUNT(*) AS hits FROM events"
                    " WHERE type = 'ACCESS_DENIED' AND uid IS NOT NULL"
                    " GROUP BY uid ORDER BY MAX(id) DESC LIMIT ?", (limit,))]
        except Exception as e:
            logger.error("Unknown-UID query failed: %s", e)
            return []
