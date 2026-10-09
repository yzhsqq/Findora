"""Local purchase plans use trusted CJ snapshots and existing buyer identity."""
from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.cj_live_quote import CJQuoteError
from app.application.ports.catalog import catalog_capabilities
from app.presentation.identity import require_buyer


class PurchaseRecordWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    product_id: str = Field(min_length=1, max_length=100)
    sku_id: str = Field(default="", max_length=100)


def register_purchase_record_routes(api, get_store, get_catalog, get_live=None):
    @api.get("/commerce/purchase-records")
    async def list_records(request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        records = await get_store().list(buyer)
        catalog = get_catalog()
        if catalog_capabilities(catalog).purchase_records and records:
            cards = await catalog.cards_by_ids([r["product"]["product_id"] for r in records])
            latest = {card["product_id"]: card for card in cards}
            localized = await catalog.localize_saved_cards([r["product"] for r in records])
            for record, product in zip(records, localized):
                # A saved URL must not survive later removal/unavailability in the catalog.
                for field in ("source_url", "source_url_status", "source_url_checked_at"):
                    product.pop(field, None)
                    if field in latest.get(product["product_id"], {}):
                        product[field] = latest[product["product_id"]][field]
                record["product"] = product
        else:
            for record in records:
                record["product"].pop("source_url", None)
        return {"records": records, "total": len(records)}

    @api.put("/commerce/purchase-records")
    async def save_record(body: PurchaseRecordWrite, request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        catalog = get_catalog()
        if not catalog_capabilities(catalog).purchase_records:
            raise HTTPException(409, "当前未启用 CJ 商品库。")
        cards = await catalog.cards_by_ids([body.product_id])
        if not cards:
            raise HTTPException(404, "商品不在当前 CJ 商品库中。")
        product = cards[0]
        if product.get("source_platform") != "CJdropshipping":
            raise HTTPException(404, "商品不在当前 CJ 商品库中。")
        if body.sku_id:
            live = get_live() if get_live else None
            if live is not None:
                try:
                    product = await live.cached_detail(body.product_id)
                except CJQuoteError as error:
                    raise HTTPException(422, str(error)) from error
            if not any(sku["sku_id"] == body.sku_id for sku in product["skus"]):
                raise HTTPException(422, "规格不属于这件商品，请重新查看商品详情。")
        try:
            return await get_store().save(buyer, product, body.sku_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @api.delete("/commerce/purchase-records/{record_id}")
    async def delete_record(record_id: str, request: Request, buyer_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        await get_store().delete(buyer, record_id)
        return {"removed": True}
