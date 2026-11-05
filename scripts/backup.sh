#!/bin/sh
# Daily Stacklog DB backup.
#
# Takes a consistent SQLite snapshot inside the running container (safe while
# the app is serving requests), checks it, copies it out of the Docker volume
# and keeps only the newest KEEP copies.
#
# Install on the host (example: daily at 03:00):
#   0 3 * * * /DATA/AppData/stacklog/backup.sh >> /DATA/AppData/stacklog/backups/daily/cron.log 2>&1
set -e

CONTAINER=${CONTAINER:-stacklog}
DIR=${BACKUP_DIR:-/DATA/AppData/stacklog/backups/daily}
KEEP=${KEEP:-7}
SNAP=/tmp/stacklog-snap.db
# on ZimaOS the default docker config dir is root-owned
[ -d /DATA/AppData/.docker-oruam ] && export DOCKER_CONFIG=${DOCKER_CONFIG:-/DATA/AppData/.docker-oruam}

mkdir -p "$DIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$DIR/stacklog-$TS.db"

docker exec "$CONTAINER" python3 -c "
import sqlite3
src = sqlite3.connect('/data/stacklog.db')
dst = sqlite3.connect('$SNAP')
src.backup(dst)
ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
n = dst.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
dst.close(); src.close()
assert ok == 'ok', ok
print(n, 'tasks')
"
docker cp "$CONTAINER:$SNAP" "$OUT"
docker exec "$CONTAINER" rm -f "$SNAP"

# keep the newest KEEP backups (plain loop: ZimaOS has no xargs)
ls -1t "$DIR"/stacklog-*.db | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; done

echo "$(date '+%F %T') ok $(basename "$OUT") $(wc -c < "$OUT") bytes" >> "$DIR/backup.log"
