#!/bin/sh
set -eu

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker não encontrado. Instale Docker Engine e o plugin Compose antes de continuar."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 não encontrado."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  db_password="$(openssl rand -hex 24)"
  app_secret="$(openssl rand -hex 32)"
  file_key="$(openssl rand -base64 32 | tr '+/' '-_')"
  sed -i "s/CHANGE_ME_POSTGRES_PASSWORD/${db_password}/g" .env
  sed -i "s/CHANGE_ME_APPLICATION_SECRET/${app_secret}/g" .env
  sed -i "s#CHANGE_ME_FERNET_KEY#${file_key}#g" .env
  chmod 600 .env
  echo "Arquivo .env seguro criado. Guarde uma cópia offline."
fi

docker compose up -d --build
docker compose ps
echo "Sistema iniciado. Acesse http://IP_DO_SERVIDOR:${APP_PORT:-8080}"
