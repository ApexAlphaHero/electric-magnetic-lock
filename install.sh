#!/usr/bin/env bash
# Door Access Control — installer
# Run as root:  sudo bash install.sh
# Or one-liner: curl -fsSL https://raw.githubusercontent.com/ApexAlphaHero/electric-magnetic-lock/main/install.sh | sudo bash
#
# Target: Raspberry Pi OS / Debian 13 (trixie) or newer, Python >= 3.10.
# On Debian 12+ PEP 668 blocks `pip install` into the system interpreter, so all
# Python dependencies are installed from apt instead.

set -euo pipefail

REPO="https://raw.githubusercontent.com/ApexAlphaHero/electric-magnetic-lock/main"
APP_DIR="/opt/door_access"
CFG_DIR="/etc/door_access"
LOG_FILE="/var/log/door_access.log"
SERVICE_FILE="/etc/systemd/system/door_access.service"
POLKIT_RULE="/etc/polkit-1/rules.d/50-door-pcsc.rules"
CCID_PLIST="/etc/libccid_Info.plist"

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

download() {
    local dest="$1" url="$2"
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
        python3-rpi-lgpio

    info "Enabling pcscd ..."
    systemctl enable pcscd
    systemctl start pcscd
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
    install -d -m 755           "$APP_DIR"
    install -d -m 750 -o door -g door "$CFG_DIR"
}

# ── 6. Application files ─────────────────────────────────────────────────────────

install_app_files() {
    info "Downloading application files ..."
    download "$APP_DIR/main.py"             "$REPO/main.py"
    download "$APP_DIR/nfc_reader.py"       "$REPO/nfc_reader.py"
    download "$APP_DIR/lock_controller.py"  "$REPO/lock_controller.py"
    download "$APP_DIR/door_sensor.py"      "$REPO/door_sensor.py"
    download "$APP_DIR/mqtt_handler.py"     "$REPO/mqtt_handler.py"

    chown -R door:door "$APP_DIR"
    chmod 644 "$APP_DIR"/*.py

    # Config: only download if not already present (never clobber user edits)
    if [[ ! -f "$CFG_DIR/config.json" ]]; then
        info "Installing default config ..."
        download "$CFG_DIR/config.json" "$REPO/config.json"
        configure_mqtt
        chown door:door "$CFG_DIR/config.json"
        chmod 640 "$CFG_DIR/config.json"
    else
        warn "Existing config found at $CFG_DIR/config.json — skipping (not overwritten)"
    fi
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
    info "Creating log file ..."
    touch "$LOG_FILE"
    chown door:door "$LOG_FILE"
    chmod 640 "$LOG_FILE"
}

# ── 8. systemd service ───────────────────────────────────────────────────────────

install_service() {
    info "Installing systemd service ..."
    download "$SERVICE_FILE" "$REPO/door_access.service"
    chmod 644 "$SERVICE_FILE"

    systemctl daemon-reload
    systemctl enable door_access
    info "Service enabled (not started — edit config first)"
}

# ── main ───────────────────────────────────────────────────────────────────────

require_root
install_packages
enable_reader_escape
create_user
install_polkit_rule
create_dirs
install_app_files
create_log_file
install_service

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           Door Access Control — Installation Complete        ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  1. Edit the config:                                         ║"
echo "║     sudo nano /etc/door_access/config.json                   ║"
echo "║                                                              ║"
echo "║  2. Set your MQTT broker, credentials, and authorized UIDs   ║"
echo "║                                                              ║"
echo "║  3. Start the service:                                       ║"
echo "║     sudo systemctl start door_access                         ║"
echo "║                                                              ║"
echo "║  4. Check logs:                                              ║"
echo "║     journalctl -u door_access -f                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
