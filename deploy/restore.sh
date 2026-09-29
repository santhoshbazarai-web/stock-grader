#!/bin/sh
# Restore a backup into the production database (run on the host, from the repo root):
#   deploy/restore.sh backups/daily/stockgrader-2026-09-28T233000.dump [--yes]
# Takes a safety backup first, stops the API/worker/web, replaces every table from the dump in
# one transaction, applies any newer migrations, and starts the services again.
set -eu

file="${1:-}"
[ -n "$file" ] && [ -f "$file" ] || { echo "usage: $0 <backup.dump> [--yes]" >&2; exit 2; }
compose="docker compose -f docker-compose.prod.yml --env-file ${ENV_FILE:-.env.production}"

if [ "${2:-}" != "--yes" ]; then
  printf 'Replace ALL data in the production database with %s? [y/N] ' "$file"
  read -r answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || { echo "aborted"; exit 1; }
fi

echo "==> safety backup of the current database"
$compose exec -T backup /backup/backup.sh

echo "==> stopping api, worker, web"
$compose stop api worker web

echo "==> restoring $file"
$compose exec -T db sh -c \
  'pg_restore --clean --if-exists --no-owner --no-privileges --single-transaction --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$file"

echo "==> migrations and restart"
$compose up -d --wait
echo "restored $file"
