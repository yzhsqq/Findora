"""Public, read-only product snapshot browse endpoint."""
from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, HTTPException, Query

from app.infrastructure.persistence.cj_catalog import CJCatalog


def register_catalog_routes(api: FastAPI, get_catalog: Callable) -> None:
    @api.get("/commerce/catalog")
    async def browse_catalog(
        query: str = Query(default="", max_length=120),
        category: str = Query(default="", max_length=80),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=24, ge=1, le=60),
    ) -> dict:
        catalog = get_catalog()
        if not isinstance(catalog, CJCatalog):
            return {"source": "fixture", "total": 0, "all_count": 0, "detail_count": 0,
                    "inventory_count": 0, "page": page, "page_size": page_size,
                    "categories": [], "products": []}
        try:
            return await catalog.browse(query, category, page, page_size)
        except ValueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
