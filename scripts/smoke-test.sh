#!/usr/bin/env bash
# Teste de fumaça da pilha completa: cria estação, envia uma leitura como o
# firmware, lê de volta pela API e confere o Grafana. Uso: scripts/smoke-test.sh [URL]
set -euo pipefail
fail() { echo "FALHOU: $*"; exit 1; }
BASE=${1:-http://localhost}
ID="smoke-$(date +%s)"

TOKEN=$(docker compose exec -T api python -m app.cli add-station "$ID" --name "Teste" \
        | sed -n 's/^Token (mostrado uma única vez): //p')
[ -n "$TOKEN" ] || fail "criar estação"

NOW_D=$(date -u +%d/%m/%Y); NOW_T=$(date -u +%H:%M:%S)
BODY="{\"estacao_id\":\"$ID\",\"temperatura_c\":30.1,\"umidade_relativa_perc\":77,
\"pressao_atmosferica_hpa\":1008.2,\"gps_fix\":true,\"latitude\":-2.5558,\"longitude\":-44.3008,
\"data_gps\":\"$NOW_D\",\"hora_gps\":\"$NOW_T\"}"

echo "→ ingestão"
R=$(curl -sS -H 'Content-Type: text/plain' -d "$BODY" "$BASE/ingest/$TOKEN") || fail "ingestão: sem resposta"
echo "$R" | grep -q '"inserted":1' || fail "ingestão: $R"
echo; echo "→ token inválido deve ser recusado"
C=$(curl -s -o /dev/null -w '%{http_code}' -d "$BODY" "$BASE/ingest/errado"); [ "$C" = 401 ] || fail "token inválido retornou $C"
echo "→ leitura pela API"
R=$(curl -sS "$BASE/api/v1/readings?station=$ID"); echo "$R" | grep -q '"temperatura_c":30.1' || fail "readings: ${R:0:300}"
R=$(curl -sS "$BASE/api/v1/stations"); echo "$R" | grep -q "\"$ID\"" || fail "stations: ${R:0:300}"
echo "→ agregados horários"
docker compose exec -T api python -m app.cli refresh-hourly >/dev/null
R=$(curl -sS "$BASE/api/v1/hourly?station=$ID"); echo "$R" | grep -q '"n_records":1' || fail "hourly: ${R:0:300}"
echo "→ Grafana"
curl -fsS "$BASE/login" | grep -qi grafana || fail "Grafana não respondeu em /login"
echo "→ token não aparece nos logs do Caddy"
if docker compose logs caddy 2>/dev/null | grep -q "$TOKEN"; then fail "token apareceu no log do Caddy"; fi
echo "OK: pilha funcionando de ponta a ponta"
