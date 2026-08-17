#!/bin/sh
set -eu

mkdir -p /backups

while true; do
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  pg_dump --host=db --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom \
    --file="/backups/family_finance_${timestamp}.dump"
  find /backups -type f -name 'family_finance_*.dump' -mtime "+${BACKUP_RETENTION_DAYS:-30}" -delete
  sleep 86400
done
