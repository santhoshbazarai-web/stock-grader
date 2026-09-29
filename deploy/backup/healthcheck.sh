#!/bin/sh
# Healthy while the last successful backup is younger than BACKUP_MAX_AGE_HOURS (default 26).
last=$(cat "${BACKUP_DIR:-/backups}/.last-success" 2>/dev/null || echo 0)
[ $(( $(date +%s) - last )) -lt $(( ${BACKUP_MAX_AGE_HOURS:-26} * 3600 )) ]
