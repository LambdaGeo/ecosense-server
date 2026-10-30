# Alterações

## 1.0.0 — 2026-10-30

Primeira versão para publicação.

- Mensagens classificadas como leitura ou evento de energia; `evento_energia`,
  `ciclo_deepsleep`, `motivo_despertar` e tempos de sono em colunas próprias.
- Deduplicação por estação, instante, tipo e evento: a última leitura e o
  `ENTRANDO_DEEP_SLEEP` no mesmo segundo GNSS não colidem mais.
- Importação de datas `dd/mm/aaaa hh:mm:ss` da planilha, com fusos
  configuráveis (`--server-tz`, `--device-tz`).
- Controle de faixa física (DHT22, BMP180, GNSS): valores fora da faixa viram
  nulos e o motivo fica em `qc_flags`; posição sem fix ou (0, 0) é descartada.
- Instalações (`sites`) e atribuição de cada leitura por GNSS
  (`readings_located`), para estações realocadas.
- Visão `gaps` com interrupções, separando sono programado de falhas.
- Chaves de API com escopo por estação, validade e revogação; registro de
  acesso (`access_log`); `PUBLIC_READ` substitui o `READ_TOKEN` global.
- Rota `/api/v1/gaps`; `/api/v1/stations` informa estado (online, sono
  programado, sem comunicação).
- Comando `summary` com os indicadores de integridade de uma campanha.
- Painel Grafana revisto para operação com sono profundo.
- TimescaleDB opcional: o esquema roda também em PostgreSQL puro.
- Testes de integração, CI com a pilha completa e teste de fumaça.

## 0.1.0 — 2026-10-08

Esqueleto inicial: ingestão, ponte com o Apps Script, importação de CSV e Grafana.
