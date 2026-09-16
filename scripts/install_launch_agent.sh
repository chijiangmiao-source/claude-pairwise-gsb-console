#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.local.claude-pairwise-gsb-console"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/Claude A-B GSB Console"
APP_ROOT="$HOME/Library/Application Support/Claude A-B GSB Console/app"
mkdir -p "$(dirname "$PLIST")" "$LOG_DIR" "$APP_ROOT"
# LaunchAgents cannot reliably traverse a user Documents folder when macOS
# privacy controls are enabled. Install an isolated runtime copy under Library.
/usr/bin/rsync -a --delete \
  --exclude '.git' --exclude '.data' --exclude 'projects' --exclude '__pycache__' \
  "$ROOT/" "$APP_ROOT/"
chmod +x "$APP_ROOT/scripts/start.sh"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$APP_ROOT/scripts/start.sh</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/server.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/server-error.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5; do
  if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    break
  fi
  sleep 1
done
launchctl print "gui/$(id -u)/$LABEL" >/dev/null
launchctl enable "gui/$(id -u)/$LABEL"
echo "Installed $LABEL at http://127.0.0.1:8865"
