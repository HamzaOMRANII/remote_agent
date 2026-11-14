#!/usr/bin/env bash
# Runs the Remote agent as a systemd service (Linux, Raspberry Pi): starts at boot once the network is up,
# restarts 5 s after any crash, logs to the journal.
#
#   sudo ./service/install-linux.sh              install + start (runs as the user who called sudo)
#   sudo ./service/install-linux.sh --user pi    run as another user
#   sudo ./service/install-linux.sh --uninstall  stop + remove
#   ./service/install-linux.sh --print           only show the unit file (nothing installed)
#
# Logs: journalctl -u remote-agent -f        Status: systemctl status remote-agent
set -euo pipefail

NAME=remote-agent
UNIT=/etc/systemd/system/$NAME.service
DIR=$(cd "$(dirname "$0")/.." && pwd)
RUN_USER=${SUDO_USER:-$(id -un)}
MODE=install

while [ $# -gt 0 ]; do
  case "$1" in
    --user) RUN_USER=$2; shift 2 ;;
    --uninstall) MODE=uninstall; shift ;;
    --print) MODE=print; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || { echo "python3 not found: install Python 3.8+ first." >&2; exit 1; }

unit() {
  cat <<UNIT
[Unit]
Description=Remote agent (remote.omranisystems.com): camera, GPIO, scripts, discovery
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$PYTHON -u $DIR/agent/remote_agent.py --config $DIR/config.json
Restart=always
RestartSec=5
$(getent group gpio >/dev/null && printf '# GPIO on a Pi needs the gpio group\nSupplementaryGroups=gpio\n')
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
}

case "$MODE" in
  print) unit; exit 0 ;;
  uninstall)
    [ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
    systemctl disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT"; systemctl daemon-reload
    echo "Removed. The agent no longer starts by itself."; exit 0 ;;
esac

[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0" >&2; exit 1; }
id "$RUN_USER" >/dev/null 2>&1 || { echo "user '$RUN_USER' does not exist" >&2; exit 1; }
[ -f "$DIR/config.json" ] || { echo "No config.json yet: pair this device first (see README, --pair)." >&2; exit 1; }
grep -q '"token"' "$DIR/config.json" || { echo "config.json has no token: pair this device first." >&2; exit 1; }
if ! sudo -u "$RUN_USER" "$PYTHON" -c "import aiortc" 2>/dev/null; then
  echo "Note: video (this computer's cameras + network cameras) needs aiortc, not installed for $RUN_USER: pip install aiortc (as $RUN_USER)." >&2
fi

unit > "$UNIT"
systemctl daemon-reload
systemctl enable --now "$NAME"
sleep 2
systemctl --no-pager --lines=5 status "$NAME" || true
echo
echo "Installed: the agent now starts at boot and restarts after a crash."
echo "Logs: journalctl -u $NAME -f    Stop: sudo systemctl stop $NAME    Remove: sudo $0 --uninstall"
