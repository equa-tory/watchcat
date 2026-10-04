#!/usr/bin/env bash
# watchcat installer (Linux/macOS). Needs only python3.
#   ./install.sh            interactive: asks for password, offers a systemd service
#   ./install.sh --run      just start in the foreground
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
DIR="$PWD"

command -v python3 >/dev/null || { echo "python3 not found - install it first (e.g. sudo apt install python3)"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Python 3.8+ required"; exit 1; }

if [ ! -f .env ]; then
  pw=""
  if [ -t 0 ] && [ "${1:-}" != "--run" ]; then
    read -rsp "Password for making changes (empty = no login): " pw; echo
  fi
  { echo "PASSWORD=$pw"; echo "PORT=8888"; } > .env
  chmod 600 .env
  echo "Created .env"
fi
PORT=$(sed -n 's/^PORT=//p' .env | tail -1); PORT=${PORT:-8888}

if [ "${1:-}" != "--run" ] && [ -t 0 ] && command -v systemctl >/dev/null; then
  read -rp "Install as a systemd service in /etc/systemd/system (autostart, needs sudo)? [y/N] " a
  if [[ "$a" =~ ^[Yy] ]]; then
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    SVC_USER="${SUDO_USER:-$(id -un)}"
    $SUDO tee /etc/systemd/system/watchcat.service >/dev/null <<UNIT
[Unit]
Description=watchcat status dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
ExecStart=$(command -v python3) $DIR/server.py
WorkingDirectory=$DIR
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now watchcat
    echo "Running: http://localhost:$PORT   (logs: journalctl -u watchcat -f)"
    exit 0
  fi
fi

echo "Starting on http://localhost:$PORT  (Ctrl+C to stop)"
exec python3 server.py
