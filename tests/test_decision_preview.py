"""Decision previews use the real catalog and buyer-scoped evidence store."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from ag_ui.core import RunAgentInput

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus, observe_run_events
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.presentation.decisions import register_decision_routes


@pytest.mark.asyncio
async def test_preview_is_capped_and_recoverable(tmp_path):
    search = CatalogSearchUseCase(InMemoryProductRepository())
    evidence = ContextEvidenceStore(tmp_path / "evidence.db")
    api = FastAPI()
    register_decision_routes(api, lambda: search, lambda: evidence)
    request = {
        "buyer_id": "buyer-a", "session_id": "session-a", "query": "旅行 背包",
        "ship_to": "CN", "target_currency": "CNY", "budget_basis": "product",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post("/commerce/decisions/preview", json=request)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["version"] == 2
        assert len(report["candidates"]) <= 5
        assert report["request"]["ship_to"] == "CN"
        assert report["evidence_refs"]

        recovered = await client.get("/commerce/decisions/preview", params={
            "buyer_id": "buyer-a", "session_id": "session-a",
        })
        assert recovered.status_code == 200
        assert recovered.json()["report"] == report
        other = await client.get("/commerce/decisions/preview", params={
            "buyer_id": "buyer-b", "session_id": "session-b",
        })
        assert other.json() == {"report": None}


@pytest.mark.asyncio
async def test_landed_budget_requires_destination(tmp_path):
    api = FastAPI()
    register_decision_routes(
        api, lambda: CatalogSearchUseCase(InMemoryProductRepository()),
        lambda: ContextEvidenceStore(tmp_path / "evidence.db"),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post("/commerce/decisions/preview", json={
            "buyer_id": "buyer-a", "session_id": "session-a", "query": "背包",
            "budget_basis": "landed", "price_max_major": 300,
        })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_preview_budget_basis_filters_on_the_selected_total(tmp_path):
    api = FastAPI()
    register_decision_routes(
        api, lambda: CatalogSearchUseCase(InMemoryProductRepository()),
        lambda: ContextEvidenceStore(tmp_path / "evidence.db"),
    )
    request = {
        "buyer_id": "buyer-a", "session_id": "session-a", "query": "P1001",
        "ship_to": "CN", "target_currency": "CNY", "price_max_major": 189,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        product = await client.post("/commerce/decisions/preview", json={**request, "budget_basis": "product"})
        landed = await client.post("/commerce/decisions/preview", json={**request, "budget_basis": "landed"})
    assert product.status_code == landed.status_code == 200
    assert [item["product"]["product_id"] for item in product.json()["candidates"]] == ["P1001"]
    assert landed.json()["status"] == "no_match"
    assert landed.json()["candidates"] == []
    assert landed.json()["excluded"][0]["reason"] == "超出估算到手价预算"


@pytest.mark.asyncio
async def test_agent_search_projects_decision_into_ag_ui_state():
    body = RunAgentInput.model_validate({
        "threadId": "session-a", "runId": "run-a", "state": {},
        "messages": [{"id": "user-a", "role": "user", "content": "寄到中国的旅行背包"}],
        "tools": [], "context": [], "forwardedProps": {"buyerId": "buyer-a"},
    })
    emitted = []
    adapter = AGUIRunAdapter(body, emitted.append)
    tool = build_product_search_tool(CatalogSearchUseCase(InMemoryProductRepository()), TradeEventBus())
    token = ShoppingContext.set(ShoppingContextSnapshot("session-a", "buyer-a", "zh-CN", "CNY"))
    try:
        with observe_run_events(adapter.on_trade_event):
            await tool(normalized_query="旅行 背包", ship_to="CN", top_k=5)
    finally:
        ShoppingContext.reset(token)
    report = adapter.state["decisionReport"]
    assert report["version"] == 2
    assert report["request"]["ship_to"] == "CN"
    assert len(report["candidates"]) <= 5
    assert len(report["candidates"]) <= len(adapter.state["products"])
