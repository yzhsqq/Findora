# -*- coding: utf-8 -*-
"""HttpReranker

HTTP 精排客户端（兼容 /rerank 网关及百炼 qwen3-rerank 的 /reranks）。
RERANKER_BASE_URL 未配置时组装根不会实例化本类；调用失败抛异常，
由 CatalogSearchUseCase 降级为按向量分排序并标注 rerank_applied=false。
"""
from __future__ import annotations

import math

import httpx

from app.domain.catalog.ports.retrieval_ports import Reranker
from app.infrastructure.settings import Settings


class HttpReranker(Reranker):
    def __init__(self, settings: Settings, timeout_seconds: float = 3.0) -> None:
        endpoint = settings.reranker_base_url.rstrip("/")
        # 内部网关给出的是完整 /services/reranker endpoint；通用服务若只给根地址，
        # 仍兼容补上 /rerank。
        self._url = (
            endpoint
            if endpoint.endswith(("/rerank", "/reranker", "/reranks"))
            else f"{endpoint}/rerank"
        )
        self._api_key = settings.reranker_api_key or settings.llm_api_key
        self._model = settings.reranker_model
        self._timeout = timeout_seconds

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "query": query, "documents": documents},
            )
            response.raise_for_status()
            body = response.json()
        # 兼容 {results:[{index, relevance_score}]} 协议（Jina/TEI/vLLM rerank 通用形态）
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or len(results) != len(documents):
            raise RuntimeError("rerank 响应缺少完整的候选分数")
        scores = [0.0] * len(documents)
        seen: set[int] = set()
        for item in results:
            if not isinstance(item, dict):
                raise RuntimeError("rerank 结果项格式非法")
            index = item.get("index")
            if type(index) is not int or not 0 <= index < len(documents) or index in seen:
                raise RuntimeError("rerank 候选索引非法或重复")
            try:
                score = float(item.get("relevance_score", item.get("score")))
            except (TypeError, ValueError) as error:
                raise RuntimeError("rerank 候选分数非法") from error
            if not math.isfinite(score):
                raise RuntimeError("rerank 候选分数必须有限")
            seen.add(index)
            scores[index] = score
        return scores
