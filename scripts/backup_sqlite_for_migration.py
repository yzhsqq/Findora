"""迁移前备份：用 SQLite backup API 导出数据库，保证 WAL 中未 checkpoint 的页一并落盘。

直接复制 .db 文件在 WAL 模式下会漏掉 -wal 中尚未合并的数据；本脚本走 sqlite3 的
在线 backup 接口，导出结果是自洽的单一文件。

用法：
    python scripts/backup_sqlite_for_migration.py <data_dir> <dest_dir>
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# 热路径与业务状态库：迁移前必须完整留底。
TARGETS = [
    "findora.db",
    "globex.db",
    "ag_ui_runs.db",
    "buyer_favorites.db",
    "buyer_memory.db",
    "buyer_skills.db",
    "capabilities.db",
    "context_evidence.db",
    "prompts/registry.sqlite3",
]


def backup(src: Path, dst: Path) -> tuple[str, int]:
    if not src.exists():
        return "跳过（不存在）", 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 源库可能被服务占用；backup API 允许并发读。
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        source.close()
    return f"已备份（integrity={integrity}）", dst.stat().st_size


def main() -> int:
    data_dir = Path(sys.argv[1]).resolve()
    dest_dir = Path(sys.argv[2]).resolve()
    print(f"源目录: {data_dir}\n目标目录: {dest_dir}\n")
    failures = 0
    for name in TARGETS:
        src = data_dir / name
        dst = dest_dir / name
        try:
            status, size = backup(src, dst)
        except Exception as exc:  # 单个库失败不阻断其余备份，但要显式报出
            failures += 1
            print(f"  {name:<32} 失败: {exc}")
            continue
        size_text = f"{size:,} B" if size else "-"
        print(f"  {name:<32} {status:<28} {size_text}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
