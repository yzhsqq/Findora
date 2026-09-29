"""Read-only CJ snapshot used by both the catalog page and agent search.

The collector owns writes. This adapter never calls CJ or an embedding API while
serving a request, and does not turn missing fulfillment facts into product facts.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import html
import json
import re
import sqlite3
from pathlib import Path

from app.domain.catalog.product_search_spec import ProductSearchSpec


_WORDS = {
    "背包": "backpack", "旅行": "travel", "行李": "luggage", "包": "bag",
    "耳机": "headphone", "耳塞": "earbud", "蓝牙": "bluetooth", "手机": "phone",
    "充电": "charger", "电脑": "computer", "键盘": "keyboard", "鼠标": "mouse",
    "露营": "camping", "户外": "outdoor", "运动": "sport", "宠物": "pet",
    "猫": "cat", "狗": "dog", "儿童": "kids", "婴儿": "baby",
    "家居": "home", "收纳": "storage", "厨房": "kitchen", "灯": "light",
    "美妆": "beauty", "化妆": "makeup", "办公": "office", "玩具": "toy",
}
_CATEGORIES = {
    "旅行装备": ("Bags & Shoes",), "户外运动": ("Sports & Outdoors",),
    "数码配件": ("Consumer Electronics", "Phones & Accessories", "Computer & Office"),
    "家居生活": ("Home, Garden & Furniture",),
    "美妆个护": ("Health, Beauty & Hair",),
    "厨房餐饮": ("Home, Garden & Furniture",),
    "办公学习": ("Computer & Office",),
    "母婴宠物": ("Toys, Kids & Babies", "Pet Supplies"),
}
_AMOUNT = re.compile(r"\d+(?:\.\d+)?")
_HTML_TAG = re.compile(r"<[^>]+>")


def _price(raw: object) -> tuple[float, str, str]:
    text = str(raw or "").strip()
    amounts = [float(x) for x in _AMOUNT.findall(text)]
    if not amounts:
        return 0.0, "报价待核实", "unknown"
    if len(amounts) >= 2 and amounts[0] != amounts[1]:
        return min(amounts), f"US${min(amounts):.2f}–{max(amounts):.2f}", "range"
    return amounts[0], f"US${amounts[0]:.2f}", "listing"


def _plain(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<\s*(?:br|/p|/div)\b[^>]*>", "。", value, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(_HTML_TAG.sub(" ", text))).strip()[:1400]


def _labels(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _positive_count(value: object) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _terms(query: str) -> list[str]:
    english = re.findall(r"[a-zA-Z]{3,}", query.lower())
    translated = [word for zh, word in _WORDS.items() if zh in query and not any(
        zh != other and zh in other and other in query for other in _WORDS
    )]
    return list(dict.fromkeys([*translated, *english]))[:8]


class CJCatalog:
    source = "cj"

    def __init__(self, path: Path):
        self.path = path

    def _db(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise ValueError("CJ 商品快照不存在，请先运行采集脚本")
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _card(row: sqlite3.Row, score: float = 1.0) -> dict:
        listing = json.loads(row["list_json"])
        detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
        inventory = json.loads(row["inventory_json"]) if row["inventory_json"] else {}
        price, price_text, price_kind = _price(listing.get("sellPrice"))
        stocks = {}
        for item in inventory.get("variantInventories") or []:
            counts = [warehouse.get("totalInventory") for warehouse in item.get("inventory") or []]
            if counts and all(type(count) is int and count >= 0 for count in counts):
                stocks[str(item.get("vid"))] = sum(counts)
        skus = []
        for item in (detail.get("variants") or [])[:60]:
            if not isinstance(item, dict):
                continue
            sku_id = str(item.get("variantSku") or item.get("vid") or "")
            amount, _, kind = _price(item.get("variantSellPrice"))
            if sku_id and kind != "unknown":
                skus.append({
                    "sku_id": sku_id, "spec": str(item.get("variantKey") or item.get("variantNameEn") or sku_id),
                    "variant_id": str(item.get("vid") or ""),
                    "price_major": amount, "currency": "USD",
                    "stock": stocks.get(str(item.get("vid")), 0),
                    "stock_known": str(item.get("vid")) in stocks,
                })
        image = detail.get("bigImage") or listing.get("bigImage")
        material = _labels(detail.get("materialNameEnSet") or detail.get("materialNameEn"))
        warehouses = [str(item.get("countryCode")) for item in inventory.get("inventories") or []
                      if isinstance(item, dict) and item.get("countryCode") and _positive_count(item.get("totalInventoryNum"))]
        weight = detail.get("productWeight")
        try:
            weight_kg = max(0.0, float(weight) / 1000) if weight is not None else None
        except (TypeError, ValueError):
            weight_kg = None
        return {
            "product_id": str(row["pid"]),
            "canonical_product_id": str(row["pid"]),
            "title": str(listing.get("nameEn") or detail.get("productNameEn") or "CJ 商品"),
            "brand": "", "supplier_name": str(detail.get("supplierName") or listing.get("supplierName") or ""),
            "category": str(row["first_category"]), "origin_country": "",
            "price_major": price, "currency": "USD", "price_text": price_text,
            "price_kind": price_kind, "highlights": [str(row["second_category"]), str(row["third_category"])],
            "skus": skus, "default_sku_id": skus[0]["sku_id"] if skus else "",
            "score": score, "source_platform": "CJdropshipping", "image_url": image,
            "image_kind": "source" if image else "placeholder", "image_alt": str(listing.get("nameEn") or "CJ 商品"),
            "description": _plain(detail.get("description") or listing.get("description")),
            "ships_to": [], "ship_from_warehouses": list(dict.fromkeys(warehouses)),
            "material_tags": material, "weight_kg": weight_kg,
            "updated_at": row["detail_fetched_at"] or row["list_fetched_at"],
            "inventory_checked_at": row["inventory_fetched_at"],
            "stock_known": bool(inventory),
            "detail_available": bool(detail),
        }

    def _browse(self, query: str, category: str, page: int, page_size: int) -> dict:
        direct_id = query.strip() if re.fullmatch(r"[0-9]{16,24}", query.strip()) else ""
        terms = [] if direct_id else _terms(query)
        clauses: list[str] = []
        args: list[str] = []
        categories = _CATEGORIES.get(category, (category,)) if category else ()
        if categories:
            clauses.append("first_category IN (" + ",".join("?" for _ in categories) + ")")
            args.extend(categories)
        if direct_id:
            clauses.append("pid = ?")
            args.append(direct_id)
        elif terms:
            clauses.append("(" + " OR ".join("lower(json_extract(list_json, '$.nameEn')) LIKE ?" for _ in terms) + ")")
            args.extend(f"%{term}%" for term in terms)
        elif query.strip():
            clauses.append("lower(json_extract(list_json, '$.nameEn')) LIKE ?")
            args.append(f"%{query.strip().lower()}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order_args: list[object] = []
        if terms:
            weights = " + ".join("(CASE WHEN lower(json_extract(list_json, '$.nameEn')) LIKE ? THEN ? ELSE 0 END)" for _ in terms)
            order = f"({weights}) DESC, list_fetched_at DESC, pid DESC"
            for term in terms:
                order_args.extend((f"%{term}%", len(term)))
        else:
            # The collector visits categories sequentially. Listing by fetch
            # time would fill the first storefront page with one category.
            order = "pid DESC" if not category else "list_fetched_at DESC, pid DESC"
        with closing(self._db()) as db:
            total = db.execute("SELECT count(*) FROM products" + where, args).fetchone()[0]
            rows = db.execute("SELECT * FROM products" + where + " ORDER BY " + order + " LIMIT ? OFFSET ?", [*args, *order_args, page_size, (page - 1) * page_size]).fetchall()
            all_count = db.execute("SELECT count(*) FROM products").fetchone()[0]
            detailed = db.execute("SELECT count(*) FROM products WHERE detail_json IS NOT NULL").fetchone()[0]
            inventory = db.execute("SELECT count(*) FROM products WHERE inventory_json IS NOT NULL").fetchone()[0]
        return {
            "source": "cj", "total": total, "all_count": all_count,
            "detail_count": detailed, "inventory_count": inventory,
            "page": page, "page_size": page_size,
            "categories": list(dict.fromkeys(name for names in _CATEGORIES.values() for name in names)),
            "products": [self._card(row) for row in rows],
        }

    async def browse(self, query: str = "", category: str = "", page: int = 1, page_size: int = 24) -> dict:
        return await asyncio.to_thread(self._browse, query, category, page, page_size)

    async def execute(self, spec: ProductSearchSpec) -> dict:
        # Unknown shipping, material and currency conversions are reported as
        # unknown by the decision layer; they must not be used as hard filters.
        page = await self.browse(spec.normalized_query, spec.category or "", 1, max(spec.top_k * 4, 20))
        hits = page["products"]
        if spec.price_max_major is not None and spec.target_currency == "USD" and spec.budget_basis == "product":
            hits = [item for item in hits if item["price_kind"] == "unknown" or item["price_major"] <= spec.price_max_major]
        hits = [{**item, "skus": item["skus"][:8]} for item in hits[:spec.top_k]]
        return {
            "source": "cj", "hits": hits, "total_candidates": page["total"],
            "recall_strategy": "cj_snapshot_keyword", "rerank_applied": False,
            "data_scope": "CJ 快照关键词匹配；USD 列表参考价。库存、目的地配送、运费、税费和最终到手价未实时核验，不能当作已满足的筛选条件。",
            "filtered_out": [],
        }
