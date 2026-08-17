#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Uso: ./scripts/restore.sh caminho/backup.dump"
  exit 1
fi

backup_file="$1"
if [ ! -f "$backup_file" ]; then
  echo "Backup não encontrado: $backup_file"
  exit 1
fi

echo "A restauração substitui os dados atuais. Confirme digitando RESTAURAR:"
read -r confirmation
[ "$confirmation" = "RESTAURAR" ] || exit 1

docker compose exec -T db pg_restore --clean --if-exists --no-owner \
  --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" < "$backup_file"
