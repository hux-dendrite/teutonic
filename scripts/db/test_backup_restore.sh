#!/bin/sh
set -eu

: "${TEUTONIC_DATABASE_URL:?TEUTONIC_DATABASE_URL is required}"
: "${TEUTONIC_RESTORE_DATABASE_URL:?TEUTONIC_RESTORE_DATABASE_URL is required}"
: "${TEUTONIC_RESTORE_CONFIRM_DATABASE:?TEUTONIC_RESTORE_CONFIRM_DATABASE is required}"

backup_file=/backups/control-plane.dump
export TEUTONIC_BACKUP_FILE="$backup_file"

psql --dbname=postgres --set=ON_ERROR_STOP=1 \
    --command="DROP DATABASE IF EXISTS teutonic_restore_test WITH (FORCE)"
psql --dbname=postgres --set=ON_ERROR_STOP=1 \
    --command="CREATE DATABASE teutonic_restore_test"

sh /app/scripts/db/backup_control_plane.sh
sh /app/scripts/db/restore_control_plane.sh

for table_name in registrations uploads r2_parent_tokens evaluations king_reigns evaluation_early_stopping_policies; do
    source_count=$(psql "$TEUTONIC_DATABASE_URL" --no-psqlrc --tuples-only --no-align \
        --command="SELECT count(*) FROM control_plane.$table_name")
    restore_count=$(psql "$TEUTONIC_RESTORE_DATABASE_URL" --no-psqlrc --tuples-only --no-align \
        --command="SELECT count(*) FROM control_plane.$table_name")
    if [ "$source_count" != "$restore_count" ]; then
        echo "restored row count differs for $table_name" >&2
        exit 1
    fi
done

restore_owner=$(
    psql "$TEUTONIC_RESTORE_DATABASE_URL" --no-psqlrc --tuples-only --no-align --command="
        SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'control_plane'"
)
if [ "$restore_owner" != "teutonic_schema_owner" ]; then
    echo "restored control_plane schema owner is incorrect: $restore_owner" >&2
    exit 1
fi

restored_grants=$(
    psql "$TEUTONIC_RESTORE_DATABASE_URL" --no-psqlrc --tuples-only --no-align --command="
        SELECT has_table_privilege('teutonic_validator', 'control_plane.evaluations', 'SELECT')
           AND NOT has_table_privilege(
               'teutonic_validator', 'control_plane.r2_parent_tokens', 'SELECT'
           )"
)
if [ "$restored_grants" != "t" ]; then
    echo "restored service grants are incorrect" >&2
    exit 1
fi

echo "backup/restore rows, ownership, and grants match"
