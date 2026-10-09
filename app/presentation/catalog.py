"""Public, read-only product snapshot browse endpoint."""
from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.cj_live_quote import CJQuoteError
from app.application.ports.catalog import catalog_capabilities
from app.presentation.identity import require_buyer


class CJDetailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(min_length=1, max_length=100)


class CJQuoteRequest(CJDetailRequest):
    sku_id: str = Field(min_length=1, max_length=100)
    ship_to: str = Field(min_length=2, max_length=2)
    quantity: int = Field(default=1, ge=1, le=10)


def register_catalog_routes(api: FastAPI, get_catalog: Callable, get_live: Callable | None = None) -> None:
    @api.get("/commerce/catalog/capabilities")
    async def describe_catalog_capabilities() -> dict:
        # Capabilities come from the assembled mode, never from a successful
        # product read: a damaged snapshot must not hide the navigation.
        catalog = get_catalog()
        capabilities = catalog_capabilities(catalog)
        return {"source": capabilities.source, "platforms": list(capabilities.platforms),
                "local_orders": capabilities.local_orders, "purchase_records": capabilities.purchase_records,
                "cj_quote": "cj" in capabilities.platforms and get_live is not None and get_live() is not None}

    @api.get("/commerce/catalog")
    async def browse_catalog(
        query: str = Query(default="", max_length=120),
        category: str = Query(default="", max_length=80),
        platform: str = Query(default="", pattern="^(cj|amazon|ebay)?$"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=24, ge=1, le=60),
    ) -> dict:
        catalog = get_catalog()
        capabilities = catalog_capabilities(catalog)
        if capabilities.source == "fixture":
            return {"source": "fixture", "total": 0, "all_count": 0, "detail_count": 0,
                    "inventory_count": 0, "page": page, "page_size": page_size,
                    "categories": [], "products": []}
        try:
            return await catalog.browse(query, category, page, page_size, platform)
        except ValueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @api.post("/commerce/catalog/detail")
    async def fetch_detail(request: Request, body: CJDetailRequest, buyer_id: str = Query(min_length=1)) -> dict:
        await require_buyer(request, buyer_id)
        service = get_live() if get_live is not None else None
        if service is None:
            raise HTTPException(status_code=503, detail="CJ 详情试运行未启用")
        try:
            return {"product": await service.detail(body.product_id)}
        except CJQuoteError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @api.post("/commerce/catalog/quote")
    async def quote(request: Request, body: CJQuoteRequest, buyer_id: str = Query(min_length=1)) -> dict:
        await require_buyer(request, buyer_id)
        service = get_live() if get_live is not None else None
        if service is None:
            raise HTTPException(status_code=503, detail="CJ 物流试运行未启用")
        try:
            return {"quote": await service.quote(body.product_id, body.sku_id, body.ship_to, body.quantity)}
        except CJQuoteError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
