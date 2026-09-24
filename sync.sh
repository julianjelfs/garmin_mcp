#!/bin/bash
# Daily Garmin sync: pull new data, parse into SQLite. Idempotent.
# Run by garmin-sync.timer on the Pi, by the trigger_sync tool, or manually.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python"
DB="${GARMIN_DB_PATH:-$HOME/.garmin-assistant/garmin.db}"
LOG="${GARMIN_SYNC_LOG:-$DIR/sync.log}"

# The timer and trigger_sync can overlap; two exports writing one cache would
# corrupt it, so the second run waits for the first.
exec 9>"$DIR/.sync.lock"
flock 9 2>/dev/null || true

echo "===== sync $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$LOG"

cd "$DIR/vendor-garmin-export" || exit 1
"$PY" garmin_export.py --update >> "$LOG" 2>&1
export_rc=$?

"$PY" "$DIR/import_to_sqlite.py" --db "$DB" --update >> "$LOG" 2>&1
import_rc=$?

# Heal any activity missing time-series detail (e.g. cache that landed after the
# scalar import). No-op once every activity has streams. Idempotent.
"$PY" "$DIR/import_to_sqlite.py" --db "$DB" --backfill >> "$LOG" 2>&1
backfill_rc=$?

echo "export_rc=$export_rc import_rc=$import_rc backfill_rc=$backfill_rc" >> "$LOG"
# Non-zero export (e.g. expired token / 429) still lets the importer refresh
# from the last good file; the server's freshness field will flag staleness.
exit 0
