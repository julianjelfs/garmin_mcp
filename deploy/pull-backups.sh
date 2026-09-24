#!/usr/bin/env bash
# Pull the Pi's backups onto this laptop. Safe to run by hand at any time.
set -euo pipefail

HOST="${GARMIN_PI_HOST:-julian_jelfs@pi.local}"
DEST="${GARMIN_BACKUP_DEST:-$HOME/Backups/garmin-mcp}"
KEEP=30

mkdir -p "$DEST"
# No --delete: if the Pi loses its backups, this side should not lose them too.
rsync -az -e "ssh -o BatchMode=yes -o ConnectTimeout=10" "$HOST:~/garmin-backups/" "$DEST/"
ls -1t "$DEST"/garmin-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -I{} rm -f {}
echo "$(ls -1 "$DEST"/garmin-*.db 2>/dev/null | wc -l | tr -d ' ') backups in $DEST"
