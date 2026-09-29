#!/bin/sh
# One Postgres backup (docker-compose.prod.yml `backup` service; also `make prod-backup`).
#   /backups/daily/stockgrader-<local time>.dump   pg_dump custom format, kept BACKUP_KEEP_DAYS
#   /backups/monthly/                               first backup of each month, kept BACKUP_KEEP_MONTHS
# The dump is written to a temp file and checked with pg_restore --list before it gets its
# name, so a half-written file never looks like a backup; no backup is ever overwritten.
# Connection settings come from the PG* variables.
set -eu

dir="${BACKUP_DIR:-/backups}"
keep_days="${BACKUP_KEEP_DAYS:-14}"
keep_months="${BACKUP_KEEP_MONTHS:-12}"
umask 077
mkdir -p "$dir/daily" "$dir/monthly"

stamp=$(date +%Y-%m-%dT%H%M%S)
tmp=$(mktemp "$dir/.partial-XXXXXX")
trap 'rm -f "$tmp"' EXIT

pg_dump --format=custom --compress=6 --file="$tmp"
pg_restore --list "$tmp" >/dev/null

# Claim the name with ln, which fails if it exists: a backup is never overwritten, even by a
# concurrent run started in the same second.
name="stockgrader-$stamp.dump"
n=1
until ln "$tmp" "$dir/daily/$name" 2>/dev/null; do
  n=$((n + 1))
  [ "$n" -le 100 ] || { echo "cannot find a free backup name for $stamp" >&2; exit 1; }
  name="stockgrader-$stamp-$n.dump"
done
rm -f "$tmp"

month=$(date +%Y-%m)
if ! ls "$dir/monthly/stockgrader-$month-"*.dump >/dev/null 2>&1; then
  ln "$dir/daily/$name" "$dir/monthly/$name" 2>/dev/null || cp "$dir/daily/$name" "$dir/monthly/$name"
fi

# Retention: daily by age, monthly by count (names sort chronologically).
find "$dir/daily" -name 'stockgrader-*.dump' -type f -mtime +"$keep_days" -delete
count=$(ls -1 "$dir/monthly" | grep -c '^stockgrader-.*\.dump$' || true)
if [ "$count" -gt "$keep_months" ]; then
  ls -1 "$dir/monthly" | grep '^stockgrader-.*\.dump$' | sort | head -n $((count - keep_months)) |
    while read -r old; do rm -f "$dir/monthly/$old"; done
fi

size=$(du -h "$dir/daily/$name" | cut -f1)
date +%s > "$dir/.last-success"
echo "backup ok: daily/$name ($size)"
