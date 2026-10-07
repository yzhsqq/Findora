"""Pending CJ plans persist and stay isolated without paid CJ or order writes."""
import asyncio
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from app.infrastructure.cj_live_quote import CJLiveQuoteService
from app.infrastructure.purchase_records import PurchaseRecordStore
from app.infrastructure.persistence.cj_catalog import CJCatalog
from app.presentation.purchase_records import register_purchase_record_routes
from scripts.enrich_cj_catalog import migrate, seed_links, store_detail
from scripts.sync_cj_catalog import open_db, store_list_page

PID = "05B050F6-9DF5-4488-9218-B1D919650ADE"
URL = f"https://cjdropshipping.com/product/green-sandalwood-hair-comb-p-{PID}.html"


@pytest.fixture
async def env(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    db = open_db(path)
    store_list_page(db, ("c", "Beauty", "Hair", "Combs"), 1,
                    [{"id": PID, "spu": "CJBJJFTF00034", "nameEn": "Comb", "sellPrice": "3.00"}])
    migrate(db)
    seed_links(db, [{"pid": PID, "spu": "CJBJJFTF00034", "url": URL, "observed_at": "2026-10-05"}])
    store = PurchaseRecordStore(tmp_path / "records.sqlite3")
    live = CJLiveQuoteService.from_snapshot(path, tmp_path / "live.sqlite3")
    live._client = Mock(side_effect=AssertionError("Saving plans must not call CJ"))
    api = FastAPI()
    register_purchase_record_routes(api, lambda: store, lambda: CJCatalog(path), lambda: live)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client, store, db, live
    db.close()


async def save(client, buyer="alice", **body):
    return await client.put("/commerce/purchase-records", params={"buyer_id": buyer},
                            json={"product_id": PID, **body})


async def test_pending_save_is_idempotent_survives_restart_and_does_not_create_order(env):
    client, store, db, live = env
    results = await asyncio.gather(save(client), save(client))
    assert all(r.status_code == 200 for r in results)
    assert sum(r.json()["created"] for r in results) == 1
    record = results[0].json()["record"]
    assert record["status"] == "PENDING_PURCHASE" and record["sku_id"] == ""
    assert record["product"]["source_url"] == URL
    assert await PurchaseRecordStore(store.path).list("alice") == [record]
    assert await store.list("bob") == []
    assert db.execute("SELECT count(*) FROM cj_enrichment_calls").fetchone()[0] == 0
    live._client.assert_not_called()


async def test_delete_is_buyer_scoped_and_does_not_delete_another_buyers_plan(env):
    client, store, _, _ = env
    record_id = (await save(client)).json()["record"]["record_id"]
    await client.delete(f"/commerce/purchase-records/{record_id}", params={"buyer_id": "bob"})
    assert len(await store.list("alice")) == 1
    await client.delete(f"/commerce/purchase-records/{record_id}", params={"buyer_id": "alice"})
    assert await store.list("alice") == []


async def test_selected_sku_uses_existing_live_detail_without_network(env):
    client, store, _, live = env
    # A user may have loaded a detail absent from the published catalog.
    with live._db() as db:
        store_detail(db, PID, {"result": True, "data": {"pid": PID, "variants": [
            {"vid": "v1", "variantSku": "REAL-SKU", "variantKey": "Green", "variantSellPrice": "3.25"}]}})
    response = await save(client, sku_id="REAL-SKU")
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["sku_id"] == "REAL-SKU" and record["product"]["skus"][0]["price_major"] == 3.25
    assert (await save(client, sku_id="OTHER-SKU")).status_code == 422
    assert len(await store.list("alice")) == 1
    live._client.assert_not_called()


async def test_unknown_product_and_client_supplied_price_or_url_are_rejected(env):
    client, store, _, _ = env
    assert (await save(client, product={"source_url": "https://evil.test"})).status_code == 422
    assert (await save(client, price_major=0)).status_code == 422
    response = await client.put("/commerce/purchase-records", params={"buyer_id": "alice"}, json={"product_id": "missing"})
    assert response.status_code == 404
    assert await store.list("alice") == []


async def test_saved_plan_gets_new_links_and_loses_unavailable_links(env):
    client, _, db, _ = env
    db.execute("UPDATE products SET source_url_status='derived'")
    db.commit()
    assert "source_url" not in (await save(client)).json()["record"]["product"]
    db.execute("UPDATE products SET source_url_status='observed'")
    db.commit()
    records = (await client.get("/commerce/purchase-records", params={"buyer_id": "alice"})).json()["records"]
    assert records[0]["product"]["source_url"] == URL
    db.execute("UPDATE products SET detail_status='unavailable:1602001'")
    db.commit()
    records = (await client.get("/commerce/purchase-records", params={"buyer_id": "alice"})).json()["records"]
    assert "source_url" not in records[0]["product"]
