#!/usr/bin/env bash
# Door Access Control — installer
# Run as root:  sudo bash install.sh
# Or one-liner: curl -fsSL https://raw.githubusercontent.com/ApexAlphaHero/electric-magnetic-lock/master/install.sh | sudo bash
#
# Target: Raspberry Pi OS / Debian 13 (trixie) or newer, Python >= 3.10.
# On Debian 12+ PEP 668 blocks `pip install` into the system interpreter, so all
# Python dependencies are installed from apt instead.

set -euo pipefail

REPO="https://raw.githubusercontent.com/ApexAlphaHero/electric-magnetic-lock/master"
APP_DIR="/opt/door_access"
CFG_DIR="/etc/door_access"
LOG_DIR="/var/log/door_access"
LOG_FILE="$LOG_DIR/door_access.log"
DATA_DIR="/var/lib/door_access"
TLS_DIR="$CFG_DIR/tls"
VENV_DIR="$APP_DIR/venv"
REPO_DIR="$APP_DIR/repo"
REPO_URL="https://github.com/ApexAlphaHero/electric-magnetic-lock.git"
REPO_BRANCH="master"
SERVICE_FILE="/etc/systemd/system/door_access.service"
WEB_SERVICE_FILE="/etc/systemd/system/door_admin.service"
UPDATE_SERVICE_FILE="/etc/systemd/system/door_update.service"
UPDATE_CHECK_SERVICE_FILE="/etc/systemd/system/door_update_check.service"
SUDOERS_FILE="/etc/sudoers.d/door_update"
POLKIT_RULE="/etc/polkit-1/rules.d/50-door-pcsc.rules"
CCID_PLIST="/etc/libccid_Info.plist"
ADMIN_GROUP="dooradmin"
WEB_USER="doorweb"
WEB_PORT="${DOOR_WEB_PORT:-8443}"

# ── helpers ────────────────────────────────────────────────────────────────────

info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*"; }
error() { echo "[ERROR] $*" >&2; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)."
        exit 1
    fi
}

# Set DOOR_SRC_DIR to install from a local checkout instead of GitHub — used for
# deploying unreleased changes, and for installing on a Pi with no internet.
SRC_DIR="${DOOR_SRC_DIR:-}"

download() {
    local dest="$1" url="$2"
    if [[ -n "$SRC_DIR" ]]; then
        local rel="${url#"$REPO"/}"
        if [[ ! -f "$SRC_DIR/$rel" ]]; then
            error "$SRC_DIR/$rel not found"
            exit 1
        fi
        info "Installing $rel from $SRC_DIR ..."
        install -D -m 644 "$SRC_DIR/$rel" "$dest"
        return
    fi
    info "Downloading $(basename "$dest") ..."
    curl -fsSL "$url" -o "$dest"
}

# ── 1. System + Python packages (all via apt; see PEP 668 note above) ────────────

install_packages() {
    info "Updating package lists ..."
    apt-get update -qq

    info "Installing dependencies ..."
    # python3-rpi-lgpio is the lgpio-backed drop-in for RPi.GPIO. The classic
    # python3-rpi.gpio package does not work on the 6.x kernels shipped with
    # current Raspberry Pi OS; do NOT install both (they conflict).
    apt-get install -y \
        pcscd \
        pcsc-tools \
        libpcsclite-dev \
        python3-pyscard \
        python3-paho-mqtt \
        python3-rpi-lgpio \
        python3-venv \
        libpam0g-dev \
        openssl \
        git

    info "Enabling pcscd ..."
    systemctl enable pcscd
    systemctl start pcscd
}

# ── 1b. Web admin virtualenv ─────────────────────────────────────────────────────

create_venv() {
    # The web admin's dependencies are installed into a venv rather than via apt.
    # Flask and gunicorn are packaged, but the PAM binding is not reliably
    # available under a stable name across Debian releases, and PEP 668 blocks
    # pip into the system interpreter. A self-contained venv sidesteps both.
    # The door service itself keeps using system python3 with apt packages only.
    info "Creating web admin virtualenv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    # 'six' is an undeclared dependency of python-pam 2.0.2 — its __internals
    # imports it, but the wheel does not require it, so the module fails to
    # import unless it is installed explicitly.
    "$VENV_DIR/bin/pip" install --quiet Flask gunicorn python-pam six

    # Fail loudly here rather than at the first login attempt.
    if ! "$VENV_DIR/bin/python3" -c "import flask, pam" 2>/dev/null; then
        error "Web admin dependencies failed to import — check the pip output above"
        exit 1
    fi

    # Readable and executable by the doorweb service user, writable by nobody.
    chown -R root:root "$VENV_DIR"
    chmod -R go-w "$VENV_DIR"
}

# ── 2. Enable CCID escape commands (reader LED / buzzer feedback) ────────────────

enable_reader_escape() {
    # The ACR1552 LED and buzzer are driven via CCID "escape" commands, which the
    # libccid driver rejects unless DRIVER_OPTION_CCID_EXCHANGE_AUTHORIZED (0x0001)
    # is set in ifdDriverOptions. Safe to leave on for a dedicated appliance.
    if [[ ! -f "$CCID_PLIST" ]]; then
        warn "libccid plist not found at $CCID_PLIST — skipping escape enable (LED/buzzer may not work)"
        return
    fi
    if grep -A1 ifdDriverOptions "$CCID_PLIST" | grep -q '0x0001'; then
        info "CCID escape commands already enabled"
    else
        info "Enabling CCID escape commands (reader LED/buzzer) ..."
        cp -n "$CCID_PLIST" "${CCID_PLIST}.bak"
        # Replace the value on the line following the ifdDriverOptions key.
        sed -i '/ifdDriverOptions/{n;s/0x0000/0x0001/}' "$CCID_PLIST"
        systemctl restart pcscd
    fi
}

# ── 3. System user ───────────────────────────────────────────────────────────────

create_user() {
    if id -u door &>/dev/null; then
        info "User 'door' already exists, skipping creation"
    else
        info "Creating system user 'door' ..."
        useradd --system --no-create-home --shell /usr/sbin/nologin door
    fi

    info "Adding 'door' to hardware groups ..."
    usermod -aG gpio    door
    usermod -aG plugdev door
    usermod -aG spi     door

    # The admin group is the web UI's access control: authenticating as a valid
    # Pi user is not enough, you must also be a member of this group.
    if getent group "$ADMIN_GROUP" &>/dev/null; then
        info "Group '$ADMIN_GROUP' already exists"
    else
        info "Creating admin group '$ADMIN_GROUP' ..."
        groupadd --system "$ADMIN_GROUP"
    fi

    # 'door' must be a member so it can hand the control socket to the group.
    usermod -aG "$ADMIN_GROUP" door

    if id -u "$WEB_USER" &>/dev/null; then
        info "User '$WEB_USER' already exists, skipping creation"
    else
        info "Creating system user '$WEB_USER' ..."
        useradd --system --no-create-home --shell /usr/sbin/nologin "$WEB_USER"
    fi
    # 'shadow' lets pam_unix verify passwords; the admin group lets it reach the
    # control socket and the event database.
    usermod -aG shadow       "$WEB_USER"
    usermod -aG "$ADMIN_GROUP" "$WEB_USER"
}

grant_web_admins() {
    local admins
    # Testing for *set* rather than non-empty lets the updater pass
    # DOOR_WEB_ADMINS="" to skip the prompt entirely. There is no terminal
    # during an unattended update, and `read` at EOF would abort under `set -e`.
    if [[ -n "${DOOR_WEB_ADMINS+x}" ]]; then
        admins="$DOOR_WEB_ADMINS"
    else
        echo ""
        echo "─── Web Admin Access ─────────────────────────────────────────"
        echo "  Only members of the '$ADMIN_GROUP' group can sign in to the"
        echo "  web UI, even with a valid Pi password."
        echo ""
        read -rp "  Pi usernames to grant access (space separated, blank to skip): " admins || admins=""
    fi

    for u in $admins; do
        if id -u "$u" &>/dev/null; then
            usermod -aG "$ADMIN_GROUP" "$u"
            info "Granted web admin access to '$u'"
        else
            warn "No such user '$u' — skipped"
        fi
    done
    echo "──────────────────────────────────────────────────────────────"
}

# ── 4. polkit rule so the session-less 'door' user can reach pcscd ───────────────

install_polkit_rule() {
    # pcsc-lite's default polkit policy only allows users with an active local
    # session. The 'door' systemd service has no session, so without this rule it
    # gets "SCardEstablishContext: Access denied".
    info "Installing polkit rule for PC/SC access ..."
    install -d -m 755 "$(dirname "$POLKIT_RULE")"
    cat > "$POLKIT_RULE" <<'EOF'
// Allow the 'door' service user (which has no active login session) to
// access the PC/SC daemon and smartcards.
polkit.addRule(function(action, subject) {
    if ((action.id == "org.debian.pcsc-lite.access_pcsc" ||
         action.id == "org.debian.pcsc-lite.access_card") &&
        subject.user == "door") {
        return polkit.Result.YES;
    }
});
EOF
    chmod 644 "$POLKIT_RULE"
    systemctl restart polkit || true
}

# ── 5. Directories ───────────────────────────────────────────────────────────────

create_dirs() {
    info "Creating application directories ..."
    install -d -m 755 "$APP_DIR"
    install -d -m 755 "$APP_DIR/templates"
    install -d -m 755 "$APP_DIR/static"

    # Config dir is group-traversable by the admin group so the web service can
    # read web.json and manage its session secret. config.json itself stays
    # door-only (it holds the MQTT password).
    install -d -m 750 -o door -g "$ADMIN_GROUP" "$CFG_DIR"
    install -d -m 750 -o door -g "$ADMIN_GROUP" "$TLS_DIR"

    # setgid (2770) so the events database the door service creates inside
    # inherits the admin group automatically and stays readable by the web UI.
    install -d -m 2770 -o door -g "$ADMIN_GROUP" "$DATA_DIR"
}

# ── 5b. TLS certificate for the web admin ────────────────────────────────────────

create_tls_cert() {
    if [[ -f "$TLS_DIR/cert.pem" && -f "$TLS_DIR/key.pem" ]]; then
        info "TLS certificate already present — keeping it"
        return
    fi

    local ip host
    host="$(hostname)"
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [[ -n "$ip" ]] || ip="127.0.0.1"

    info "Generating a local CA and TLS certificate for $host / $ip ..."
    # A separate CA plus a leaf signed by it, rather than one self-signed cert.
    # Android will only install a certificate as trusted if it has CA:TRUE, and
    # a self-signed leaf that also claims to be a CA is honoured inconsistently.
    # With a real CA you install ca.pem on the phone once and every future leaf
    # renewal is trusted automatically.
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TLS_DIR/ca-key.pem" -out "$TLS_DIR/ca.pem" \
        -subj "/CN=Door Access Local CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

    # Chrome ignores the legacy CN entirely; without a matching SAN it rejects
    # the certificate even after the CA is trusted — which would also mean no
    # Web NFC, since that requires a secure context.
    openssl req -newkey rsa:2048 -nodes \
        -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/csr.pem" \
        -subj "/CN=$host" 2>/dev/null

    openssl x509 -req -in "$TLS_DIR/csr.pem" -days 3650 \
        -CA "$TLS_DIR/ca.pem" -CAkey "$TLS_DIR/ca-key.pem" -CAcreateserial \
        -out "$TLS_DIR/cert.pem" \
        -extfile <(printf 'subjectAltName=DNS:%s,DNS:%s.local,DNS:localhost,IP:%s,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' \
                   "$host" "$host" "$ip") 2>/dev/null

    rm -f "$TLS_DIR/csr.pem"
    chown root:"$ADMIN_GROUP" "$TLS_DIR"/*.pem
    chmod 640 "$TLS_DIR/key.pem" "$TLS_DIR/ca-key.pem"
    chmod 644 "$TLS_DIR/cert.pem" "$TLS_DIR/ca.pem"
    info "Certificate written to $TLS_DIR/cert.pem (valid 10 years)"
    info "Install $TLS_DIR/ca.pem on your phone to enable NFC scanning in Chrome"
}

# ── 6. Application files ─────────────────────────────────────────────────────────

install_app_files() {
    info "Downloading application files ..."
    download "$APP_DIR/main.py"             "$REPO/main.py"
    download "$APP_DIR/nfc_reader.py"       "$REPO/nfc_reader.py"
    download "$APP_DIR/lock_controller.py"  "$REPO/lock_controller.py"
    download "$APP_DIR/door_sensor.py"      "$REPO/door_sensor.py"
    download "$APP_DIR/mqtt_handler.py"     "$REPO/mqtt_handler.py"
    download "$APP_DIR/event_store.py"      "$REPO/event_store.py"
    download "$APP_DIR/control_socket.py"   "$REPO/control_socket.py"
    download "$APP_DIR/web_admin.py"        "$REPO/web_admin.py"

    info "Downloading web admin templates ..."
    for t in base.html login.html dashboard.html tags.html history.html \
             updates.html error.html _events_table.html; do
        download "$APP_DIR/templates/$t" "$REPO/templates/$t"
    done
    for s in app.css nfc.js updates.js; do
        download "$APP_DIR/static/$s" "$REPO/static/$s"
    done

    # World-readable so the doorweb service user can load the app it runs.
    chown -R root:root "$APP_DIR"/*.py "$APP_DIR/templates" "$APP_DIR/static"
    chmod 644 "$APP_DIR"/*.py "$APP_DIR/templates"/* "$APP_DIR/static"/*

    # Config: only download if not already present (never clobber user edits)
    if [[ ! -f "$CFG_DIR/config.json" ]]; then
        info "Installing default config ..."
        download "$CFG_DIR/config.json" "$REPO/config.json"
        configure_mqtt
        # Stays door-only: it holds the MQTT password, which the web user has
        # no reason to be able to read.
        chown door:door "$CFG_DIR/config.json"
        chmod 640 "$CFG_DIR/config.json"
    else
        warn "Existing config found at $CFG_DIR/config.json — skipping (not overwritten)"
        migrate_config
        # Older installs left this world-readable. It holds the MQTT password and
        # there is now a second service account on the box, so tighten it.
        chown door:door "$CFG_DIR/config.json"
        chmod 640 "$CFG_DIR/config.json"
    fi

    if [[ ! -f "$CFG_DIR/web.json" ]]; then
        info "Installing web admin config ..."
        download "$CFG_DIR/web.json" "$REPO/web.json"
        chown root:"$ADMIN_GROUP" "$CFG_DIR/web.json"
        chmod 640 "$CFG_DIR/web.json"
    else
        warn "Existing web config found at $CFG_DIR/web.json — skipping (not overwritten)"
    fi

    # Listen address lives here alone, so there is one source of truth for it.
    if [[ ! -f "$CFG_DIR/web.env" ]]; then
        echo "DOOR_WEB_BIND=0.0.0.0:$WEB_PORT" > "$CFG_DIR/web.env"
        chown root:"$ADMIN_GROUP" "$CFG_DIR/web.env"
        chmod 640 "$CFG_DIR/web.env"
    fi

    # Session-signing key. Created here with tight permissions so the service
    # never has to generate it itself on a path it may not be able to write.
    if [[ ! -f "$CFG_DIR/web_secret" ]]; then
        info "Generating web session secret ..."
        openssl rand -hex 32 > "$CFG_DIR/web_secret"
        chown "$WEB_USER":"$WEB_USER" "$CFG_DIR/web_secret"
        chmod 600 "$CFG_DIR/web_secret"
    fi
}

migrate_config() {
    # Add settings introduced after this config was written. The door service
    # falls back to identical defaults, so this is purely so the file documents
    # what is available. Never touches keys that are already present.
    info "Checking config for missing sections ..."
    python3 - "$CFG_DIR/config.json" <<'EOF'
import json, sys

path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)

added = []
web = cfg.setdefault("web", {})
for key, value in (
    ("enabled", True),
    ("control_socket", "/run/door_access/control.sock"),
    ("event_db", "/var/lib/door_access/events.db"),
    ("event_retention_days", 90),
):
    if key not in web:
        web[key] = value
        added.append(f"web.{key}")

if added:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print("  added: " + ", ".join(added))
else:
    print("  nothing to add")
EOF
}

configure_mqtt() {
    echo ""
    echo "─── MQTT Configuration ───────────────────────────────────────"

    read -rp "  MQTT broker IP or hostname (leave blank to configure later): " mqtt_ip
    if [[ -z "$mqtt_ip" ]]; then
        warn "Skipping MQTT setup — set broker IP manually in $CFG_DIR/config.json"
        return
    fi

    read -rp "  MQTT username: " mqtt_user
    read -rsp "  MQTT password: " mqtt_pass
    echo ""
    echo "──────────────────────────────────────────────────────────────"

    info "Writing MQTT settings to config ..."
    python3 - "$CFG_DIR/config.json" "$mqtt_ip" "$mqtt_user" "$mqtt_pass" <<'EOF'
import sys, json

config_path, broker, username, password = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

with open(config_path) as f:
    cfg = json.load(f)

cfg["mqtt"]["enabled"] = True
cfg["mqtt"]["broker"] = broker
cfg["mqtt"]["username"] = username
cfg["mqtt"]["password"] = password

with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
EOF
    info "MQTT configured (broker=$mqtt_ip user=$mqtt_user)"
}

# ── 7. Log file ──────────────────────────────────────────────────────────────────

create_log_file() {
    # The log lives in its own door-owned directory so Python's
    # TimedRotatingFileHandler can create rotated files (it renames within the
    # directory, which /var/log itself does not permit a non-root user to do).
    info "Creating log directory and file ..."
    install -d -m 750 -o door -g door "$LOG_DIR"
    touch "$LOG_FILE"
    chown door:door "$LOG_FILE"
    chmod 640 "$LOG_FILE"
}

# ── 8. systemd service ───────────────────────────────────────────────────────────

install_service() {
    info "Installing systemd services ..."
    download "$SERVICE_FILE"     "$REPO/door_access.service"
    download "$WEB_SERVICE_FILE" "$REPO/door_admin.service"
    chmod 644 "$SERVICE_FILE" "$WEB_SERVICE_FILE"

    systemctl daemon-reload
    systemctl enable door_access
    systemctl enable door_admin
    info "Services enabled (not started — edit config first)"
}

# ── 9. In-place updates from the web admin ──────────────────────────────────────

install_updater() {
    # A git checkout is what the updater fast-forwards; the running app is still
    # the copy installed under /opt/door_access, not this checkout.
    if [[ -d "$REPO_DIR/.git" ]]; then
        info "Update checkout already present at $REPO_DIR"
    else
        info "Cloning $REPO_URL for future updates ..."
        rm -rf "$REPO_DIR"
        if ! git clone --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"; then
            warn "Clone failed — the web Updates page will not work until you run:"
            warn "  sudo git clone --branch $REPO_BRANCH $REPO_URL $REPO_DIR"
            return
        fi
    fi
    # Root-owned and not group-writable: this checkout is the source the updater
    # installs from, so anyone able to write here could run code as root.
    chown -R root:root "$REPO_DIR"
    chmod -R go-w "$REPO_DIR"

    info "Installing updater ..."
    download "$APP_DIR/update.sh" "$REPO/update.sh"
    chown root:root "$APP_DIR/update.sh"
    chmod 755 "$APP_DIR/update.sh"

    download "$UPDATE_SERVICE_FILE"       "$REPO/door_update.service"
    download "$UPDATE_CHECK_SERVICE_FILE" "$REPO/door_update_check.service"
    chmod 644 "$UPDATE_SERVICE_FILE" "$UPDATE_CHECK_SERVICE_FILE"

    # sudoers: validate before installing. A malformed file in sudoers.d can
    # break sudo for every user on the machine, so it is checked in a temporary
    # location first and only then moved into place.
    info "Installing sudoers rule for the updater ..."
    local tmp_sudoers
    tmp_sudoers="$(mktemp)"
    download "$tmp_sudoers" "$REPO/door-update.sudoers"
    chmod 440 "$tmp_sudoers"
    if visudo -c -f "$tmp_sudoers" >/dev/null 2>&1; then
        install -m 440 -o root -g root "$tmp_sudoers" "$SUDOERS_FILE"
        info "Updater sudoers rule installed"
    else
        warn "sudoers rule failed validation — NOT installed; the Updates page will not work"
    fi
    rm -f "$tmp_sudoers"

    systemctl daemon-reload
}

# ── main ───────────────────────────────────────────────────────────────────────

require_root
install_packages
create_venv
enable_reader_escape
create_user
install_polkit_rule
create_dirs
create_tls_cert
install_app_files
create_log_file
install_service
install_updater
grant_web_admins

PI_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  Door Access Control — Installation Complete"
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "  1. Edit the config:"
echo "       sudo nano /etc/door_access/config.json"
echo "     Set your MQTT broker, credentials, and authorized UIDs."
echo ""
echo "  2. Start both services:"
echo "       sudo systemctl start door_access door_admin"
echo ""
echo "  3. Open the web admin:"
echo "       https://${PI_IP:-<pi-ip>}:$WEB_PORT"
echo "     Sign in with a Pi account that is in the '$ADMIN_GROUP' group."
echo "     Add more admins later with:"
echo "       sudo usermod -aG $ADMIN_GROUP <username>"
echo "     (the user must log out and back in for it to take effect)"
echo ""
echo "  4. To scan tags with your phone, install the CA certificate:"
echo "       $TLS_DIR/ca.pem"
echo "     Android: Settings → Security → Encryption & credentials →"
echo "              Install a certificate → CA certificate"
echo "     Web NFC needs Chrome on Android; iOS Safari cannot do it."
echo ""
echo "  5. Check logs:"
echo "       journalctl -u door_access -f"
echo "       journalctl -u door_admin -f"
echo ""
echo "══════════════════════════════════════════════════════════════"
