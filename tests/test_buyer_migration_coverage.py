"""Offline buyer migration includes new ledgers and refuses partial merges."""
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.migrate_local_buyer import FILES, migrate


def ledger(path, owner="old"):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE records (id TEXT, buyer_id TEXT, snapshot TEXT)")
        db.execute("INSERT INTO records VALUES ('r1', ?, ?)", (owner, json.dumps({"buyer_id": owner, "title": "中文"})))


def test_pending_and_file_trade_ledgers_are_migrated_and_backed_up(tmp_path):
    for name in ("purchase_records.sqlite3", "trade.db"):
        ledger(tmp_path / name)
    report = migrate(tmp_path, "old", "new")
    backup = Path(report["backup"])
    for name in ("purchase_records.sqlite3", "trade.db"):
        with sqlite3.connect(tmp_path / name) as db:
            owner, snapshot = db.execute("SELECT buyer_id, snapshot FROM records").fetchone()
            assert owner == json.loads(snapshot)["buyer_id"] == "new"
        with sqlite3.connect(backup / name) as db:
            assert db.execute("SELECT buyer_id FROM records").fetchone()[0] == "old"
    assert json.loads((backup / "migration.json").read_text(encoding="utf-8"))["target"] == "new"


def test_target_conflict_rolls_back_all_attached_ledgers(tmp_path):
    ledger(tmp_path / "trade.db", "new")
    ledger(tmp_path / "purchase_records.sqlite3")
    with pytest.raises(ValueError, match="目标已有记录"):
        migrate(tmp_path, "old", "new")
    with sqlite3.connect(tmp_path / "purchase_records.sqlite3") as db:
        assert db.execute("SELECT buyer_id FROM records").fetchone()[0] == "old"


def test_backup_includes_uncheckpointed_wal_rows(tmp_path):
    path = tmp_path / "trade.db"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE orders (buyer_id TEXT)")
        writer.execute("INSERT INTO orders VALUES ('old')")
        writer.commit()
        report = migrate(tmp_path, "old", "new")
        with sqlite3.connect(Path(report["backup"]) / "trade.db") as saved:
            assert saved.execute("SELECT buyer_id FROM orders").fetchone()[0] == "old"


def test_too_many_databases_fail_before_any_backup_or_update(tmp_path):
    with sqlite3.connect(":memory:") as db:
        if len(FILES) <= db.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED):
            pytest.skip("SQLite build supports all ledgers")
    for name in FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger(path)
    with pytest.raises(ValueError, match="附加上限"):
        migrate(tmp_path, "old", "new")
    assert not (tmp_path / "backups").exists()
