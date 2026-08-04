#!/usr/bin/env bash
# deploy/vps-ai-pbx/scripts/backup.sh
#
# Timestamped backup of everything this deployment needs to be restorable
# from: Postgres (pg_dump -Fc, includes pgvector data), the minio-data
# volume, and the freeswitch-config volume (captures anything an operator
# hand-added post-deploy, e.g. a second gateway — see BACKUP_RESTORE.md).
#
# Usage: ./backup.sh [output-dir]   (default: ./backups)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.vps.yml"
OUTPUT_DIR="${1:-$SCRIPT_DIR/../backups}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$OUTPUT_DIR/$TIMESTAMP"

info() { printf '\033[1;34m[backup]\033[0m %s\n' "$1"; }

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

mkdir -p "$BACKUP_DIR"
info "Writing backup to $BACKUP_DIR"

# Resolve the actual (project-prefixed) Docker volume names rather than
# guessing the prefix — Compose prefixes every named volume with its project
# name (normally this directory's basename, but overridable via
# COMPOSE_PROJECT_NAME/-p), so "minio-data" in docker-compose.vps.yml is not
# necessarily the real volume name docker itself uses.
PROJECT_NAME="$(compose config --format json | grep -o '"name": *"[^"]*"' | head -1 | sed -E 's/.*"([^"]+)"$/\1/')"
[ -n "$PROJECT_NAME" ] || { echo "backup.sh: could not resolve the Compose project name" >&2; exit 1; }

# 1. Postgres — custom format (-Fc), the standard choice for pg_restore
#    compatibility across Postgres versions.
info "Dumping Postgres..."
compose exec -T postgres pg_dump -U postgres -Fc postgres > "$BACKUP_DIR/postgres.dump"

# 2. MinIO — tar the whole data volume directly (simpler and more complete
#    than `mc mirror` for a full-volume backup; see BACKUP_RESTORE.md's
#    "Future: per-tenant data" section for selective per-tenant export via
#    mc mirror instead, once that's needed). Uses a throwaway alpine
#    container rather than `docker compose exec minio` because the minio
#    image itself has no tar/shell utilities to run one with.
info "Archiving MinIO data volume..."
docker run --rm \
    -v "${PROJECT_NAME}_minio-data":/data:ro \
    -v "$BACKUP_DIR":/backup \
    alpine tar czf /backup/minio-data.tar.gz -C /data .

# 3. FreeSWITCH config volume — captures anything beyond the rendered
#    bootstrap set (see docker-compose.vps.yml's freeswitch-config volume
#    comment).
info "Archiving freeswitch-config volume..."
docker run --rm \
    -v "${PROJECT_NAME}_freeswitch-config":/data:ro \
    -v "$BACKUP_DIR":/backup \
    alpine tar czf /backup/freeswitch-config.tar.gz -C /data .

# 4. .env — NOT the secrets inside it re-derived, just the file itself, so
#    a restore can recreate an identical stack. Contains secrets: this
#    backup directory must be protected the same way .env itself is.
if [ -f "$SCRIPT_DIR/../.env" ]; then
    cp "$SCRIPT_DIR/../.env" "$BACKUP_DIR/env.snapshot"
    chmod 600 "$BACKUP_DIR/env.snapshot"
fi

info "Backup complete: $BACKUP_DIR"
info "Contains secrets (.env snapshot, DB dump) — store it as securely as .env itself, see BACKUP_RESTORE.md."
