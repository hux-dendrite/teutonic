#!/bin/sh
set -eu

: "${TEUTONIC_RESTORE_DATABASE_URL:?TEUTONIC_RESTORE_DATABASE_URL is required}"
: "${TEUTONIC_BACKUP_FILE:?TEUTONIC_BACKUP_FILE is required}"
: "${TEUTONIC_RESTORE_CONFIRM_DATABASE:?TEUTONIC_RESTORE_CONFIRM_DATABASE is required}"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_database=$(
    psql "$TEUTONIC_RESTORE_DATABASE_URL" --no-psqlrc --tuples-only --no-align \
        --command="SELECT current_database()"
)

if [ "$target_database" != "$TEUTONIC_RESTORE_CONFIRM_DATABASE" ]; then
    echo "refusing restore into $target_database; confirmation names another database" >&2
    exit 1
fi

existing_control_plane=$(
    psql "$TEUTONIC_RESTORE_DATABASE_URL" --no-psqlrc --tuples-only --no-align \
        --command="SELECT count(*) FROM pg_namespace WHERE nspname = 'control_plane'"
)
if [ "$existing_control_plane" != "0" ]; then
    echo "refusing restore into non-empty control-plane database: $target_database" >&2
    exit 1
fi

backup_parent=$(dirname "$TEUTONIC_BACKUP_FILE")
backup_name=$(basename "$TEUTONIC_BACKUP_FILE")
(
    cd "$backup_parent"
    sha256sum -c "$backup_name.sha256"
)
psql "$TEUTONIC_RESTORE_DATABASE_URL" \
    --no-psqlrc \
    --set=ON_ERROR_STOP=1 \
    --file="$script_dir/bootstrap_control_plane_roles.sql" >/dev/null
pg_restore \
    --dbname="$TEUTONIC_RESTORE_DATABASE_URL" \
    --exit-on-error \
    --single-transaction \
    "$TEUTONIC_BACKUP_FILE"
echo "control-plane restore verified: $target_database"
