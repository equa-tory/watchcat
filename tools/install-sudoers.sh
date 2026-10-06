#!/usr/bin/env bash
# One-time setup so watchcat's Server page can start/stop/restart xray and restart frp WITHOUT a password.
# It allows exactly these four command lines for the watchcat user (no wildcards, nothing else):
#   systemctl start|stop|restart <xray unit>      systemctl restart <frp unit>
#
#   sudo bash tools/install-sudoers.sh            # install (user = whoever ran sudo)
#   bash tools/install-sudoers.sh --print         # just show what would be installed
#   sudo bash tools/install-sudoers.sh --remove   # undo
# Options: --user NAME  --xray-unit NAME  --frp-unit NAME   (defaults: you, xray, frp)
set -euo pipefail

WC_USER="${SUDO_USER:-$(id -un)}"; XRAY=xray; FRP=frp; MODE=install
while [ $# -gt 0 ]; do
  case "$1" in
    --user) WC_USER="$2"; shift 2 ;;
    --xray-unit) XRAY="$2"; shift 2 ;;
    --frp-unit) FRP="$2"; shift 2 ;;
    --print) MODE=print; shift ;;
    --remove) MODE=remove; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done
for v in "$WC_USER" "$XRAY" "$FRP"; do
  [[ "$v" =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo "refusing odd value: $v"; exit 2; }
done
XRAY="${XRAY%.service}.service"; FRP="${FRP%.service}.service"
SYSTEMCTL=/usr/bin/systemctl
TARGET=/etc/sudoers.d/watchcat

content() {
  cat <<SUDOERS
# Installed by watchcat's tools/install-sudoers.sh - remove with: sudo rm $TARGET
# Lets the watchcat service (user $WC_USER) control exactly these units without a password.
$WC_USER ALL=(root) NOPASSWD: $SYSTEMCTL start $XRAY, $SYSTEMCTL stop $XRAY, $SYSTEMCTL restart $XRAY, $SYSTEMCTL restart $FRP
SUDOERS
}

case "$MODE" in
  print) content; exit 0 ;;
  remove) [ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }; rm -fv "$TARGET"; exit 0 ;;
esac

[ "$(id -u)" -eq 0 ] || { echo "run with sudo:  sudo bash $0"; exit 1; }
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
content > "$tmp"
visudo -cf "$tmp" >/dev/null || { echo "generated sudoers file did not validate - nothing installed"; exit 1; }
install -m 0440 -o root -g root "$tmp" "$TARGET"
echo "installed $TARGET:"; cat "$TARGET"
echo "watchcat can now control $XRAY and restart $FRP (reload the Server page)."
