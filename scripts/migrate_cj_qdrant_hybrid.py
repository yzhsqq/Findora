"""Copy CJ dense vectors into a Qdrant server collection with native BM25."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import dotenv_values
from qdrant_client import QdrantClient, models

from app.infrastructure.persistence.cj_catalog import CJCatalog
from app.infrastructure.vector.index_bootstrap import _fingerprint


ROOT = Path(__file__).resolve().parents[1]


def _args() -> argparse.Namespace:
    env = dotenv_values(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=env.get("DATA_DIR") or ROOT / "data")
    parser.add_argument("--url", default=os.getenv("QDRANT_URL") or env.get("QDRANT_URL") or "http://localhost:6333")
    parser.add_argument("--source", default="globex_products_cj_snapshot")
    parser.add_argument("--target", default="globex_products_cj_hybrid_cj_snapshot")
    parser.add_argument("--model", default=env.get("EMBEDDING_MODEL") or "text-embedding-v4")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=100)
    return parser.parse_args()


def _check_source(source: QdrantClient, collection: str, documents: dict, model: str, dimension: int) -> None:
    if not source.collection_exists(collection):
        raise RuntimeError(f"本地集合不存在：{collection}")
    found: set[str] = set()
    mismatched: list[str] = []
    offset = None
    while True:
        points, offset = source.scroll(
            collection_name=collection, limit=256, offset=offset,
            with_payload=["product_id", "index_fingerprint"], with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            product_id = payload.get("product_id")
            document = documents.get(product_id)
            if not isinstance(product_id, str) or product_id in found:
                raise RuntimeError(f"本地索引缺少商品 ID 或有重复 ID：{point.id}")
            found.add(product_id)
            if document is None or payload.get("index_fingerprint") != _fingerprint(document, model):
                mismatched.append(product_id)
        if offset is None:
            break
    missing = set(documents) - found
    if missing or mismatched:
        raise RuntimeError(
            f"向量与当前 CJ 快照不一致：缺失 {len(missing)}、指纹不符 {len(mismatched)}；"
            f"示例 {sorted(missing)[:3] + mismatched[:3]}。请先核对快照，不能复制过期向量。"
        )
    info = source.get_collection(collection)
    vectors = info.config.params.vectors
    if not isinstance(vectors, models.VectorParams) or vectors.size != dimension:
        raise RuntimeError(f"旧集合不是 {dimension} 维的单一稠密向量集合")
    print(f"已核对 {len(found)} 条商品、向量模型 {model}、维度 {dimension}", flush=True)


def _ensure_target(remote: QdrantClient, collection: str, dimension: int) -> None:
    if not remote.collection_exists(collection):
        remote.create_collection(
            collection_name=collection,
            vectors_config={"dense": models.VectorParams(size=dimension, distance=models.Distance.COSINE)},
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        return
    info = remote.get_collection(collection)
    vectors = info.config.params.vectors
    sparse = info.config.params.sparse_vectors or {}
    if (not isinstance(vectors, dict) or "dense" not in vectors
            or vectors["dense"].size != dimension or "bm25" not in sparse
            or sparse["bm25"].modifier != models.Modifier.IDF):
        raise RuntimeError(f"目标集合 {collection} 的 dense/bm25 配置不符合预期")


def main() -> None:
    args = _args()
    if args.batch_size < 1 or args.dimension < 1 or args.source == args.target:
        raise ValueError("batch-size、dimension 必须为正数，源和目标集合不能相同")
    data_dir = args.data_dir if args.data_dir.is_absolute() else ROOT / args.data_dir
    local_path = data_dir / "qdrant"
    catalog_path = data_dir / "cj_catalog.sqlite3"
    if not local_path.is_dir() or not catalog_path.is_file():
        raise FileNotFoundError(f"找不到本地 Qdrant 或 CJ 快照：{local_path} / {catalog_path}")

    documents = {item.product_id: item for item in CJCatalog(catalog_path)._load_documents()}
    try:
        source = QdrantClient(path=str(local_path))
    except RuntimeError as error:
        raise RuntimeError("本地 Qdrant 被后端占用；请先停止本机后端再迁移") from error
    try:
        _check_source(source, args.source, documents, args.model, args.dimension)
        # cloud_inference=True tells qdrant-client to forward Document to the server.
        # The dense vector is already numeric; only the sparse BM25 text is inferred.
        remote = QdrantClient(url=args.url, cloud_inference=True, timeout=120)
        try:
            _ensure_target(remote, args.target, args.dimension)
            offset = None
            copied = 0
            while True:
                points, offset = source.scroll(
                    collection_name=args.source, limit=args.batch_size, offset=offset,
                    with_payload=True, with_vectors=True,
                )
                batch = []
                for point in points:
                    payload = point.payload or {}
                    product_id = payload["product_id"]
                    vector = point.vector
                    if not isinstance(vector, list) or len(vector) != args.dimension:
                        raise RuntimeError(f"商品 {product_id} 的稠密向量维度异常")
                    batch.append(models.PointStruct(
                        id=point.id,
                        vector={
                            "dense": vector,
                            "bm25": models.Document(
                                text=documents[product_id].searchable_text(), model="Qdrant/bm25",
                            ),
                        },
                        payload=payload,
                    ))
                if batch:
                    remote.upsert(collection_name=args.target, points=batch, wait=True)
                    copied += len(batch)
                    print(f"已迁移 {copied}/{len(documents)}", flush=True)
                if offset is None:
                    break
            actual = remote.count(collection_name=args.target, exact=True).count
            if actual != len(documents):
                raise RuntimeError(f"目标集合数量异常：{actual}/{len(documents)}")
            print(f"完成：{actual} 条；复用全部稠密向量，千问 embedding 请求 0 次", flush=True)
        finally:
            remote.close()
    finally:
        source.close()


if __name__ == "__main__":
    main()
