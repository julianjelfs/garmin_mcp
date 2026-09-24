#!/usr/bin/env bash
# Nightly snapshot of the Garmin database and athlete context, taken on the Pi.
set -euo pipefail

DB="${GARMIN_DB_PATH:-$HOME/.garmin-assistant/garmin.db}"
CONTEXT="${ATHLETE_CONTEXT_PATH:-$HOME/.garmin-assistant/athlete_context.yaml}"
DEST="${GARMIN_BACKUP_DIR:-$HOME/garmin-backups}"
KEEP=14

mkdir -p "$DEST"
stamp="$(date +%Y%m%d-%H%M%S)"
out="$DEST/garmin-$stamp.db"

# SQLite's online backup API copies a consistent snapshot even mid-sync.
# `cp` would catch a half-written page and give us a file that only looks like a backup.
if ! python3 - "$DB" "$out" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
with target:
    source.backup(target)

state = target.execute("PRAGMA integrity_check").fetchone()[0]
try:
    days = target.execute("select count(*) from daily_health").fetchone()[0]
except sqlite3.Error:
    days = 0
target.close()
source.close()

if state != "ok":
    raise SystemExit(f"integrity check failed: {state}")
if days == 0:
    raise SystemExit("backup holds no daily_health rows")
print(f"backed up {days} days of health data")
PY
then
	# A bad copy must not count as a backup, or it rotates a good one away.
	rm -f "$out"
	exit 1
fi

if [ -f "$CONTEXT" ]; then cp "$CONTEXT" "$DEST/athlete_context-$stamp.yaml"; fi

echo "wrote $out ($(du -h "$out" | cut -f1))"
ls -1t "$DEST"/garmin-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f || true
ls -1t "$DEST"/athlete_context-*.yaml 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f || true
echo "keeping $(ls -1 "$DEST"/garmin-*.db 2>/dev/null | wc -l | tr -d ' ') backups in $DEST"
