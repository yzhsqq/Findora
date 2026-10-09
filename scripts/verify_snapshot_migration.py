"""独立交叉校验：源 SQLite 快照 与 目标 MySQL 快照 逐行比对字节长度 + SHA-256。

与迁移脚本内置校验**刻意独立**（不 import 迁移逻辑、不复用其校验代码），
目的是交叉验证而非自证：迁移脚本自己算自己验，出系统性偏差时看不出来。

关键是**不让大 payload 经过 asyncmy**：校验只在 MySQL 侧算 ``SHA2(col, 256)`` 与
``LENGTH(col)``（字节口径），把这两样小结果取回来比对，列本体一个字节都不传。
这不是偷懒——实测逐行读回 386KB 的 raw_json 会直接撞 asyncmy 读缓冲 resize 的
BufferError（见 migrate_snapshot_to_mysql._CHUNK_BYTES 注释），整表/单行读回都会崩；
服务端算哈希既绕开驱动缺陷，也快一个数量级。

用法：
    python scripts/verify_snapshot_migration.py <mysql_url> <sqlite_path> --platform {cj,amazon,ebay}
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.persistence.sql.repositories import create_engine  # noqa: E402
from scripts.migrate_snapshot_to_mysql import get_spec  # noqa: E402


def _source_rows(sqlite_path: Path, spec) -> dict[str, dict[str, tuple[str, int]]]:
    """读源库：{pk: {col: (sha256, 字节长度)}}；以原始字节为准，不做任何解码。"""
    db = sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True)
    db.text_factory = bytes
    try:
        out: dict[str, dict[str, tuple[str, int]]] = {}
        cols = ", ".join([spec.pk, *spec.json_columns])
        for row in db.execute(f"SELECT {cols} FROM {spec.table}"):
            key = row[0].decode("utf-8")
            out[key] = {
                col: (hashlib.sha256(val).hexdigest(), len(val))
                for col, val in zip(spec.json_columns, row[1:])
                if val is not None
            }
        return out
    finally:
        db.close()


async def _target_rows(mysql_url: str, spec, keys: list[str]) -> dict[str, dict[str, tuple[str, int]]]:
    """读目标库：只取主键 + 各 JSON 列的 SHA2/LENGTH，分页小结果集。

    MySQL 的 ``LENGTH()`` 是字节口径（与 SQLite ``LENGTH()`` 的字符口径不同，别混用），
    ``SHA2(col, 256)`` 返回小写 hex，与 Python ``hashlib.sha256(...).hexdigest()`` 同格式。
    """
    engine = create_engine(mysql_url)
    try:
        out: dict[str, dict[str, tuple[str, int]]] = {}
        cols = ", ".join(
            f"SHA2(`{c}`, 256) AS `{c}_sha`, LENGTH(`{c}`) AS `{c}_len`" for c in spec.json_columns
        )
        async with engine.connect() as conn:
            for start in range(0, len(keys), 200):
                page = keys[start:start + 200]
                rows = (await conn.execute(
                    text(
                        f"SELECT `{spec.pk}` AS pk, {cols} FROM `{spec.table}` "
                        f"WHERE `{spec.pk}` IN :keys"
                    ).bindparams(bindparam("keys", expanding=True)),
                    {"keys": page},
                )).mappings().all()
                for row in rows:
                    entry: dict[str, tuple[str, int]] = {}
                    for col in spec.json_columns:
                        if row[f"{col}_sha"] is None:
                            continue  # 目标为 NULL，与源 NULL 对齐
                        entry[col] = (row[f"{col}_sha"], int(row[f"{col}_len"]))
                    out[row["pk"]] = entry
        return out
    finally:
        await engine.dispose()


async def verify(mysql_url: str, sqlite_path: str | Path, platform: str) -> int:
    spec = get_spec(platform)
    sqlite_path = Path(sqlite_path).resolve()
    src = _source_rows(sqlite_path, spec)
    tgt = await _target_rows(mysql_url, spec, sorted(src))

    missing = [k for k in src if k not in tgt]
    extra = [k for k in tgt if k not in src]
    mismatch: list[str] = []
    checked = 0
    for key, src_cols in src.items():
        tgt_cols = tgt.get(key, {})
        for col, (digest, length) in src_cols.items():
            got = tgt_cols.get(col)
            checked += 1
            if got != (digest, length):
                mismatch.append(f"{key}.{col}")
        # 源为 NULL 的列不应在目标侧凭空出现
        for col in spec.json_columns:
            if col not in src_cols and col in tgt_cols:
                mismatch.append(f"{key}.{col}(源 NULL 目标非 NULL)")

    print(f"platform={platform} table={spec.table}")
    print(f"源行数={len(src)} 目标行数={len(tgt)}")
    print(f"校验 JSON 值数={checked}")
    print(f"缺失行={len(missing)} 多余行={len(extra)} 不匹配={len(mismatch)}")
    if missing[:5]:
        print(f"  缺失样例: {missing[:5]}")
    if extra[:5]:
        print(f"  多余样例: {extra[:5]}")
    if mismatch[:5]:
        print(f"  不匹配样例: {mismatch[:5]}")
    ok = not missing and not extra and not mismatch
    print("结论:", "PASS 逐行字节一致" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mysql_url", help="目标 MySQL URL（含库名）")
    parser.add_argument("sqlite_path", help="源快照 SQLite 路径")
    parser.add_argument("--platform", default="cj", choices=["cj", "amazon", "ebay"])
    args = parser.parse_args(argv)
    if not make_url(args.mysql_url).database:
        parser.error("mysql_url 必须带库名")
    return asyncio.run(verify(args.mysql_url, args.sqlite_path, args.platform))


if __name__ == "__main__":
    raise SystemExit(main())
