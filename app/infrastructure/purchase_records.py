"""Buyer-owned CJ purchase plans; never write to inventory or the order ledger."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid


class PurchaseRecordStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS purchase_records (
                record_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL,
                product_id TEXT NOT NULL, sku_id TEXT NOT NULL,
                snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(buyer_id, product_id, sku_id))""")

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        return {"record_id": row["record_id"], "status": "PENDING_PURCHASE",
                "product": json.loads(row["snapshot"]), "sku_id": row["sku_id"],
                "quantity": 1, "created_at": row["created_at"]}

    async def save(self, buyer_id: str, product: dict, sku_id: str) -> dict:
        def write():
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM purchase_records WHERE buyer_id=? AND product_id=? AND sku_id=?",
                                 (buyer_id, product["product_id"], sku_id)).fetchone()
                if row is None and db.execute("SELECT count(*) FROM purchase_records WHERE buyer_id=?", (buyer_id,)).fetchone()[0] >= 100:
                    raise ValueError("最多保存 100 条待购记录，请先移除不再需要的商品。")
                db.execute("""INSERT INTO purchase_records VALUES (?,?,?,?,?,?)
                    ON CONFLICT(buyer_id,product_id,sku_id) DO UPDATE SET snapshot=excluded.snapshot""",
                    (f"pending-{uuid.uuid4().hex}", buyer_id, product["product_id"], sku_id,
                     json.dumps(product, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
                saved = db.execute("SELECT * FROM purchase_records WHERE buyer_id=? AND product_id=? AND sku_id=?",
                                   (buyer_id, product["product_id"], sku_id)).fetchone()
                return {"record": self._record(saved), "created": row is None}
        return await asyncio.to_thread(write)

    async def list(self, buyer_id: str) -> list[dict]:
        def read():
            with closing(sqlite3.connect(self.path, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                return [self._record(row) for row in db.execute(
                    "SELECT * FROM purchase_records WHERE buyer_id=? ORDER BY created_at DESC,record_id", (buyer_id,))]
        return await asyncio.to_thread(read)

    async def delete(self, buyer_id: str, record_id: str) -> None:
        def remove():
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute("DELETE FROM purchase_records WHERE buyer_id=? AND record_id=?", (buyer_id, record_id))
        await asyncio.to_thread(remove)
