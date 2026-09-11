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
  # Fase 4 (docs/WORK_ORDER_LOCAL_MFA_TOTP.md, "Separação de chaves"): must
  # be generated independently from file_key above -- reusing the same
  # random value (or the same sed placeholder) for both would defeat the
  # whole point of a key exclusive to encrypting the TOTP secret at rest.
  mfa_key="$(openssl rand -base64 32 | tr '+/' '-_')"
  advisor_secret="$(openssl rand -hex 32)"
  sed -i "s/CHANGE_ME_POSTGRES_PASSWORD/${db_password}/g" .env
  sed -i "s/CHANGE_ME_APPLICATION_SECRET/${app_secret}/g" .env
  sed -i "s#CHANGE_ME_FERNET_KEY#${file_key}#g" .env
  sed -i "s#CHANGE_ME_MFA_FERNET_KEY#${mfa_key}#g" .env
  sed -i "s/CHANGE_ME_ADVISOR_SECRET/${advisor_secret}/g" .env
  chmod 600 .env
  echo "Arquivo .env seguro criado. Guarde uma cópia offline."
fi

if ! grep -Eq '^MFA_ENCRYPTION_KEY=.{40,}$' .env || grep -q 'CHANGE_ME_MFA_FERNET_KEY' .env; then
  # Fase 4 (docs/WORK_ORDER_LOCAL_MFA_TOTP.md): an installation that
  # bootstrapped its `.env` before this slice has no `MFA_ENCRYPTION_KEY`
  # line at all -- `app.config.Settings` requires it with no default and
  # refuses to start without it, by design ("falhar explicitamente...
  # não cair para plaintext"). Generated the same way as `file_key` above,
  # but as its own independent random value, never reused across keys.
  mfa_key="$(openssl rand -base64 32 | tr '+/' '-_')"
  if grep -q '^MFA_ENCRYPTION_KEY=' .env; then
    sed -i "s#^MFA_ENCRYPTION_KEY=.*#MFA_ENCRYPTION_KEY=${mfa_key}#" .env
  else
    printf '\nMFA_ENCRYPTION_KEY=%s\n' "$mfa_key" >> .env
  fi
  chmod 600 .env
  echo "Chave de criptografia do MFA (MFA_ENCRYPTION_KEY) adicionada ao .env existente."
fi

if ! grep -Eq '^ADVISOR_SHARED_SECRET=.{32,}$' .env || grep -q 'CHANGE_ME_ADVISOR_SECRET' .env; then
  advisor_secret="$(openssl rand -hex 32)"
  if grep -q '^ADVISOR_SHARED_SECRET=' .env; then
    sed -i "s/^ADVISOR_SHARED_SECRET=.*/ADVISOR_SHARED_SECRET=${advisor_secret}/" .env
  else
    printf '\nADVISOR_ENABLED=true\nADVISOR_URL=http://advisor:8081\nADVISOR_SHARED_SECRET=%s\nADVISOR_TIMEOUT_SECONDS=75\nCODEX_MODEL=\nCODEX_TIMEOUT_MS=70000\nWHISPER_MODEL=tiny\nWHISPER_LANGUAGE=pt\n' "$advisor_secret" >> .env
  fi
  chmod 600 .env
  echo "Configuração segura do consultor adicionada ao .env existente."
fi

docker compose up -d --build
docker compose ps
echo "Sistema iniciado. Acesse http://IP_DO_SERVIDOR:${APP_PORT:-8080}"
