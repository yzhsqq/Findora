"""Contract checks for CJ snapshot presentation, without network calls."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.application.usecases.shopping_decision import build_decision_report
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient, ProductVectorIndex, VectorHit
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.cj_catalog import CJCatalog
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex


class _Embedding(EmbeddingClient):
    def __init__(self):
        self.texts = []

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_real_local_index_bootstrap_and_cj_search_report_dense_capability(tmp_path):
    path = tmp_path / "cj.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "2507170748351600701", "Pet Supplies", "Dogs", "Toys",
            json.dumps({"nameEn": "Dog toy", "sellPrice": "4"}), "2026-10-08", None, None, None, None))
    index = QdrantProductIndex(SimpleNamespace(qdrant_url="", qdrant_collection="test", data_dir=tmp_path))
    catalog = CJCatalog(path, embedder=_Embedding(), vector_index=index, hybrid_enabled=True)
    try:
        ready = await bootstrap_product_index(catalog, catalog.embedder, index, "fake", 2)
        assert ready
        catalog.set_vector_available(ready)
        result = await catalog.execute(ProductSearchSpec("dog toy", raw_query="狗玩具"))
        assert result["hits"][0]["product_id"] == "2507170748351600701"
        assert result["recall_mode"] == "dense_only" and result["query_variants"]["bm25"] == ""
        assert result["recall_strategy"] == "cj_qdrant_dense"
    finally:
        await index.close()


class _VectorIndex(ProductVectorIndex):
    def __init__(self, hits: list[VectorHit]):
        self.hits = hits
        self.hybrid_calls = []

    async def product_fingerprints(self) -> dict[str, str]:
        return {}

    async def ensure_ready(self, vector_dim: int) -> None:
        return None

    async def upsert_products(self, products, embeddings, fingerprints) -> None:
        return None

    async def delete_products(self, product_ids: list[str]) -> None:
        return None

    async def search(self, embedding: list[float], top_n: int) -> list[VectorHit]:
        return self.hits[:top_n]

    async def hybrid_search(self, dense_queries: list[list[float]], english_query: str, top_n: int) -> list[VectorHit]:
        self.hybrid_calls.append((dense_queries, english_query, top_n))
        return self.hits[:top_n]


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


@pytest.mark.asyncio
async def test_cj_catalog_finds_exact_sku_even_with_previous_category_filter(tmp_path):
    path = tmp_path / "cj_catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "2507170748351600700", "Home, Garden & Furniture", "Storage", "Bags",
            json.dumps({"nameEn": "Bouquet Buggy Hanging Flower Bag", "sellPrice": "3.02"}),
            "2026-01-01", json.dumps({"variants": [{"variantSku": "CJYD243282601AZ",
                                                 "vid": "2507170748351601500", "variantSellPrice": "3.02"}]}),
            "2026-01-01", None, None,
        ))
    catalog = CJCatalog(path)
    by_sku = await catalog.browse("cjyd243282601az", "Bags & Shoes")
    assert by_sku["total"] == 1
    assert by_sku["products"][0]["product_id"] == "2507170748351600700"
    assert (await catalog.browse("2507170748351601500"))["total"] == 1


@pytest.mark.asyncio
async def test_cj_catalog_finds_uuid_product_id_without_matching_title_or_category(tmp_path):
    path = tmp_path / "cj_catalog.sqlite3"
    product_id = "04AF4351-7F6B-471D-81C0-DBF17E5CD296"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
            product_id, "Consumer Electronics", "Audio", "Speakers",
            json.dumps({"nameEn": "Alarm clock bluetooth speaker", "sellPrice": "5.00"}),
            "2026-01-01", None, None, None, None,
        ))
    catalog = CJCatalog(path)
    page = await catalog.browse(product_id.lower(), "Bags & Shoes")
    assert page["total"] == 1
    assert page["products"][0]["product_id"] == product_id


@pytest.mark.asyncio
async def test_cj_catalog_matches_chinese_attributes_to_english_title(tmp_path):
    path = tmp_path / "cj_catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        for pid, title in (
            ("comb", "Green Sandalwood Hair Comb"),
            ("tray", "Sandalwood Incense Tray"),
            ("hat", "Green Outdoor Hat"),
        ):
            db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
                pid, "Health, Beauty & Hair", "Hair", "Combs",
                json.dumps({"nameEn": title, "sellPrice": "2.00"}),
                "2026-01-01", None, None, None, None,
            ))
    default_page = await CJCatalog(path).browse("绿色檀木梳头用的梳子")
    assert default_page["products"] == []
    page = await CJCatalog(path, experimental_lexicon=True).browse("绿色檀木梳头用的梳子")
    assert page["products"][0]["product_id"] == "comb"


@pytest.mark.asyncio
async def test_cj_hybrid_uses_vector_for_cross_language_recall_and_category_is_soft(tmp_path):
    path = tmp_path / "cj_catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        for pid, category, title in (
            ("dog-cup", "Pet Supplies", "Portable Outdoor Stainless Steel Water Cup for Small Dog"),
            ("travel-bag", "Bags & Shoes", "Lightweight Travel Bag"),
        ):
            db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
                pid, category, "Accessories", "Other",
                json.dumps({"nameEn": title, "sellPrice": "5.00"}),
                "2026-01-01", None, None, None, None,
            ))
    embedder = _Embedding()
    index = _VectorIndex([VectorHit("dog-cup", 0.95)])
    catalog = CJCatalog(
        path,
        embedder=embedder,
        vector_index=index,
        hybrid_enabled=True,
    )
    catalog.set_vector_available(True)

    result = await catalog.execute(ProductSearchSpec(
        "portable stainless steel dog water cup", raw_query="给小狗户外喝水的不锈钢便携水杯",
        category="旅行装备", top_k=5,
    ))

    assert result["recall_strategy"] == "cj_qdrant_rrf"
    assert result["category_mode"] == "soft_boost"
    assert result["vector_available"] is True
    assert result["hits"][0]["product_id"] == "dog-cup"
    assert embedder.texts == ["给小狗户外喝水的不锈钢便携水杯", "portable stainless steel dog water cup"]
    assert index.hybrid_calls[0][1] == "portable stainless steel dog water cup"
    assert result["query_variants"]["bm25"] == "portable stainless steel dog water cup"

    mixed = await catalog.execute(ProductSearchSpec(
        "Type-C 千兆网卡", raw_query="没有网口的笔记本用 Type-C 接千兆有线网络", top_k=5,
    ))
    assert embedder.texts[-2:] == ["没有网口的笔记本用 Type-C 接千兆有线网络", "Type-C 千兆网卡"]
    assert len(index.hybrid_calls[-1][0]) == 2
    assert index.hybrid_calls[-1][1] == "Type-C"
    assert mixed["query_variants"] == {
        "dense": ["没有网口的笔记本用 Type-C 接千兆有线网络", "Type-C 千兆网卡"],
        "bm25": "Type-C",
    }


@pytest.mark.asyncio
async def test_cj_hybrid_outage_is_not_reported_as_empty_catalog(tmp_path):
    path = tmp_path / "cj_catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "camera", "Consumer Electronics", "Camera", "Digital Cameras",
            json.dumps({"nameEn": "Waterproof Dual Screen Digital Camera", "sellPrice": "20.00"}),
            "2026-01-01", None, None, None, None,
        ))
    catalog = CJCatalog(path, hybrid_enabled=True)

    with pytest.raises(ValueError, match="检索暂不可用"):
        await catalog.execute(ProductSearchSpec(
            "waterproof digital camera", category="旅行装备", top_k=5,
        ))


@pytest.mark.asyncio
async def test_cj_hybrid_failure_can_recover_explicit_id_from_raw_query(tmp_path):
    path = tmp_path / "cj_catalog.sqlite3"
    product_id = "2507170748351600700"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
            product_id, "Pet Supplies", "Dog", "Cups",
            json.dumps({"nameEn": "Dog Water Cup", "sellPrice": "5.00"}),
            "2026-01-01", None, None, None, None,
        ))

    class FailingIndex(_VectorIndex):
        async def hybrid_search(self, dense_queries, english_query, top_n):
            raise RuntimeError("Qdrant unavailable")

    catalog = CJCatalog(path, embedder=_Embedding(), vector_index=FailingIndex([]), hybrid_enabled=True)
    catalog.set_vector_available(True)
    result = await catalog.execute(ProductSearchSpec("dog cup", raw_query=f"请查询商品 {product_id}"))
    assert result["hits"][0]["product_id"] == product_id
    assert result["degraded_from"] == "cj_qdrant_unavailable"


@pytest.mark.asyncio
async def test_cj_hybrid_does_not_invent_match_for_unknown_opaque_identifier(tmp_path):
    path = tmp_path / "cj_catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "camera", "Consumer Electronics", "Camera", "Digital Cameras",
            json.dumps({"nameEn": "Waterproof Dual Screen Digital Camera", "sellPrice": "20.00"}),
            "2026-01-01", None, None, None, None,
        ))
    catalog = CJCatalog(
        path,
        embedder=_Embedding(),
        vector_index=_VectorIndex([VectorHit("camera", 0.99)]),
        hybrid_enabled=True,
    )
    catalog.set_vector_available(True)

    result = await catalog.execute(ProductSearchSpec("zzzxxyyqplm90001", top_k=5))

    assert result["hits"] == []
    assert result["recall_strategy"] == "cj_snapshot_keyword"
