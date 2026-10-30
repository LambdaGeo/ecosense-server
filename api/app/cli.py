"""Comandos de administração (rodar dentro do contêiner: docker compose exec api ...).

Estações
  python -m app.cli add-station estacao-01 --name "Sá Viana"
  python -m app.cli rotate-token estacao-01

Instalações (atribuição dos registros por GNSS)
  python -m app.cli add-site sa-viana estacao-01 --name "Sá Viana" \
      --lat -2.55582 --lon -44.30077 [--radius 1500] [--from 2026-09-27] [--to ...]
  python -m app.cli list-sites

Chaves de API para leitura
  python -m app.cli add-client agga --name "Agga Tech" --stations estacao-01 [--expires 2027-12-31]
  python -m app.cli revoke-client agga
  python -m app.cli list-clients

Dados
  python -m app.cli import-csv /data/planilha.csv [--station estacao-01] \
      [--server-tz America/Fortaleza] [--device-tz America/Fortaleza]
  python -m app.cli refresh-hourly
  python -m app.cli summary estacao-01 --start 2026-09-27 --end 2026-10-18
"""
import argparse
import csv
import secrets
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import db
from .normalize import normalize

LOCAL = ZoneInfo("America/Fortaleza")


def _local_date(s):
    """'2026-09-27' -> meia-noite local; ISO com hora é aceito como está."""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = datetime.combine(dt.date(), dt.time() if len(s) > 10 else time(0), LOCAL)
    return dt


# ── Estações ──────────────────────────────────────────────────────────────
def add_station(conn, a):
    token = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO stations (station_id, name, token_sha256) VALUES (%s, %s, %s)",
                 (a.station_id, a.name, db.sha256(token)))
    print(f"Estação {a.station_id} criada.\nToken (mostrado uma única vez): {token}\n"
          f"URL para o config.h: https://SEU_DOMINIO/ingest/{token}")


def rotate_token(conn, a):
    token = secrets.token_urlsafe(24)
    cur = conn.execute("UPDATE stations SET token_sha256 = %s WHERE station_id = %s",
                       (db.sha256(token), a.station_id))
    if cur.rowcount == 0:
        sys.exit(f"estação {a.station_id} não existe")
    print(f"Novo token (o anterior deixou de valer): {token}")


# ── Instalações ───────────────────────────────────────────────────────────
def add_site(conn, a):
    conn.execute(
        """INSERT INTO sites (site_id, station_id, name, lat, lon, radius_m, height_m,
                              valid_from, valid_to, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (site_id) DO UPDATE SET station_id = EXCLUDED.station_id,
             name = EXCLUDED.name, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
             radius_m = EXCLUDED.radius_m, height_m = EXCLUDED.height_m,
             valid_from = EXCLUDED.valid_from, valid_to = EXCLUDED.valid_to,
             notes = EXCLUDED.notes""",
        (a.site_id, a.station_id, a.name or a.site_id, a.lat, a.lon, a.radius, a.height,
         _local_date(getattr(a, "from")), _local_date(a.to), a.notes))
    print(f"Instalação {a.site_id} gravada.")


def list_sites(conn, a):
    for r in conn.execute("SELECT site_id, station_id, name, lat, lon, radius_m, valid_from, "
                          "valid_to FROM sites ORDER BY station_id, site_id"):
        print(*["" if v is None else v for v in r], sep="\t")


# ── Clientes da API ───────────────────────────────────────────────────────
def add_client(conn, a):
    key = "esm_" + secrets.token_urlsafe(32)
    stations = a.stations.split(",") if a.stations else None
    conn.execute(
        "INSERT INTO api_clients (client_id, name, key_sha256, stations, expires_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (a.client_id, a.name, db.sha256(key), stations, _local_date(a.expires)))
    print(f"Cliente {a.client_id} criado; estações: {stations or 'todas'}.\n"
          f"Chave (mostrada uma única vez): {key}\n"
          f"Uso: curl -H 'X-API-Key: {key}' https://SEU_DOMINIO/api/v1/stations")


def revoke_client(conn, a):
    cur = conn.execute("UPDATE api_clients SET revoked = true WHERE client_id = %s", (a.client_id,))
    if cur.rowcount == 0:
        sys.exit(f"cliente {a.client_id} não existe")
    print(f"Chave de {a.client_id} revogada.")


def list_clients(conn, a):
    for r in conn.execute("SELECT client_id, name, stations, expires_at, revoked, created_at "
                          "FROM api_clients ORDER BY client_id"):
        print(*["" if v is None else v for v in r], sep="\t")


# ── Dados ─────────────────────────────────────────────────────────────────
def import_csv(conn, a):
    server_tz = ZoneInfo(a.server_tz)
    device_tz = ZoneInfo(a.device_tz)
    total = new = 0
    batch = []
    with open(a.path, newline="", encoding="utf-8-sig") as f:
        for rec in csv.DictReader(f):
            rec = {k.strip(): v for k, v in rec.items() if k and v not in (None, "")}
            if not rec:
                continue
            batch.append(normalize(rec, source="csv", station_id=a.station,
                                   server_tz=server_tz, device_tz=device_tz))
            if len(batch) >= 1000:
                new += db.insert_rows(conn, batch)
                total += len(batch)
                batch.clear()
    new += db.insert_rows(conn, batch)
    total += len(batch)
    print(f"{total} linhas lidas, {new} novas inseridas.")
    db.refresh_hourly(conn)
    print("Agregados horários recalculados.")


def refresh_hourly(conn, a):
    db.refresh_hourly(conn)
    print("Agregados horários recalculados.")


def summary(conn, a):
    """Indicadores de integridade da campanha (equivalentes à Tabela 6 do artigo)."""
    db.refresh_hourly(conn)
    p = dict(st=a.station_id, start=_local_date(a.start), end=_local_date(a.end))
    w = "station_id = %(st)s AND observed_at >= %(start)s AND observed_at < %(end)s"
    q = lambda sql: conn.execute(sql, p).fetchall()  # noqa: E731

    (n_read, n_event), = q(f"SELECT count(*) FILTER (WHERE kind='reading'), "
                           f"count(*) FILTER (WHERE kind='event') FROM readings WHERE {w}")
    (n_dates,), = q(f"SELECT count(DISTINCT (observed_at AT TIME ZONE 'America/Fortaleza')::date) "
                    f"FROM readings WHERE {w} AND kind='reading'")
    (h_with,), = q(f"SELECT count(*) FROM readings_hourly WHERE station_id=%(st)s "
                   f"AND hour >= %(start)s AND hour < %(end)s")
    h_total = int((p["end"] - p["start"]).total_seconds() // 3600)
    (med_dt, n_gt5), = q(f"""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d), count(*) FILTER (WHERE d > 300)
        FROM (SELECT extract(epoch FROM observed_at - lag(observed_at) OVER (ORDER BY observed_at)) d
              FROM readings WHERE {w} AND kind='reading') t WHERE d IS NOT NULL""")
    sites = q(f"SELECT coalesce(site_id, '(indeterminada)'), count(*) FROM readings_located "
              f"WHERE {w} AND kind='reading' GROUP BY 1 ORDER BY 1")
    events = q(f"SELECT event_key, count(*) FROM readings WHERE {w} AND kind='event' "
               f"GROUP BY 1 ORDER BY 1")
    (n_pairs, med_sleep), = q(f"""
        SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY d) FROM (
          SELECT event_key, extract(epoch FROM lead(observed_at) OVER o - observed_at) d,
                 lead(event_key) OVER o AS nxt
          FROM readings WHERE {w} AND event_key IN ('ENTRANDO_DEEP_SLEEP','ACORDOU_DEEP_SLEEP')
          WINDOW o AS (ORDER BY observed_at)) t
        WHERE event_key='ENTRANDO_DEEP_SLEEP' AND nxt='ACORDOU_DEEP_SLEEP'""")
    (n_batt,), = q(f"SELECT count(status_bateria_v) FROM readings WHERE {w}")
    stats = q(f"""SELECT 'temperatura_c', count(temperatura_c), avg(temperatura_c), stddev(temperatura_c),
                         min(temperatura_c), max(temperatura_c),
                         percentile_cont(0.5) WITHIN GROUP (ORDER BY temperatura_c)
                  FROM readings WHERE {w} AND kind='reading'
                  UNION ALL
                  SELECT 'umidade_relativa_perc', count(umidade_relativa_perc), avg(umidade_relativa_perc),
                         stddev(umidade_relativa_perc), min(umidade_relativa_perc), max(umidade_relativa_perc),
                         percentile_cont(0.5) WITHIN GROUP (ORDER BY umidade_relativa_perc)
                  FROM readings WHERE {w} AND kind='reading'
                  UNION ALL
                  SELECT 'pressao_atmosferica_hpa', count(pressao_atmosferica_hpa), avg(pressao_atmosferica_hpa),
                         stddev(pressao_atmosferica_hpa), min(pressao_atmosferica_hpa),
                         max(pressao_atmosferica_hpa),
                         percentile_cont(0.5) WITHIN GROUP (ORDER BY pressao_atmosferica_hpa)
                  FROM readings WHERE {w} AND kind='reading'""")

    f2 = lambda v: "—" if v is None else f"{v:.2f}"  # noqa: E731
    print(f"Estação {a.station_id}, {p['start']:%d/%m/%Y} a {p['end']:%d/%m/%Y} (fim exclusivo, hora local)")
    print(f"  Leituras meteorológicas ............ {n_read}")
    print(f"  Mensagens de evento ................ {n_event}")
    print(f"  Datas com leituras ................. {n_dates}")
    print(f"  Horas civis com ao menos uma leitura {h_with} de {h_total} "
          f"({100 * h_with / h_total:.2f}%)" if h_total else "")
    print(f"  Intervalo mediano entre leituras ... {f2(med_dt)} s")
    print(f"  Intervalos entre leituras > 5 min .. {n_gt5}")
    for s, n in sites:
        print(f"  Leituras em {s:.<24} {n}")
    for e, n in events:
        print(f"  Evento {e or '(sem nome)':.<29} {n}")
    print(f"  Pares entrada–despertar ............ {n_pairs} (mediana {f2(med_sleep)} s)")
    print(f"  Tensão de bateria: valores numéricos {n_batt}")
    print("  Variável                     n      média     DP      mín      máx   mediana")
    for name, n, m, sd, mn, mx, md in stats:
        print(f"  {name:<24} {n:>6} {f2(m):>9} {f2(sd):>6} {f2(mn):>8} {f2(mx):>8} {f2(md):>8}")


def main():
    p = argparse.ArgumentParser(prog="app.cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add-station"); s.add_argument("station_id"); s.add_argument("--name")
    s.set_defaults(fn=add_station)
    s = sub.add_parser("rotate-token"); s.add_argument("station_id"); s.set_defaults(fn=rotate_token)

    s = sub.add_parser("add-site")
    s.add_argument("site_id"); s.add_argument("station_id"); s.add_argument("--name")
    s.add_argument("--lat", type=float, required=True); s.add_argument("--lon", type=float, required=True)
    s.add_argument("--radius", type=float, default=1500.0); s.add_argument("--height", type=float)
    s.add_argument("--from"); s.add_argument("--to"); s.add_argument("--notes")
    s.set_defaults(fn=add_site)
    sub.add_parser("list-sites").set_defaults(fn=list_sites)

    s = sub.add_parser("add-client"); s.add_argument("client_id"); s.add_argument("--name")
    s.add_argument("--stations", help="lista separada por vírgulas; omitir = todas")
    s.add_argument("--expires", help="data de validade (aaaa-mm-dd)")
    s.set_defaults(fn=add_client)
    s = sub.add_parser("revoke-client"); s.add_argument("client_id"); s.set_defaults(fn=revoke_client)
    sub.add_parser("list-clients").set_defaults(fn=list_clients)

    s = sub.add_parser("import-csv"); s.add_argument("path"); s.add_argument("--station")
    s.add_argument("--server-tz", default="America/Fortaleza",
                   help="fuso de data_servidor sem offset no CSV (padrão: fuso da planilha)")
    s.add_argument("--device-tz", default="America/Fortaleza",
                   help="fuso de timestamp_recebimento sem offset")
    s.set_defaults(fn=import_csv)
    sub.add_parser("refresh-hourly").set_defaults(fn=refresh_hourly)
    s = sub.add_parser("summary"); s.add_argument("station_id")
    s.add_argument("--start", required=True); s.add_argument("--end", required=True)
    s.set_defaults(fn=summary)

    a = p.parse_args()
    with db.connect() as conn:
        a.fn(conn, a)


if __name__ == "__main__":
    main()
