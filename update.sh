#!/usr/bin/env bash
# Door Access — updater.
#
# Run as root by door_update_check.service (mode "check") and
# door_update.service (mode "apply"). The web admin never executes this
# directly: it runs unprivileged as `doorweb` and may only ask systemd to start
# these two units, via a narrow sudoers rule. That keeps "trigger an update"
# and "run arbitrary code as root" as separate capabilities.
#
# Progress is written to a status JSON the web UI polls, because applying an
# update restarts door_admin — the browser's connection dies mid-flight, so
# state has to live on disk rather than in the request.

set -uo pipefail

MODE="${1:-check}"
REPO_DIR="${DOOR_REPO_DIR:-/opt/door_access/repo}"
BRANCH="${DOOR_REPO_BRANCH:-master}"
EXPECTED_REMOTE="${DOOR_REPO_URL:-https://github.com/ApexAlphaHero/electric-magnetic-lock.git}"
STATE_DIR="${DOOR_STATE_DIR:-/var/lib/door_access}"
STATUS="$STATE_DIR/update-status.json"
LOG="$STATE_DIR/update.log"
HEALTH_WAIT=8

# Everything below is logged; the UI shows the tail of this file.
exec >>"$LOG" 2>&1
echo ""
echo "===== $(date -Is) update.sh $MODE ====="

write_status() {
    # $1 state, $2 phase, $3 message. Git facts are recomputed here rather than
    # passed in, so the status file can never disagree with the actual checkout.
    python3 - "$STATUS" "$REPO_DIR" "$BRANCH" "$1" "$2" "$3" <<'PY'
import datetime, json, os, subprocess, sys

status_path, repo, branch, state, phase, message = sys.argv[1:7]


def git(*args):
    try:
        r = subprocess.run(["git", "-C", repo, *args],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def commits(rev_range, limit=25):
    out = git("log", f"--max-count={limit}", "--pretty=%h%x1f%s%x1f%cI", rev_range)
    items = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            items.append({"sha": parts[0], "subject": parts[1], "date": parts[2]})
    return items


previous = {}
try:
    with open(status_path) as f:
        previous = json.load(f)
except Exception:
    pass

now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
current = commits("HEAD", 1)
pending = commits(f"HEAD..origin/{branch}")

payload = {
    "state": state,
    "phase": phase,
    "message": message,
    "branch": branch,
    "current": current[0] if current else None,
    "pending": pending,
    "behind": len(pending),
    "updated_at": now,
    "started_at": previous.get("started_at") if state == "running" else now,
    "last_checked": now if phase in ("check", "fetch") else previous.get("last_checked"),
}
if state == "running" and not previous.get("state") == "running":
    payload["started_at"] = now

tmp = status_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
os.replace(tmp, status_path)
# Readable by the web service via the dooradmin group (setgid parent dir).
try:
    os.chmod(status_path, 0o640)
except OSError:
    pass
PY
}

fail() {
    echo "FAILED: $*"
    write_status error "${PHASE:-unknown}" "$*"
    exit 1
}

# ── sanity ──────────────────────────────────────────────────────────────────────

PHASE=verify
[[ -d "$REPO_DIR/.git" ]] || fail "no git checkout at $REPO_DIR — re-run install.sh to create it"

# Refuse to pull from anywhere but the expected origin. Without this, anyone who
# could rewrite the remote URL would turn the update button into "run their code
# as root".
ACTUAL_REMOTE="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null)"
if [[ "$ACTUAL_REMOTE" != "$EXPECTED_REMOTE" ]]; then
    fail "origin is '$ACTUAL_REMOTE', expected '$EXPECTED_REMOTE' — refusing to update"
fi

# ── fetch ───────────────────────────────────────────────────────────────────────

PHASE=fetch
write_status running fetch "Fetching from origin…"
git -C "$REPO_DIR" fetch --prune origin "$BRANCH" || fail "git fetch failed (no network?)"

BEFORE="$(git -C "$REPO_DIR" rev-parse HEAD)"
TARGET="$(git -C "$REPO_DIR" rev-parse "origin/$BRANCH")"

if [[ "$MODE" == "check" ]]; then
    if [[ "$BEFORE" == "$TARGET" ]]; then
        write_status idle check "Up to date."
    else
        write_status idle check "An update is available."
    fi
    echo "check complete: BEFORE=$BEFORE TARGET=$TARGET"
    exit 0
fi

if [[ "$BEFORE" == "$TARGET" ]]; then
    write_status ok apply "Already up to date — nothing to do."
    exit 0
fi

# ── apply ───────────────────────────────────────────────────────────────────────

PHASE=merge
write_status running merge "Updating working copy…"
# --ff-only: never create a merge commit or take a surprising history. If local
# files were hand-edited this fails loudly instead of silently discarding them.
git -C "$REPO_DIR" merge --ff-only "origin/$BRANCH" \
    || fail "cannot fast-forward — the checkout has local changes; fix it on the Pi"

AFTER="$(git -C "$REPO_DIR" rev-parse HEAD)"
echo "merged $BEFORE -> $AFTER"

reinstall() {
    # DOOR_WEB_ADMINS is exported (even empty) so install.sh skips its
    # interactive prompt; there is no terminal here.
    DOOR_SRC_DIR="$REPO_DIR" DOOR_WEB_ADMINS="" \
        bash "$REPO_DIR/install.sh"
}

PHASE=install
write_status running install "Installing files…"
if ! reinstall; then
    echo "install failed, rolling back to $BEFORE"
    PHASE=rollback
    write_status running rollback "Install failed — rolling back…"
    git -C "$REPO_DIR" reset --hard "$BEFORE"
    reinstall
    systemctl restart door_access
    write_status rolled_back rollback "Install failed; rolled back to ${BEFORE:0:7}."
    exit 1
fi

PHASE=restart
write_status running restart "Restarting the door service…"
systemctl restart door_access
sleep "$HEALTH_WAIT"

# The door is the thing that must not stay broken. If it did not come back,
# undo the update rather than leaving the lock unmanaged.
if ! systemctl is-active --quiet door_access; then
    echo "door_access did not come back, rolling back to $BEFORE"
    PHASE=rollback
    write_status running rollback "Door service failed to start — rolling back…"
    git -C "$REPO_DIR" reset --hard "$BEFORE"
    reinstall
    systemctl restart door_access
    sleep "$HEALTH_WAIT"
    if systemctl is-active --quiet door_access; then
        write_status rolled_back rollback "Update failed; rolled back to ${BEFORE:0:7} and the door service is running."
    else
        write_status error rollback "Update failed AND rollback failed — the door service is down. Check journalctl -u door_access."
    fi
    exit 1
fi

write_status ok restart "Updated to ${AFTER:0:7}. Restarting the web admin…"
echo "update complete: $AFTER"

# Restarted last and detached: this kills our caller's HTTP connection, so the
# browser polls back to a fresh process and reads the final status from disk.
systemd-run --unit=door-admin-restart-$$ --no-block \
    /usr/bin/systemctl restart door_admin >/dev/null 2>&1 \
    || systemctl restart door_admin
exit 0
