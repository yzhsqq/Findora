"""Quote math, source labels and cache behavior without spending CJ points."""
import json
from datetime import datetime, timezone

import httpx
import pytest

import app.infrastructure.cj_live_quote as quote_module
from app.infrastructure.cj_live_quote import CJLiveQuoteService
from app.infrastructure.persistence.cj_catalog import CJCatalog
from scripts.sync_cj_catalog import open_db, store_list_page


def _snapshot(path):
    db = open_db(path)
    store_list_page(db, ("cat", "Bags & Shoes", "Travel", "Backpacks"), 1, [{
        "id": "1234567890123456", "nameEn": "Travel Backpack", "sellPrice": "6.00-8.00",
        "description": "<p>Water-resistant travel bag</p>",
    }])
    now = datetime.now(timezone.utc).isoformat()
    detail = {
        "pid": "1234567890123456", "description": "<p>Detailed carry bag</p>",
        "supplierName": "Sample Supplier", "materialNameEnSet": ["Nylon"], "productWeight": "500",
        "variants": [{"vid": "vid-1", "variantSku": "sku-1", "variantKey": "Blue", "variantSellPrice": "6.00"}],
    }
    stock = {"inventories": [{"countryCode": "CN", "totalInventoryNum": 10}],
             "variantInventories": [{"vid": "vid-1", "inventory": [{"countryCode": "CN", "totalInventory": 10,
                                                                         "cjInventory": 10}]}]}
    with db:
        db.execute("UPDATE products SET detail_json=?,detail_fetched_at=?,inventory_json=?,inventory_fetched_at=? WHERE pid=?",
                   (json.dumps(detail), now, json.dumps(stock), now, "1234567890123456"))
    db.close()


@pytest.mark.asyncio
async def test_quote_uses_selected_variant_cj_fee_and_short_cache(tmp_path, monkeypatch):
    path = tmp_path / "cj.sqlite3"
    _snapshot(path)
    service = CJLiveQuoteService(path)
    calls = []

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(service, "_client", lambda: (Client(), "test-token"))

    def call(client, token, db, endpoint, **kwargs):
        calls.append(endpoint)
        assert kwargs["body"]["products"] == [{"quantity": 2, "vid": "vid-1"}]
        return [{"logisticName": "CJ route", "totalPostageFee": "3.00",
                 "taxesFee": None, "clearanceOperationFee": None}]

    monkeypatch.setattr(service, "_call", call)
    quote = await service.quote("1234567890123456", "sku-1", "CN", 2)
    assert quote["product_subtotal_usd"] == 12
    assert quote["cj_trial_total_usd"] == 15
    assert quote["fee_status"] == "tax_or_clearance_unknown"
    assert quote["ship_from_warehouse"] == "CN"
    assert quote["selection_mode"] == "selected_sku"
    assert (await service.quote("1234567890123456", "sku-1", "CN", 2))["cache_hit"] is True
    assert calls == ["/logistic/freightCalculate"]

    restarted = CJLiveQuoteService(path)
    monkeypatch.setattr(restarted, "_client", lambda: (_ for _ in ()).throw(AssertionError("cache missed")))
    assert (await restarted.quote("1234567890123456", "sku-1", "CN", 2))["cache_hit"] is True


@pytest.mark.asyncio
async def test_existing_snapshot_exposes_supplier_not_brand(tmp_path):
    path = tmp_path / "cj.sqlite3"
    _snapshot(path)
    card = (await CJCatalog(path).browse())["products"][0]
    assert card["supplier_name"] == "Sample Supplier" and card["brand"] == ""
    assert card["origin_country"] == "" and card["ship_from_warehouses"] == ["CN"]
    assert card["material_tags"] == ["Nylon"] and card["weight_kg"] == 0.5
    assert card["skus"][0]["variant_id"] == "vid-1"


def test_auth_falls_back_to_official_mirror_and_reuses_token(tmp_path, monkeypatch):
    service = CJLiveQuoteService(tmp_path / "cj.sqlite3")
    calls = []

    class Client:
        def __init__(self, *, base_url, timeout):
            self.base_url = base_url

        def post(self, endpoint, *, json):
            calls.append((self.base_url, endpoint))
            if self.base_url == quote_module.API_BASE:
                raise httpx.ConnectError("primary unavailable")

            class Response:
                def json(self):
                    return {"result": True, "data": {"accessToken": "cached-token"}}

            return Response()

        def close(self):
            pass

    monkeypatch.setattr(quote_module.httpx, "Client", Client)
    monkeypatch.setattr(quote_module, "dotenv_values", lambda _: {"CJdropshipping_key": "test-key"})
    monkeypatch.setattr(service, "_pace", lambda: None)
    first, first_token = service._client()
    second, second_token = service._client()
    first.close()
    second.close()
    assert first_token == second_token == "cached-token"
    assert calls == [
        (quote_module.API_BASE, "/authentication/getAccessToken"),
        (quote_module.API_MIRROR, "/authentication/getAccessToken"),
    ]
