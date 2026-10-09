"""把 SQLite 业务库搬迁到 MySQL。

只搬数据，不改结构：表结构由 SQLAlchemy 模型定义（tables / session_store / trade_tables），
本脚本先按模型建表，再逐表复制。

安全约定（与仓库既有迁移规则一致）：
  - 目标表非空即拒绝，绝不覆盖已有数据；
  - 逐表复制在单个事务内完成，失败整表回滚；
  - 复制后校验行数，不一致即报错退出。

用法：
    python scripts/migrate_sqlite_to_mysql.py <mysql_url> <sqlite_path> [表名 ...]

不传表名时迁移源库中所有已在模型中定义的表；传了表名则只迁移这些表（用于定向重迁）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 导入所有定义表结构的模块，否则 Base.metadata 不完整，create_all 会漏表。
import app.infrastructure.persistence.sql.agui_tables  # noqa: F401,E402
import app.infrastructure.persistence.sql.session_store  # noqa: F401,E402
import app.infrastructure.persistence.sql.trade_tables  # noqa: F401,E402
from app.infrastructure.persistence.sql.repositories import create_engine  # noqa: E402
from app.infrastructure.persistence.sql.tables import Base  # noqa: E402


def _quote(name: str) -> str:
    """MySQL 标识符加反引号，避免与保留字冲突。"""
    return "`" + name.replace("`", "``") + "`"


async def _migrate_table(engine, sqlite_path: Path, table: str) -> tuple[int, int]:
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute(f"SELECT * FROM {_quote(table)}").fetchall()
        if not rows:
            return 0, 0
        columns = list(rows[0].keys())
        records = [dict(row) for row in rows]
    finally:
        source.close()

    async with engine.begin() as conn:
        existing = await conn.scalar(text(f"SELECT COUNT(*) FROM {_quote(table)}"))
        if existing:
            raise RuntimeError(f"{table}: 目标表已有 {existing} 行，拒绝覆盖")
        statement = text(
            f"INSERT INTO {_quote(table)} ({', '.join(_quote(c) for c in columns)}) "
            f"VALUES ({', '.join(':' + c for c in columns)})"
        )
        await conn.execute(statement, records)
        written = await conn.scalar(text(f"SELECT COUNT(*) FROM {_quote(table)}"))
    return len(records), int(written)


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    mysql_url, sqlite_path = sys.argv[1], Path(sys.argv[2]).resolve()
    only = set(sys.argv[3:])
    if not sqlite_path.exists():
        print(f"源库不存在: {sqlite_path}")
        return 2

    engine = create_engine(mysql_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print(f"源: {sqlite_path}\n目标: {engine.url.render_as_string(hide_password=True)}\n")

        source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            tables = [
                row[0] for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
        finally:
            source.close()

        total = 0
        failures: list[str] = []
        for table in tables:
            if only and table not in only:
                continue
            if table not in Base.metadata.tables:
                print(f"  {table:<26} 跳过（模型未定义，可能是 journal 等独立库的表）")
                continue
            try:
                copied, written = await _migrate_table(engine, sqlite_path, table)
            except Exception as exc:
                failures.append(f"{table}: {exc}")
                print(f"  {table:<26} 失败: {exc}")
                continue
            flag = "OK" if copied == written else "行数不符!"
            if copied != written:
                failures.append(f"{table}: 复制 {copied} 行但目标 {written} 行")
            print(f"  {table:<26} {copied:>6} 行  {flag}")
            total += written
        print(f"\n合计 {total} 行")
        if failures:
            print("存在失败项：")
            for item in failures:
                print(f"  - {item}")
            return 1
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
