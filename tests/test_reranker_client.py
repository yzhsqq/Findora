# -*- coding: utf-8 -*-
"""Reranker HTTP 契约回归。"""
from __future__ import annotations

import pytest

from app.infrastructure.rerank import http_reranker
from app.infrastructure.rerank.http_reranker import HttpReranker
from app.infrastructure.settings import load_settings


@pytest.mark.asyncio
async def test_full_reranker_endpoint_is_used_with_gateway_authorization(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.1},
                ],
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url: str, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setenv("LLM_API_KEY", "test-gateway-key")
    monkeypatch.setenv("RERANKER_API_KEY", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "RERANKER_BASE_URL",
        "https://1688openai.alibaba-inc.com/v1/services/reranker",
    )
    monkeypatch.setenv("RERANKER_MODEL", "qwen-text-rerank")
    monkeypatch.setattr(http_reranker.httpx, "AsyncClient", lambda **_: FakeClient())

    scores = await HttpReranker(load_settings()).rerank(
        "轻便旅行背包",
        ["20L 轻量旅行背包", "陶瓷咖啡杯"],
    )

    assert scores == [0.9, 0.1]
    assert captured["url"] == "https://1688openai.alibaba-inc.com/v1/services/reranker"
    assert captured["headers"] == {"Authorization": "Bearer test-gateway-key"}
    assert captured["json"] == {
        "model": "qwen-text-rerank",
        "query": "轻便旅行背包",
        "documents": ["20L 轻量旅行背包", "陶瓷咖啡杯"],
    }


def _fake_service(monkeypatch, body):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return body

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(http_reranker.httpx, "AsyncClient", lambda **_: Client())
    monkeypatch.setenv("LLM_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("RERANKER_API_KEY", "test-bailian-key")
    return captured


@pytest.mark.asyncio
async def test_bailian_reranks_uses_independent_key_and_restores_input_order(monkeypatch):
    captured = _fake_service(monkeypatch, {
        "results": [{"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.1}],
        "model": "qwen3-rerank",
    })
    endpoint = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
    monkeypatch.setenv("RERANKER_BASE_URL", endpoint)
    monkeypatch.setenv("RERANKER_MODEL", "qwen3-rerank")
    settings = load_settings()
    assert "test-bailian-key" not in repr(settings)
    assert await HttpReranker(settings).rerank("水杯", ["背包", "水杯"]) == [0.1, 0.8]
    assert captured["url"] == endpoint
    assert captured["headers"]["Authorization"] == "Bearer test-bailian-key"
    assert captured["json"] == {"model": "qwen3-rerank", "query": "水杯", "documents": ["背包", "水杯"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("path,expected", [
    ("/v1", "/v1/rerank"),
    ("/v1/rerank/", "/v1/rerank"),
    ("/v1/reranks/", "/v1/reranks"),
    ("/v1/services/reranker", "/v1/services/reranker"),
])
async def test_rerank_endpoint_paths_remain_compatible(monkeypatch, path, expected):
    captured = _fake_service(monkeypatch, {"results": [{"index": 0, "score": 0.6}]})
    monkeypatch.setenv("RERANKER_BASE_URL", "https://example.com" + path)
    assert await HttpReranker(load_settings()).rerank("cup", ["cup"]) == [0.6]
    assert captured["url"] == "https://example.com" + expected


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [
    [{"index": 0, "score": 0.1}, {"index": 0, "score": 0.8}],
    [{"index": -1, "score": 0.1}, {"index": 1, "score": 0.8}],
    [{"index": 2, "score": 0.1}, {"index": 1, "score": 0.8}],
    [{"index": True, "score": 0.1}, {"index": 0, "score": 0.8}],
    [{"index": 0}, {"index": 1, "score": 0.8}],
    [{"index": 0, "score": float("nan")}, {"index": 1, "score": 0.8}],
    [{"index": 0, "score": 0.1}, {"index": 1, "score": float("inf")}],
    ["invalid", {"index": 1, "score": 0.8}],
    [{"index": 0, "score": 0.1}],
])
async def test_incomplete_or_invalid_result_cannot_claim_rerank(monkeypatch, results):
    _fake_service(monkeypatch, {"results": results})
    with pytest.raises(RuntimeError, match="rerank"):
        await HttpReranker(load_settings()).rerank("cup", ["bag", "cup"])
