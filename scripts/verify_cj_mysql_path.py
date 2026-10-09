"""对照验证：CJ 主检索链路在 SQLite 与 MySQL 上返回结果是否一致（M12/M13/M15）。

同一份快照分别走两条连接执行 _browse，逐用例比对总数与商品 ID 序列。
用法：
    python scripts/verify_cj_mysql_path.py [sqlite_path] [mysql_dsn]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.persistence.cj_catalog import CJCatalog  # noqa: E402

DEFAULT_SQLITE = ROOT / "data" / "cj_catalog.sqlite3"
# DSN 不落仓库（铁律：密码不进仓库）：优先读环境变量，其次命令行第 2 个参数，缺省占位符。
DEFAULT_DSN = os.getenv("CJ_MYSQL_DSN") or "mysql+pymysql://root:PASSWORD@127.0.0.1:3306/findora_cj_snapshot_verify"


def sample_identifiers(sqlite_path: Path) -> tuple[str, str]:
    """取一个真实 pid 与一个真实 variantSku，用于直接命中分支。"""
    db = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT pid, detail_json FROM products WHERE detail_json IS NOT NULL").fetchone()
        pid = str(row["pid"])
        sku = ""
        try:
            variants = json.loads(row["detail_json"]).get("variants") or []
            for item in variants:
                if isinstance(item, dict) and item.get("variantSku"):
                    sku = str(item["variantSku"])
                    break
        except (TypeError, ValueError):
            pass
        return pid, sku
    finally:
        db.close()


def run_case(label: str, query: str, category: str, sqlite_cat: CJCatalog, mysql_cat: CJCatalog) -> bool:
    try:
        left = sqlite_cat._browse(query, category, 1, 24)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label}: SQLite 侧异常 {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False
    try:
        right = mysql_cat._browse(query, category, 1, 24)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label}: MySQL 侧异常 {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False

    left_ids = [p["product_id"] for p in left["products"]]
    right_ids = [p["product_id"] for p in right["products"]]
    ok = (
        left["total"] == right["total"]
        and left_ids == right_ids
        and left["all_count"] == right["all_count"]
        and left["detail_count"] == right["detail_count"]
    )
    marker = "OK  " if ok else "FAIL"
    print(f"[{marker}] {label}: total {left['total']} vs {right['total']}, "
          f"ids_match={left_ids == right_ids}, all_count {left['all_count']} vs {right['all_count']}")
    if not ok and left_ids[:3] != right_ids[:3]:
        print(f"        SQLite 前 3: {left_ids[:3]}")
        print(f"        MySQL  前 3: {right_ids[:3]}")
    return ok


def main() -> int:
    sqlite_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SQLITE
    dsn = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DSN

    if not sqlite_path.is_file():
        print(f"源库不存在: {sqlite_path}")
        return 2

    pid, sku = sample_identifiers(sqlite_path)
    print(f"源库: {sqlite_path}")
    print(f"DSN : {dsn}")
    print(f"样本 pid={pid} variantSku={sku or '(无)'}")

    sqlite_cat = CJCatalog(sqlite_path, localization=None)
    mysql_cat = CJCatalog(sqlite_path, mysql_dsn=dsn, localization=None)

    cases: list[tuple[str, str, str]] = [
        ("空查询全量", "", ""),
        ("英文关键词", "backpack", ""),
        ("英文多词", "women bag", ""),
        ("精确 pid", pid, ""),
        ("小写 pid", pid.lower(), ""),
    ]
    if sku:
        cases.append(("精确 SKU", sku, ""))
        cases.append(("小写 SKU", sku.lower(), ""))

    passed = sum(run_case(label, q, c, sqlite_cat, mysql_cat) for label, q, c in cases)
    total = len(cases)
    print(f"\n结果: {passed}/{total} 用例一致")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
