#!/bin/sh
set -eu

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker e Docker Compose v2 são necessários."
  exit 1
fi

if [ ! -f .env ]; then
  echo "Arquivo .env não encontrado. Execute primeiro: ./scripts/bootstrap.sh"
  exit 1
fi

if ! grep -Eq '^ADVISOR_SHARED_SECRET=.{32,}$' .env || grep -q 'CHANGE_ME_ADVISOR_SECRET' .env; then
  echo "O segredo interno do consultor não foi configurado. Execute ./scripts/bootstrap.sh."
  exit 1
fi

echo "Construindo o serviço isolado do Codex..."
docker compose build advisor

echo ""
echo "Faça login com a mesma conta do ChatGPT quando o código de dispositivo aparecer."
echo "Nenhuma chave de API será solicitada ou gravada no projeto."
docker compose run --rm --no-deps advisor codex login --device-auth

docker compose up -d advisor app
docker compose exec advisor codex login status
docker compose ps

echo ""
echo "Codex conectado. O motor financeiro local continuará funcionando mesmo se o Codex estiver indisponível."
