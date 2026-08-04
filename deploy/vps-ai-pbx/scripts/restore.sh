#!/usr/bin/env bash
# deploy/vps-ai-pbx/scripts/restore.sh
#
# Inverse of backup.sh. DESTRUCTIVE — overwrites the live Postgres database,
# MinIO data, and FreeSWITCH config volume with the contents of a backup
# directory. Requires an explicit confirmation before touching anything.
#
# Usage: ./restore.sh <backup-dir>   (a directory produced by backup.sh,
#                                     e.g. ./backups/20260804T120000Z)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.vps.yml"
BACKUP_DIR="${1:-}"

info() { printf '\033[1;34m[restore]\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31m[restore]\033[0m %s\n' "$1" >&2; exit 1; }

[ -n "$BACKUP_DIR" ] || fail "Usage: $0 <backup-dir>"
[ -d "$BACKUP_DIR" ] || fail "No such directory: $BACKUP_DIR"
[ -f "$BACKUP_DIR/postgres.dump" ] || fail "$BACKUP_DIR does not look like a backup.sh output directory (missing postgres.dump)."

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

echo
echo "This will OVERWRITE the live stack's data with the contents of:"
echo "  $BACKUP_DIR"
echo "Specifically: the Postgres database, the entire MinIO data volume, and"
echo "the entire freeswitch-config volume. This cannot be undone unless you"
echo "have a separate, more recent backup."
echo
read -r -p "Type 'restore' to proceed: " confirmation
[ "$confirmation" = "restore" ] || fail "Aborted — confirmation not given."

PROJECT_NAME="$(compose config --format json | grep -o '"name": *"[^"]*"' | head -1 | sed -E 's/.*"([^"]+)"$/\1/')"
[ -n "$PROJECT_NAME" ] || fail "Could not resolve the Compose project name."

info "Stopping the stack..."
compose down

# 1. Postgres — pg_restore needs a running server, so bring only postgres
#    back up first, restore into it, then bring the rest of the stack up.
info "Starting Postgres alone for restore..."
compose up -d postgres
compose exec -T postgres sh -c 'until pg_isready -U postgres; do sleep 1; done'
info "Restoring Postgres from $BACKUP_DIR/postgres.dump (dropping and recreating public schema first)..."
compose exec -T postgres psql -U postgres -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
compose exec -T postgres pg_restore -U postgres -d postgres --no-owner < "$BACKUP_DIR/postgres.dump"

# 2. MinIO data volume — replace entirely.
if [ -f "$BACKUP_DIR/minio-data.tar.gz" ]; then
    info "Restoring MinIO data volume..."
    docker run --rm \
        -v "${PROJECT_NAME}_minio-data":/data \
        -v "$BACKUP_DIR":/backup \
        alpine sh -c 'rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xzf /backup/minio-data.tar.gz -C /data'
fi

# 3. FreeSWITCH config volume — replace entirely.
if [ -f "$BACKUP_DIR/freeswitch-config.tar.gz" ]; then
    info "Restoring freeswitch-config volume..."
    docker run --rm \
        -v "${PROJECT_NAME}_freeswitch-config":/data \
        -v "$BACKUP_DIR":/backup \
        alpine sh -c 'rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xzf /backup/freeswitch-config.tar.gz -C /data'
fi

info "Bringing the full stack back up..."
compose up -d

if [ -f "$BACKUP_DIR/env.snapshot" ]; then
    info "Note: $BACKUP_DIR/env.snapshot exists but was NOT applied automatically —"
    info "compare it against the live .env yourself before copying it over, in case"
    info "the live .env has since changed for reasons unrelated to this restore."
fi

info "Restore complete. Verify with: docker compose -f $COMPOSE_FILE ps"
