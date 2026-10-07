"""eBay snapshots keep provenance without inventing stock or landed prices."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.amazon_catalog import AmazonCatalog
from app.infrastructure.persistence.amazon_catalog import import_snapshot as import_amazon
from app.infrastructure.persistence.cj_catalog import CJCatalog
from app.infrastructure.persistence.ebay_catalog import EbayCatalog, import_snapshot, normalize
from app.infrastructure.persistence.multi_platform_catalog import MultiPlatformCatalog
from app.presentation.catalog import register_catalog_routes


def record(**overrides):
    return {"product_id": "960005457882", "title": "Dog Toy Holo Card Lot 10x", "brand": "Pokemon",
            "price": "$4.99", "currency": "USD", "condition": "Ungraded - Near mint or better",
            "root_category": "Toys & Hobbies", "product_category": "Toys & Hobbies>Collectible Card Games>Single Cards",
            "availability": "in_stock", "store_country": "US", "seller_name": "XoticMerchandise",
            "item_location": "Saint Paul, Minnesota, United States",
            "url": "https://www.ebay.com/itm/960005457882",
            "image_url": "https://i.ebayimg.com/images/g/bScAAeSwADBqwE6n/s-l1600.webp",
            "product_specifications": [{"specification_name": "Condition", "specification_value": "Near Mint"},
                                       {"specification_name": "Game", "specification_value": "Pokémon TCG"}],
            "variants": [{"variant_type": "Card", "variant_options": [
                {"option_id": "459441505389", "option_name": "STR-01 Yor Forger", "option_price": 4.99},
                {"option_id": "459441505390", "option_name": "STR-02 Mai Sakurajima", "option_price": 5.99,
                 "in_stock": False}]}], **overrides}


# Collected eBay files carry no per-record timestamp; the importer uses file mtime.
FALLBACK = "2026-10-06T13:38:29.838862+00:00"


def amazon_record():
    return {"asin": "B000MD58UM", "title": "Dog toy extra large pack of one", "brand": "KONG",
            "final_price": 17.95, "currency": "USD", "timestamp": "2026-10-06T08:54:40Z",
            "categories": ["Pet Supplies"], "is_available": True,
            "url": "https://www.amazon.com/dp/B000MD58UM"}


@pytest.fixture
def catalogs(tmp_path):
    source, path = tmp_path / "ebay.json", tmp_path / "ebay.sqlite3"
    source.write_text(json.dumps([record(), record(product_id="128116556198", title="Dog Toy Chew Rope Ball",
                                                   price="$2.99", variants=[], url="https://www.ebay.com/itm/128116556198")]))
    import_snapshot(source, path)
    cj_path = tmp_path / "cj.sqlite3"
    with sqlite3.connect(cj_path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        for number in range(1, 3):
            db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
                "250717074835160070" + str(number), "Pet Supplies", "Dogs", "Toys",
                json.dumps({"nameEn": f"Dog toy rope pack {number}", "sellPrice": str(number + 1)}),
                "2026-10-01", None, None, None, None))
    return CJCatalog(cj_path), EbayCatalog(path)


def test_normalization_keeps_listing_facts_and_unknown_stock():
    card = normalize(record(), fallback_timestamp=FALLBACK)
    assert card["updated_at"] == FALLBACK
    assert card["product_id"] == card["canonical_product_id"] == "ebay:us:960005457882"
    assert card["price_major"] == 4.99 and card["price_text"] == "US$4.99"
    assert card["source_platform"] == "eBay" and card["condition"] == "Ungraded - Near mint or better"
    assert card["category"] == "Toys & Hobbies" and card["seller_name"] == "XoticMerchandise"
    assert card["source_url_status"] == "observed" and card["match_status"] == "unverified"
    assert card["stock_known"] is False and all(s["stock"] == 0 for s in card["skus"])
    assert card["skus"][0]["sku_id"] == "ebay:us:960005457882" and card["default_sku_id"] == card["skus"][0]["sku_id"]
    assert card["skus"][1]["spec"] == "Card: STR-01 Yor Forger"
    # A snapshot never proves live stock, so page quantities are not exposed.
    assert "quantity_available" not in card and "landed_total_major" not in card["landed_price"]


def test_missing_price_stays_unknown_and_variation_without_price_is_skipped():
    card = normalize(record(price=None, variants=[{"variant_type": "Card", "variant_options": [
        {"option_id": "459441505389", "option_name": "STR-01", "option_price": None}]}]), fallback_timestamp=FALLBACK)
    assert card["price_kind"] == "unknown" and card["price_text"] == "报价待核实"
    assert "default_sku_id" not in card and card["skus"] == []


def test_record_timestamp_wins_and_missing_observation_time_is_rejected():
    assert normalize(record(timestamp="2026-10-01T00:00:00Z"))["updated_at"] == "2026-10-01T00:00:00Z"
    with pytest.raises(ValueError):
        normalize(record())
    with pytest.raises(ValueError):
        normalize(record(timestamp="2026-10-06 08:54:40"))


def test_import_uses_file_mtime_as_observation_time(tmp_path):
    source, path = tmp_path / "in.json", tmp_path / "catalog.sqlite3"
    source.write_text(json.dumps([record()]))
    import_snapshot(source, path)
    with sqlite3.connect(path) as db:
        card = json.loads(db.execute("SELECT card_json FROM ebay_products").fetchone()[0])
    assert card["updated_at"] == datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat()


@pytest.mark.parametrize("url", ["https://ebay.com.evil.test/itm/960005457882", "http://www.ebay.com/itm/960005457882",
    "https://user:pass@www.ebay.com/itm/960005457882", "https://www.ebay.com/itm/128116556198",
    "https://www.ebay.com:9999/itm/960005457882"])
def test_external_urls_require_correct_platform_and_item(url):
    assert "source_url" not in normalize(record(url=url), fallback_timestamp=FALLBACK)


def test_import_replaces_snapshot_and_rejects_invalid_records(tmp_path):
    source, path = tmp_path / "in.json", tmp_path / "catalog.sqlite3"
    source.write_text(json.dumps([record(), record(product_id="bad")]))
    with pytest.raises(ValueError):
        import_snapshot(source, path)
    assert not path.exists()
    result = import_snapshot(source, path, skip_invalid=True)
    assert result["products"] == 1 and result["skipped_invalid"] == 1
    with sqlite3.connect(path) as db:
        card, raw = db.execute("SELECT card_json,raw_json FROM ebay_products").fetchone()
        assert json.loads(card)["price_major"] == 4.99
        assert json.loads(raw)["seller_name"] == "XoticMerchandise"


@pytest.mark.asyncio
async def test_browse_platform_filter_and_item_id_lookup(catalogs):
    cj, ebay = catalogs
    page = await ebay.browse("holo", "", 1, 10)
    assert page["all_count"] == 2 and page["total"] == 1
    by_id = await ebay.browse("960005457882", "", 1, 10)
    assert by_id["total"] == 1 and by_id["products"][0]["product_id"] == "ebay:us:960005457882"
    catalog = MultiPlatformCatalog(cj, None, None, ebay)
    assert (await catalog.browse(platform="ebay"))["source_counts"] == {"ebay": 2}
    with pytest.raises(ValueError):
        await catalog.browse(platform="amazon")


@pytest.mark.asyncio
async def test_three_platform_recall_keeps_one_representative_each(catalogs):
    cj, ebay = catalogs
    amazon_source = ebay.path.parent / "amazon.json"
    amazon_source.write_text(json.dumps([amazon_record()]))
    amazon_path = ebay.path.parent / "amazon.sqlite3"
    import_amazon(amazon_source, amazon_path)
    amazon = AmazonCatalog(amazon_path)
    catalog = MultiPlatformCatalog(cj, amazon, None, ebay)
    result = await catalog.execute(ProductSearchSpec("dog toy", raw_query="狗玩具", top_k=3))
    assert result["source_candidate_counts"] == {"cj": 2, "amazon": 1, "ebay": 2}
    assert result["source_status"] == {"cj": "ok", "amazon": "ok", "ebay": "ok"}
    assert {c["source_platform"] for c in result["hits"]} == {"CJdropshipping", "Amazon", "eBay"}


@pytest.mark.asyncio
async def test_ebay_item_id_query_skips_fusion_and_reports_existence(catalogs):
    cj, ebay = catalogs
    catalog = MultiPlatformCatalog(cj, None, None, ebay)
    result = await catalog.execute(ProductSearchSpec("960005457882"))
    assert not result["rerank_applied"]
    assert result["hits"] and result["hits"][0]["product_id"] == "ebay:us:960005457882"


def test_ebay_decision_does_not_assert_stock_budget_or_china_shipping():
    from app.application.usecases.shopping_decision import build_decision_report
    result = {"source": "multi", "hits": [normalize(record(), fallback_timestamp=FALLBACK)],
              "query_conditions": {"ship_to": "CN", "price_max_major": 100, "target_currency": "CNY"}}
    report = build_decision_report(result)
    assert report["catalog_source"] == "multi" and report["status"] == "ready"
    checks = report["candidates"][0]["checks"]
    assert all(c["status"] == "unknown" and c["evidence"]["kind"] == "ebay_snapshot"
               for c in checks if c["field"] in {"skus.stock", "ships_to", "price_max_major"})
    assert any("同款" in u for u in report["candidates"][0]["unknowns"])
    assert any(c["field"] == "condition" and c["status"] == "pass"
               and "Near mint" in str(c.get("detail")) and "以商品页为准" in str(c.get("detail")) for c in checks)
    result["hits"] = [normalize(record(is_sold=True, availability=None), fallback_timestamp=FALLBACK)]
    assert build_decision_report(result)["status"] == "no_match"


@pytest.mark.asyncio
async def test_catalog_api_accepts_ebay_platform(catalogs):
    api = FastAPI()
    register_catalog_routes(api, lambda: MultiPlatformCatalog(catalogs[0], None, None, catalogs[1]))
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/commerce/catalog?platform=ebay")
        assert response.status_code == 200
        assert all(p["source_platform"] == "eBay" for p in response.json()["products"])
        assert (await client.get("/commerce/catalog?platform=bad")).status_code == 422
