# -*- coding: utf-8 -*-
"""QdrantProductIndex

商品向量索引的 Qdrant 实现（COSINE）。两种形态同一套代码：
    - QDRANT_URL 已配置 → 连 Qdrant 服务端（Docker / 远程）
    - 未配置          → qdrant-client 本地嵌入模式（落盘 DATA_DIR/qdrant，零外部依赖）

point id 用 product_id 的确定性 UUID5，payload 存 product_id，upsert 幂等。
"""
from __future__ import annotations

import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointIdsList, PointStruct, VectorParams

from app.domain.catalog.ports.retrieval_ports import ProductVectorIndex, VectorHit
from app.domain.catalog.product import Product
from app.infrastructure.settings import Settings


def _point_id(product_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"globex/product/{product_id}"))


class QdrantProductIndex(ProductVectorIndex):
    def __init__(self, settings: Settings, *, collection: str | None = None) -> None:
        if settings.qdrant_url:
            self._client = AsyncQdrantClient(url=settings.qdrant_url)
        else:
            local_path = settings.data_dir / "qdrant"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self._client = AsyncQdrantClient(path=str(local_path))
        self._collection = collection or settings.qdrant_collection

    async def product_fingerprints(self) -> dict[str, str]:
        if not await self._client.collection_exists(self._collection):
            return {}
        fingerprints: dict[str, str] = {}
        offset = None
        while True:
            points, offset = await self._client.scroll(
                collection_name=self._collection,
                limit=256,
                offset=offset,
                with_payload=["product_id", "index_fingerprint"],
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                product_id = payload.get("product_id")
                if isinstance(product_id, str):
                    fingerprints[product_id] = str(payload.get("index_fingerprint") or "")
            if offset is None:
                return fingerprints

    async def ensure_ready(self, vector_dim: int) -> None:
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
            )
            return
        info = await self._client.get_collection(self._collection)
        vectors = info.config.params.vectors
        if not isinstance(vectors, VectorParams) or vectors.size != vector_dim:
            raise ValueError(
                f"商品向量维度与现有集合 {self._collection} 不一致；"
                "请使用新的 QDRANT_COLLECTION 完成模型维度迁移"
            )

    async def upsert_products(
        self, products: list[Product], embeddings: list[list[float]], fingerprints: list[str],
    ) -> None:
        if len(products) != len(embeddings) or len(products) != len(fingerprints):
            raise ValueError("products、embeddings 与 fingerprints 数量不一致")
        if not products:
            return
        points = [
            PointStruct(
                id=_point_id(product.product_id),
                vector=embedding,
                payload={"product_id": product.product_id, "index_fingerprint": fingerprint},
            )
            for product, embedding, fingerprint in zip(products, embeddings, fingerprints)
        ]
        await self._client.upsert(collection_name=self._collection, points=points, wait=True)

    async def delete_products(self, product_ids: list[str]) -> None:
        if product_ids and await self._client.collection_exists(self._collection):
            await self._client.delete(
                collection_name=self._collection,
                points_selector=PointIdsList(points=[_point_id(product_id) for product_id in product_ids]),
                wait=True,
            )

    async def search(self, embedding: list[float], top_n: int) -> list[VectorHit]:
        result = await self._client.query_points(
            collection_name=self._collection,
            query=embedding,
            limit=top_n,
            with_payload=True,
        )
        return [
            VectorHit(product_id=point.payload["product_id"], score=point.score)
            for point in result.points
            if point.payload and "product_id" in point.payload
        ]

    async def close(self) -> None:
        await self._client.close()
