#!/bin/zsh
# Install the hourly canary as a launchd job on this Mac and run one tick now.
# Remove: launchctl bootout gui/$(id -u)/com.ocarlsen.receipts-canary && rm ~/Library/LaunchAgents/com.ocarlsen.receipts-canary.plist
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p out/canary
# The canary resets the fake core on every tick, so it gets its own database (kept in step with the plist).
CANARY_DB=fernhollow_canary
if ! psql -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname = '$CANARY_DB'" | grep -q 1; then
  createdb "$CANARY_DB"
  psql -q -v ON_ERROR_STOP=1 -d "$CANARY_DB" -f schema.sql
fi
sed "s|__REPO__|$PWD|g" ops/com.ocarlsen.receipts-canary.plist > ~/Library/LaunchAgents/com.ocarlsen.receipts-canary.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ocarlsen.receipts-canary.plist
launchctl kickstart gui/$(id -u)/com.ocarlsen.receipts-canary
sleep 8
launchctl print gui/$(id -u)/com.ocarlsen.receipts-canary | grep -E "state =|last exit code|run interval"
