from __future__ import annotations

import pytest

from ceskb.classify.engine import classify_all
from ceskb.ingest.pipeline import ingest
from ceskb.ingest.sources import FixtureSource
from ceskb.project.usdm import project_all
from ceskb.store.db import connect, initialise, load_vocabulary_into_db
from ceskb.vocab.loader import load_vocabulary


@pytest.fixture(scope="session")
def vocab():
    return load_vocabulary()


@pytest.fixture(scope="session")
def built_db(tmp_path_factory, vocab):
    """A database with the fixture corpus ingested, classified and projected."""
    path = tmp_path_factory.mktemp("ceskb") / "test.duckdb"
    with connect(path) as conn:
        initialise(conn)
        load_vocabulary_into_db(conn, vocab)
        ingest(conn, FixtureSource(), write_bronze=False)
        classify_all(conn, vocab=vocab)
        project_all(conn, vocab=vocab)
    return path


@pytest.fixture
def conn(built_db):
    with connect(built_db, read_only=True) as connection:
        yield connection
