#!/bin/bash
# Daily Garmin sync: pull new data, parse into SQLite. Idempotent.
# Run by launchd (see com.julian.garmin-sync.plist) or manually.
set -uo pipefail

DIR="/Users/julianjelfs/work/garmin_mcp"
PY="$DIR/.venv/bin/python"
DB="$HOME/.garmin-assistant/garmin.db"
LOG="$DIR/sync.log"

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
