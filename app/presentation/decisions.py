"""Buyer-scoped, read-only previews of a structured shopping decision."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.application.usecases.shopping_decision import build_decision_report
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.presentation.identity import require_buyer, require_session


class DecisionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    buyer_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=80)
    ship_to: str | None = Field(default=None, max_length=8)
    target_currency: str = Field(default="CNY", min_length=3, max_length=3)
    price_max_major: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    budget_basis: Literal["product", "landed"] = "product"
    excluded_material_tags: list[str] = Field(default_factory=list, max_length=12)
    required_material_tags: list[str] = Field(default_factory=list, max_length=12)


def register_decision_routes(api: FastAPI, get_search: Callable, get_evidence_store: Callable) -> None:
    @api.post("/commerce/decisions/preview")
    async def preview(request: Request, body: DecisionPreviewRequest) -> dict:
        buyer_id = await require_buyer(request, body.buyer_id)
        await require_session(request, buyer_id, body.session_id)
        if body.budget_basis == "landed" and not body.ship_to:
            raise HTTPException(status_code=422, detail="到手价预算需要先选择配送目的地")
        try:
            spec = ProductSearchSpec(
                normalized_query=body.query,
                raw_query=body.query,
                category=body.category or None,
                ship_to=body.ship_to.upper() if body.ship_to else None,
                top_k=5,
                target_currency=body.target_currency.upper(),
                price_max_major=body.price_max_major,
                budget_basis=body.budget_basis,
                excluded_material_tags=body.excluded_material_tags,
                required_material_tags=body.required_material_tags,
            )
            result = await get_search().execute(spec)
            result["query_conditions"] = {
                "normalized_query": spec.normalized_query,
                "category": spec.category,
                "ship_to": spec.ship_to,
                "target_currency": spec.target_currency,
                "price_max_major": body.price_max_major,
                "budget_basis": body.budget_basis,
                "excluded_material_tags": list(spec.excluded_material_tags),
                "required_material_tags": list(spec.required_material_tags),
            }
            result["observed_at"] = datetime.now(timezone.utc).isoformat()
            evidence = get_evidence_store()
            result["result_ref"] = await evidence.save(buyer_id, body.session_id, "products", result)
            report = build_decision_report(result, budget_basis=body.budget_basis)
            await evidence.save(buyer_id, body.session_id, "decision_preview", report)
            return report
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @api.get("/commerce/decisions/preview")
    async def latest_preview(request: Request, buyer_id: str = Query(min_length=1), session_id: str = Query(min_length=1)) -> dict:
        buyer_id = await require_buyer(request, buyer_id)
        await require_session(request, buyer_id, session_id)
        reports = await get_evidence_store().search(buyer_id, session_id, kind="decision_preview", limit=1)
        return {"report": reports[0]["data"] if reports else None}
