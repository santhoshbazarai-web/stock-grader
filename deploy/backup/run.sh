#!/bin/sh
# Scheduler for the `backup` service: one backup a day at BACKUP_AT (HH:MM) in BACKUP_TZ,
# plus one at start-up when the last successful backup is older than a day (or missing).
set -eu

at="${BACKUP_AT:-23:30}"
# POSIX TZ string, so no tzdata is needed in the image. IST is UTC+5:30 → "IST-5:30".
export TZ="${BACKUP_TZ:-IST-5:30}"
dir="${BACKUP_DIR:-/backups}"

case "$at" in
  [0-2][0-9]:[0-5][0-9]) date -d "2000-01-01 $at" >/dev/null 2>&1 || at=invalid ;;
  *) at=invalid ;;
esac
[ "$at" != invalid ] || { echo "BACKUP_AT must be HH:MM (00:00-23:59), got '${BACKUP_AT}'" >&2; exit 1; }

until pg_isready -q; do sleep 2; done

last=$(cat "$dir/.last-success" 2>/dev/null || echo 0)
if [ $(( $(date +%s) - last )) -gt 86400 ]; then
  /backup/backup.sh || echo "backup failed" >&2
fi

while :; do
  now=$(date +%s)
  next=$(date -d "$(date +%Y-%m-%d) $at" +%s)
  [ "$next" -gt "$now" ] || next=$((next + 86400))
  echo "next backup at $(date -d "@$next" '+%Y-%m-%d %H:%M %Z')"
  sleep $((next - now))
  /backup/backup.sh || echo "backup failed" >&2
done
