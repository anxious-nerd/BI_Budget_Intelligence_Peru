#!/usr/bin/env bash
# Crea la base, el modelo y los usuarios. Lo corre el servicio `mssql-init` del compose cada vez que
# se levanta el stack; todos los scripts son idempotentes, así que repetirlo es seguro.
set -euo pipefail

SQLCMD=/opt/mssql-tools18/bin/sqlcmd

for v in MSSQL_SA_PASSWORD MSSQL_DB MSSQL_ADMIN_USER MSSQL_ADMIN_PASSWORD MSSQL_READER_USER MSSQL_READER_PASSWORD; do
  if [ -z "${!v:-}" ]; then echo "Falta la variable $v en .env" >&2; exit 1; fi
done
for v in MSSQL_ADMIN_PASSWORD MSSQL_READER_PASSWORD; do
  case "${!v}" in *"'"*) echo "$v no puede contener comillas simples (')" >&2; exit 1;; esac
done

# -C: confía en el certificado autofirmado del contenedor · -b: aborta al primer error · -I: QUOTED_IDENTIFIER
for f in /sql/*.sql; do
  echo "== $(basename "$f")"
  "$SQLCMD" -S "${MSSQL_HOST:-mssql}" -U sa -P "$MSSQL_SA_PASSWORD" -C -b -I -i "$f" \
    -v MSSQL_DB="$MSSQL_DB" \
       MSSQL_ADMIN_USER="$MSSQL_ADMIN_USER"   MSSQL_ADMIN_PASSWORD="$MSSQL_ADMIN_PASSWORD" \
       MSSQL_READER_USER="$MSSQL_READER_USER" MSSQL_READER_PASSWORD="$MSSQL_READER_PASSWORD"
done
echo "Inicialización terminada."