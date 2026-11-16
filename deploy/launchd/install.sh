#!/bin/bash
# Install paperbot's launchd jobs on macOS, so the web page, the live scanner and the paper bot
# start at login and come back if they stop. No admin rights needed: these are per-user agents.
#
#   deploy/launchd/install.sh              # bot on the aggressive profile
#   deploy/launchd/install.sh scalp        # or default / scalp
#   deploy/launchd/install.sh --uninstall  # stop and remove all three
#
# Jobs (logs in ~/Library/Logs/paperbot/):
#   com.paperbot.web       paperbot web              always kept up                  web.log
#   com.paperbot.spy-live  paperbot scan --live      restarted if it fails           spy-live.log
#   com.paperbot.bot       paperbot --profile X run  restarted if it fails           bot.log
# The scanner and the bot run under `caffeinate -i`, so the Mac doesn't idle-sleep while they run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/paperbot"
DOMAIN="gui/$(id -u)"
LABELS="com.paperbot.web com.paperbot.spy-live com.paperbot.bot"
PROFILE="${1:-aggressive}"

stop_job() {
  launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do  # bootout returns before the job is fully gone
    launchctl print "$DOMAIN/$1" >/dev/null 2>&1 || return 0
    sleep 1
  done
}

if [ "$PROFILE" = "--uninstall" ]; then
  for label in $LABELS; do
    stop_job "$label"
    rm -f "$AGENTS/$label.plist"
  done
  echo "Removed $LABELS"
  exit 0
fi

case "$PROFILE" in default|scalp|aggressive) ;; *) echo "unknown profile: $PROFILE" >&2; exit 2 ;; esac
[ -x "$PY" ] || { echo "no virtualenv at $PY: see Quick start in README.md" >&2; exit 1; }
[ -f "$ROOT/.env" ] || { echo "no $ROOT/.env: copy .env.example and fill it in" >&2; exit 1; }
mkdir -p "$AGENTS" "$LOGS"

# write_job LABEL KEEPALIVE-XML LOGFILE PROGRAM ARGS...
write_job() {
  local label=$1 keepalive=$2 log=$3
  shift 3
  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
EOF
    for arg in "$@"; do printf '    <string>%s</string>\n' "$arg"; done
    cat <<EOF
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>$keepalive
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOGS/$log</string>
  <key>StandardErrorPath</key><string>$LOGS/$log</string>
</dict>
</plist>
EOF
  } > "$AGENTS/$label.plist"
  plutil -lint -s "$AGENTS/$label.plist"
}

ALWAYS='<true/>'
ON_FAILURE='<dict><key>SuccessfulExit</key><false/></dict>'  # a clean stop (exit 0) stays stopped
write_job com.paperbot.web "$ALWAYS" web.log "$PY" -m paperbot web
write_job com.paperbot.spy-live "$ON_FAILURE" spy-live.log /usr/bin/caffeinate -i "$PY" -m paperbot scan --live
write_job com.paperbot.bot "$ON_FAILURE" bot.log /usr/bin/caffeinate -i "$PY" -m paperbot --profile "$PROFILE" run

for label in $LABELS; do
  stop_job "$label"
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
done
echo "Installed $LABELS (bot profile: $PROFILE). Logs: $LOGS"
