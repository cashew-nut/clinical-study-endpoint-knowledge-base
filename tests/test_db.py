from __future__ import annotations

import pytest

from clinical_endpoints.db import MissingAactCredentialsError, attach_aact, connect


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
