"""Ingestão, controle de acesso, instalações, lacunas e importação de CSV."""
import csv
import io
import subprocess
import sys
import types

import pytest
from fastapi.testclient import TestClient

from app import cli, db, main


def reading(hora, temp=30.0, lat=-2.55582, lon=-44.30077, data="05/10/2026", **kw):
    return {"estacao_id": "estacao-01", "temperatura_c": temp, "umidade_relativa_perc": 78,
            "pressao_atmosferica_hpa": 1008.0, "gps_fix": True, "latitude": lat,
            "longitude": lon, "satellites": 8, "data_gps": data, "hora_gps": hora, **kw}


def event(hora, nome, data="05/10/2026"):
    return {"estacao_id": "estacao-01", "evento_energia": nome, "estado_energia": "DEEP_SLEEP"
            if nome == "ENTRANDO_DEEP_SLEEP" else "ATIVO", "tempo_sleep_programado_s": 600,
            "data_gps": data, "hora_gps": hora}


@pytest.fixture()
def setup(conn, monkeypatch, capsys):
    cli.add_station(conn, types.SimpleNamespace(station_id="estacao-01", name="Sá Viana"))
    cli.add_station(conn, types.SimpleNamespace(station_id="estacao-02", name="Outra"))
    out = capsys.readouterr().out
    tokens = [l.split(": ")[1] for l in out.splitlines() if l.startswith("Token")]
    for site, lat, lon in [("sa-viana", -2.55582, -44.30077),
                           ("jardim-tropical", -2.57521, -44.17429)]:
        cli.add_site(conn, types.SimpleNamespace(
            site_id=site, station_id="estacao-01", name=site, lat=lat, lon=lon, radius=1500,
            height=3.5, to=None, notes=None, **{"from": None}))
    monkeypatch.setattr(main, "PUBLIC_READ", False)
    return TestClient(main.app), tokens


def key_for(conn, capsys, client_id, stations=None, expires=None):
    cli.add_client(conn, types.SimpleNamespace(client_id=client_id, name=client_id,
                                               stations=stations, expires=expires))
    return [l.split(": ")[1] for l in capsys.readouterr().out.splitlines()
            if l.startswith("Chave")][0]


def test_ingestao_eventos_e_dedup(setup):
    c, (tok1, _) = setup
    last = reading("12:00:00")
    msgs = [reading("11:59:30"), last, event("12:00:00", "ENTRANDO_DEEP_SLEEP"),
            event("12:10:11", "ACORDOU_DEEP_SLEEP"), reading("12:10:30")]
    r = c.post(f"/ingest/{tok1}", content=__import__("json").dumps(msgs),
               headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and r.json()["inserted"] == 5
    # reenvio (ponte/CSV sobrepostos) não duplica
    assert c.post(f"/ingest/{tok1}", json=msgs).json()["inserted"] == 0
    assert c.post("/ingest/errado", json=msgs[0]).status_code == 401
    assert c.post(f"/ingest/{tok1}", content=b"{x").status_code == 400


def test_controle_de_acesso(setup, conn, capsys):
    c, (tok1, tok2) = setup
    c.post(f"/ingest/{tok1}", json=reading("12:00:00"))
    c.post(f"/ingest/{tok2}", json={**reading("12:00:00"), "estacao_id": "estacao-02"})

    assert c.get("/api/v1/stations").status_code == 401           # sem chave
    assert c.get("/api/v1/stations", headers={"X-API-Key": "x"}).status_code == 401

    k_all = key_for(conn, capsys, "lab")
    k_one = key_for(conn, capsys, "agga", stations="estacao-01")
    k_old = key_for(conn, capsys, "antiga", expires="2020-01-01")

    st = c.get("/api/v1/stations", headers={"X-API-Key": k_all}).json()
    assert {s["station_id"] for s in st} == {"estacao-01", "estacao-02"}
    st = c.get("/api/v1/stations", headers={"Authorization": f"Bearer {k_one}"}).json()
    assert [s["station_id"] for s in st] == ["estacao-01"]
    rows = c.get("/api/v1/readings", headers={"X-API-Key": k_one}).json()
    assert {r["station_id"] for r in rows} == {"estacao-01"}
    assert c.get("/api/v1/readings?station=estacao-02",
                 headers={"X-API-Key": k_one}).status_code == 403
    assert c.get("/api/v1/stations", headers={"X-API-Key": k_old}).status_code == 401

    cli.revoke_client(conn, types.SimpleNamespace(client_id="agga"))
    assert c.get("/api/v1/stations", headers={"X-API-Key": k_one}).status_code == 401

    log = conn.execute("SELECT client_id, path FROM access_log ORDER BY at").fetchall()
    assert ("agga", "/api/v1/readings") in log


def test_instalacoes_por_gnss(setup, conn, capsys):
    c, (tok1, _) = setup
    msgs = [reading("12:00:00"),                                  # Sá Viana
            reading("12:00:30", lat=-2.57521, lon=-44.17429),     # Jardim Tropical
            reading("12:01:00", lat=-2.40, lon=-44.30),           # longe de ambos
            {**reading("12:01:30"), "gps_fix": False}]            # sem fix
    c.post(f"/ingest/{tok1}", json=msgs)
    k = key_for(conn, capsys, "lab")
    rows = c.get("/api/v1/readings", headers={"X-API-Key": k}).json()
    assert [r["site_id"] for r in rows] == ["sa-viana", "jardim-tropical", None, None]
    assert c.get("/api/v1/readings?site=sa-viana", headers={"X-API-Key": k}).json()[0][
        "observed_local"].startswith("2026-10-05T09:00:00")


def test_status_sono_lacunas_e_horarios(setup, conn, capsys):
    c, (tok1, _) = setup
    msgs = [reading("12:00:00"), reading("12:00:30"), event("12:00:30", "ENTRANDO_DEEP_SLEEP"),
            event("12:10:41", "ACORDOU_DEEP_SLEEP"), reading("12:11:00"),
            reading("14:00:00")]  # lacuna não programada
    c.post(f"/ingest/{tok1}", json=msgs)
    k = key_for(conn, capsys, "lab")
    gaps = c.get("/api/v1/gaps", headers={"X-API-Key": k}).json()
    assert [(round(g["gap_s"]), g["programmed"]) for g in gaps] == [(611, True), (6540, False)]
    hourly = c.get("/api/v1/hourly", headers={"X-API-Key": k}).json()
    # só leituras entram nos agregados (eventos ficam de fora)
    assert [(h["hour_local"][:16], h["n_records"]) for h in hourly] == [
        ("2026-10-05T09:00", 3), ("2026-10-05T11:00", 1)]

    st = c.get("/api/v1/stations", headers={"X-API-Key": k}).json()[0]
    assert st["status"] == "sem_comunicacao" and st["last_reading_at"].startswith("2026-10-05")

    # estação que acabou de entrar em sono programado não aparece como offline
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc) - timedelta(seconds=300)
    c.post(f"/ingest/{tok1}", json=event(t.strftime("%H:%M:%S"), "ENTRANDO_DEEP_SLEEP",
                                         data=t.strftime("%d/%m/%Y")))
    st = c.get("/api/v1/stations", headers={"X-API-Key": k}).json()[0]
    assert st["status"] == "sono_programado"


def test_importacao_csv_da_planilha(setup, conn, tmp_path, capsys):
    c, _ = setup
    path = tmp_path / "planilha.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["data_servidor", "estacao_id", "timestamp_recebimento", "temperatura_c",
                    "umidade_relativa_perc", "pressao_atmosferica_hpa", "gps_fix",
                    "latitude", "longitude", "data_gps", "hora_gps", "evento_energia"])
        for i in range(120):  # 1 h de leituras a cada 30 s, sem GNSS válido
            m, s = divmod(i * 30, 60)
            w.writerow([f"27/09/2026 10:{m:02d}:{s + 1:02d}", "estacao-01",
                        f"27/09/2026 10:{m:02d}:{s:02d}", "31,5", "75", "1008,9", "FALSO",
                        "0", "0", "", "", ""])
        w.writerow(["27/09/2026 11:00:00", "estacao-01", "27/09/2026 11:00:00",
                    "", "", "", "", "", "", "", "", "ENTRANDO_DEEP_SLEEP"])
    args = types.SimpleNamespace(path=str(path), station=None,
                                 server_tz="America/Fortaleza", device_tz="America/Fortaleza")
    cli.import_csv(conn, args)
    cli.import_csv(conn, args)
    out = capsys.readouterr().out
    assert "121 linhas lidas, 121 novas" in out and "121 linhas lidas, 0 novas" in out

    cli.summary(conn, types.SimpleNamespace(station_id="estacao-01",
                                            start="2026-09-27", end="2026-09-28"))
    out = capsys.readouterr().out
    assert "Leituras meteorológicas ............ 120" in out
    assert "Mensagens de evento ................ 1" in out
    assert "Horas civis com ao menos uma leitura 1 de 24" in out
    assert "Intervalo mediano entre leituras ... 30.00 s" in out
    assert "(indeterminada)........." in out


@pytest.mark.parametrize("event_first", [True, False])
def test_sono_programado_independe_da_ordem_de_chegada(setup, conn, capsys, event_first):
    """Última leitura e ENTRANDO_DEEP_SLEEP no mesmo segundo GNSS: a lacuna
    seguinte é sono programado qualquer que seja a ordem de inserção."""
    c, (tok1, _) = setup
    last, sleep = reading("12:00:30"), event("12:00:30", "ENTRANDO_DEEP_SLEEP")
    wake, after = event("12:10:41", "ACORDOU_DEEP_SLEEP"), reading("12:10:41")
    msgs = [sleep, last, after, wake] if event_first else [last, sleep, wake, after]
    for m in msgs:  # uma requisição por mensagem, como o firmware
        c.post(f"/ingest/{tok1}", json=m)
    k = key_for(conn, capsys, "lab")
    gaps = c.get("/api/v1/gaps", headers={"X-API-Key": k}).json()
    assert [(round(g["gap_s"]), g["programmed"]) for g in gaps] == [(611, True)]
