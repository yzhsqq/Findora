"""CJ Agent rerank boundaries, using isolated snapshots and no network calls."""
import asyncio
import json
import sqlite3

import pytest

from app.domain.catalog.ports.retrieval_ports import VectorHit
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence import cj_catalog
from app.infrastructure.persistence.cj_catalog import CJCatalog


_IDS = ["2507170748351600701", "2507170748351600702", "2507170748351600703"]


class _Embedding:
    def __init__(self):
        self.calls = []

    async def embed_batch(self, texts):
        self.calls.append(texts)
        return [[1.0, 0.0] for _ in texts]


class _Index:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def hybrid_search(self, vectors, english_query, top_n):
        self.calls.append((vectors, english_query, top_n))
        return self.hits[:top_n]


class _Reranker:
    def __init__(self, scores=None, error=None):
        self.scores = scores
        self.error = error
        self.calls = []

    async def rerank(self, query, documents):
        self.calls.append((query, documents))
        if self.error:
            raise self.error
        return self.scores


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE products(pid TEXT PRIMARY KEY,first_category TEXT,second_category TEXT,
            third_category TEXT,list_json TEXT,list_fetched_at TEXT,detail_json TEXT,detail_fetched_at TEXT,
            inventory_json TEXT,inventory_fetched_at TEXT)""")
        for pid, category, title, price in (
            (_IDS[0], "Bags & Shoes", "Travel Backpack", "10.00"),
            (_IDS[1], "Home, Garden & Furniture", "Stainless Steel Travel Water Cup", "5.00"),
            (_IDS[2], "Home, Garden & Furniture", "Premium Travel Bottle", "100.00"),
        ):
            db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?)", (
                pid, category, "Accessories", "Other",
                json.dumps({"nameEn": title, "sellPrice": price}), "2026-01-01",
                json.dumps({"variants": [{"variantSku": "CJTEST000000" + pid[-1]}]}),
                "2026-01-01", None, None,
            ))
    return path


def _catalog(snapshot, reranker, *, hits=None, hybrid=True):
    index = _Index(hits if hits is not None else [
        VectorHit(pid, score) for pid, score in zip(_IDS, [0.9, 0.8, 0.7])
    ])
    catalog = CJCatalog(snapshot, embedder=_Embedding(), vector_index=index,
                        reranker=reranker, hybrid_enabled=hybrid)
    catalog.set_vector_available(True)
    return catalog


@pytest.mark.asyncio
async def test_rerank_promotes_candidate_before_top_k_and_uses_original_need(snapshot):
    reranker = _Reranker([0.1, 0.9, 0.2])
    catalog = _catalog(snapshot, reranker)
    result = await catalog.execute(ProductSearchSpec(
        "travel water cup", raw_query="出门用的不锈钢水杯，不要背包", category="旅行装备", top_k=1,
    ))
    assert result["hits"][0]["product_id"] == _IDS[1]
    assert result["hits"][0]["score"] == 0.9
    assert result["recall_strategy"] == "cj_qdrant_rrf_rerank"
    assert result["rerank_applied"] is True
    assert result["category_mode"] == "rerank"
    assert result["total_candidates"] == 3
    query, documents = reranker.calls[0]
    assert query == "出门用的不锈钢水杯，不要背包"
    assert len(documents) == 3
    assert "Travel Backpack" in documents[0]
    assert "Stainless Steel Travel Water Cup" in documents[1]


@pytest.mark.asyncio
async def test_rerank_keeps_budget_filter_and_fills_top_k(snapshot):
    catalog = _catalog(snapshot, _Reranker([0.1, 0.8, 0.99]))
    result = await catalog.execute(ProductSearchSpec(
        "travel cup", top_k=1, price_max_major=10, target_currency="USD",
    ))
    assert result["hits"][0]["product_id"] == _IDS[1]
    assert result["rerank_applied"] is True
    assert result["hits"][0]["stock_known"] is False
    assert result["hits"][0]["ships_to"] == []
    assert "landed_price" not in result["hits"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,strategy", [
    ("travel cup", "cj_qdrant_rrf_rerank"),
    ("旅行水杯", "cj_qdrant_dense_rerank"),
])
async def test_rerank_without_raw_query_uses_normalized_query(snapshot, query, strategy):
    reranker = _Reranker([0.1, 0.9, 0.2])
    result = await _catalog(snapshot, reranker).execute(ProductSearchSpec(query))
    assert reranker.calls[0][0] == query
    assert result["recall_strategy"] == strategy


@pytest.mark.asyncio
@pytest.mark.parametrize("scores,error", [
    (None, RuntimeError("service unavailable")),
    (None, TimeoutError()),
    ([0.9], None),
    ([0.1, float("nan"), 0.2], None),
    ([0.1, float("inf"), 0.2], None),
    ([0.1, "invalid", 0.2], None),
    (None, None),
])
async def test_invalid_rerank_preserves_recall_and_reports_no_rerank(snapshot, scores, error):
    result = await _catalog(snapshot, _Reranker(scores, error)).execute(ProductSearchSpec("travel"))
    assert [card["product_id"] for card in result["hits"]] == _IDS
    assert result["hits"][0]["score"] == 0.9
    assert result["recall_strategy"] == "cj_qdrant_rrf"
    assert result["rerank_applied"] is False
    assert result["category_mode"] == "soft_boost"


@pytest.mark.asyncio
async def test_slow_rerank_is_cancelled_and_preserves_recall(snapshot, monkeypatch):
    cancelled = []

    class SlowReranker:
        async def rerank(self, query, documents):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

    monkeypatch.setattr(cj_catalog, "_RERANK_TIMEOUT_SECONDS", 0.01)
    result = await _catalog(snapshot, SlowReranker()).execute(ProductSearchSpec("travel"))
    assert cancelled == [True]
    assert result["rerank_applied"] is False
    assert result["hits"][0]["product_id"] == _IDS[0]


@pytest.mark.asyncio
async def test_missing_reranker_keeps_existing_retrieval(snapshot):
    result = await _catalog(snapshot, None).execute(ProductSearchSpec("travel"))
    assert result["rerank_applied"] is False
    assert result["recall_strategy"] == "cj_qdrant_rrf"


@pytest.mark.asyncio
async def test_no_candidates_does_not_call_reranker(snapshot):
    reranker = _Reranker([])
    result = await _catalog(snapshot, reranker, hits=[]).execute(ProductSearchSpec("travel"))
    assert result["hits"] == []
    assert result["rerank_applied"] is False
    assert reranker.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["travel", _IDS[0], "CJTEST0000001"])
async def test_direct_search_never_calls_embedding_index_or_rerank(snapshot, query):
    reranker = _Reranker(error=AssertionError("direct search must stay local"))
    catalog = _catalog(snapshot, reranker)
    page = await catalog.browse(query)
    assert page["total"] > 0
    assert catalog.embedder.calls == []
    assert catalog.vector_index.calls == []
    assert reranker.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [_IDS[0], "CJTEST0000001", "zzzxxyyqplm90001"])
async def test_agent_exact_identifiers_bypass_rerank(snapshot, query):
    reranker = _Reranker(error=AssertionError("exact lookup must stay local"))
    catalog = _catalog(snapshot, reranker)
    result = await catalog.execute(ProductSearchSpec(query))
    assert result["recall_strategy"] == "cj_snapshot_keyword"
    assert result["rerank_applied"] is False
    assert catalog.embedder.calls == []
    assert catalog.vector_index.calls == []
    assert reranker.calls == []
    expected = [] if query == "zzzxxyyqplm90001" else [_IDS[0]]
    assert [card["product_id"] for card in result["hits"]] == expected


@pytest.mark.asyncio
async def test_disabled_hybrid_keeps_keyword_path_without_rerank(snapshot):
    reranker = _Reranker(error=AssertionError("keyword path must stay local"))
    result = await _catalog(snapshot, reranker, hybrid=False).execute(ProductSearchSpec("travel"))
    assert result["hits"]
    assert result["rerank_applied"] is False
    assert reranker.calls == []
