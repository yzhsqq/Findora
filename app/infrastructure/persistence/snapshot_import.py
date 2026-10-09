"""Shared snapshot replacement: validate before writing, preserve data on error."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Callable


def import_records(input_path: Path, output_path: Path, *, table: str, label: str,
                   normalize: Callable[[dict], dict], merge: bool = False, skip_invalid: bool = False) -> dict:
    if table not in {"amazon_products", "ebay_products"}:
        raise ValueError("Unsupported snapshot table")
    records = json.loads(input_path.read_text(encoding="utf-8-sig"))
    if not isinstance(records, list) or not records:
        raise ValueError(f"输入必须为非空 {label} JSON 数组")
    reused, invalid = 0, 0
    if merge and output_path.is_file():
        with closing(sqlite3.connect(output_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            try:
                previous = [json.loads(row[0]) for row in db.execute(f"SELECT raw_json FROM {table}")]
            except sqlite3.OperationalError:
                previous = []
        reused = len(previous)
        records = previous + records
    normalized = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"{label} 记录必须为对象")
        try:
            card = normalize(record)
        except ValueError:
            if not skip_invalid:
                raise
            invalid += 1
            continue
        key = card["product_id"]
        previous = normalized.get(key)
        if previous and datetime.fromisoformat(previous[0]["updated_at"].replace("Z", "+00:00")) > datetime.fromisoformat(card["updated_at"].replace("Z", "+00:00")):
            continue
        normalized[key] = (card, record)
    if not normalized:
        raise ValueError(f"{label} 批次没有有效商品，保留已有快照，未执行替换")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(output_path)) as db, db:
        db.execute(f"CREATE TABLE IF NOT EXISTS {table} (product_id TEXT PRIMARY KEY, card_json TEXT NOT NULL, raw_json TEXT NOT NULL)")
        db.execute(f"DELETE FROM {table}")
        db.executemany(f"INSERT INTO {table} VALUES (?,?,?)", [
            (key, json.dumps(card, ensure_ascii=False), json.dumps(raw, ensure_ascii=False))
            for key, (card, raw) in normalized.items()])
    return {"products": len(normalized), "priced": sum(c[0]["price_kind"] != "unknown" for c in normalized.values()),
            "duplicates": len(records) - len(normalized) - invalid, "reused_existing": reused,
            "skipped_invalid": invalid, "output": str(output_path.resolve())}
