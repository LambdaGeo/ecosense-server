"""Ponte de transição: copia leituras do Apps Script atual para o banco.

Não exige nenhuma mudança na estação. A cada BRIDGE_INTERVAL_S segundos,
consulta APPSSCRIPT_URL?limit=1000 (teto do script atual) e insere o que for
novo. As duplicatas são descartadas pelo índice único.

Uso: python -m app.bridge            (laço contínuo)
     python -m app.bridge --once     (uma única passada)
"""
import json
import logging
import os
import sys
import time
import urllib.request

from . import db
from .normalize import normalize

URL = os.environ.get("APPSSCRIPT_URL", "")
INTERVAL = int(os.environ.get("BRIDGE_INTERVAL_S", "60"))
LIMIT = int(os.environ.get("BRIDGE_LIMIT", "1000"))

log = logging.getLogger("bridge")


def fetch():
    sep = "&" if "?" in URL else "?"
    req = urllib.request.Request(f"{URL}{sep}limit={LIMIT}&t={int(time.time())}",
                                 headers={"User-Agent": "ecosense-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:  # segue o redirect 302 do Google
        data = json.loads(r.read().decode("utf-8-sig"))
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict)]


def run_once(conn) -> tuple[int, int]:
    records = fetch()
    rows = [normalize(r, source="bridge") for r in records]
    return len(rows), db.insert_rows(conn, rows)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not URL:
        log.error("APPSSCRIPT_URL não definida; nada a fazer")
        sys.exit(1)
    once = "--once" in sys.argv
    while True:
        try:
            with db.connect() as conn:
                got, new = run_once(conn)
            log.info("recebidos=%d novos=%d", got, new)
        except Exception as e:  # rede ou Apps Script fora do ar: tenta de novo
            log.warning("falha: %s", e)
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
