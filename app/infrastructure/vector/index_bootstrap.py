# -*- coding: utf-8 -*-
"""启动时对比商品检索文本指纹，仅同步新增、变化和删除的商品。"""
from __future__ import annotations

import hashlib
import logging

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient, ProductVectorIndex
from app.domain.catalog.product import Product

logger = logging.getLogger(__name__)
_BATCH_SIZE = 100
_TEXT_SCHEMA_VERSION = "product-search-v1"


def _fingerprint(product: Product, embedding_model: str) -> str:
    content = "\n".join((_TEXT_SCHEMA_VERSION, embedding_model, product.searchable_text()))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def bootstrap_product_index(
    product_repo: ProductRepository,
    embedder: EmbeddingClient,
    vector_index: ProductVectorIndex,
    embedding_model: str = "",
    embedding_dim: int = 0,
    *,
    batch_size: int = _BATCH_SIZE,
) -> bool:
    """索引不变时不调用 embedding；失败保留已有索引并返回 False。"""
    try:
        if batch_size < 1:
            raise ValueError("商品向量索引 batch_size 必须为正整数")
        products = await product_repo.list_all()
        by_id = {product.product_id: product for product in products}
        if len(by_id) != len(products):
            raise ValueError("商品目录存在重复 product_id")
        indexed = await vector_index.product_fingerprints()
        if indexed and embedding_dim:
            await vector_index.ensure_ready(vector_dim=embedding_dim)
        changed = [product for product in products if indexed.get(product.product_id) != _fingerprint(product, embedding_model)]
        deleted = sorted(set(indexed) - set(by_id))

        for start in range(0, len(changed), batch_size):
            batch = changed[start : start + batch_size]
            vectors = await embedder.embed_batch([product.searchable_text() for product in batch])
            if len(vectors) != len(batch) or not vectors or not vectors[0]:
                raise ValueError("商品 embedding 返回数量或维度异常")
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError("同批商品 embedding 维度不一致")
            if embedding_dim and len(vectors[0]) != embedding_dim:
                raise ValueError(f"商品 embedding 返回维度 {len(vectors[0])}，预期 {embedding_dim}")
            await vector_index.ensure_ready(vector_dim=len(vectors[0]))
            await vector_index.upsert_products(
                batch, vectors, [_fingerprint(product, embedding_model) for product in batch],
            )

        await vector_index.delete_products(deleted)
        logger.info(
            "商品向量索引同步完成：总数=%d，新建或文本变化=%d，删除=%d，复用=%d",
            len(products), len(changed), len(deleted), len(products) - len(changed),
        )
        return True
    except Exception as err:  # noqa: BLE001 —— 建库失败不阻塞启动
        logger.warning("商品向量索引同步失败，检索可能降级关键词召回：%s", err)
        return False
