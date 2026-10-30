"""API do EcoSense Monitor.

Ingestão (token por estação, no caminho porque o firmware não envia cabeçalhos):
  POST /ingest/{token}

Leitura (pública ou por chave de API, conforme PUBLIC_READ):
  GET /api/v1/stations   última mensagem de cada estação e estado
  GET /api/v1/sites      instalações cadastradas
  GET /api/v1/readings   série bruta (json ou csv), com instalação atribuída
  GET /api/v1/hourly     agregados horários (só leituras, sem interpolação)
  GET /api/v1/gaps       intervalos sem mensagens > 5 min
  GET /health
"""
import csv
import io
import json
import os
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from . import db
from .normalize import normalize

PUBLIC_READ = os.environ.get("PUBLIC_READ", "true").lower() in {"1", "true", "yes"}
MAX_ROWS = 500_000
MAX_BODY = 256 * 1024  # 256 KiB por requisição de ingestão

app = FastAPI(title="EcoSense Monitor API", version="1.0.0")


# ── Controle de acesso ────────────────────────────────────────────────────
class Access:
    def __init__(self, client_id, stations):
        self.client_id = client_id
        self.stations = stations  # None = todas

    def check(self, station: str | None):
        if station and self.stations is not None and station not in self.stations:
            raise HTTPException(403, "chave sem acesso a esta estação")

    def scope(self):
        """Lista de estações permitidas para filtrar o SQL (None = todas)."""
        return self.stations


def read_access(request: Request,
                authorization: str | None = Header(default=None),
                x_api_key: str | None = Header(default=None)) -> Access:
    key = x_api_key or (authorization or "").removeprefix("Bearer ").strip() or None
    if key:
        with db.connect() as conn:
            c = db.client_for_key(conn, key)
        if c is None:
            raise HTTPException(401, "chave de API inválida, expirada ou revogada")
        return Access(*c)
    if PUBLIC_READ:
        return Access(None, None)
    raise HTTPException(401, "esta instância exige chave de API (X-API-Key ou Bearer)")


def _log(conn, access: Access, request: Request, n_rows: int):
    conn.execute(
        "INSERT INTO access_log (client_id, path, query, n_rows, remote) VALUES (%s, %s, %s, %s, %s)",
        (access.client_id, request.url.path, str(request.url.query) or None, n_rows,
         request.headers.get("x-forwarded-for") or (request.client.host if request.client else None)))


def _out(cols, rows, fmt, filename):
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else
                        (v.isoformat() if isinstance(v, datetime) else
                         json.dumps(v) if isinstance(v, (dict, list)) else v) for v in r])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})
    return JSONResponse(json.loads(json.dumps([dict(zip(cols, r)) for r in rows], default=_iso)))


def _iso(v):
    """Datas em ISO 8601 (com offset quando houver); o resto como texto."""
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _run(sql, params, access, request, fmt="json", filename="data.csv"):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
        _log(conn, access, request, len(rows))
    return _out(cols, rows, fmt, filename)


SCOPE = "(%(scope)s::text[] IS NULL OR station_id = ANY(%(scope)s))"


# ── Rotas ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/ingest/{token}")
async def ingest(token: str, request: Request):
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "requisição grande demais")
    try:  # aceita qualquer Content-Type (o firmware pode enviar text/plain)
        data = json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "JSON inválido")
    records = data if isinstance(data, list) else [data]
    if not records or not all(isinstance(r, dict) for r in records):
        raise HTTPException(400, "esperado um objeto JSON ou uma lista de objetos")

    with db.connect() as conn:
        station = db.station_for_token(conn, token)
        if station is None:
            raise HTTPException(401, "token desconhecido")
        rows = [normalize(r, source="ingest", station_id=station) for r in records]
        new = db.insert_rows(conn, rows)
    return {"status": "ok", "received": len(rows), "inserted": new}


@app.get("/api/v1/stations")
def stations(request: Request, access: Access = Depends(read_access)):
    sql = f"""
        SELECT s.station_id, s.name, m.observed_at AS last_message_at, m.kind AS last_kind,
               m.event_key AS last_event, m.tempo_sleep_programado_s,
               r.observed_at AS last_reading_at, r.temperatura_c, r.umidade_relativa_perc,
               r.pressao_atmosferica_hpa, r.rssi, r.site_id,
               CASE
                 WHEN m.observed_at IS NULL THEN 'sem_dados'
                 WHEN m.event_key = 'ENTRANDO_DEEP_SLEEP'
                      AND now() - m.observed_at
                          < make_interval(secs => coalesce(m.tempo_sleep_programado_s, 600) * 2 + 120)
                   THEN 'sono_programado'
                 WHEN now() - m.observed_at < interval '5 minutes' THEN 'online'
                 ELSE 'sem_comunicacao'
               END AS status
        FROM stations s
        LEFT JOIN LATERAL (SELECT * FROM readings x WHERE x.station_id = s.station_id
                           ORDER BY observed_at DESC LIMIT 1) m ON TRUE
        LEFT JOIN LATERAL (SELECT * FROM readings_located x WHERE x.station_id = s.station_id
                           AND x.kind = 'reading' ORDER BY observed_at DESC LIMIT 1) r ON TRUE
        WHERE {SCOPE.replace('station_id', 's.station_id')}
        ORDER BY s.station_id"""
    return _run(sql, {"scope": access.scope()}, access, request)


@app.get("/api/v1/sites")
def sites(request: Request, access: Access = Depends(read_access)):
    sql = f"""SELECT site_id, station_id, name, lat, lon, radius_m, height_m,
                     valid_from, valid_to, notes
              FROM sites WHERE {SCOPE} ORDER BY station_id, site_id"""
    return _run(sql, {"scope": access.scope()}, access, request)


@app.get("/api/v1/readings")
def readings(request: Request, access: Access = Depends(read_access),
             station: str | None = None, site: str | None = None,
             kind: str | None = Query(None, pattern="^(reading|event)$"),
             start: datetime | None = None, end: datetime | None = None,
             format: str = Query("json", pattern="^(json|csv)$"),
             include_raw: bool = False,
             limit: int = Query(10_000, ge=1, le=MAX_ROWS)):
    access.check(station)
    raw_col = ", raw" if include_raw else ""
    sql = f"""
        SELECT observed_at, observed_local, station_id, site_id, site_distance_m, kind,
               event_key, ts_source, received_at, ts_device_raw, ts_gnss, clock_skew_s,
               temperatura_c, umidade_relativa_perc, pressao_atmosferica_hpa,
               altitude_m, temperatura_bmp_c, status_bateria_v, rssi, gps_fix,
               latitude, longitude, altitude_gps_m, satellites, hdop,
               acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z,
               estado_energia, evento_energia, ciclo_deepsleep, motivo_despertar,
               tempo_ativo_s, tempo_sleep_programado_s, duracao_sleep_anterior_s,
               qc_flags, source{raw_col}
        FROM readings_located
        WHERE {SCOPE}
          AND (%(station)s::text IS NULL OR station_id = %(station)s)
          AND (%(site)s::text IS NULL OR site_id = %(site)s)
          AND (%(kind)s::text IS NULL OR kind = %(kind)s)
          AND (%(start)s::timestamptz IS NULL OR observed_at >= %(start)s)
          AND (%(end)s::timestamptz IS NULL OR observed_at < %(end)s)
        ORDER BY observed_at LIMIT %(limit)s"""
    params = dict(scope=access.scope(), station=station, site=site, kind=kind,
                  start=start, end=end, limit=limit)
    return _run(sql, params, access, request, format, "readings.csv")


@app.get("/api/v1/hourly")
def hourly(request: Request, access: Access = Depends(read_access),
           station: str | None = None,
           start: datetime | None = None, end: datetime | None = None,
           format: str = Query("json", pattern="^(json|csv)$")):
    access.check(station)
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM pg_extension WHERE extname='timescaledb'").fetchone():
            db.refresh_hourly(conn)  # sem Timescale não há atualização contínua
    sql = f"""
        SELECT station_id, hour, (hour AT TIME ZONE 'America/Fortaleza') AS hour_local,
               n_records, n_temp, n_rh, n_p, temperatura_c, umidade_relativa_perc,
               pressao_atmosferica_hpa, rssi, bateria_min_v
        FROM readings_hourly
        WHERE {SCOPE}
          AND (%(station)s::text IS NULL OR station_id = %(station)s)
          AND (%(start)s::timestamptz IS NULL OR hour >= %(start)s)
          AND (%(end)s::timestamptz IS NULL OR hour < %(end)s)
        ORDER BY station_id, hour"""
    params = dict(scope=access.scope(), station=station, start=start, end=end)
    return _run(sql, params, access, request, format, "hourly.csv")


@app.get("/api/v1/gaps")
def gaps(request: Request, access: Access = Depends(read_access),
         station: str | None = None, programmed: bool | None = None,
         start: datetime | None = None, end: datetime | None = None,
         format: str = Query("json", pattern="^(json|csv)$")):
    access.check(station)
    sql = f"""
        SELECT station_id, gap_start, gap_end, gap_s, programmed FROM gaps
        WHERE {SCOPE}
          AND (%(station)s::text IS NULL OR station_id = %(station)s)
          AND (%(programmed)s::boolean IS NULL OR programmed = %(programmed)s)
          AND (%(start)s::timestamptz IS NULL OR gap_end >= %(start)s)
          AND (%(end)s::timestamptz IS NULL OR gap_start < %(end)s)
        ORDER BY station_id, gap_start"""
    params = dict(scope=access.scope(), station=station, programmed=programmed,
                  start=start, end=end)
    return _run(sql, params, access, request, format, "gaps.csv")
