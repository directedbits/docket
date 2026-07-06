import pytest

from docket import config, db


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh, isolated SQLite store per test (DB path pointed at a tmp file)."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "docs.db"))
    db.init_db()
    return db
