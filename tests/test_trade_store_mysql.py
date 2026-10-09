"""trade_store 在 MySQL/InnoDB 下的并发幂等契约测试。

SQLite 版（test_trade_store.py）的正确性靠 BEGIN IMMEDIATE 整库写锁；本文件验证
换成 InnoDB 行锁 + FOR UPDATE + 唯一索引后，同一批幂等保证依然成立。

仅在设置 MYSQL_TEST_URL 时运行（CI 无 MySQL），否则整文件跳过。URL 为不带库名的
服务器地址，测试自建独立临时库，不触碰已迁移的 findora 库。本机运行：

    MYSQL_TEST_URL="mysql+asyncmy://root:PASSWORD@127.0.0.1:3306" \
    uv run python -m pytest tests/test_trade_store_mysql.py -q
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.sku import Sku
from app.domain.order.ports.trade_store import TradeStoreError
from app.infrastructure.persistence.sql.repositories import create_engine
from app.infrastructure.persistence.sql.tables import OrderLineRow, OrderRow
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
from app.infrastructure.persistence.sql.trade_tables import TradeOperationRow

pytestmark = pytest.mark.asyncio

MYSQL_TEST_URL = os.getenv("MYSQL_TEST_URL")
NOW = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
ADDRESS = {"recipient_name": "测试买家", "country": "CN", "state": "上海", "city": "上海",
           "address_line": "测试路 1 号", "postal_code": "200000", "phone": "13800000000"}


def product(*, stock=5, price=12900, currency="CNY", sku_id="sku-1", product_id="p-1"):
    return Product(product_id=product_id, title="测试商品", brand="test", category="test",
        origin_country="CN", description="测试用", skus=[Sku(sku_id, "标准款", Money.of(price, currency), stock)])


def payload(*, quantity=2, price=12900, currency="CNY", sku_id="sku-1", product_id="p-1"):
    return {"items": [{"product_id": product_id, "sku_id": sku_id, "title": "测试商品",
        "unit_price_minor": price, "currency": currency, "quantity": quantity}],
        "shipping_address": dict(ADDRESS)}


@pytest.fixture
async def stores():
    if not MYSQL_TEST_URL:
        pytest.skip("未设置 MYSQL_TEST_URL，跳过 MySQL 交易并发契约")
    base = MYSQL_TEST_URL.rstrip("/")
    db_name = f"findora_test_trade_{uuid.uuid4().hex[:12]}"
    # 建临时库：用独立管理连接，跑完整个测试再 drop。
    admin = create_engine(base)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4"))
    except Exception:
        await admin.dispose()
        raise
    url = f"{base}/{db_name}"
    engine1, engine2 = create_engine(url), create_engine(url)
    first, second = SqlTradeStore(engine1, clock=lambda: NOW), SqlTradeStore(engine2, clock=lambda: NOW)
    await first.initialize_inventory([product()])
    yield first, second, engine1, engine2
    await engine1.dispose()
    await engine2.dispose()
    async with admin.begin() as conn:
        await conn.execute(text(f"DROP DATABASE `{db_name}`"))
    await admin.dispose()


async def prepare(store, *, operation_id="operation-1", buyer_id="buyer-1", session_id="session-1",
                  body=None, action="create", expiry=None):
    return await store.prepare_confirmation(operation_id=operation_id, buyer_id=buyer_id, session_id=session_id,
        action=action, payload=body or payload(), expires_at=expiry or NOW + timedelta(minutes=5))


async def resolve(store, confirmation, *, approved=True, buyer_id="buyer-1", session_id="session-1", hash_value=None):
    return await store.resolve_confirmation(confirmation["confirmation_id"], buyer_id=buyer_id, session_id=session_id,
        snapshot_hash=hash_value or confirmation["snapshot_hash"], approved=approved)


async def counts(engine):
    async with engine.connect() as db:
        return {table.__tablename__: await db.scalar(select(func.count()).select_from(table))
                for table in [OrderRow, OrderLineRow, TradeOperationRow]}


async def test_concurrent_resolve_of_same_confirmation_yields_one_order(stores):
    first, second, engine, _ = stores
    confirmation = await prepare(first)
    results = await asyncio.gather(resolve(first, confirmation), resolve(second, confirmation))
    assert results[0] == results[1]
    assert await first.get_inventory() == {"sku-1": 3}
    assert await counts(engine) == {"orders": 1, "order_items": 1, "trade_operations": 1}


async def test_concurrent_prepare_of_same_operation_is_idempotent(stores):
    first, second, _, _ = stores
    original = await prepare(first)
    a, b = await asyncio.gather(prepare(first, expiry=NOW + timedelta(minutes=10)),
                                prepare(second, expiry=NOW + timedelta(minutes=10)))
    assert a == original and b == original
    await resolve(first, original)
    assert (await prepare(second))["status"] == "approved"


async def test_independent_operations_race_for_last_stock(stores):
    first, second, engine, _ = stores
    c1 = await prepare(first, operation_id="a", body=payload(quantity=4))
    c2 = await prepare(second, operation_id="b", body=payload(quantity=4))
    results = await asyncio.gather(resolve(first, c1), resolve(second, c2), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    errors = [r for r in results if isinstance(r, TradeStoreError)]
    assert len(errors) == 1 and errors[0].code == "INSUFFICIENT_STOCK"
    assert await second.get_inventory() == {"sku-1": 1}
    assert await counts(engine) == {"orders": 1, "order_items": 1, "trade_operations": 1}


async def test_concurrent_cancel_restores_stock_once(stores):
    first, second, engine, _ = stores
    placed = await resolve(first, await prepare(first))
    order_id = placed["result"]["order_id"]
    cancellation = await prepare(first, operation_id="cancel", action="cancel",
                                 body={"order_id": order_id, "reason": "不需要了"})
    results = await asyncio.gather(resolve(first, cancellation), resolve(second, cancellation))
    assert results[0] == results[1]
    assert results[0]["result"]["status"] == "CANCELLED"
    assert await second.get_inventory() == {"sku-1": 5}
    assert await counts(engine) == {"orders": 1, "order_items": 1, "trade_operations": 2}
