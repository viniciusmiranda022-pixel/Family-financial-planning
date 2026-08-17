#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
  echo "Uso: ./scripts/import-plan.sh CAMINHO/Plano.xlsx [--apply] [--household NOME]"
  exit 2
fi

workbook="$1"
shift

if [ ! -f "$workbook" ]; then
  echo "Planilha não encontrada: $workbook"
  exit 2
fi

workbook_dir="$(CDPATH= cd -- "$(dirname -- "$workbook")" && pwd)"
workbook_name="$(basename -- "$workbook")"

docker compose run --rm -T --no-deps \
  --entrypoint python \
  --volume "${workbook_dir}:/local-import:ro" \
  app -m app.cli.import_plan "/local-import/${workbook_name}" "$@"
