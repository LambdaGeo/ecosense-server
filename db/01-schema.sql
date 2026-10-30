-- EcoSense Monitor — esquema do banco
-- PostgreSQL >= 14. TimescaleDB é usado quando disponível (imagem do compose);
-- sem ele, o esquema funciona igual com tabela comum e visão materializada.
--
-- Princípio: o payload original é guardado sem alteração (raw);
-- as colunas tipadas são uma leitura normalizada dele.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
  END IF;
END $$;

-- ── Estações e tokens de ingestão (só o hash SHA-256) ─────────────────────
CREATE TABLE IF NOT EXISTS stations (
    station_id   text PRIMARY KEY,
    name         text,
    token_sha256 text UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ── Instalações: onde a estação esteve, identificado pelo GNSS ────────────
-- Uma estação pode ser realocada (ex.: Sá Viana -> Jardim Tropical).
-- Cada registro com fix válido é atribuído à instalação mais próxima cujo
-- centro está a até radius_m metros; valid_from/valid_to são opcionais.
CREATE TABLE IF NOT EXISTS sites (
    site_id     text PRIMARY KEY,
    station_id  text NOT NULL REFERENCES stations (station_id) ON UPDATE CASCADE,
    name        text NOT NULL,
    lat         double precision NOT NULL,
    lon         double precision NOT NULL,
    radius_m    double precision NOT NULL DEFAULT 1500,
    height_m    double precision,          -- altura do sensor acima do solo
    valid_from  timestamptz,
    valid_to    timestamptz,
    notes       text
);

-- ── Leituras e eventos ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS readings (
    observed_at     timestamptz NOT NULL,   -- tempo canônico (ver ts_source)
    station_id      text        NOT NULL,
    kind            text        NOT NULL CHECK (kind IN ('reading', 'event')),
    event_key       text        NOT NULL DEFAULT '',  -- evento_energia ou ''
    ts_source       text        NOT NULL CHECK (ts_source IN ('gnss', 'device', 'received')),
    received_at     timestamptz NOT NULL DEFAULT now(),
    ts_device_raw   text,                   -- timestamp_recebimento como veio
    ts_gnss         timestamptz,            -- data_gps + hora_gps (UTC)
    clock_skew_s    double precision,       -- relógio do dispositivo − GNSS

    temperatura_c            double precision,
    umidade_relativa_perc    double precision,
    pressao_atmosferica_hpa  double precision,   -- absoluta, nível da estação
    altitude_m               double precision,
    temperatura_bmp_c        double precision,
    status_bateria_v         double precision,
    rssi                     double precision,
    gps_fix                  boolean,
    latitude                 double precision,
    longitude                double precision,
    altitude_gps_m           double precision,
    satellites               integer,
    hdop                     double precision,
    acc_x double precision, acc_y double precision, acc_z double precision,
    gyro_x double precision, gyro_y double precision, gyro_z double precision,

    estado_energia            text,
    evento_energia            text,
    ciclo_deepsleep           integer,
    motivo_despertar          text,
    tempo_ativo_s             double precision,
    tempo_sleep_programado_s  double precision,
    duracao_sleep_anterior_s  double precision,

    qc_flags      jsonb,                    -- campos anulados e motivo
    source        text  NOT NULL,           -- 'ingest' | 'bridge' | 'csv'
    payload_hash  text  NOT NULL,           -- md5 do payload (auditoria)
    raw           jsonb NOT NULL
);

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    PERFORM create_hypertable('readings', 'observed_at', if_not_exists => TRUE);
  END IF;
END $$;

-- Uma mensagem por estação, instante, tipo e evento: a mesma mensagem vinda
-- da ponte, do CSV ou da ingestão direta não é duplicada.
CREATE UNIQUE INDEX IF NOT EXISTS readings_dedup
    ON readings (station_id, observed_at, kind, event_key);

-- ── Distância em metros (haversine) ───────────────────────────────────────
CREATE OR REPLACE FUNCTION distance_m(lat1 float8, lon1 float8, lat2 float8, lon2 float8)
RETURNS float8 LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT 2 * 6371008.8 * asin(sqrt(
      power(sin(radians(lat2 - lat1) / 2), 2)
    + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2)))
$$;

-- ── Leituras com instalação atribuída e hora civil local ──────────────────
-- site_id NULL = posição indeterminada (sem fix ou fora de todos os raios).
CREATE OR REPLACE VIEW readings_located AS
SELECT r.*,
       s.site_id,
       s.dist_m AS site_distance_m,
       (r.observed_at AT TIME ZONE 'America/Fortaleza') AS observed_local
FROM readings r
LEFT JOIN LATERAL (
    SELECT st.site_id, distance_m(r.latitude, r.longitude, st.lat, st.lon) AS dist_m
    FROM sites st
    WHERE st.station_id = r.station_id
      AND r.gps_fix IS TRUE AND r.latitude IS NOT NULL AND r.longitude IS NOT NULL
      AND (st.valid_from IS NULL OR r.observed_at >= st.valid_from)
      AND (st.valid_to   IS NULL OR r.observed_at <  st.valid_to)
      AND distance_m(r.latitude, r.longitude, st.lat, st.lon) <= st.radius_m
    ORDER BY dist_m
    LIMIT 1
) s ON TRUE;

-- ── Agregados horários: só leituras, média das válidas, sem interpolação ──
-- America/Fortaleza é UTC−3 fixo, então a hora UTC coincide com a civil local.
DO $$
DECLARE
  bucket text;
  opts   text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    bucket := 'time_bucket(''1 hour'', observed_at)';
    -- materialized_only = false: a hora corrente (incompleta) também aparece
    opts   := 'WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS';
  ELSE
    bucket := 'date_trunc(''hour'', observed_at)';
    opts   := 'AS';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_matviews WHERE matviewname = 'readings_hourly') THEN
    EXECUTE format($q$
      CREATE MATERIALIZED VIEW readings_hourly %s
      SELECT station_id,
             %s                            AS hour,
             count(*)                      AS n_records,
             count(temperatura_c)          AS n_temp,
             count(umidade_relativa_perc)  AS n_rh,
             count(pressao_atmosferica_hpa) AS n_p,
             avg(temperatura_c)            AS temperatura_c,
             avg(umidade_relativa_perc)    AS umidade_relativa_perc,
             avg(pressao_atmosferica_hpa)  AS pressao_atmosferica_hpa,
             avg(rssi)                     AS rssi,
             min(status_bateria_v)         AS bateria_min_v
      FROM readings
      WHERE kind = 'reading'
      GROUP BY station_id, %s
      WITH NO DATA$q$, opts, bucket, bucket);
  END IF;
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    PERFORM add_continuous_aggregate_policy('readings_hourly',
        start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 minute',
        schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);
  ELSE
    CREATE UNIQUE INDEX IF NOT EXISTS readings_hourly_pk ON readings_hourly (station_id, hour);
  END IF;
END $$;

-- ── Lacunas: intervalos entre mensagens consecutivas maiores que o esperado ─
-- Um sono programado normal aparece como ENTRANDO_DEEP_SLEEP seguido de
-- ACORDOU_DEEP_SLEEP; a coluna 'programmed' marca essas lacunas.
--
-- Ordem dentro do mesmo instante (empates de segundo GNSS são comuns):
-- ACORDOU_DEEP_SLEEP antes das leituras, ENTRANDO_DEEP_SLEEP depois delas.
-- Sem isso, a ordem entre a última leitura e a entrada em sono é arbitrária.
CREATE OR REPLACE FUNCTION message_order(event_key text)
RETURNS integer LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE event_key WHEN 'ACORDOU_DEEP_SLEEP' THEN 0
                        WHEN 'ENTRANDO_DEEP_SLEEP' THEN 2 ELSE 1 END
$$;

CREATE OR REPLACE VIEW gaps AS
SELECT station_id,
       prev_at   AS gap_start,
       observed_at AS gap_end,
       extract(epoch FROM observed_at - prev_at)::float8 AS gap_s,
       (prev_event = 'ENTRANDO_DEEP_SLEEP' AND event_key = 'ACORDOU_DEEP_SLEEP') AS programmed
FROM (
    SELECT station_id, observed_at, event_key,
           lag(observed_at) OVER w AS prev_at,
           lag(event_key)   OVER w AS prev_event
    FROM readings
    WINDOW w AS (PARTITION BY station_id
                 ORDER BY observed_at, message_order(event_key), kind, event_key)
) t
WHERE prev_at IS NOT NULL AND observed_at - prev_at > INTERVAL '5 minutes';

-- ── Clientes da API de leitura: chaves com escopo e validade ──────────────
CREATE TABLE IF NOT EXISTS api_clients (
    client_id   text PRIMARY KEY,
    name        text,
    key_sha256  text UNIQUE NOT NULL,
    stations    text[],                 -- NULL = todas as estações
    expires_at  timestamptz,            -- NULL = sem validade
    revoked     boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ── Auditoria de leituras pela API ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS access_log (
    at        timestamptz NOT NULL DEFAULT now(),
    client_id text,                     -- NULL = acesso público
    path      text NOT NULL,
    query     text,
    n_rows    integer,
    remote    text
);
CREATE INDEX IF NOT EXISTS access_log_at ON access_log (at DESC);
