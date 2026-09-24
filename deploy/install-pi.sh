#!/usr/bin/env bash
# Install (or re-install) the Garmin MCP server on the Pi. Run it ON the Pi:
#
#   cd ~/garmin_mcp && ./deploy/install-pi.sh
#
# Safe to re-run: never touches the database, the export cache, the athlete
# context or the Garmin tokens. Those are copied across once, by hand.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=8020
TAILNET_PORT=8447
[ "$(id -un)" = root ] && { echo "run this as your normal user, not root" >&2; exit 1; }

echo "==> python dependencies"
UV="$HOME/.local/bin/uv"
"$UV" --version >/dev/null 2>&1 || { echo "uv is not installed: https://astral.sh/uv" >&2; exit 1; }
[ -d "$ROOT/.venv" ] || "$UV" venv -q "$ROOT/.venv"
"$UV" pip install -q --python "$ROOT/.venv/bin/python" -r "$ROOT/requirements.txt"

echo "==> systemd units"
for unit in garmin-mcp.service garmin-sync.service garmin-sync.timer garmin-backup.service garmin-backup.timer; do
	sudo cp "$ROOT/deploy/$unit" "/etc/systemd/system/$unit"
done
sudo systemctl daemon-reload
sudo systemctl enable garmin-mcp.service garmin-sync.timer garmin-backup.timer >/dev/null
sudo systemctl restart garmin-mcp.service
sudo systemctl start garmin-sync.timer garmin-backup.timer

echo "==> waiting for the server"
for _ in $(seq 1 40); do
	curl -sf -m 1 "http://127.0.0.1:$PORT/health" >/dev/null && break
	sleep 0.5
done
curl -sf -m 3 "http://127.0.0.1:$PORT/health" >/dev/null \
	|| { echo "the server did not come up; see: sudo journalctl -u garmin-mcp -n 50" >&2; exit 1; }

echo "==> tailnet"
sudo tailscale serve --bg --https="$TAILNET_PORT" "$PORT" >/dev/null

echo
echo "installed."
printf "  server     : %s\n" "$(systemctl is-active garmin-mcp)"
printf "  next sync  : %s\n" "$(systemctl list-timers garmin-sync --no-pager 2>/dev/null | awk 'NR==2 {print $1, $2, $3}')"
printf "  next backup: %s\n" "$(systemctl list-timers garmin-backup --no-pager 2>/dev/null | awk 'NR==2 {print $1, $2, $3}')"
printf "  mcp url    : https://pi.tail50bfbf.ts.net:%s/mcp\n" "$TAILNET_PORT"
