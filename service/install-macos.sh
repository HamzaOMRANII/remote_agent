#!/usr/bin/env bash
# Runs the Remote agent by itself on macOS: a LaunchAgent that starts when you sign in and restarts after a crash.
#   ./service/install-macos.sh               install + start
#   ./service/install-macos.sh --uninstall   stop + remove
# Log: ~/Library/Logs/remote-agent.log
set -euo pipefail
LABEL=com.omranisystems.remote-agent
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
DIR=$(cd "$(dirname "$0")/.." && pwd)

if [ "${1:-}" = "--uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"
  echo "Removed. The agent no longer starts by itself."; exit 0
fi
PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || { echo "python3 not found" >&2; exit 1; }
[ -f "$DIR/config.json" ] && grep -q '"token"' "$DIR/config.json" || { echo "Pair this device first (see README, --pair)." >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PYTHON</string><string>-u</string><string>$DIR/agent/remote_agent.py</string>
    <string>--config</string><string>$DIR/config.json</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/remote-agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/remote-agent.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed: the agent starts when you sign in and restarts after a crash. Log: ~/Library/Logs/remote-agent.log"
