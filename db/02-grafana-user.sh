#!/bin/bash
# Cria o usuário somente leitura do Grafana com a senha do .env.
# Ele não enxerga tokens, chaves de API nem o log de acesso.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE grafana_ro LOGIN PASSWORD '${GRAFANA_DB_PASSWORD}';
GRANT SELECT ON readings, readings_located, readings_hourly, gaps, sites TO grafana_ro;
GRANT SELECT (station_id, name, created_at) ON stations TO grafana_ro;
GRANT EXECUTE ON FUNCTION distance_m(float8, float8, float8, float8) TO grafana_ro;
SQL
