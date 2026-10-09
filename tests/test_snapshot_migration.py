"""M09 CJ 快照 MySQL 迁移的契约测试。

分两类：
  - 纯逻辑校验（字节长度 / SHA-256 / UTF-8 / JSON / 库名防护 / 批提交边界 / schema 解耦），
    不依赖 MySQL，始终运行；
  - 需真实 MySQL 的端到端用例，按 test_trade_store_mysql.py 的模式用 ``MYSQL_TEST_URL``
    门控，未设则跳过（CI 无 MySQL 不受影响）。URL 为不带库名的服务器地址，测试自建
    独立临时库（库名以 ``_snapshot_verify`` 结尾），不触碰 findora 业务库。

本机运行（密码用占位符，不落仓库文件）：
    MYSQL_TEST_URL="mysql+asyncmy://root:PASSWORD@127.0.0.1:3306" \
    uv run python -m pytest tests/test_snapshot_migration.py -q --basetemp=.pytest-snap -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import mysql as mysql_dialect

from app.infrastructure.persistence.sql import tables
from app.infrastructure.persistence.sql.repositories import create_engine
from app.infrastructure.persistence.sql.snapshot_tables import (
    AmazonProductRow,
    CJProductRow,
    EbayProductRow,
    SnapshotBase,
)
from scripts.migrate_snapshot_to_mysql import (
    _BadRow,
    _check_db_name,
    _check_sql_mode,
    _prepare_row,
    _should_flush,
    get_spec,
    run,
)

PLATFORM_COLS = ["product_id", "card_json", "raw_json"]

MYSQL_TEST_URL = os.getenv("MYSQL_TEST_URL")

SOURCE_COLS = [
    "pid", "first_category", "second_category", "third_category",
    "list_json", "list_fetched_at", "detail_json", "detail_fetched_at",
    "detail_status", "inventory_json", "inventory_fetched_at", "inventory_status",
    "source_url", "source_url_status", "source_url_checked_at", "source_url_evidence",
    "source_url_page_status", "source_url_page_checked_at", "enrichment_attempted_at",
]


def _row(pid, list_json, detail_json=None, inventory_json=None):
    """构造一条与 sync_cj_catalog.py DDL 对齐的源行；JSON 列可为 str 或 bytes。"""
    return {
        "pid": pid,
        "first_category": "Consumer Electronics",
        "second_category": "Phones & Accessories",
        "third_category": "Phone Cases",
        "list_json": list_json,
        "list_fetched_at": "2026-10-01T00:00:00+00:00",
        "detail_json": detail_json,
        "detail_fetched_at": "2026-10-01T00:00:00+00:00" if detail_json is not None else None,
        "detail_status": "ok" if detail_json is not None else None,
        "inventory_json": inventory_json,
        "inventory_fetched_at": "2026-10-01T00:00:00+00:00" if inventory_json is not None else None,
        "inventory_status": "ok" if inventory_json is not None else None,
        "source_url": None, "source_url_status": None, "source_url_checked_at": None,
        "source_url_evidence": None, "source_url_page_status": None,
        "source_url_page_checked_at": None, "enrichment_attempted_at": None,
    }


def _raw(pid, **overrides):
    """构造 bytes 值字典，供 _prepare_row 纯逻辑测试使用。"""
    row = {
        "pid": pid.encode("utf-8"),
        "first_category": b"cat1", "second_category": b"cat2", "third_category": b"cat3",
        "list_json": b'{"ok": true}',
        "list_fetched_at": b"2026-10-01T00:00:00+00:00",
        "detail_json": None, "detail_fetched_at": None, "detail_status": None,
        "inventory_json": None, "inventory_fetched_at": None, "inventory_status": None,
        "source_url": None, "source_url_status": None, "source_url_checked_at": None,
        "source_url_evidence": None, "source_url_page_status": None,
        "source_url_page_checked_at": None, "enrichment_attempted_at": None,
    }
    row.update({k: v for k, v in overrides.items()})
    return row


def make_platform_source(path: Path, table: str, rows: list[dict]) -> Path:
    """在 path 建 Amazon/eBay 快照表（3 列，与 snapshot_import.py 的 DDL 一致）。"""
    db = sqlite3.connect(path)
    db.execute(
        f"CREATE TABLE {table} ("
        "product_id TEXT PRIMARY KEY, card_json TEXT NOT NULL, raw_json TEXT NOT NULL)"
    )
    for row in rows:
        db.execute(
            f"INSERT INTO {table} (product_id, card_json, raw_json) VALUES (?,?,?)",
            [row["product_id"], row["card_json"], row["raw_json"]],
        )
    db.commit()
    db.close()
    return path


def make_source(path: Path, rows: list[dict]) -> Path:
    """在 path 建 CJ products 表并写入 rows（JSON 列可含 bytes 制造坏行）。"""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE products (
            pid TEXT PRIMARY KEY,
            first_category TEXT NOT NULL,
            second_category TEXT NOT NULL,
            third_category TEXT NOT NULL,
            list_json TEXT NOT NULL,
            list_fetched_at TEXT NOT NULL,
            detail_json TEXT,
            detail_fetched_at TEXT,
            detail_status TEXT,
            inventory_json TEXT,
            inventory_fetched_at TEXT,
            inventory_status TEXT,
            source_url TEXT,
            source_url_status TEXT,
            source_url_checked_at TEXT,
            source_url_evidence TEXT,
            source_url_page_status TEXT,
            source_url_page_checked_at TEXT,
            enrichment_attempted_at TEXT
        );
        """
    )
    cols = ", ".join(SOURCE_COLS)
    placeholders = ", ".join("?" * len(SOURCE_COLS))
    for row in rows:
        db.execute(
            f"INSERT INTO products ({cols}) VALUES ({placeholders})",
            [row[c] for c in SOURCE_COLS],
        )
    db.commit()
    db.close()
    return path


# ---- 纯逻辑校验（不依赖 MySQL，始终运行） ----

def test_snapshot_base_isolated_from_hot_path():
    # 独立 Base / metadata，与热路径 tables 零耦合
    assert SnapshotBase is not tables.Base
    assert CJProductRow.__table__.metadata is not tables.Base.metadata
    # _LongText 在 snapshot_tables 内重定义，非 import 自 tables
    import app.infrastructure.persistence.sql.snapshot_tables as snap
    assert snap._LongText is not tables._LongText
    # 表名不交叉：products 只在快照侧
    assert "products" not in tables.Base.metadata.tables
    assert "products" in SnapshotBase.metadata.tables


def test_cj_product_row_exact_columns():
    cols = {c.name: c for c in CJProductRow.__table__.columns}
    assert set(cols) == set(SOURCE_COLS)
    assert cols["pid"].primary_key and cols["pid"].type.length == 64
    assert cols["first_category"].type.length == 128
    assert cols["list_fetched_at"].type.length == 40
    assert cols["detail_status"].type.length == 32
    # enrichment 扩展列：小文本、可空
    assert cols["source_url"].type.length == 512
    assert cols["source_url"].nullable
    assert cols["source_url_evidence"].nullable
    assert cols["enrichment_attempted_at"].nullable
    # 可空性：list_json 必填，detail/inventory 可空
    assert not cols["list_json"].nullable
    assert cols["detail_json"].nullable
    assert cols["inventory_json"].nullable


def test_json_columns_compile_to_longtext_on_mysql():
    for name in ("list_json", "detail_json", "inventory_json"):
        col = CJProductRow.__table__.columns[name]
        assert str(col.type.compile(dialect=mysql_dialect.dialect())) == "LONGTEXT"


def test_amazon_ebay_rows_exact_columns():
    # 与 snapshot_import.py 的建表 DDL 一一对应：整段 JSON，不是多列
    for model in (AmazonProductRow, EbayProductRow):
        cols = {c.name: c for c in model.__table__.columns}
        assert set(cols) == set(PLATFORM_COLS), model.__name__
        # product_id 实测最长 20 字节，VARCHAR(128) 留足余量；MySQL 主键必须带长度
        assert cols["product_id"].primary_key and cols["product_id"].type.length == 128
        # 两列均 NOT NULL（snapshot_import 的 DDL 如此）
        assert not cols["card_json"].nullable and not cols["raw_json"].nullable


def test_amazon_ebay_json_columns_compile_to_longtext_on_mysql():
    # M10 的核心结论：raw_json 实测最大 386947 字节，TEXT(64KB) 存不下 → 必须 LONGTEXT
    for model in (AmazonProductRow, EbayProductRow):
        for name in ("card_json", "raw_json"):
            col = model.__table__.columns[name]
            assert str(col.type.compile(dialect=mysql_dialect.dialect())) == "LONGTEXT"


def test_platform_specs_registered():
    amazon = get_spec("amazon")
    ebay = get_spec("ebay")
    assert amazon.table == "amazon_products" and amazon.pk == "product_id"
    assert ebay.table == "ebay_products" and ebay.pk == "product_id"
    assert amazon.json_columns == ("card_json", "raw_json") == ebay.json_columns
    assert amazon.model is AmazonProductRow and ebay.model is EbayProductRow
    # 两平台共用同一条 3 列 DDL（与 snapshot_import 一致）
    assert amazon.ddl == ebay.ddl
    # 未知平台必须报错，绝不静默落到 CJ
    with pytest.raises(ValueError):
        get_spec("walmart")


def test_prepare_row_platform_uses_product_id_pk():
    spec = get_spec("amazon")
    raw = {
        "product_id": b"amazon:us:B0TEST",
        "card_json": b'{"title": "a"}',
        "raw_json": b'{"raw": true}',
    }
    prepared, expected = _prepare_row(raw, PLATFORM_COLS, spec)
    assert prepared["product_id"] == "amazon:us:B0TEST"
    for col in ("card_json", "raw_json"):
        digest, length = expected[col]
        assert digest == hashlib.sha256(raw[col]).hexdigest()
        assert length == len(raw[col])


def test_prepare_row_byte_length_and_sha256():
    list_json = '{"title": "测试"}'
    prepared, expected = _prepare_row(_raw("pid-1", list_json=list_json.encode("utf-8")), SOURCE_COLS)
    assert prepared["pid"] == "pid-1"
    assert prepared["list_json"] == list_json
    digest, length = expected["list_json"]
    assert digest == hashlib.sha256(list_json.encode("utf-8")).hexdigest()
    assert length == len(list_json.encode("utf-8"))
    # 可空列无值时不产生期望值
    assert "detail_json" not in expected and "inventory_json" not in expected


def test_prepare_row_accepts_4byte_emoji():
    detail = '{"title": "火箭 🚀"}'
    prepared, expected = _prepare_row(_raw("p-emoji", detail_json=detail.encode("utf-8")), SOURCE_COLS)
    assert "🚀" in prepared["detail_json"]
    assert len("🚀".encode("utf-8")) == 4  # 4 字节 emoji，utf8mb4 才存得下
    digest, length = expected["detail_json"]
    assert digest == hashlib.sha256(detail.encode("utf-8")).hexdigest()
    assert length == len(detail.encode("utf-8"))


def test_prepare_row_detects_invalid_utf8():
    with pytest.raises(_BadRow) as exc:
        _prepare_row(_raw("p-bad", detail_json=b"\xff\xfe\xfa"), SOURCE_COLS)
    assert exc.value.pid == "p-bad"
    assert exc.value.column == "detail_json"
    assert exc.value.kind == "invalid-utf-8"


def test_prepare_row_detects_malformed_json():
    with pytest.raises(_BadRow) as exc:
        _prepare_row(_raw("p-json", detail_json=b"not-json"), SOURCE_COLS)
    assert exc.value.pid == "p-json"
    assert exc.value.column == "detail_json"
    assert exc.value.kind == "malformed-json"


def test_check_db_name_guard():
    # 合法：以 _snapshot_verify 结尾，或显式 allow_db
    _check_db_name("findora_cj_snapshot_verify", allow_db=False)
    _check_db_name("findora", allow_db=True)
    # 非法：普通库名且未 allow_db
    with pytest.raises(RuntimeError):
        _check_db_name("findora", allow_db=False)
    with pytest.raises(RuntimeError):
        _check_db_name("findora_snapshot", allow_db=False)


def test_check_sql_mode():
    # 含 STRICT_TRANS_TABLES 放行
    _check_sql_mode("STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION")
    # 缺 STRICT_TRANS_TABLES / 空值拒绝
    with pytest.raises(RuntimeError):
        _check_sql_mode("NO_ENGINE_SUBSTITUTION")
    with pytest.raises(RuntimeError):
        _check_sql_mode(None)


def test_should_flush_batch_boundary():
    # 行数边界：999 不提交，1000 / 1001 提交
    assert not _should_flush(999, 0)
    assert _should_flush(1000, 0)
    assert _should_flush(1001, 0)
    # payload 边界：64MB 提交，略少不提交
    assert _should_flush(1, 64 * 1024 * 1024)
    assert not _should_flush(1, 64 * 1024 * 1024 - 1)


# ---- 需真实 MySQL 的端到端用例（未设 MYSQL_TEST_URL 时跳过） ----

def _base_url() -> str:
    return MYSQL_TEST_URL.rstrip("/")


async def _make_db(name: str) -> str:
    admin = create_engine(_base_url())
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
    finally:
        await admin.dispose()
    return f"{_base_url()}/{name}"


async def _drop_db(name: str) -> None:
    admin = create_engine(_base_url())
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP DATABASE `{name}`"))
    finally:
        await admin.dispose()


async def test_migration_roundtrip_mysql(tmp_path):
    if not MYSQL_TEST_URL:
        pytest.skip("未设置 MYSQL_TEST_URL，跳过 MySQL 迁移端到端")
    big = '{"title": "big", "blob": "' + ("x" * (70 * 1024)) + '"}'
    emoji = '{"title": "🚀"}'
    source = make_source(tmp_path / "cj.sqlite3", [
        _row("p-1", '{"title": "a"}'),
        _row("p-2", '{"title": "big"}', detail_json=big),
        _row("p-3", '{"title": "emoji"}', detail_json=emoji),
    ])
    db_name = f"findora_cj_{uuid.uuid4().hex[:8]}_snapshot_verify"
    url = await _make_db(db_name)
    try:
        assert await run(url, source) == 0
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT COUNT(*) FROM products")) == 3
                # 三 JSON 列 LONGTEXT + utf8mb4
                cols = {
                    r["COLUMN_NAME"]: r
                    for r in (await conn.execute(text(
                        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_SET_NAME, COLLATION_NAME "
                        "FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'"
                    ))).mappings().all()
                }
                for name in ("list_json", "detail_json", "inventory_json"):
                    assert cols[name]["DATA_TYPE"] == "longtext"
                    assert cols[name]["CHARACTER_SET_NAME"] == "utf8mb4"
                    assert cols[name]["COLLATION_NAME"] == "utf8mb4_unicode_ci"
                # >64KB 行无截断，字节级 round-trip
                big_back = (await conn.execute(
                    text("SELECT detail_json FROM products WHERE pid = 'p-2'"))).scalar()
                assert big_back == big
                assert len(big_back.encode("utf-8")) > 64 * 1024
                # 4 字节 emoji 完整保留
                emoji_back = (await conn.execute(
                    text("SELECT detail_json FROM products WHERE pid = 'p-3'"))).scalar()
                assert "🚀" in emoji_back
        finally:
            await engine.dispose()
    finally:
        await _drop_db(db_name)


@pytest.mark.parametrize("platform", ["amazon", "ebay"])
async def test_platform_migration_roundtrip_mysql(tmp_path, platform):
    """M10：Amazon/eBay 快照迁入 MySQL，>64KB 的 raw_json 必须完整保留。"""
    if not MYSQL_TEST_URL:
        pytest.skip("未设置 MYSQL_TEST_URL，跳过平台快照迁移端到端")
    spec = get_spec(platform)
    # 70KB raw_json：超 MySQL TEXT 上限，用 TEXT 会静默截断
    big_raw = '{"desc": "' + ("y" * (70 * 1024)) + '"}'
    emoji_card = '{"title": "🚀"}'
    source = make_platform_source(tmp_path / f"{platform}.sqlite3", spec.table, [
        {"product_id": "x-1", "card_json": '{"title": "a"}', "raw_json": '{"raw": 1}'},
        {"product_id": "x-2", "card_json": emoji_card, "raw_json": big_raw},
    ])
    db_name = f"findora_{platform}_{uuid.uuid4().hex[:8]}_snapshot_verify"
    url = await _make_db(db_name)
    try:
        assert await run(url, source, platform=platform) == 0
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                assert await conn.scalar(text(f"SELECT COUNT(*) FROM {spec.table}")) == 2
                cols = {
                    r["COLUMN_NAME"]: r
                    for r in (await conn.execute(text(
                        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_SET_NAME, COLLATION_NAME "
                        "FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                    ), {"t": spec.table})).mappings().all()
                }
                for name in ("card_json", "raw_json"):
                    assert cols[name]["DATA_TYPE"] == "longtext"
                    assert cols[name]["CHARACTER_SET_NAME"] == "utf8mb4"
                    assert cols[name]["COLLATION_NAME"] == "utf8mb4_unicode_ci"
                # 服务端口径比对：不把大 payload 取回客户端（规避 asyncmy 读缓冲 bug）
                row = (await conn.execute(text(
                    f"SELECT SHA2(`raw_json`, 256) AS sha, LENGTH(`raw_json`) AS len, "
                    f"`card_json` FROM `{spec.table}` WHERE `{spec.pk}` = 'x-2'"
                ))).mappings().one()
                assert row["sha"] == hashlib.sha256(big_raw.encode("utf-8")).hexdigest()
                assert row["len"] == len(big_raw.encode("utf-8")) > 64 * 1024
                assert "🚀" in row["card_json"]
        finally:
            await engine.dispose()
    finally:
        await _drop_db(db_name)


async def test_batch_boundary_1001_rows_mysql(tmp_path):
    if not MYSQL_TEST_URL:
        pytest.skip("未设置 MYSQL_TEST_URL，跳过批提交边界")
    source = make_source(
        tmp_path / "cj.sqlite3",
        [_row(f"p-{i}", json.dumps({"i": i})) for i in range(1001)],
    )
    db_name = f"findora_cj_{uuid.uuid4().hex[:8]}_snapshot_verify"
    url = await _make_db(db_name)
    try:
        assert await run(url, source) == 0
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT COUNT(*) FROM products")) == 1001
        finally:
            await engine.dispose()
    finally:
        await _drop_db(db_name)


async def test_skip_bad_rows_mysql(tmp_path):
    if not MYSQL_TEST_URL:
        pytest.skip("未设置 MYSQL_TEST_URL，跳过 --skip-bad-rows")
    source = make_source(tmp_path / "cj.sqlite3", [
        _row("p-good", '{"ok": true}'),
        _row("p-bad-utf8", '{"ok": true}', detail_json=b"\xff\xfe\xfa"),
        _row("p-bad-json", '{"ok": true}', detail_json="not-json"),
    ])
    db_name = f"findora_cj_{uuid.uuid4().hex[:8]}_snapshot_verify"
    url = await _make_db(db_name)
    try:
        assert await run(url, source, skip_bad_rows=True) == 0
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                pids = (await conn.execute(text("SELECT pid FROM products ORDER BY pid"))).scalars().all()
                assert pids == ["p-good"]
        finally:
            await engine.dispose()
        # 跳过清单落源库同目录，且包含两个坏行 pid
        skip_files = list(tmp_path.glob("snapshot_skipped_*.txt"))
        assert len(skip_files) == 1
        content = skip_files[0].read_text(encoding="utf-8")
        assert "p-bad-utf8" in content and "p-bad-json" in content
    finally:
        await _drop_db(db_name)


async def test_fail_fast_on_bad_row_mysql(tmp_path):
    if not MYSQL_TEST_URL:
        pytest.skip("未设置 MYSQL_TEST_URL，跳过 fail-fast")
    source = make_source(tmp_path / "cj.sqlite3", [
        _row("p-good", '{"ok": true}'),
        _row("p-bad", '{"ok": true}', detail_json=b"\xff\xfe\xfa"),
    ])
    db_name = f"findora_cj_{uuid.uuid4().hex[:8]}_snapshot_verify"
    url = await _make_db(db_name)
    try:
        assert await run(url, source) == 1  # 默认 fail-fast
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                # 目标 products 不应存在（临时表已 DROP，目标原样）
                cnt = await conn.scalar(text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'"))
                assert cnt == 0
        finally:
            await engine.dispose()
    finally:
        await _drop_db(db_name)


async def test_target_db_name_guard_mysql(tmp_path):
    if not MYSQL_TEST_URL:
        pytest.skip("未设置 MYSQL_TEST_URL，跳过库名防护")
    source = make_source(tmp_path / "cj.sqlite3", [_row("p-1", '{"ok": true}')])
    db_name = f"findora_bad_{uuid.uuid4().hex[:8]}"  # 不以 _snapshot_verify 结尾
    url = await _make_db(db_name)
    try:
        assert await run(url, source) == 2  # 连接前被库名闸拦下
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                cnt = await conn.scalar(text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'"))
                assert cnt == 0
        finally:
            await engine.dispose()
    finally:
        await _drop_db(db_name)


async def test_target_db_state_json_guard_mysql(tmp_path):
    if not MYSQL_TEST_URL:
        pytest.skip("未设置 MYSQL_TEST_URL，跳过热路径表防护")
    source = make_source(tmp_path / "cj.sqlite3", [_row("p-1", '{"ok": true}')])
    db_name = f"findora_cj_{uuid.uuid4().hex[:8]}_snapshot_verify"
    url = await _make_db(db_name)
    try:
        # 在目标库塞一张含 state_json 列的热路径表，模拟业务库
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "CREATE TABLE agent_session_states ("
                    "session_id VARCHAR(64) PRIMARY KEY, state_json LONGTEXT, updated_at DATETIME)"))
        finally:
            await engine.dispose()
        # 即使 --allow-db 也不放行（state_json 扫描是第二道硬闸）
        assert await run(url, source, allow_db=True) == 1
    finally:
        await _drop_db(db_name)
