import hashlib
import os

import psycopg

from .normalize import COLUMNS

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://ecosense:ecosense@db:5432/ecosense")

_INSERT = (
    f"INSERT INTO readings ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('%(' + c + ')s' for c in COLUMNS)}) "
    "ON CONFLICT DO NOTHING"
)


def connect():
    # sessão em UTC: tudo sai com offset explícito
    return psycopg.connect(DATABASE_URL, autocommit=True, options="-c TimeZone=UTC")


def insert_rows(conn, rows) -> int:
    """Insere linhas normalizadas; devolve quantas eram novas."""
    new = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(_INSERT, row)
            new += cur.rowcount
    return new


def sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def station_for_token(conn, token: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT station_id FROM stations WHERE token_sha256 = %s", (sha256(token),))
        r = cur.fetchone()
    return r[0] if r else None


def client_for_key(conn, key: str):
    """(client_id, stations | None) de uma chave válida, ou None."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT client_id, stations FROM api_clients
               WHERE key_sha256 = %s AND NOT revoked
                 AND (expires_at IS NULL OR expires_at > now())""",
            (sha256(key),))
        return cur.fetchone()


def refresh_hourly(conn):
    try:
        conn.execute("CALL refresh_continuous_aggregate('readings_hourly', NULL, NULL)")
    except psycopg.errors.UndefinedFunction:  # PostgreSQL sem TimescaleDB
        conn.execute("REFRESH MATERIALIZED VIEW readings_hourly")
