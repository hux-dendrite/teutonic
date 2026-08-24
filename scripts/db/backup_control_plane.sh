#!/bin/sh
set -eu

: "${TEUTONIC_DATABASE_URL:?TEUTONIC_DATABASE_URL is required}"
: "${TEUTONIC_BACKUP_FILE:?TEUTONIC_BACKUP_FILE is required}"

if [ -e "$TEUTONIC_BACKUP_FILE" ] && [ "${TEUTONIC_BACKUP_ALLOW_OVERWRITE:-}" != "1" ]; then
    echo "refusing to overwrite existing backup: $TEUTONIC_BACKUP_FILE" >&2
    exit 1
fi

backup_parent=$(dirname "$TEUTONIC_BACKUP_FILE")
if [ ! -d "$backup_parent" ]; then
    echo "backup directory does not exist: $backup_parent" >&2
    exit 1
fi

umask 077
pg_dump \
    --dbname="$TEUTONIC_DATABASE_URL" \
    --format=custom \
    --compress=9 \
    --file="$TEUTONIC_BACKUP_FILE"
pg_restore --list "$TEUTONIC_BACKUP_FILE" >/dev/null
backup_name=$(basename "$TEUTONIC_BACKUP_FILE")
(
    cd "$backup_parent"
    sha256sum "$backup_name" >"$backup_name.sha256"
)
echo "control-plane backup verified: $TEUTONIC_BACKUP_FILE"
