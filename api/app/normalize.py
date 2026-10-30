"""Normalização de um registro de telemetria do EcoSense Monitor.

Regras:
- O payload original é preservado inteiro (coluna raw); nada é inventado,
  interpolado ou preenchido com o último valor válido.
- Valores inválidos (None, "", NaN, sentinela -1) viram NULL nas colunas tipadas.
- Valores fora da faixa física do sensor viram NULL e o motivo fica em qc_flags.
- Tempo canônico (observed_at): hora GNSS se válida; senão o relógio do
  dispositivo; senão a hora de chegada ao servidor. A fonte usada fica em
  ts_source e a diferença dispositivo − GNSS em clock_skew_s.
- Cada mensagem é classificada como 'reading' (traz ao menos uma variável
  meteorológica) ou 'event' (só estado do firmware). O valor de
  evento_energia entra na chave de deduplicação, de modo que a última
  leitura e um ENTRANDO_DEEP_SLEEP no mesmo segundo não colidem.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _tz(name: str, default: str):
    return ZoneInfo(os.environ.get(name, default))


# Fuso dos timestamps sem offset vindos do relógio do dispositivo
# (timestamp_recebimento). O firmware usa NTP com UTC−3.
DEVICE_TZ = _tz("DEVICE_TZ", "America/Fortaleza")
# Fuso dos timestamps sem offset gravados pelo servidor de origem
# (data_servidor). O JSON do Apps Script vem em UTC; o CSV exportado da
# planilha vem no fuso da planilha (use --server-tz na importação).
SERVER_TZ = _tz("SERVER_TZ", "UTC")

MET_FIELDS = ["temperatura_c", "umidade_relativa_perc", "pressao_atmosferica_hpa"]

# Campos em que -1 é usado como "inválido" pelo firmware/dashboard.
# acc/gyro ficam de fora: -1 g é um valor físico legítimo.
SENTINEL_FIELDS = {
    "temperatura_c", "umidade_relativa_perc", "pressao_atmosferica_hpa",
    "altitude_m", "temperatura_bmp_c", "status_bateria_v",
    "latitude", "longitude", "altitude_gps_m", "hdop",
}
FLOAT_FIELDS = [
    "temperatura_c", "umidade_relativa_perc", "pressao_atmosferica_hpa",
    "altitude_m", "temperatura_bmp_c", "status_bateria_v", "rssi",
    "latitude", "longitude", "altitude_gps_m", "hdop",
    "acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z",
    "tempo_ativo_s", "tempo_sleep_programado_s", "duracao_sleep_anterior_s",
]
# Faixas físicas de operação (folhas de dados DHT22, BMP180, NEO-6M)
RANGES = {
    "temperatura_c": (-40.0, 80.0),
    "umidade_relativa_perc": (0.0, 100.0),
    "pressao_atmosferica_hpa": (300.0, 1100.0),
    "temperatura_bmp_c": (-40.0, 85.0),
    "latitude": (-90.0, 90.0),
    "longitude": (-180.0, 180.0),
    "status_bateria_v": (0.0, 6.0),
    "rssi": (-120.0, 0.0),
}
SERVER_KEYS = {"data_servidor"}  # adicionados pelo servidor, fora do hash

COLUMNS = [
    "observed_at", "station_id", "kind", "event_key", "ts_source", "received_at",
    "ts_device_raw", "ts_gnss", "clock_skew_s", *FLOAT_FIELDS, "gps_fix",
    "satellites", "estado_energia", "evento_energia", "ciclo_deepsleep",
    "motivo_despertar", "qc_flags", "source", "payload_hash", "raw",
]

_DT_FORMATS = (
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S",
)


def _num(v, sentinel: bool):
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(str(v).strip().replace(",", ".")) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(n) or math.isinf(n):
        return None
    if sentinel and n == -1:
        return None
    return n


def _bool(v):
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return None
    return str(v).strip().lower() in {"1", "true", "sim", "yes", "verdadeiro"}


def _text(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse_ts(v, naive_tz=timezone.utc) -> datetime | None:
    """ISO 8601 ou dd/mm/aaaa hh:mm[:ss]; sem offset, assume naive_tz."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        base = s.split(".")[0]
        for fmt in _DT_FORMATS:
            try:
                dt = datetime.strptime(base, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(timezone.utc)


def parse_gnss(date_s, time_s) -> datetime | None:
    """data_gps + hora_gps, sempre UTC. Aceita dd/mm/aaaa e aaaa-mm-dd."""
    if not date_s or not time_s:
        return None
    d, t = str(date_s).strip(), str(time_s).strip().split(".")[0]
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(f"{d} {t}", fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # receptor sem fix costuma devolver 2000-01-01 ou 0/0/2000
        return dt if dt.year >= 2020 else None
    return None


def payload_hash(payload: dict) -> str:
    core = {k: v for k, v in payload.items() if k not in SERVER_KEYS}
    return hashlib.md5(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def normalize(payload: dict, *, source: str, station_id: str | None = None,
              received_at: datetime | None = None,
              server_tz=None, device_tz=None) -> dict:
    now = datetime.now(timezone.utc)
    received = (received_at
                or parse_ts(payload.get("data_servidor"), server_tz or SERVER_TZ)
                or now)

    ts_device_raw = payload.get("timestamp_recebimento") or payload.get("timestamp")
    ts_device = parse_ts(ts_device_raw, naive_tz=device_tz or DEVICE_TZ)
    ts_gnss = parse_gnss(payload.get("data_gps"), payload.get("hora_gps"))

    if ts_gnss is not None:
        observed, ts_source = ts_gnss, "gnss"
    elif ts_device is not None:
        observed, ts_source = ts_device, "device"
    else:
        observed, ts_source = received, "received"

    row = {
        "observed_at": observed,
        "station_id": station_id or str(payload.get("estacao_id") or "desconhecida"),
        "ts_source": ts_source,
        "received_at": received,
        "ts_device_raw": _text(ts_device_raw),
        "ts_gnss": ts_gnss,
        "clock_skew_s": (ts_device - ts_gnss).total_seconds()
                        if ts_device and ts_gnss else None,
        "gps_fix": _bool(payload.get("gps_fix")),
        "estado_energia": _text(payload.get("estado_energia")),
        "evento_energia": _text(payload.get("evento_energia")),
        "ciclo_deepsleep": None,
        "motivo_despertar": _text(payload.get("motivo_despertar")),
        "source": source,
        "payload_hash": payload_hash(payload),
        "raw": json.dumps(payload, default=str, ensure_ascii=False),
    }

    flags = {}
    for f in FLOAT_FIELDS:
        v = _num(payload.get(f), f in SENTINEL_FIELDS)
        if v is not None and f in RANGES:
            lo, hi = RANGES[f]
            if not lo <= v <= hi:
                flags[f] = "fora_da_faixa"
                v = None
        row[f] = v
    # (0, 0) é a posição padrão de receptor sem fix
    if row["latitude"] == 0 and row["longitude"] == 0:
        row["latitude"] = row["longitude"] = None
        flags["latitude"] = flags["longitude"] = "posicao_nula"
    if row["gps_fix"] is False:
        row["latitude"] = row["longitude"] = None

    sats = _num(payload.get("satellites"), sentinel=False)
    row["satellites"] = int(sats) if sats is not None else None
    ciclo = _num(payload.get("ciclo_deepsleep"), sentinel=False)
    row["ciclo_deepsleep"] = int(ciclo) if ciclo is not None else None

    row["kind"] = "reading" if any(row[f] is not None for f in MET_FIELDS) else "event"
    # chave que separa mensagens distintas no mesmo instante
    # (ex.: última leitura e ENTRANDO_DEEP_SLEEP no mesmo segundo GNSS)
    row["event_key"] = row["evento_energia"] or ""
    row["qc_flags"] = json.dumps(flags) if flags else None
    return row
