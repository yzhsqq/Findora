"""扫描 Amazon / eBay 快照库中 JSON 列的字节长度分布（M10）。

用途：判定 TEXT（64KB 上限）是否安全，还是必须升级为 LONGTEXT。

字节口径说明：一律用 ``len(value.encode('utf-8'))``，不要用 SQLite 的 ``LENGTH()``
（它按字符计数，会低估 UTF-8 中文的字节数）。

用法::

    python scripts/scan_snapshot_json_bytes.py [sqlite_path ...]

不传参数时默认扫描 ``data/amazon_catalog.sqlite3`` 与 ``data/ebay_catalog.sqlite3``。
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

TEXT_LIMIT = 64 * 1024
DEFAULT_TARGETS = {
    "data/amazon_catalog.sqlite3": ("amazon_products", ("card_json", "raw_json"), "product_id"),
    "data/ebay_catalog.sqlite3": ("ebay_products", ("card_json", "raw_json"), "product_id"),
}


def scan_one(sqlite_path: pathlib.Path, table: str, columns: tuple[str, ...], pk: str = "pid") -> bool:
    if not sqlite_path.is_file():
        print(f"[skip] {sqlite_path} 不存在")
        return False

    db = sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        info = db.execute(f"PRAGMA table_info({table})").fetchall()
        print(f"\n=== {sqlite_path} :: {table} ===")
        print(f"  schema: {[(r[1], r[2]) for r in info]}")
        total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pk_bytes = [len(r[0] or b"") for r in db.execute(f"SELECT {pk} FROM {table}")]
        print(f"  rows={total} {pk}_max_bytes={max(pk_bytes) if pk_bytes else 0}")
        for col in columns:
            sizes = [len(r[0].encode("utf-8")) for r in db.execute(f"SELECT {col} FROM {table}")]
            if not sizes:
                print(f"  {col}: 无数据")
                continue
            over = [n for n in sizes if n > TEXT_LIMIT]
            print(
                f"  {col}: rows={len(sizes)} max_bytes={max(sizes)} "
                f"avg_bytes={sum(sizes) // len(sizes)} >64KB={len(over)}"
            )
            if over:
                print(f"    超过 TEXT 上限的行字节数（前 10）: {sorted(over, reverse=True)[:10]}")
    finally:
        db.close()
    return True


def main(argv: list[str]) -> int:
    if argv:
        targets = {
            a: DEFAULT_TARGETS.get(a.replace("\\", "/"), ("products", ("card_json", "raw_json"), "pid"))
            for a in argv
        }
    else:
        targets = DEFAULT_TARGETS

    ok = False
    for raw, (table, cols, pk) in targets.items():
        ok |= scan_one(pathlib.Path(raw), table, cols, pk)
    if not ok:
        print("没有可扫描的库文件")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
