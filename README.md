# EcoSense Monitor — servidor aberto

Pilha auto-hospedável de ingestão, armazenamento, consulta e visualização dos
dados da estação EcoSense Monitor. Roda em qualquer máquina virtual Linux com
Docker e substitui o Google Apps Script e plataformas proprietárias, sem
alterar o código do firmware.

```
ESP32 ──HTTPS POST──▶ Caddy ──▶ API (FastAPI) ──▶ PostgreSQL + TimescaleDB ◀── Grafana
                     (TLS)      /ingest/<token>    leituras, eventos,           (painel)
                                /api/v1/*          instalações, agregados
```

| Componente | Função | Licença |
| --- | --- | --- |
| Caddy 2.10 | TLS automático (Let's Encrypt) e proxy reverso | Apache-2.0 |
| API (este repositório) | Ingestão, normalização, consulta e exportação | MIT |
| PostgreSQL 17 + TimescaleDB 2.21 | Série temporal e agregados horários | PostgreSQL / Apache-2.0 |
| Grafana OSS 12 | Painel público e por usuário | AGPL-3.0 |

## Requisitos

- VM Linux com 1 vCPU, 1–2 GB de RAM e 10 GB de disco
- Docker Engine e Docker Compose v2
- Um nome de domínio apontando para a VM, com as portas 80 e 443 abertas

## Instalação

```bash
git clone https://github.com/LambdaGeo/ecosense-server && cd ecosense-server
cp .env.example .env          # edite domínio e senhas
docker compose up -d --wait
docker compose exec api python -m app.cli add-station estacao-01 --name "EcoSense 01"
```

O último comando imprime o token da estação, mostrado uma única vez. O
`station_id` deve ser igual ao `estacao_id` configurado no firmware.

Cadastre as instalações para que cada leitura seja atribuída ao local pelo GNSS:

```bash
docker compose exec api python -m app.cli add-site sa-viana estacao-01 \
    --name "Sá Viana" --lat -2.55582 --lon -44.30077 --radius 1500 --height 3.5
docker compose exec api python -m app.cli add-site jardim-tropical estacao-01 \
    --name "Jardim Tropical" --lat -2.57521 --lon -44.17429 --radius 1500 --height 3.5
```

Painel: `https://SEU_DOMINIO/` · Documentação interativa da API:
`https://SEU_DOMINIO/docs`

## Conectar a estação

**Sem tocar na estação (ponte).** O Apps Script continua recebendo e o serviço
`bridge` copia as mensagens para o banco a cada minuto:

```bash
# APPSSCRIPT_URL=... no .env
docker compose --profile bridge up -d
```

O Apps Script devolve no máximo as 1000 mensagens mais recentes. Para o
histórico completo, exporte a planilha (Arquivo › Fazer download › CSV) e importe:

```bash
cp planilha.csv data/
docker compose exec api python -m app.cli import-csv /data/planilha.csv
```

**Ingestão direta (só configuração).** Em `config.h`:

```c
const char* APPSSCRIPT_URL = "https://SEU_DOMINIO/ingest/<token-da-estacao>";
```

A API responde `200` direto (sem o redirect 302 do Google), aceita qualquer
`Content-Type` e um objeto JSON ou uma lista. Mensagens repetidas são
descartadas, então ponte, CSV e ingestão direta podem se sobrepor.

## Tratamento dos dados

| Regra | Implementação |
| --- | --- |
| Nada é inventado | O JSON original fica em `raw`; não há interpolação nem preenchimento com o último valor válido |
| Valores inválidos | `null`, `""`, `NaN` e a sentinela `-1` viram `NULL` (exceto aceleração e giro, onde −1 é físico) |
| Faixa física | Fora da faixa do sensor (ex.: UR > 100 %) vira `NULL`; o motivo fica em `qc_flags` |
| Posição | Descartada sem `gps_fix` ou em (0, 0) |
| Leitura × evento | `kind = reading` se houver temperatura, umidade ou pressão válida; senão `event` |
| Tempo canônico | Hora GNSS se válida; senão `timestamp_recebimento` em `DEVICE_TZ`; senão a chegada. A fonte fica em `ts_source` e a defasagem em `clock_skew_s` |
| Instalação | Centro mais próximo dentro de `radius_m`, só com fix válido; senão indeterminada |
| Agregados horários | Só leituras; média das válidas por hora civil; horas sem leitura não aparecem |
| Lacunas | Intervalos > 5 min entre mensagens; `programmed = true` quando é um par `ENTRANDO_DEEP_SLEEP` → `ACORDOU_DEEP_SLEEP` |
| Fuso | Gravado como `timestamptz`; exportado em UTC com offset, e em hora local nas colunas `*_local` |

Se `clock_skew_s` ficar perto de ±10 800 s, o fuso configurado do relógio do
dispositivo está errado: ajuste `DEVICE_TZ` (ou `--device-tz`) e reimporte.

O dicionário de campos está em [`docs/modelo-de-dados.md`](docs/modelo-de-dados.md).

## API

| Rota | Uso |
| --- | --- |
| `POST /ingest/{token}` | Recebe o JSON do firmware |
| `GET /api/v1/stations` | Última mensagem, última leitura e estado de cada estação |
| `GET /api/v1/sites` | Instalações cadastradas |
| `GET /api/v1/readings` | Série bruta; filtros `station`, `site`, `kind`, `start`, `end`, `format=csv`, `include_raw` |
| `GET /api/v1/hourly` | Agregados horários |
| `GET /api/v1/gaps` | Interrupções; filtro `programmed=false` para as não programadas |
| `GET /health` | Verificação de funcionamento |

Exportar os dados de uma campanha, por exemplo para o depósito no Zenodo:

```bash
P="station=estacao-01&start=2026-09-27T00:00:00-03:00&end=2026-10-18T00:00:00-03:00&format=csv"
curl "https://SEU_DOMINIO/api/v1/readings?$P&include_raw=true&limit=500000" -o raw.csv
curl "https://SEU_DOMINIO/api/v1/hourly?$P" -o hourly.csv
curl "https://SEU_DOMINIO/api/v1/gaps?$P" -o gaps.csv
docker compose exec api python -m app.cli summary estacao-01 --start 2026-09-27 --end 2026-10-18
```

## Controle de acesso

| Camada | Mecanismo |
| --- | --- |
| Envio | Token por estação (só o hash é guardado); `rotate-token` invalida o anterior |
| Leitura pela API | Pública (`PUBLIC_READ=true`) ou por chave. Cada chave tem escopo de estações, validade opcional e pode ser revogada |
| Auditoria | Toda leitura pela API fica em `access_log` (cliente, rota, consulta, linhas, origem) |
| Painel | Grafana com visitante somente leitura (`GRAFANA_ANONYMOUS`) ou login obrigatório; usuários, equipes e permissões por painel |
| Banco | O Grafana usa um usuário somente leitura que não enxerga tokens, chaves nem o log de acesso |

```bash
docker compose exec api python -m app.cli add-client agga --name "Agga Tech" \
    --stations estacao-01 --expires 2027-12-31
curl -H "X-API-Key: <chave>" https://SEU_DOMINIO/api/v1/readings?station=estacao-01
docker compose exec api python -m app.cli revoke-client agga
```

O escopo por estação vale para a API. No Grafana, as permissões são por
painel e pasta; para restringir dados por estação no painel, crie painéis ou
pastas separados.

## Segurança

- O token de ingestão vai no caminho da URL porque o firmware atual não envia
  cabeçalhos extras. O Caddy omite esse trecho dos logs.
- O firmware usa `setInsecure()`. Com o certificado Let's Encrypt da VM, o
  próximo passo é fixar o certificado raiz ISRG Root X1 no firmware.
- Não há limite de requisições por cliente; se a instância for exposta a
  muitos consumidores, use um firewall ou um proxy com limitação de taxa.

## Administração

```bash
docker compose exec api python -m app.cli --help
docker compose exec db pg_dump -U ecosense ecosense | gzip > backup-$(date +%F).sql.gz
```

## Testes

```bash
cd api && pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                                    # só unitários
TEST_DATABASE_URL=postgresql://postgres@localhost/postgres pytest -q   # + integração
scripts/smoke-test.sh http://localhost                       # pilha completa no ar
```

A integração contínua (`.github/workflows/ci.yml`) roda os testes contra a
mesma imagem TimescaleDB do compose e sobe a pilha completa com o teste de
fumaça.

## Citação

Veja [`CITATION.cff`](CITATION.cff). Licença: MIT ([`LICENSE`](LICENSE)).
