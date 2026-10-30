"""Testes de integração usam um PostgreSQL real.

Defina TEST_DATABASE_URL (ex.: postgresql://postgres@localhost:5432/postgres).
O banco ecosense_test é recriado a cada sessão. Sem a variável, esses testes
são pulados e só os testes unitários rodam.
"""
import os
import pathlib

import psycopg
import pytest

SCHEMA = pathlib.Path(__file__).resolve().parents[2] / "db" / "01-schema.sql"


@pytest.fixture(scope="session")
def database_url():
    admin = os.environ.get("TEST_DATABASE_URL")
    if not admin:
        pytest.skip("TEST_DATABASE_URL não definida")
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS ecosense_test WITH (FORCE)")
        c.execute("CREATE DATABASE ecosense_test")
    url = psycopg.conninfo.make_conninfo(admin, dbname="ecosense_test")
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(SCHEMA.read_text())
    return url


@pytest.fixture()
def conn(database_url, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATABASE_URL", database_url)
    with db.connect() as c:
        c.execute("TRUNCATE readings, sites, stations, api_clients, access_log CASCADE")
        yield c
