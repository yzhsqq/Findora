"""Database upgrades preserve the existing ledger without copying WAL files."""
import sqlite3

import pytest

from app.infrastructure.settings import _database_url


@pytest.fixture(autouse=True)
def default_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MYSQL_URL", raising=False)


def test_new_directory_selects_findora(tmp_path):
    assert _database_url(tmp_path) == f"sqlite+aiosqlite:///{tmp_path / 'findora.db'}"


def test_legacy_database_keeps_uncheckpointed_records(tmp_path):
    legacy = tmp_path / "globex.db"
    with sqlite3.connect(legacy) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE orders (id TEXT)")
        writer.execute("INSERT INTO orders VALUES ('old-order')")
        writer.commit()
        selected = _database_url(tmp_path).removeprefix("sqlite+aiosqlite:///")
        with sqlite3.connect(selected) as reader:
            assert reader.execute("SELECT id FROM orders").fetchall() == [("old-order",)]
        assert not (tmp_path / "findora.db").exists()


def test_two_databases_require_explicit_choice(tmp_path, monkeypatch):
    for name in ("globex.db", "findora.db"):
        (tmp_path / name).touch()
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        _database_url(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'globex.db'}")
    assert _database_url(tmp_path).endswith("globex.db")


@pytest.mark.parametrize("name", ["DATABASE_URL", "MYSQL_URL"])
def test_explicit_database_is_not_rewritten(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "file")
    assert _database_url(tmp_path) == "file"
