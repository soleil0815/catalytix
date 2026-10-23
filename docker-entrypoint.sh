#!/bin/sh
# Starts as root, makes sure the data folder has the right owner
# (e.g. when Docker created ./data as root) and then starts the app
# without root rights as user PUID:PGID (default 1000:1000).
set -e

# New files (database, covers, backups, keys) readable for the app user only
umask 077

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA="${CATALYTIX_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA" "$DATA/covers"
    # Only fix when needed (spares large folders)
    if [ "$(stat -c %u "$DATA")" != "$PUID" ] || [ -n "$(find "$DATA" \! -user "$PUID" -print -quit)" ]; then
        echo "Catalytix: setting owner of $DATA to $PUID:$PGID"
        chown -R "$PUID:$PGID" "$DATA" || echo "WARNING: could not change the owner everywhere in $DATA (e.g. a NAS share inside the data folder) – give $PUID:$PGID write access there." >&2
    fi
    # Database, keys and covers contain personal data and password hashes: not readable for other users
    # of the host (symlinks are not followed)
    if [ -n "$(find "$DATA" \( -type f -o -type d \) -perm /077 -print -quit)" ]; then
        echo "Catalytix: restricted permissions in $DATA to the app user"
        find "$DATA" \( -type f -o -type d \) -perm /077 -exec chmod go-rwx {} + 2>/dev/null || true
    fi
    # Separate backup folder (CATALYTIX_BACKUP_DIR, e.g. a NAS share) – also only when needed
    BACKUP="${CATALYTIX_BACKUP_DIR:-}"
    case "$BACKUP" in
        ""|"$DATA"|"$DATA"/*) ;;    # inside the data folder – already handled above
        *)
            # Without a mounted volume the backups would live inside the container and be gone after the next update
            if command -v mountpoint >/dev/null 2>&1 && ! mountpoint -q "$BACKUP" 2>/dev/null; then
                echo "WARNING: $BACKUP (CATALYTIX_BACKUP_DIR) is not a mounted volume – backups there are lost at the next update. Check the volume in docker-compose.override.yml." >&2
            fi
            if ! mkdir -p "$BACKUP" 2>/dev/null; then
                echo "WARNING: cannot create the backup folder $BACKUP." >&2
            elif [ "$(stat -c %u "$BACKUP")" != "$PUID" ] || [ -n "$(find "$BACKUP" \! -user "$PUID" -print -quit)" ]; then
                echo "Catalytix: setting owner of $BACKUP to $PUID:$PGID"
                chown -R "$PUID:$PGID" "$BACKUP" || echo "WARNING: cannot change the owner of $BACKUP (e.g. a NAS share) – give $PUID:$PGID write access there." >&2
            fi
            ;;
    esac
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

# Already running without root (e.g. with "user:" in the Compose file)
if [ ! -w "$DATA" ]; then
    echo "ERROR: $DATA is not writable for user $(id -u)." >&2
    echo "Run on the host:  sudo chown -R $(id -u):$(id -g) ./data" >&2
    exit 1
fi
exec "$@"
