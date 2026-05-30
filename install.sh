#!/usr/bin/env bash
# Door Access Control — installer
# Run as root:  sudo bash install.sh
# Or one-liner: curl -fsSL https://raw.githubusercontent.com/ApexAlphaHero/electric-magnetic-lock/main/install.sh | sudo bash

set -euo pipefail

REPO="https://raw.githubusercontent.com/ApexAlphaHero/electric-magnetic-lock/main"
APP_DIR="/opt/door_access"
CFG_DIR="/etc/door_access"
LOG_FILE="/var/log/door_access.log"
SERVICE_FILE="/etc/systemd/system/door_access.service"

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

# ── 1. System packages ─────────────────────────────────────────────────────────

install_packages() {
    info "Updating package lists ..."
    apt-get update -qq

    info "Installing system dependencies ..."
    apt-get install -y \
        python3-pip \
        pcscd \
        pcsc-tools \
        libpcsclite-dev \
        python3-systemd

    info "Enabling pcscd ..."
    systemctl enable pcscd
    systemctl start pcscd
}

# ── 2. Python packages ─────────────────────────────────────────────────────────

install_python_packages() {
    info "Installing Python packages ..."
    pip3 install --quiet RPi.GPIO pyscard

    # paho-mqtt is only needed if mqtt.enabled is true in config.json
    # Install it anyway so it's ready when you enable MQTT later
    pip3 install --quiet paho-mqtt || warn "paho-mqtt install failed — MQTT will be unavailable"
}

# ── 3. System user ─────────────────────────────────────────────────────────────

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

# ── 4. Directories ─────────────────────────────────────────────────────────────

create_dirs() {
    info "Creating application directories ..."
    install -d -m 755           "$APP_DIR"
    install -d -m 750 -o door -g door "$CFG_DIR"
}

# ── 5. Application files ───────────────────────────────────────────────────────

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

# ── 6. Log file ────────────────────────────────────────────────────────────────

create_log_file() {
    info "Creating log file ..."
    touch "$LOG_FILE"
    chown door:door "$LOG_FILE"
    chmod 640 "$LOG_FILE"
}

# ── 7. systemd service ─────────────────────────────────────────────────────────

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
install_python_packages
create_user
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
