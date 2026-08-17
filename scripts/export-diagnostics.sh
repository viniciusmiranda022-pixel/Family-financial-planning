#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
  echo "Uso: ./scripts/export-diagnostics.sh AAAA-MM [PASTA_DE_SAIDA]"
  exit 2
fi

month="$1"
output_dir="${2:-./diagnostics}"

case "$month" in
  ????-??) ;;
  *)
    echo "Mês inválido. Use o formato AAAA-MM, por exemplo: 2026-08"
    exit 2
    ;;
esac

mkdir -p "$output_dir"
output_dir_abs="$(CDPATH= cd -- "$output_dir" && pwd)"

docker compose up -d db >/dev/null
docker compose run --rm -T --no-deps \
  --user "$(id -u):$(id -g)" \
  --entrypoint python \
  --volume "${output_dir_abs}:/diagnostics" \
  app -m app.cli.export_diagnostics "$month" --output-dir /diagnostics

echo "Arquivo disponível em: ${output_dir_abs}"
