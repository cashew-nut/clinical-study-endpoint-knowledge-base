from __future__ import annotations

import socket

import duckdb
import pytest

from clinical_endpoints.db import AactConnectionError, MissingAactCredentialsError, attach_aact, connect


def test_connect_creates_all_schemas(tmp_path):
    con = connect(tmp_path / "warehouse.duckdb")
    schemas = {
        row[0]
        for row in con.execute(
            "SELECT schema_name FROM information_schema.schemata"
        ).fetchall()
    }
    assert {"raw", "vocab", "conformed", "graph"} <= schemas
    con.close()


def test_attach_aact_raises_clear_error_when_credentials_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    # don't let a real .env in the repo leak into this test
    monkeypatch.setattr("clinical_endpoints.db.load_dotenv", lambda: None)

    con = connect(tmp_path / "warehouse.duckdb")
    with pytest.raises(MissingAactCredentialsError, match="PGHOST"):
        attach_aact(con)
    con.close()


def test_attach_aact_raises_clear_error_on_unreachable_host(tmp_path, monkeypatch):
    # bind a local socket to grab a free port, then close it so the connection
    # attempt below fails fast with "connection refused" instead of timing out
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", str(free_port))
    monkeypatch.setenv("PGDATABASE", "aact")
    monkeypatch.setenv("PGUSER", "someone")
    monkeypatch.setenv("PGPASSWORD", "secret")
    monkeypatch.setattr("clinical_endpoints.db.load_dotenv", lambda: None)

    con = connect(tmp_path / "warehouse.duckdb")
    try:
        con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
    except duckdb.Error:
        con.close()
        pytest.skip("postgres extension unavailable (no network in this environment)")

    with pytest.raises(AactConnectionError, match="Could not connect to AACT Postgres"):
        attach_aact(con)
    con.close()
