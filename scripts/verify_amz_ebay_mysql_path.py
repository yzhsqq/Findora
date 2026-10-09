"""冒烟验证：Amazon/eBay 目录读 SQLite 与 MySQL 的卡片数是否一致。

这两个平台搜索全在 Python 侧完成，唯一 SQL 是 ``SELECT card_json ...``，故只比对
行数与若干查询的命中数即可。
用法：
    python scripts/verify_amz_ebay_mysql_path.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.persistence.amazon_catalog import AmazonCatalog  # noqa: E402
from app.infrastructure.persistence.ebay_catalog import EbayCatalog  # noqa: E402

AMZ_SQLITE = ROOT / "data" / "amazon_catalog.sqlite3"
EBAY_SQLITE = ROOT / "data" / "ebay_catalog.sqlite3"
# DSN 不落仓库（铁律：密码不进仓库）：优先读环境变量，缺省为占位符，须自行导出真实 DSN 才能连通。
# 这两个变量名与 settings 的 Amazon/eBay 目录数据源一致。
AMZ_DSN = os.getenv("AMAZON_MYSQL_DSN") or "mysql+pymysql://root:PASSWORD@127.0.0.1:3306/findora_amazon_snapshot_verify"
EBAY_DSN = os.getenv("EBAY_MYSQL_DSN") or "mysql+pymysql://root:PASSWORD@127.0.0.1:3306/findora_ebay_snapshot_verify"


def main() -> int:
    amazon_sqlite = AmazonCatalog(AMZ_SQLITE)
    amazon_mysql = AmazonCatalog(AMZ_SQLITE, mysql_dsn=AMZ_DSN)
    ebay_sqlite = EbayCatalog(EBAY_SQLITE)
    ebay_mysql = EbayCatalog(EBAY_SQLITE, mysql_dsn=EBAY_DSN)

    ok = True
    for label, left, right in (
        ("amazon", amazon_sqlite, amazon_mysql),
        ("ebay", ebay_sqlite, ebay_mysql),
    ):
        lc, rc = len(left._cards()), len(right._cards())
        match = lc == rc
        ok &= match
        print(f"[{'OK  ' if match else 'FAIL'}] {label} cards: sqlite={lc} mysql={rc}")

        for q in ("", "watch", "bag"):
            try:
                lb = left._browse(q, "", 1, 24)
                rb = right._browse(q, "", 1, 24)
            except Exception as exc:  # noqa: BLE001
                print(f"        browse({q!r}) 异常: {type(exc).__name__}: {exc}")
                ok = False
                continue
            match = lb["total"] == rb["total"]
            ok &= match
            print(f"        browse({q!r}) total: sqlite={lb['total']} mysql={rb['total']} "
                  f"{'OK' if match else 'FAIL'}")

    print("\n结果:", "一致" if ok else "存在差异")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
