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
echo "Antes de continuar, habilite o login por código de dispositivo em:"
echo "ChatGPT > Configurações > Segurança > Login por código de dispositivo para o Codex CLI."
echo "Documentação: https://developers.openai.com/codex/auth"
echo ""
echo "Faça login com a mesma conta do ChatGPT quando o código de dispositivo aparecer."
echo "Nenhuma chave de API será solicitada ou gravada no projeto."
if ! docker compose run --rm --no-deps advisor codex login --device-auth; then
  echo ""
  echo "A autenticação não foi concluída. Confirme que o login por código de dispositivo"
  echo "está habilitado nas configurações de Segurança do ChatGPT e tente novamente."
  echo "Se continuar falhando, execute este diagnóstico e envie somente a saída do erro:"
  echo "docker compose run --rm --no-deps advisor node -e \"fetch('https://auth.openai.com').then(r=>console.log('HTTPS',r.status)).catch(e=>console.error(e.cause||e))\""
  exit 1
fi

docker compose up -d advisor app
docker compose exec advisor codex login status
docker compose ps

echo ""
echo "Codex conectado. O motor financeiro local continuará funcionando mesmo se o Codex estiver indisponível."
