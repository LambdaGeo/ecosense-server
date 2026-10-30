#!/usr/bin/env bash
# Teste de fumaça da pilha completa: cria estação, envia uma leitura como o
# firmware, lê de volta pela API e confere o Grafana. Uso: scripts/smoke-test.sh [URL]
set -euo pipefail
BASE=${1:-http://localhost}
ID="smoke-$(date +%s)"

TOKEN=$(docker compose exec -T api python -m app.cli add-station "$ID" --name "Teste" \
        | sed -n 's/^Token (mostrado uma única vez): //p')
[ -n "$TOKEN" ] || { echo "falha ao criar estação"; exit 1; }

NOW_D=$(date -u +%d/%m/%Y); NOW_T=$(date -u +%H:%M:%S)
BODY="{\"estacao_id\":\"$ID\",\"temperatura_c\":30.1,\"umidade_relativa_perc\":77,
\"pressao_atmosferica_hpa\":1008.2,\"gps_fix\":true,\"latitude\":-2.5558,\"longitude\":-44.3008,
\"data_gps\":\"$NOW_D\",\"hora_gps\":\"$NOW_T\"}"

echo "→ ingestão"
curl -fsS -H 'Content-Type: text/plain' -d "$BODY" "$BASE/ingest/$TOKEN" | tee /dev/stderr | grep -q '"inserted":1'
echo; echo "→ token inválido deve ser recusado"
[ "$(curl -s -o /dev/null -w '%{http_code}' -d "$BODY" "$BASE/ingest/errado")" = 401 ]
echo "→ leitura pela API"
curl -fsS "$BASE/api/v1/readings?station=$ID" | grep -q '"temperatura_c":30.1'
curl -fsS "$BASE/api/v1/stations" | grep -q "\"$ID\""
echo "→ agregados horários"
docker compose exec -T api python -m app.cli refresh-hourly >/dev/null
curl -fsS "$BASE/api/v1/hourly?station=$ID" | grep -q '"n_records":1'
echo "→ Grafana"
curl -fsS "$BASE/login" | grep -qi grafana
echo "→ token não aparece nos logs do Caddy"
! docker compose logs caddy 2>/dev/null | grep -q "$TOKEN"
echo "OK: pilha funcionando de ponta a ponta"
