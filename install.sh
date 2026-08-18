#!/usr/bin/env bash
# Install Raspberry Pi voice client via Docker + enable git auto-update timer.
#
# Usage (on Pi, after clone):
#   cd ~/voice-client-pi
#   cp .env.example .env   # edit VOICE_BACKEND_URL / MUSIC_DEVICE_ID
#   bash install.sh
#
# Or one-shot with overrides:
#   BACKEND=http://voice.pora-ai.ru DEVICE_ID=pi-livingroom bash install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="${BACKEND:-}"
DEVICE_ID="${DEVICE_ID:-}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
HOME_DIR="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
REPO_DIR="${REPO_DIR:-${SCRIPT_DIR}}"
UPDATE_TIMER="${UPDATE_TIMER:-true}"

echo "==> Repo:       ${REPO_DIR}"
echo "==> User:       ${SERVICE_USER}"
echo "==> Update timer: ${UPDATE_TIMER}"

cd "${REPO_DIR}"

# --- Docker ---
if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker…"
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl git
  curl -fsSL https://get.docker.com | sudo sh
fi

sudo usermod -aG docker "${SERVICE_USER}" || true

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin missing — install docker-compose-plugin" >&2
  exit 1
fi

# --- .env ---
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "==> Created .env from .env.example"
  else
    echo "Missing .env and .env.example" >&2
    exit 1
  fi
fi

if [[ -n "${BACKEND}" ]]; then
  if grep -q '^VOICE_BACKEND_URL=' .env; then
    sed -i "s|^VOICE_BACKEND_URL=.*|VOICE_BACKEND_URL=${BACKEND}|" .env
  else
    echo "VOICE_BACKEND_URL=${BACKEND}" >> .env
  fi
fi

if [[ -n "${DEVICE_ID}" ]]; then
  if grep -q '^MUSIC_DEVICE_ID=' .env; then
    sed -i "s|^MUSIC_DEVICE_ID=.*|MUSIC_DEVICE_ID=${DEVICE_ID}|" .env
  else
    echo "MUSIC_DEVICE_ID=${DEVICE_ID}" >> .env
  fi
elif ! grep -q '^MUSIC_DEVICE_ID=.\+' .env; then
  DEFAULT_ID="pi-$(hostname -s 2>/dev/null || echo default)"
  sed -i "s|^MUSIC_DEVICE_ID=.*|MUSIC_DEVICE_ID=${DEFAULT_ID}|" .env
fi

echo "==> Backend / device from .env:"
grep -E '^(VOICE_BACKEND_URL|MUSIC_DEVICE_ID)=' .env || true

# --- ALSA host tools (optional diagnostics) ---
sudo apt-get update
sudo apt-get install -y --no-install-recommends alsa-utils git

# --- Build & run ---
echo "==> Building and starting container…"
# If current user is not yet in docker group in this shell, use sudo
if docker info >/dev/null 2>&1; then
  docker compose up -d --build
else
  sudo docker compose up -d --build
fi

# --- systemd update timer ---
if [[ "${UPDATE_TIMER}" == "true" ]]; then
  UNIT_DIR="/etc/systemd/system"
  sudo tee "${UNIT_DIR}/voice-client-update.service" >/dev/null <<EOF
[Unit]
Description=Update voice-client-pi from Git and rebuild Docker
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/scripts/update.sh
EOF

  sudo tee "${UNIT_DIR}/voice-client-update.timer" >/dev/null <<EOF
[Unit]
Description=Periodically update voice-client-pi from Git

[Timer]
OnBootSec=3min
OnUnitActiveSec=30s
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
EOF

  chmod +x "${REPO_DIR}/scripts/update.sh"
  sudo systemctl daemon-reload
  sudo systemctl enable --now voice-client-update.timer
  echo "==> Enabled voice-client-update.timer"
fi

# --- Bluetooth auto-connect (user service, needs BT_DEVICE_MAC in .env) ---
chmod +x "${REPO_DIR}/scripts/bt-connect.sh"
USER_UNIT_DIR="${HOME_DIR}/.config/systemd/user"
mkdir -p "${USER_UNIT_DIR}"
cat > "${USER_UNIT_DIR}/voice-bt-connect.service" <<EOF
[Unit]
Description=Auto-connect Bluetooth speaker for voice-client-pi
After=bluetooth.target network.target sound.target
Wants=bluetooth.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
Environment=ENV_FILE=${REPO_DIR}/.env
Environment=BT_CONNECT_ONCE=false
ExecStart=${REPO_DIR}/scripts/bt-connect.sh
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

# Linger so the user service runs headless (no SSH session required).
sudo loginctl enable-linger "${SERVICE_USER}" || true
# BlueZ: reconnect trusted devices when they appear
if [[ -f /etc/bluetooth/main.conf ]]; then
  sudo sed -i 's/^#AutoEnable=true/AutoEnable=true/' /etc/bluetooth/main.conf || true
  if ! grep -q '^AutoEnable=true' /etc/bluetooth/main.conf; then
    sudo sed -i '/^\[Policy\]/a AutoEnable=true' /etc/bluetooth/main.conf || true
  fi
fi

if grep -qE '^BT_DEVICE_MAC=.+' "${REPO_DIR}/.env" 2>/dev/null; then
  # systemctl --user needs a session bus; try best-effort from install.
  if systemctl --user daemon-reload 2>/dev/null \
    && systemctl --user enable --now voice-bt-connect.service 2>/dev/null; then
    echo "==> Enabled voice-bt-connect.service (BT_DEVICE_MAC from .env)"
  else
    echo "==> BT auto-connect unit installed. After login run:"
    echo "    systemctl --user daemon-reload"
    echo "    systemctl --user enable --now voice-bt-connect.service"
  fi
else
  echo "==> BT auto-connect: set BT_DEVICE_MAC in .env, then:"
  echo "    systemctl --user daemon-reload && systemctl --user enable --now voice-bt-connect.service"
fi

echo
echo "Done."
echo "  Logs:    docker compose -f ${REPO_DIR}/docker-compose.yml logs -f"
echo "  Config:  ${REPO_DIR}/.env"
echo "  Update:  ${REPO_DIR}/scripts/update.sh"
echo "  Timer:   systemctl list-timers voice-client-update.timer"
echo "  BT:      systemctl --user status voice-bt-connect.service"
echo "  Mic:     arecord -l && speaker-test -t wav -c 2"
echo
echo "If docker permission denied: log out/in (or newgrp docker), then:"
echo "  cd ${REPO_DIR} && docker compose up -d"
