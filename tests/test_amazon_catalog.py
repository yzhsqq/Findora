"""Imported snapshots retain provenance without inventing stock or landed prices."""
import json
import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.application.usecases.shopping_decision import build_decision_report
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.amazon_catalog import AmazonCatalog, import_snapshot, normalize
from app.infrastructure.persistence.cj_catalog import CJCatalog
from app.infrastructure.persistence.multi_platform_catalog import MultiPlatformCatalog
from app.infrastructure.persistence.context_evidence import product_decision_view
from app.infrastructure.purchase_records import PurchaseRecordStore
from app.presentation.catalog import register_catalog_routes
from app.presentation.purchase_records import register_purchase_record_routes


def record(**overrides):
    return {"asin": "B000MD58UM", "title": "Dog toy extra large pack of one", "brand": "KONG",
            "final_price": 17.95, "currency": "USD", "timestamp": "2026-10-06T08:54:40Z",
            "categories": ["Pet Supplies"], "zipcode": "11001", "is_available": True,
            "max_quantity_available": 30, "url": "https://www.amazon.com/dp/B000MD58UM?th=1",
            "prices_breakdown": {"deal_type": "Prime Big Deal"},
            "variations": [{"asin": "B000MD58UM", "name": "XL Pack of 1", "price": 99, "currency": "USD"},
                           {"asin": "B000MD57ZI", "name": "L Pack of 1", "price": 12.99, "currency": "USD"},
                           {"asin": "B000MCZW5E", "price": None}], **overrides}


@pytest.fixture
def catalogs(tmp_path):
    source, path = tmp_path / "amazon.json", tmp_path / "amazon.sqlite3"
    source.write_text(json.dumps([record(), record(asin="B000AAAA01", title="Dog toy chew rope", final_price=5,
                                                    url="https://www.amazon.com/dp/B000AAAA01", variations=[])]))
    import_snapshot(source, path)
    cj_path = tmp_path / "cj.sqlite3"
    with sqlite3.connect(cj_path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        for number in range(1, 4):
            db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
                "250717074835160070" + str(number), "Pet Supplies", "Dogs", "Toys",
                json.dumps({"nameEn": f"Dog toy rope pack {number}", "sellPrice": str(number + 1)}),
                "2026-10-01", None, None, None, None))
    return CJCatalog(cj_path), AmazonCatalog(path)


def test_normalization_keeps_unknowns_and_variant_price_scope():
    card = normalize(record())
    assert card["product_id"] == card["canonical_product_id"] == "amazon:us:B000MD58UM"
    assert card["price_major"] == card["skus"][0]["price_major"] == 17.95
    assert len(card["skus"]) == 2  # null-priced variations are not zero-dollar products.
    assert card["stock_known"] is False
    assert all(s["stock"] == 0 and not s["stock_known"] for s in card["skus"])
    assert "ships_to" not in card and "landed_total_major" not in card["landed_price"]
    assert card["origin_country"] == "" and card["delivery_zipcode"] == "11001"
    assert card["source_url_status"] == "observed" and card["match_status"] == "unverified"
    assert "Prime Big Deal" in card["price_conditions"][0]


def test_missing_price_remains_unknown_and_does_not_borrow_another_variant_price():
    card = normalize(record(final_price=None))
    assert card["price_kind"] == "unknown" and card["price_text"] == "报价待核实"
    assert "default_sku_id" not in card
    assert all(s["variant_id"] != "B000MD58UM" for s in card["skus"])


@pytest.mark.parametrize("url", ["https://amazon.com.evil.test/dp/B000MD58UM", "http://www.amazon.com/dp/B000MD58UM",
    "https://user:pass@www.amazon.com/dp/B000MD58UM", "https://www.amazon.com/dp/B000AAAA01",
    "https://www.amazon.com:9999/dp/B000MD58UM"])
def test_external_urls_require_correct_platform_and_asin(url):
    assert "source_url" not in normalize(record(url=url))


def test_import_latest_duplicate_and_failed_import_preserve_snapshot(tmp_path):
    source, path = tmp_path / "in.json", tmp_path / "catalog.sqlite3"
    source.write_text(json.dumps([record(), record(timestamp="2026-10-01T00:00:00Z", final_price=999)]))
    result = import_snapshot(source, path)
    assert result["products"] == 1 and result["duplicates"] == 1
    with sqlite3.connect(path) as db:
        card, raw = db.execute("SELECT card_json,raw_json FROM amazon_products").fetchone()
        assert json.loads(card)["price_major"] == 17.95
        assert json.loads(raw)["max_quantity_available"] == 30
    source.write_text(json.dumps([record(asin="bad")]))
    with pytest.raises(ValueError):
        import_snapshot(source, path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM amazon_products").fetchone()[0] == 1


class Reranker:
    def __init__(self, bad=None):
        self.calls = []
        self.bad = bad

    async def rerank(self, query, texts):
        self.calls.append((query, texts))
        if self.bad is not None:
            return self.bad
        return [0.9 if "extra large" in t else 0.1 for t in texts]


@pytest.mark.asyncio
async def test_union_pagination_platform_filter_and_no_browse_rerank(catalogs):
    reranker = Reranker()
    catalog = MultiPlatformCatalog(*catalogs, reranker)
    first = await catalog.browse(page=1, page_size=2)
    second = await catalog.browse(page=2, page_size=2)
    assert first["source_counts"] == {"cj": 3, "amazon": 2}
    assert first["total"] == 5
    assert {p["source_platform"] for p in first["products"]} == {"CJdropshipping", "Amazon"}
    assert not {p["product_id"] for p in first["products"]} & {p["product_id"] for p in second["products"]}
    assert (await catalog.browse(platform="amazon"))["total"] == 2
    assert reranker.calls == []


@pytest.mark.asyncio
async def test_rerank_after_joint_recall_before_top_k_and_budget(catalogs):
    reranker = Reranker()
    catalog = MultiPlatformCatalog(*catalogs, reranker)
    result = await catalog.execute(ProductSearchSpec("dog toy", raw_query="大号狗玩具", top_k=1))
    assert result["hits"][0]["external_product_id"] == "B000MD58UM"
    assert result["rerank_applied"] and result["total_candidates"] == 5
    assert len(reranker.calls) == 1 and len(reranker.calls[0][1]) == 5
    assert reranker.calls[0][0] == "大号狗玩具"
    limited = await catalog.execute(ProductSearchSpec("dog toy", top_k=1, price_max_major=10, target_currency="USD"))
    assert limited["hits"][0]["price_major"] <= 10
    assert len(reranker.calls[-1][1]) == 5


@pytest.mark.parametrize("bad", [[float("nan")] * 5, [0.3], [True] * 5])
@pytest.mark.asyncio
async def test_invalid_rerank_preserves_fused_results(catalogs, bad):
    catalog = MultiPlatformCatalog(*catalogs, Reranker(bad))
    result = await catalog.execute(ProductSearchSpec("dog toy", top_k=5))
    assert not result["rerank_applied"] and len(result["hits"]) == 5
    assert len({p["score"] for p in result["hits"]}) == 3


@pytest.mark.asyncio
async def test_comparison_keeps_platform_representatives_without_changing_scores(catalogs):
    class BiasedReranker:
        async def rerank(self, query, texts):
            return [0.9 if "rope pack" in t else 0.2 for t in texts]
    result = await MultiPlatformCatalog(*catalogs, BiasedReranker()).execute(ProductSearchSpec("dog toy", top_k=2))
    assert {c["source_platform"] for c in result["hits"]} == {"Amazon", "CJdropshipping"}
    assert [c["score"] for c in result["hits"]] == [0.9, 0.2]
    assert result["source_candidate_counts"] == {"cj": 3, "amazon": 2}


@pytest.mark.asyncio
async def test_filtered_known_identifier_is_not_reported_missing(catalogs):
    result = await catalogs[1].execute(ProductSearchSpec("B000MD58UM", price_max_major=1, target_currency="USD"))
    assert result["hits"] == [] and result["existence_checked"] and result["missing_identifiers"] == []


def test_agent_projection_retains_amazon_price_provenance():
    card = normalize(record())
    projected = product_decision_view({"hits": [card]})["hits"][0]
    for field in ("updated_at", "delivery_zipcode", "price_conditions", "source_region", "match_status", "seller_name"):
        assert projected[field] == card[field]


@pytest.mark.asyncio
async def test_multi_platform_keeps_cj_pending_purchase_and_rejects_amazon(catalogs, tmp_path):
    api = FastAPI()
    catalog = MultiPlatformCatalog(*catalogs)
    store = PurchaseRecordStore(tmp_path / "plans.sqlite3")
    register_purchase_record_routes(api, lambda: store, lambda: catalog)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.put("/commerce/purchase-records?buyer_id=test-buyer", json={"product_id": "2507170748351600701"})
        assert response.status_code == 200 and response.json()["record"]["status"] == "PENDING_PURCHASE"
        assert (await client.get("/commerce/purchase-records?buyer_id=test-buyer")).json()["total"] == 1
        response = await client.put("/commerce/purchase-records?buyer_id=test-buyer", json={"product_id": "amazon:us:B000MD58UM"})
        assert response.status_code == 404


@pytest.mark.parametrize("query,expected", [("B000MD58UM", 1), ("amazon:us:B000MD58UM", 1),
    ("B000XXXXX9", 0), ("2507170748351600701", 1)])
@pytest.mark.asyncio
async def test_exact_ids_ignore_previous_category_and_skip_rerank(catalogs, query, expected):
    reranker = Reranker()
    result = await MultiPlatformCatalog(*catalogs, reranker).execute(ProductSearchSpec(query, category="Beauty"))
    assert len(result["hits"]) == expected and not result["rerank_applied"]
    assert not reranker.calls


@pytest.mark.asyncio
async def test_ten_letter_keyword_is_not_an_identifier(catalogs):
    cj, amazon = catalogs
    result = await MultiPlatformCatalog(cj, amazon, Reranker()).execute(ProductSearchSpec("decoration"))
    assert result["recall_strategy"].startswith("multi_")


@pytest.mark.asyncio
async def test_one_source_outage_is_explicit_not_false_no_match(catalogs, monkeypatch):
    cj, amazon = catalogs
    async def broken(spec):
        raise ValueError("not ready")
    monkeypatch.setattr(cj, "execute", broken)
    result = await MultiPlatformCatalog(cj, amazon).execute(ProductSearchSpec("dog toy"))
    assert result["partial_results"] and result["source_status"]["cj"] == "unavailable"
    assert len(result["hits"]) == 2 and "cj" in result["data_scope"]
    report = build_decision_report(result)
    assert report["partial_results"] and report["source_status"]["cj"] == "unavailable"


@pytest.mark.asyncio
async def test_vector_bootstrap_delegates_only_cj_and_favorites_keep_both(catalogs):
    cj, amazon = catalogs
    catalog = MultiPlatformCatalog(cj, amazon)
    assert all(not d.product_id.startswith("amazon:") for d in await catalog.list_all())
    catalog.set_vector_available(True)
    assert cj.vector_available
    cards = (await catalog.browse())["products"]
    assert len(await catalog.localize_saved_cards(cards)) == 5


def test_amazon_decision_does_not_assert_stock_budget_or_china_shipping():
    result = {"source": "multi", "hits": [normalize(record())],
              "query_conditions": {"ship_to": "CN", "price_max_major": 100, "target_currency": "CNY"}}
    report = build_decision_report(result)
    assert report["catalog_source"] == "multi" and report["status"] == "ready"
    checks = report["candidates"][0]["checks"]
    assert all(c["status"] == "unknown" and c["evidence"]["kind"] == "amazon_snapshot"
               for c in checks if c["field"] in {"skus.stock", "ships_to", "price_max_major"})
    assert any("同款" in u for u in report["candidates"][0]["unknowns"])
    result["hits"] = [normalize(record(is_available=False))]
    assert build_decision_report(result)["status"] == "no_match"


@pytest.mark.asyncio
async def test_catalog_api_platform_filter_and_validation(catalogs):
    api = FastAPI()
    catalog = MultiPlatformCatalog(*catalogs)
    register_catalog_routes(api, lambda: catalog)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/commerce/catalog?platform=amazon&query=狗玩具")
        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert all(p["source_platform"] == "Amazon" for p in response.json()["products"])
        assert (await client.get("/commerce/catalog?platform=bad")).status_code == 422
