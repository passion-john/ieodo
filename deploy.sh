#!/bin/zsh
# Build ieodo and put it live: release build → copy to ~/.local/bin
# (outside ~/Documents, so macOS does not ask for folder access after every
# build) → restart the launchd service → wait until it answers.
set -e
cd "${0:A:h}"
BIN="$HOME/.local/bin/ieodo"

cargo build --release --locked
mkdir -p "${BIN:h}"
# Copy then rename: running relays keep the old file until they restart.
cp target/release/ieodo "$BIN.tmp"
mv "$BIN.tmp" "$BIN"

if ! launchctl print "gui/$(id -u)/com.ieodo.server" >/dev/null 2>&1; then
  echo "installed $BIN; the launchd service is not loaded yet (see README, 설치)" >&2
  exit 0
fi

launchctl kickstart -k "gui/$(id -u)/com.ieodo.server"
for i in {1..30}; do
  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:7341/health; then
    echo "ieodo is up (${i}s)"
    exit 0
  fi
  sleep 1
done
echo "ieodo did not answer in 30s; see ~/.ieodo/logs/server.log" >&2
exit 1
