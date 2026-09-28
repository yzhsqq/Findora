"""Contract checks for CJ snapshot presentation, without network calls."""
import json
import sqlite3

import pytest

from app.application.usecases.shopping_decision import build_decision_report
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.cj_catalog import CJCatalog


@pytest.mark.asyncio
async def test_cj_snapshot_has_real_fields_and_keeps_unknown_fulfillment_unknown(tmp_path):
    path = tmp_path / "cj_catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "cj-1", "Bags & Shoes", "Travel", "Backpacks",
            json.dumps({"nameEn": "Travel Backpack", "sellPrice": "10.00 -- 20.00", "bigImage": "https://example.com/p.jpg"}),
            "2026-01-01", None, None, None, None,
        ))
    catalog = CJCatalog(path)
    page = await catalog.browse("旅行背包")
    assert page["total"] == 1
    card = page["products"][0]
    assert card["source_platform"] == "CJdropshipping"
    assert card["price_kind"] == "range" and card["price_text"] == "US$10.00–20.00"
    assert card["skus"] == [] and card["stock_known"] is False
    assert card["ships_to"] == [] and "landed_price" not in card
    result = await catalog.execute(ProductSearchSpec("旅行背包", ship_to="CN", price_max_major=100, target_currency="CNY"))
    result["query_conditions"] = {"normalized_query": "旅行背包", "ship_to": "CN", "price_max_major": 100, "target_currency": "CNY"}
    report = build_decision_report(result)
    assert report["status"] == "ready"
    candidate = report["candidates"][0]
    assert candidate["sku_id"] == ""
    assert all(item["status"] != "pass" for item in candidate["checks"] if item["field"] in {"skus.stock", "ships_to", "price_max_major"})
    assert any("到手价" in value for value in candidate["unknowns"])
