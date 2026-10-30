import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.normalize import normalize, parse_gnss, parse_ts

UTC = timezone.utc
LOCAL = ZoneInfo("America/Fortaleza")

BASE = {
    "estacao_id": "estacao-01", "estacao_nome": "Sá Viana",
    "timestamp_recebimento": "2026-10-01T21:00:10",
    "temperatura_c": 30.2, "umidade_relativa_perc": 78.0,
    "pressao_atmosferica_hpa": 1009.4, "altitude_m": 32.1,
    "acc_x": 0.01, "acc_y": -0.02, "acc_z": -1.0,
    "rssi": -67, "gps_fix": True, "latitude": -2.5752, "longitude": -44.1742,
    "satellites": 8, "hdop": 1.1, "data_gps": "02/10/2026", "hora_gps": "00:00:05",
}


def test_gnss_e_tempo_canonico():
    r = normalize(BASE, source="ingest", device_tz=LOCAL)
    assert r["ts_source"] == "gnss"
    assert r["observed_at"] == datetime(2026, 10, 2, 0, 0, 5, tzinfo=UTC)
    assert r["clock_skew_s"] == 5.0  # 21:00:10 local = 00:00:10 UTC


def test_sem_gnss_usa_dispositivo_em_utc_menos_3():
    r = normalize({**BASE, "data_gps": "", "hora_gps": ""}, source="ingest", device_tz=LOCAL)
    assert r["ts_source"] == "device"
    assert r["observed_at"] == datetime(2026, 10, 2, 0, 0, 10, tzinfo=UTC)


def test_sem_tempo_algum_usa_chegada():
    p = {k: v for k, v in BASE.items() if k not in ("timestamp_recebimento", "data_gps")}
    now = datetime(2026, 10, 8, tzinfo=UTC)
    r = normalize(p, source="ingest", received_at=now)
    assert (r["ts_source"], r["observed_at"]) == ("received", now)


def test_datas_no_formato_da_planilha():
    """Bug da versão 0.1: dd/mm/aaaa virava a hora da importação."""
    p = {"estacao_id": "estacao-01", "data_servidor": "27/09/2026 10:00:02",
         "timestamp_recebimento": "27/09/2026 10:00:00", "temperatura_c": "31,5"}
    r = normalize(p, source="csv", server_tz=LOCAL, device_tz=LOCAL)
    assert r["ts_source"] == "device"
    assert r["observed_at"] == datetime(2026, 9, 27, 13, 0, 0, tzinfo=UTC)
    assert r["received_at"] == datetime(2026, 9, 27, 13, 0, 2, tzinfo=UTC)
    assert parse_ts("27/09/2026 10:00", LOCAL) == datetime(2026, 9, 27, 13, 0, tzinfo=UTC)


def test_sentinela_e_invalidos_viram_null_mas_acc_menos_1_fica():
    p = {**BASE, "temperatura_c": -1, "umidade_relativa_perc": "nan",
         "pressao_atmosferica_hpa": None, "hdop": ""}
    r = normalize(p, source="ingest")
    assert r["temperatura_c"] is None and r["umidade_relativa_perc"] is None
    assert r["pressao_atmosferica_hpa"] is None and r["hdop"] is None
    assert r["acc_z"] == -1.0
    assert '"temperatura_c": -1' in r["raw"]  # o bruto é preservado
    assert r["kind"] == "event"  # sem variável meteorológica válida


def test_fora_da_faixa_vira_null_com_motivo():
    r = normalize({**BASE, "umidade_relativa_perc": 130, "pressao_atmosferica_hpa": 50},
                  source="ingest")
    assert r["umidade_relativa_perc"] is None and r["pressao_atmosferica_hpa"] is None
    flags = json.loads(r["qc_flags"])
    assert flags == {"umidade_relativa_perc": "fora_da_faixa",
                     "pressao_atmosferica_hpa": "fora_da_faixa"}
    assert r["temperatura_c"] == 30.2 and r["kind"] == "reading"


def test_posicao_sem_fix_e_zero_zero_sao_descartadas():
    r = normalize({**BASE, "gps_fix": False}, source="ingest")
    assert r["latitude"] is None and r["longitude"] is None
    r = normalize({**BASE, "latitude": 0, "longitude": 0}, source="ingest")
    assert r["latitude"] is None and "posicao_nula" in r["qc_flags"]


def test_evento_e_leitura_no_mesmo_segundo_tem_chaves_distintas():
    """Bug da versão 0.1: o evento era descartado como duplicata."""
    ev = {"estacao_id": "estacao-01", "data_gps": "05/10/2026", "hora_gps": "12:00:00",
          "estado_energia": "DEEP_SLEEP", "evento_energia": "ENTRANDO_DEEP_SLEEP",
          "ciclo_deepsleep": "12", "tempo_sleep_programado_s": 600}
    a = normalize({**BASE, "data_gps": "05/10/2026", "hora_gps": "12:00:00"}, source="ingest")
    b = normalize(ev, source="ingest")
    assert a["observed_at"] == b["observed_at"]
    assert (a["kind"], a["event_key"]) == ("reading", "")
    assert (b["kind"], b["event_key"]) == ("event", "ENTRANDO_DEEP_SLEEP")
    assert b["ciclo_deepsleep"] == 12 and b["tempo_sleep_programado_s"] == 600


def test_strings_da_planilha():
    r = normalize({**BASE, "temperatura_c": "30,5", "gps_fix": "TRUE", "satellites": "8"},
                  source="csv")
    assert r["temperatura_c"] == 30.5 and r["gps_fix"] is True and r["satellites"] == 8


def test_gnss_sem_fix_e_ignorado():
    assert parse_gnss("01/01/2000", "00:00:00") is None
    assert parse_gnss("2026-10-02", "00:00:05.00") is not None


def test_hash_ignora_data_servidor():
    a = normalize(BASE, source="ingest")
    b = normalize({**BASE, "data_servidor": "2026-10-02T00:00:07Z"}, source="bridge")
    assert a["payload_hash"] == b["payload_hash"]
