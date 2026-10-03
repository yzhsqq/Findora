"""商品向量索引增量同步：使用落盘 Qdrant 验证真实重启行为。"""
from types import SimpleNamespace

import pytest
from qdrant_client import models

from app.domain.catalog.money import Money
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.domain.catalog.product import Product
from app.domain.catalog.sku import Sku
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex


class CountingEmbeddingClient(EmbeddingClient):
    def __init__(self, dimensions: int = 2, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.dimensions = dimensions
        self.fail = fail

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding offline")
        self.texts.extend(texts)
        return [[float(len(text))] + [1.0] * (self.dimensions - 1) for text in texts]


def _product(product_id: str, title: str) -> Product:
    return Product(
        product_id=product_id,
        title=title,
        brand="测试品牌",
        category="旅行装备",
        origin_country="CN",
        description="轻便旅行用品",
        ships_to=["CN"],
        skus=[Sku(f"{product_id}-S1", "标准", Money.from_major_units(99, "CNY"), 10)],
    )


def _index(tmp_path) -> QdrantProductIndex:
    return QdrantProductIndex(SimpleNamespace(
        qdrant_url="", data_dir=tmp_path, qdrant_collection="products",
    ))


@pytest.mark.asyncio
async def test_restart_and_offer_changes_reuse_vectors(tmp_path):
    first, removed = _product("P1", "旅行背包"), _product("P2", "收纳袋")
    repo = InMemoryProductRepository([first, removed])
    embedder = CountingEmbeddingClient()
    index = _index(tmp_path)
    assert await bootstrap_product_index(repo, embedder, index, "model-a")
    assert len(embedder.texts) == 2
    await index.close()

    # 模拟新进程重新打开落盘集合；报价和库存变化不属于检索文本。
    first.skus[0].price = Money.from_major_units(89, "CNY")
    first.skus[0].stock = 3
    index = _index(tmp_path)
    warm = CountingEmbeddingClient(fail=True)
    assert await bootstrap_product_index(repo, warm, index, "model-a")
    assert warm.texts == []

    first.title = "轻量旅行背包"
    repo = InMemoryProductRepository([first])
    changed = CountingEmbeddingClient()
    assert await bootstrap_product_index(repo, changed, index, "model-a")
    assert len(changed.texts) == 1
    assert set(await index.product_fingerprints()) == {"P1"}

    # 模型变更后，即使商品文本未变，也必须重建当前商品的向量。
    new_model = CountingEmbeddingClient()
    assert await bootstrap_product_index(repo, new_model, index, "model-b")
    assert len(new_model.texts) == 1
    await index.close()


@pytest.mark.asyncio
async def test_failed_embedding_keeps_existing_points_and_defers_deletion(tmp_path):
    first, second = _product("P1", "旅行背包"), _product("P2", "收纳袋")
    index = _index(tmp_path)
    assert await bootstrap_product_index(
        InMemoryProductRepository([first, second]), CountingEmbeddingClient(), index, "model-a",
    )
    before = await index.product_fingerprints()
    first.title = "改过标题的背包"
    assert not await bootstrap_product_index(
        InMemoryProductRepository([first]), CountingEmbeddingClient(fail=True), index, "model-a",
    )
    assert await index.product_fingerprints() == before
    await index.close()


@pytest.mark.asyncio
async def test_dimension_change_does_not_destroy_existing_collection(tmp_path):
    product = _product("P1", "旅行背包")
    repo = InMemoryProductRepository([product])
    index = _index(tmp_path)
    assert await bootstrap_product_index(repo, CountingEmbeddingClient(), index, "model-a")
    before = await index.product_fingerprints()
    assert not await bootstrap_product_index(repo, CountingEmbeddingClient(dimensions=3), index, "model-b")
    assert await index.product_fingerprints() == before
    await index.close()


@pytest.mark.asyncio
async def test_existing_named_dense_and_bm25_collection_reuses_vectors(tmp_path):
    product = _product("P1", "旅行背包")
    repo = InMemoryProductRepository([product])
    index = _index(tmp_path)
    await index._client.create_collection(
        collection_name="products",
        vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    embedder = CountingEmbeddingClient()
    assert await bootstrap_product_index(repo, embedder, index, "model-a", 2)
    assert len(embedder.texts) == 1
    assert [hit.product_id for hit in await index.search(await embedder.embed(product.searchable_text()), 5)] == ["P1"]
    await index.close()

    index = _index(tmp_path)
    no_embed = CountingEmbeddingClient(fail=True)
    assert await bootstrap_product_index(repo, no_embed, index, "model-a", 2)
    assert no_embed.texts == []
    await index.close()

    # 直接检索的调用方不会先运行启动建库流程，索引仍须识别命名向量。
    index = _index(tmp_path)
    assert [hit.product_id for hit in await index.search([float(len(product.searchable_text())), 1.0], 5)] == ["P1"]
    await index.close()
