"""把商品快照库一次性全量迁入 MySQL（CJ / Amazon / eBay 三平台）。

- CJ：``cj_catalog.sqlite3`` 的 ``products`` 表（M09 已完成）；
- Amazon / eBay：``amazon_catalog.sqlite3`` / ``ebay_catalog.sqlite3`` 的
  ``amazon_products`` / ``ebay_products`` 表（M10）。

读取路径（cj_catalog.py / amazon_catalog.py / ebay_catalog.py / composition.py）保持
裸 sqlite3 不动，本脚本只把快照数据搬到 MySQL，并逐行校验无截断、无字节损坏。

安全约定：
  - 目标库名必须以 ``_snapshot_verify`` 结尾，或显式传 ``--allow-db``，否则拒绝；
  - 若目标库已存在含 ``state_json`` 列的热路径表，直接拒绝（防写进 findora 业务库）；
  - 临时表 + 原子 RENAME 交换：全部行校验通过才换名，失败则 DROP 临时表、目标 products 原样；
  - 逐行按字节长度 + SHA-256 校验（纯 Python 侧，不依赖 DB 的 LENGTH 函数）；
  - MySQL 凭据只从 mysql_url / 环境变量读取，不落任何文件。

用法：
    python scripts/migrate_snapshot_to_mysql.py <mysql_url> <sqlite_path> [--platform {cj,amazon,ebay}]

例：
    python scripts/migrate_snapshot_to_mysql.py \
        mysql+asyncmy://root:PASS@127.0.0.1:3306/findora_amazon_snapshot_verify \
        data/amazon_catalog.sqlite3 --platform amazon

报告与跳过清单写入源库同目录；mysql_url 形如
``mysql+asyncmy://user:pass@127.0.0.1:3306/findora_cj_snapshot_verify``。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.persistence.sql.repositories import create_engine  # noqa: E402
from app.infrastructure.persistence.sql.snapshot_tables import (  # noqa: E402
    AmazonProductRow,
    CJProductRow,
    EbayProductRow,
)

logger = logging.getLogger("migrate_snapshot")

# CJ 的默认列（M09 契约测试按位置传参，保持模块级常量不变）
JSON_COLUMNS = ("list_json", "detail_json", "inventory_json")

# CJ 建表 DDL 模板：显式 utf8mb4 + LONGTEXT，与 snapshot_tables.CJProductRow 对齐
# （含 cj_enrichment 追加的 7 列，实测均 <700 字节）。
_CJ_DDL = (
    "CREATE TABLE `{table}` ("
    "  `pid` VARCHAR(64) NOT NULL,"
    "  `first_category` VARCHAR(128) NOT NULL,"
    "  `second_category` VARCHAR(128) NOT NULL,"
    "  `third_category` VARCHAR(128) NOT NULL,"
    "  `list_json` LONGTEXT NOT NULL,"
    "  `list_fetched_at` VARCHAR(40) NOT NULL,"
    "  `detail_json` LONGTEXT NULL,"
    "  `detail_fetched_at` VARCHAR(40) NULL,"
    "  `detail_status` VARCHAR(32) NULL,"
    "  `inventory_json` LONGTEXT NULL,"
    "  `inventory_fetched_at` VARCHAR(40) NULL,"
    "  `inventory_status` VARCHAR(32) NULL,"
    "  `source_url` VARCHAR(512) NULL,"
    "  `source_url_status` VARCHAR(32) NULL,"
    "  `source_url_checked_at` VARCHAR(40) NULL,"
    "  `source_url_evidence` TEXT NULL,"
    "  `source_url_page_status` VARCHAR(32) NULL,"
    "  `source_url_page_checked_at` VARCHAR(40) NULL,"
    "  `enrichment_attempted_at` VARCHAR(40) NULL,"
    "  PRIMARY KEY (`pid`)"
    ") CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
)

# Amazon / eBay 的建表 DDL 模板：两平台共用一条（snapshot_import.py 的 CREATE TABLE）。
# card_json 有界（normalize() 截断，实测 ≤12KB），raw_json 无界（实测最大 386KB）→ 两者统一
# LONGTEXT，杜绝 TEXT 静默截断。
_PLATFORM_DDL = (
    "CREATE TABLE `{table}` ("
    "  `product_id` VARCHAR(128) NOT NULL,"
    "  `card_json` LONGTEXT NOT NULL,"
    "  `raw_json` LONGTEXT NOT NULL,"
    "  PRIMARY KEY (`product_id`)"
    ") CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
)
BATCH_ROWS = 1000
BATCH_BYTES = 64 * 1024 * 1024  # 64MB：超过则提前提交，避免单个巨事务
BIG_ROW_BYTES = 64 * 1024  # 单列 64KB，MySQL TEXT 上限，LONGTEXT 才放得下
# 单个 INSERT / 读回语句的 payload 上限：把大结果集切成小片，避免 asyncmy 在超大结果集
# 下 resize 读缓冲时抛 BufferError（表现为 2006 "server has gone away"）。远小于 max_allowed_packet。
#
# 阈值是实测出来的，不是拍的（scripts/_probe_chunk.py，用 eBay 真数据二分）：
#   1024KB / 512KB → 失败；256KB → 924 行全过。
# 失败形态有两层：先 BufferError（asyncio 把它报成 "protocol.buffer_updated() call failed"），
# 读缓冲 resize 失败后协议错位，后续行解出垃圾字节，最终以
# ``UnicodeDecodeError: 'utf-8' codec can't decode byte 0xfc`` 的形式暴露——
# 所以**报错信息看起来像字符集问题，实际不是**，改 charset 无效，只能缩小读回分片。
# 256KB 对失败点（512KB）留 2 倍余量。注意：单列最大 386KB 也能过，说明触发条件是
# 分片累计字节数而非单行大小。
_CHUNK_BYTES = 256 * 1024


@dataclass(frozen=True)
class _Spec:
    """一个平台快照的迁移规格：源/目标表名、主键、JSON 列、模型与建表 DDL。"""

    key: str
    table: str
    pk: str
    json_columns: tuple[str, ...]
    model: type
    ddl: str


_SPECS: dict[str, _Spec] = {
    "cj": _Spec("cj", "products", "pid", JSON_COLUMNS, CJProductRow, _CJ_DDL),
    "amazon": _Spec("amazon", "amazon_products", "product_id", ("card_json", "raw_json"),
                    AmazonProductRow, _PLATFORM_DDL),
    "ebay": _Spec("ebay", "ebay_products", "product_id", ("card_json", "raw_json"),
                  EbayProductRow, _PLATFORM_DDL),
}

CJ_SPEC = _SPECS["cj"]


def get_spec(platform: str) -> _Spec:
    """按平台名取迁移规格；未知平台直接抛错，避免静默落到 CJ。"""
    try:
        return _SPECS[platform]
    except KeyError:
        raise ValueError(f"未知平台 {platform!r}，可选：{sorted(_SPECS)}") from None


class _BadRow(Exception):
    """单行坏数据：非法 UTF-8 或 malformed JSON。携带 pid + 列名 + 错误类型。"""

    def __init__(self, pid: str, column: str, kind: str, detail: object) -> None:
        self.pid = pid
        self.column = column
        self.kind = kind
        self.detail = detail
        super().__init__(f"{pid}: {column} {kind} ({detail})")


def _check_db_name(db_name: str, allow_db: bool) -> None:
    """目标库防护（第一道闸）：库名必须以 ``_snapshot_verify`` 结尾，除非显式 --allow-db。

    这是防误写生产库的硬闸，`--allow-db` 只放宽库名，不绕过下方的 state_json 扫描。
    """
    if db_name.endswith("_snapshot_verify") or allow_db:
        return
    raise RuntimeError(
        f"目标库 `{db_name}` 不以 `_snapshot_verify` 结尾；"
        "这是数据安全闸，防止误写生产库。确认无误后加 --allow-db 重试。"
    )


def _check_sql_mode(sql_mode: str | None) -> None:
    """目标 MySQL 的 sql_mode 必须含 STRICT_TRANS_TABLES，否则超长值会被静默截断。"""
    if "STRICT_TRANS_TABLES" in (sql_mode or ""):
        return
    raise RuntimeError(
        f"目标 MySQL 的 sql_mode 缺 STRICT_TRANS_TABLES（当前={sql_mode}），"
        "无严格模式会静默截断超长值，必须先补上再迁移。"
    )


def _should_flush(batch_size: int, payload_bytes: int) -> bool:
    """批提交边界：每 1000 行 或 累计 64MB payload 提交一次。"""
    return batch_size >= BATCH_ROWS or payload_bytes >= BATCH_BYTES


def _decode(value, *, pid: str = "?", column: str = "?") -> str | None:
    """把源 SQLite 读出的原始字节严格解码为 UTF-8 文本。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        # 非法 UTF-8：默认 fail-fast，由调用方决定 skip 或中止
        raise _BadRow(pid, column, "invalid-utf-8", exc) from exc


def _prepare_row(raw, source_cols: list[str], spec: _Spec = CJ_SPEC) -> tuple[dict, dict]:
    """解码并校验一行，返回 (准备写入的字典, 各 JSON 列期望的 (sha256, 字节长))。"""
    pk = spec.pk
    pid = _decode(raw[pk], column=pk)
    prepared: dict = {pk: pid}
    expected: dict = {}
    for col in source_cols:
        if col == pk:
            continue
        raw_val = raw[col]
        text_val = _decode(raw_val, pid=pid, column=col)
        if col in spec.json_columns and text_val is not None:
            # malformed JSON 也算坏数据，与非法 UTF-8 走同一条处理路径
            try:
                json.loads(text_val)
            except ValueError as exc:
                raise _BadRow(pid, col, "malformed-json", exc) from exc
            # 以源库存储的原始字节为准：SHA-256 与字节长度都取自原始字节，写回 MySQL 后
            # 用同一算法比对，能精确捕获截断与字符集转换造成的字节损坏。
            expected[col] = (hashlib.sha256(raw_val).hexdigest(), len(raw_val))
        prepared[col] = text_val
    return prepared, expected


def _scan_max_payload(source: sqlite3.Connection, source_cols: list[str],
                      table: str = "products") -> tuple[int, int]:
    """扫源库算总行数与单行最大 payload（供 max_allowed_packet 检查）。"""
    rows = 0
    max_payload = 0
    for raw in source.execute(f"SELECT * FROM {table}"):
        # 单行序列化 payload ≈ 各列原始字节之和（略去 SQL 文本开销，误差可忽略）
        payload = sum(len(v) for v in raw if isinstance(v, bytes))
        max_payload = max(max_payload, payload)
        rows += 1
    return rows, max_payload


def _temp_ddl(spec: _Spec, table: str) -> str:
    """临时表 DDL：按平台取模板（CJ 19 列 / Amazon&eBay 3 列），显式 utf8mb4 + LONGTEXT。"""
    return spec.ddl.format(table=table)


def _backup_source(sqlite_path: Path) -> None:
    """源文件防护：先备份；备份失败则记录 SHA-256 供审计（不阻断迁移）。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = sqlite_path.with_name(f"{sqlite_path.name}.bak.{ts}")
    try:
        shutil.copy2(sqlite_path, backup)
        logger.info("源库已备份到 %s", backup)
    except OSError as exc:
        digest = hashlib.sha256(sqlite_path.read_bytes()).hexdigest()
        logger.warning("源库备份失败（%s），已记录 SHA-256=%s 供审计", exc, digest)


async def _verify_charset(engine, table: str, spec: _Spec = CJ_SPEC) -> None:
    """建表后从 information_schema 校验表与各 JSON 列的 charset/collation。"""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                ),
                {"t": table},
            )
        ).mappings().all()
        for row in rows:
            name = row["COLUMN_NAME"]
            if name in spec.json_columns and (
                row["CHARACTER_SET_NAME"] != "utf8mb4"
                or row["COLLATION_NAME"] != "utf8mb4_unicode_ci"
            ):
                raise RuntimeError(
                    f"列 `{name}` charset={row['CHARACTER_SET_NAME']} "
                    f"collate={row['COLLATION_NAME']}，期望 utf8mb4/utf8mb4_unicode_ci"
                )


async def _insert_verify(conn, table: str, chunk: list[tuple[dict, dict]], report: dict,
                         spec: _Spec = CJ_SPEC) -> None:
    """插入一小片并读回逐行校验字节长度 + SHA-256；chunk 已按 payload 上限切小。"""
    pk = spec.pk
    json_cols = spec.json_columns
    columns = list(chunk[0][0].keys())
    cols_sql = ", ".join(f"`{c}`" for c in columns)
    ph_sql = ", ".join(f":{c}" for c in columns)
    insert_sql = text(f"INSERT INTO `{table}` ({cols_sql}) VALUES ({ph_sql})")
    pids = [p[pk] for p, _ in chunk]
    read_sql = text(
        f"SELECT `{pk}`, {', '.join(f'`{c}`' for c in json_cols)} FROM `{table}` WHERE `{pk}` IN :pids"
    ).bindparams(bindparam("pids", expanding=True))

    await conn.execute(insert_sql, [p for p, _ in chunk])
    got_rows = {
        r[pk]: r
        for r in (await conn.execute(read_sql, {"pids": pids})).mappings().all()
    }
    for prepared, expected in chunk:
        key = prepared[pk]
        got = got_rows[key]
        for col in json_cols:
            src = prepared[col]
            if src is None:
                if got[col] is not None:
                    raise RuntimeError(f"{key}: {col} 源为 NULL 目标非 NULL")
                continue
            digest, length = expected[col]
            got_bytes = got[col].encode("utf-8")
            if hashlib.sha256(got_bytes).hexdigest() != digest or len(got_bytes) != length:
                report["hash_mismatch"].append(key)
                raise RuntimeError(
                    f"{key}: {col} 写回校验失败（字节长度/SHA-256 不一致，疑似截断）"
                )
            report["hash_matched"] += 1


async def _flush_batch(engine, table: str, batch: list[tuple[dict, dict]], report: dict,
                       spec: _Spec = CJ_SPEC) -> None:
    """插入一批（提交边界 1000 行 / 64MB），内部按 ~1MB 切小片逐片插入+校验。

    同一事务内完成，任一片不一致即回滚整批；切小片是为了把单个 INSERT 与读回结果
    控制在远小于 max_allowed_packet 的范围内，规避 asyncmy 读缓冲 resize 的 BufferError。
    """
    async with engine.begin() as conn:
        chunk: list[tuple[dict, dict]] = []
        chunk_bytes = 0
        for item in batch:
            prepared = item[0]
            chunk.append(item)
            chunk_bytes += sum(
                len(prepared[c].encode("utf-8"))
                for c in spec.json_columns if prepared[c] is not None
            )
            if chunk_bytes >= _CHUNK_BYTES:
                await _insert_verify(conn, table, chunk, report, spec)
                chunk.clear()
                chunk_bytes = 0
        if chunk:
            await _insert_verify(conn, table, chunk, report, spec)


def _write_report(sqlite_path: Path, report: dict, skipped: list[tuple[str, str, str]],
                  spec: _Spec = CJ_SPEC) -> None:
    """汇总报告：控制台 + 源库同目录文件；跳过 pid 单独落文件。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        f"{spec.key} 快照 MySQL 迁移报告",
        f"时间: {ts}",
        f"源库: {sqlite_path}",
        f"源表: {spec.table}",
        f"总行数: {report['total']}",
        f"成功: {report['succeeded']}",
        f"失败: {len(report['failed'])}",
        f"跳过: {len(skipped)}",
        f">64KB 行数: {report['big_rows']}",
    ]
    for col in spec.json_columns:
        lines.append(f"{col} 最大字节数: {report['max_bytes'][col]}")
    lines.append(f"hash 匹配数: {report['hash_matched']}")
    lines.append(f"hash 不匹配 pid: {report['hash_mismatch'] or '（无）'}")
    if report["failed"]:
        lines.append("失败项:")
        lines += [f"  - {item}" for item in report["failed"]]
    body = "\n".join(lines) + "\n"

    report_file = sqlite_path.with_name(f"snapshot_migration_report_{ts}.txt")
    report_file.write_text(body, encoding="utf-8")
    print(body)
    logger.info("报告已写入 %s", report_file)

    if skipped:
        skip_file = sqlite_path.with_name(f"snapshot_skipped_{ts}.txt")
        skip_file.write_text(
            "\n".join(f"{pid}\t{column}\t{kind}" for pid, column, kind in skipped) + "\n",
            encoding="utf-8",
        )
        logger.info("跳过行已写入 %s", skip_file)


async def run(mysql_url: str, sqlite_path: str | Path, *, skip_bad_rows: bool = False,
              allow_db: bool = False, platform: str = "cj") -> int:
    """执行一次快照全量迁移；返回进程退出码（0 成功，非 0 失败/参数错误）。"""
    sqlite_path = Path(sqlite_path).resolve()
    if not sqlite_path.exists():
        logger.error("源库不存在: %s", sqlite_path)
        return 2

    try:
        spec = get_spec(platform)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    db_name = make_url(mysql_url).database or ""
    # 目标库防护（第一道闸）：库名，连接前先拦，避免带着错库名去连生产
    try:
        _check_db_name(db_name, allow_db)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    # 源库以只读 + 原始字节打开：text_factory=bytes 让我们能逐行捕获非法 UTF-8，
    # 而不是让 sqlite3 在 fetch 时整体抛错（那样拿不到 pid + 列名）。
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    source.text_factory = bytes
    source.row_factory = sqlite3.Row

    report: dict = {
        "total": 0, "succeeded": 0, "failed": [], "big_rows": 0,
        "max_bytes": {c: 0 for c in spec.json_columns},
        "hash_matched": 0, "hash_mismatch": [],
    }
    skipped: list[tuple[str, str, str]] = []
    temp_table = f"{spec.table}_tmp_{uuid.uuid4().hex[:8]}"
    engine = None

    try:
        # 列名（text_factory=bytes 下 PRAGMA 的 name 也是 bytes，需解码）
        source_cols = [r[1].decode("utf-8") for r in source.execute(f"PRAGMA table_info({spec.table})")]
        if not source_cols:
            raise RuntimeError(f"源库缺少 {spec.table} 表")
        model_cols = {c.name for c in spec.model.__table__.columns}
        if set(source_cols) != model_cols:
            raise RuntimeError(
                f"源表列与模型不一致：源={sorted(source_cols)} 模型={sorted(model_cols)}"
            )

        engine = create_engine(mysql_url)

        # ---- pre-flight（任一失败即中止，不动目标库） ----
        async with engine.connect() as conn:
            # sql_mode 必须含 STRICT_TRANS_TABLES，否则 MySQL 会静默截断超长值
            _check_sql_mode(await conn.scalar(text("SELECT @@sql_mode")))
            # 目标库防护（第二道闸）：库内不得有含 state_json 列的热路径表
            hot = await conn.scalar(text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME = 'state_json'"
            ))
            if hot:
                raise RuntimeError(
                    "目标库已存在含 state_json 列的热路径表，疑似业务库，拒绝写入。"
                )
            # max_allowed_packet：单行最大 payload 不得超过上限的 80%（留余量给 SQL 文本）
            max_packet = int(await conn.scalar(text("SELECT @@max_allowed_packet")) or 0)

        total, max_payload = _scan_max_payload(source, source_cols, spec.table)
        report["total"] = total
        if max_payload > max_packet * 0.8:
            raise RuntimeError(
                f"单行最大 payload {max_payload} 字节超过 max_allowed_packet "
                f"({max_packet}) 的 80%，客户端会拒绝发送；先调大 max_allowed_packet。"
            )

        # ---- 源文件防护：先备份，失败则记录 SHA-256 ----
        _backup_source(sqlite_path)

        # ---- 建临时表 + 校验 charset ----
        async with engine.begin() as conn:
            await conn.execute(text(_temp_ddl(spec, temp_table)))
        await _verify_charset(engine, temp_table, spec)

        # ---- 流式迁移 + 分批提交 + 读回校验 ----
        batch: list[tuple[dict, dict]] = []
        batch_bytes = 0
        for raw in source.execute(f"SELECT * FROM {spec.table}"):
            try:
                prepared, expected = _prepare_row(raw, source_cols, spec)
            except _BadRow as exc:
                if skip_bad_rows:
                    skipped.append((exc.pid, exc.column, exc.kind))
                    logger.warning("跳过坏行 %s.%s（%s）", exc.pid, exc.column, exc.kind)
                    continue
                report["failed"].append(f"{exc.pid}:{exc.column}:{exc.kind}")
                raise
            batch.append((prepared, expected))
            batch_bytes += sum(
                len(prepared[c].encode("utf-8"))
                for c in spec.json_columns if prepared[c] is not None
            )
            row_is_big = False
            for c in spec.json_columns:
                if prepared[c] is not None:
                    n = len(prepared[c].encode("utf-8"))
                    report["max_bytes"][c] = max(report["max_bytes"][c], n)
                    if n > BIG_ROW_BYTES:
                        row_is_big = True  # 任一 JSON 列 >64KB 即计入，行只计一次
            if row_is_big:
                report["big_rows"] += 1
            if _should_flush(len(batch), batch_bytes):
                await _flush_batch(engine, temp_table, batch, report, spec)
                report["succeeded"] += len(batch)
                logger.info("已迁移 %d 行", report["succeeded"])
                batch.clear()
                batch_bytes = 0
        if batch:
            await _flush_batch(engine, temp_table, batch, report, spec)
            report["succeeded"] += len(batch)

        # ---- 全部校验通过才原子换名；目标表若已存在先换名备份 ----
        target = spec.table
        async with engine.begin() as conn:
            exists = await conn.scalar(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ), {"t": target})
            if exists:
                backup_table = f"{target}_old_{uuid.uuid4().hex[:8]}"
                await conn.execute(text(
                    f"RENAME TABLE `{target}` TO `{backup_table}`, `{temp_table}` TO `{target}`"
                ))
            else:
                await conn.execute(text(f"RENAME TABLE `{temp_table}` TO `{target}`"))

        logger.info("迁移完成：%d 行已换名到 %s", report["succeeded"], target)
        return 0
    except Exception as exc:  # noqa: BLE001 —— 迁移中止，统一落报告并清理临时表
        # exc_info：2006/BufferError 这类驱动层错误的根因只在堆栈里，光看字符串无法定位
        logger.error("迁移中止: %s", exc, exc_info=True)
        report["failed"].append(str(exc))
        if engine is not None:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(f"DROP TABLE IF EXISTS `{temp_table}`"))
            except Exception:
                pass
        return 1
    finally:
        _write_report(sqlite_path, report, skipped, spec)
        source.close()
        if engine is not None:
            await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mysql_url", help="目标 MySQL URL（含库名，见文件头示例）")
    parser.add_argument("sqlite_path", help="源快照 SQLite 路径")
    parser.add_argument("--platform", default="cj", choices=sorted(_SPECS),
                        help="快照平台（默认 cj）：决定源/目标表名、主键与 JSON 列")
    parser.add_argument("--skip-bad-rows", action="store_true",
                        help="跳过非法 UTF-8 / malformed JSON 的行并落 pid 文件，默认 fail-fast")
    parser.add_argument("--allow-db", action="store_true",
                        help="允许写入不以 _snapshot_verify 结尾的库（确认无误才用）")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    return asyncio.run(run(
        args.mysql_url, args.sqlite_path,
        skip_bad_rows=args.skip_bad_rows, allow_db=args.allow_db, platform=args.platform,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
