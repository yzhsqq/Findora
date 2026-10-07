"""Read-only CJ snapshot used by both the catalog page and agent search.

The collector owns writes. Direct browsing stays local; Agent hybrid search may
call embedding and rerank services, but never CJ. Missing fulfillment facts stay
unknown.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass, replace
import html
import json
import logging
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient, ProductVectorIndex, Reranker
from app.infrastructure.context import ShoppingContext
from app.infrastructure.cj_product_links import product_link_fields
from app.infrastructure.cj_catalog_snapshot import resolve_catalog_snapshot
from app.infrastructure.persistence.cj_localization import CJLocalization


_BASE_WORDS = {
    "背包": "backpack", "旅行": "travel", "行李": "luggage", "包": "bag",
    "耳机": "headphone", "耳塞": "earbud", "蓝牙": "bluetooth", "手机": "phone",
    "充电": "charger", "电脑": "computer", "键盘": "keyboard", "鼠标": "mouse",
    "露营": "camping", "户外": "outdoor", "运动": "sport", "宠物": "pet",
    "猫": "cat", "狗": "dog", "儿童": "kids", "婴儿": "baby",
    "家居": "home", "收纳": "storage", "厨房": "kitchen", "灯": "light",
    "美妆": "beauty", "化妆": "makeup", "办公": "office", "玩具": "toy",
}
# v1 中文词典实验未通过留出集；保留代码供显式回放，默认不参与线上检索。
_EXPERIMENTAL_WORDS = {
    "绿色": "green", "檀木": "sandalwood", "梳子": "comb", "梳头": "comb",
    "防水": "waterproof", "双屏": "dual", "数码": "digital", "相机": "camera",
    "反光": "reflective", "牵引绳": "leash", "五英尺": "5 ft",
    "登山": "mountaineering", "双肩": "backpack", "战术": "tactical",
    "长方形": "rectangular", "铁皮": "tinplate", "拉扣": "clasp", "盒": "box",
}
_WORDS = {**_BASE_WORDS, **_EXPERIMENTAL_WORDS}
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
_HYBRID_CANDIDATES = 80
_CATEGORY_BOOST = 0.005
_RERANK_TIMEOUT_SECONDS = 3.0
logger = logging.getLogger(__name__)


class CJSearchUnavailable(ValueError):
    """Search infrastructure failed; it must not look like an empty catalog."""


@dataclass(frozen=True)
class CJSearchDocument:
    """Small immutable projection shared by BM25 and the existing vector index."""

    product_id: str
    title: str
    first_category: str
    second_category: str
    third_category: str
    text: str

    def searchable_text(self) -> str:
        return self.text


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


def _terms(query: str, *, experimental_lexicon: bool = False) -> list[str]:
    words = _WORDS if experimental_lexicon else _BASE_WORDS
    english = re.findall(r"[a-zA-Z]{3,}", query.lower())
    translated = [word for zh, word in words.items() if zh in query and not any(
        zh != other and zh in other and other in query for other in words
    )]
    return list(dict.fromkeys([*translated, *english]))[:8]


class CJCatalog:
    source = "cj"

    def __init__(
        self,
        path: Path,
        *,
        experimental_lexicon: bool = False,
        embedder: EmbeddingClient | None = None,
        vector_index: ProductVectorIndex | None = None,
        reranker: Reranker | None = None,
        hybrid_enabled: bool = False,
        localization: CJLocalization | None = None,
    ):
        self.path = path
        self.experimental_lexicon = experimental_lexicon
        self.embedder = embedder
        self.vector_index = vector_index
        self.reranker = reranker
        self.hybrid_enabled = hybrid_enabled
        self.localization = localization
        self.vector_available = False
        self._documents: list[CJSearchDocument] | None = None
        self._documents_by_id: dict[str, CJSearchDocument] = {}

    def _db(self) -> sqlite3.Connection:
        snapshot = resolve_catalog_snapshot(self.path)
        if not snapshot.is_file():
            raise ValueError("CJ 商品快照不存在，请先运行采集脚本")
        db = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _card(row: sqlite3.Row, score: float = 1.0) -> dict:
        listing = json.loads(row["list_json"])
        detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
        inventory = json.loads(row["inventory_json"]) if row["inventory_json"] else {}
        price, price_text, price_kind = _price(listing.get("sellPrice"))
        stocks: dict[str, dict[str, int]] = {}
        for item in inventory.get("variantInventories") or []:
            origins = item.get("inventory") or []
            counts = [origin.get("totalInventory") for origin in origins]
            if counts and all(type(count) is int and count >= 0 for count in counts):
                stock = {"total": sum(counts)}
                if all(type(origin.get("cjInventory")) is int and origin["cjInventory"] >= 0
                       and type(origin.get("factoryInventory")) is int and origin["factoryInventory"] >= 0
                       for origin in origins):
                    stock["cj"] = sum(origin["cjInventory"] for origin in origins)
                    stock["factory"] = sum(origin["factoryInventory"] for origin in origins)
                stocks[str(item.get("vid"))] = stock
        skus = []
        for item in (detail.get("variants") or [])[:60]:
            if not isinstance(item, dict):
                continue
            sku_id = str(item.get("variantSku") or item.get("vid") or "")
            amount, _, kind = _price(item.get("variantSellPrice"))
            if sku_id and kind != "unknown":
                stock = stocks.get(str(item.get("vid")))
                skus.append({
                    "sku_id": sku_id, "spec": str(item.get("variantKey") or item.get("variantNameEn") or sku_id),
                    "variant_id": str(item.get("vid") or ""),
                    "price_major": amount, "currency": "USD",
                    "stock": stock["total"] if stock else 0,
                    "stock_known": stock is not None,
                    **({"cj_stock": stock["cj"], "factory_stock": stock["factory"]}
                       if stock and "cj" in stock and "factory" in stock else {}),
                })
        image = detail.get("bigImage") or listing.get("bigImage")
        material = _labels(detail.get("materialNameSet") or detail.get("materialName"))
        if not material or not any(re.search(r"[\u3400-\u9fff]", label) for label in material):
            material = _labels(detail.get("materialNameEnSet") or detail.get("materialNameEn"))
        warehouses = [str(item.get("countryCode")) for item in inventory.get("inventories") or []
                      if isinstance(item, dict) and item.get("countryCode") and _positive_count(item.get("cjInventoryNum"))]
        factory_countries = [str(item.get("countryCode")) for item in inventory.get("inventories") or []
                             if isinstance(item, dict) and item.get("countryCode") and _positive_count(item.get("factoryInventoryNum"))]
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
            "source_description": _plain(detail.get("description")),
            **product_link_fields(row),
            "ships_to": [], "ship_from_warehouses": list(dict.fromkeys(warehouses)),
            "factory_inventory_countries": list(dict.fromkeys(factory_countries)),
            "material_tags": material, "weight_kg": weight_kg,
            "updated_at": row["detail_fetched_at"] or row["list_fetched_at"],
            "inventory_checked_at": row["inventory_fetched_at"],
            "stock_known": bool(inventory),
            "detail_available": bool(detail),
        }

    def _browse(self, query: str, category: str, page: int, page_size: int) -> dict:
        exact = query.strip()
        direct_id = exact if re.fullmatch(
            r"(?:[0-9]{16,24}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})",
            exact, flags=re.I,
        ) else ""
        direct_sku = exact if re.fullmatch(r"CJ[A-Z0-9_-]{6,96}", exact, flags=re.I) else ""
        terms = [] if direct_id or direct_sku else _terms(query, experimental_lexicon=self.experimental_lexicon)
        clauses: list[str] = []
        args: list[str] = []
        categories = _CATEGORIES.get(category, (category,)) if category else ()
        if (query.strip() and re.search(r"[\u3400-\u9fff]", query) and not (direct_id or direct_sku)
                and self.localization is not None and self.localization.available()):
            return self._browse_localized(query, categories, page, page_size)
        if categories and not (direct_id or direct_sku):
            clauses.append("first_category IN (" + ",".join("?" for _ in categories) + ")")
            args.extend(categories)
        if direct_id:
            clauses.append("(pid = ? COLLATE NOCASE OR EXISTS (SELECT 1 FROM json_each(products.detail_json, '$.variants') v "
                           "WHERE json_extract(v.value, '$.vid') = ? COLLATE NOCASE))")
            args.extend((direct_id, direct_id))
        elif direct_sku:
            clauses.append("(lower(json_extract(list_json, '$.sku')) = lower(?) OR "
                           "EXISTS (SELECT 1 FROM json_each(products.detail_json, '$.variants') v "
                           "WHERE lower(json_extract(v.value, '$.variantSku')) = lower(?)))")
            args.extend((direct_sku, direct_sku))
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
            "products": self._localized_cards(rows),
        }

    def _localized_cards(self, rows: list[sqlite3.Row]) -> list[dict]:
        localized = self.localization.lookup_many([str(row["pid"]) for row in rows]) if self.localization else {}
        return [CJLocalization.apply_card(row, self._card(row), localized.get(str(row["pid"]))) for row in rows]

    def _browse_localized(self, query: str, categories: tuple[str, ...], page: int, page_size: int) -> dict:
        assert self.localization is not None
        ranked_ids = self.localization.search(query, categories)
        selected = ranked_ids[(page - 1) * page_size:page * page_size]
        found = self._rows_by_ids(selected)
        rows = [found[pid] for pid in selected if pid in found]
        with closing(self._db()) as db:
            all_count = db.execute("SELECT count(*) FROM products").fetchone()[0]
            detailed = db.execute("SELECT count(*) FROM products WHERE detail_json IS NOT NULL").fetchone()[0]
            inventory = db.execute("SELECT count(*) FROM products WHERE inventory_json IS NOT NULL").fetchone()[0]
        return {
            "source": "cj", "total": len(ranked_ids), "all_count": all_count,
            "detail_count": detailed, "inventory_count": inventory,
            "page": page, "page_size": page_size,
            "categories": list(dict.fromkeys(name for names in _CATEGORIES.values() for name in names)),
            "products": self._localized_cards(rows),
        }

    async def browse(self, query: str = "", category: str = "", page: int = 1, page_size: int = 24) -> dict:
        return await asyncio.to_thread(self._browse, query, category, page, page_size)

    async def cards_by_ids(self, product_ids: list[str]) -> list[dict]:
        def read():
            rows = self._rows_by_ids(list(dict.fromkeys(product_ids)))
            return self._localized_cards(list(rows.values()))
        return await asyncio.to_thread(read)

    async def localize_saved_cards(self, cards: list[dict]) -> list[dict]:
        if self.localization is None or not cards:
            return cards
        def project() -> list[dict]:
            pids = [str(card.get("product_id")) for card in cards
                    if card.get("source_platform") == "CJdropshipping" and card.get("product_id")]
            rows = self._rows_by_ids(pids)
            translated = self.localization.lookup_many(pids)
            return [CJLocalization.apply_card(rows[card["product_id"]], card, translated.get(card["product_id"]))
                    if card.get("product_id") in rows else card for card in cards]
        return await asyncio.to_thread(project)

    @staticmethod
    def _index_text(row: sqlite3.Row) -> str:
        listing = json.loads(row["list_json"])
        detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
        values: list[Any] = [
            listing.get("nameEn"), detail.get("productNameEn"), row["first_category"],
            row["second_category"], row["third_category"], detail.get("entryNameEn"),
            detail.get("materialNameEnSet") or detail.get("materialNameEn"),
            detail.get("packingNameEnSet") or detail.get("packingNameEn"),
            detail.get("productKeyEnSet") or detail.get("productKeyEn"),
        ]
        parts: list[str] = []
        for value in values:
            if isinstance(value, list):
                parts.extend(str(item).strip() for item in value if str(item).strip())
            elif value is not None and str(value).strip():
                parts.append(str(value).strip())
        return " ".join(dict.fromkeys(parts))[:1200]

    def _load_documents(self) -> list[CJSearchDocument]:
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT pid,first_category,second_category,third_category,list_json,detail_json FROM products",
            ).fetchall()
        documents = [
            CJSearchDocument(
                product_id=str(row["pid"]),
                title=str(json.loads(row["list_json"]).get("nameEn") or "CJ 商品"),
                first_category=str(row["first_category"]),
                second_category=str(row["second_category"]),
                third_category=str(row["third_category"]),
                text=self._index_text(row),
            )
            for row in rows
        ]
        self._documents = documents
        self._documents_by_id = {item.product_id: item for item in documents}
        return documents

    async def list_all(self) -> list[CJSearchDocument]:
        """Return the stable search projection consumed by index_bootstrap."""
        if self._documents is None:
            return await asyncio.to_thread(self._load_documents)
        return self._documents

    def set_vector_available(self, available: bool) -> None:
        self.vector_available = bool(available)

    @staticmethod
    def _category_match(document: CJSearchDocument, category: str | None) -> bool:
        if not category:
            return False
        expected = _CATEGORIES.get(category, (category,))
        fields = {document.first_category.casefold(), document.second_category.casefold(), document.third_category.casefold()}
        return any(value.casefold() in fields for value in expected)

    def _rows_by_ids(self, product_ids: list[str]) -> dict[str, sqlite3.Row]:
        if not product_ids:
            return {}
        placeholders = ",".join("?" for _ in product_ids)
        with closing(self._db()) as db:
            rows = db.execute(f"SELECT * FROM products WHERE pid IN ({placeholders})", product_ids).fetchall()
        return {str(row["pid"]): row for row in rows}

    async def _hybrid_search(self, spec: ProductSearchSpec) -> dict:
        if not self.vector_available or self.embedder is None or self.vector_index is None:
            raise CJSearchUnavailable("CJ 商品检索暂不可用，请稍后重试")
        await self.list_all()
        snapshot = ShoppingContext.current()
        raw_query = spec.raw_query.strip() or (snapshot.raw_query.strip() if snapshot else "") or spec.normalized_query.strip()
        normalized_query = spec.normalized_query.strip()
        # Keep the complete Agent rewrite in dense recall. BM25 receives only
        # its ASCII keywords, even when the rewrite also contains Chinese.
        english_query = " ".join(re.findall(
            r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", normalized_query,
        ))[:240]
        dense_texts = [raw_query]
        if normalized_query and normalized_query.casefold() != raw_query.casefold():
            dense_texts.append(normalized_query)
        try:
            vectors = await asyncio.wait_for(self.embedder.embed_batch(dense_texts), timeout=7.0)
            fused_hits = await asyncio.wait_for(
                self.vector_index.hybrid_search(vectors, english_query, top_n=_HYBRID_CANDIDATES * 2),
                timeout=3.0,
            )
        except Exception as error:  # noqa: BLE001 - report infrastructure failure, not an empty hit list.
            logger.warning("CJ Qdrant hybrid search unavailable: %s", error)
            raise CJSearchUnavailable("CJ 商品检索暂不可用，请稍后重试") from error

        fused = [
            (hit.score, self._documents_by_id[hit.product_id])
            for hit in fused_hits if hit.product_id in self._documents_by_id
        ]

        # Category is evidence for ranking, never a reason to discard a query-relevant item.
        rescored = [
            (score + (_CATEGORY_BOOST if self._category_match(document, spec.category) else 0.0), document)
            for score, document in fused
        ]
        rescored.sort(key=lambda item: (-item[0], item[1].product_id))
        strategy = "cj_qdrant_rrf" if english_query else "cj_qdrant_dense"
        rerank_applied = False
        if self.reranker is not None and rescored:
            try:
                scores = await asyncio.wait_for(
                    self.reranker.rerank(raw_query, [document.searchable_text() for _, document in rescored]),
                    timeout=_RERANK_TIMEOUT_SECONDS,
                )
                if len(scores) != len(rescored):
                    raise ValueError("重排分数数量与候选数量不一致")
                scores = [float(score) for score in scores]
                if not all(math.isfinite(score) for score in scores):
                    raise ValueError("重排分数必须有限")
                # Replace retrieval scores only after validating the whole response.
                # Rerank all candidates before budget filtering and final top_k.
                ranked = [(score, document) for score, (_, document) in zip(scores, rescored)]
                ranked.sort(key=lambda item: (-item[0], item[1].product_id))
                rescored = ranked
                strategy += "_rerank"
                rerank_applied = True
            except Exception as error:  # noqa: BLE001 - keep the successful recall on rerank failure.
                logger.warning("CJ rerank 不可用，保留召回排序：%s", error)
        candidate_ids = [document.product_id for _, document in rescored]
        rows = self._rows_by_ids(candidate_ids)
        localized = self.localization.lookup_many(candidate_ids) if self.localization else {}
        cards: list[dict] = []
        for score, document in rescored:
            row = rows.get(document.product_id)
            if row is None:
                continue
            card = CJLocalization.apply_card(row, self._card(row, score=score), localized.get(document.product_id))
            if spec.price_max_major is not None and spec.target_currency == "USD" and spec.budget_basis == "product":
                if card["price_kind"] != "unknown" and card["price_major"] > spec.price_max_major:
                    continue
            cards.append({**card, "skus": card["skus"][:8]})
            if len(cards) >= spec.top_k:
                break
        return {
            "source": "cj",
            "hits": cards,
            "total_candidates": len(rescored),
            "recall_strategy": strategy,
            "retrieval_variant": "cj_qdrant_dense_bm25_rrf_v2",
            "vector_available": True,
            "category_mode": "rerank" if rerank_applied else "soft_boost",
            "query_variants": {"dense": dense_texts, "bm25": english_query},
            "rerank_applied": rerank_applied,
            "data_scope": "CJ 快照混合检索；USD 列表参考价。库存、目的地配送、运费、税费和最终到手价未实时核验，不能当作已满足的筛选条件。",
            "filtered_out": [],
        }

    async def execute(self, spec: ProductSearchSpec) -> dict:
        # Unknown shipping, material and currency conversions are reported as
        # unknown by the decision layer; they must not be used as hard filters.
        exact = spec.normalized_query.strip()
        direct_identifier = bool(
            re.fullmatch(r"(?:[0-9]{16,24}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})", exact, flags=re.I)
            or re.fullmatch(r"CJ[A-Z0-9_-]{6,96}", exact, flags=re.I)
        )
        # Opaque model/SKU-like strings have no semantic meaning. An unknown ID
        # must return no match instead of an unrelated nearest vector neighbor.
        opaque_identifier = bool(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{11,}", exact)
            and re.search(r"\d", exact)
        )
        if self.hybrid_enabled and not (direct_identifier or opaque_identifier):
            try:
                return await self._hybrid_search(spec)
            except CJSearchUnavailable:
                # Only an actual product ID/SKU can be recovered by exact lookup.
                # A free-text outage must remain an error, never a false "no products".
                embedded_id = re.search(
                    r"(?<![A-Za-z0-9])(?:[0-9]{16,24}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|CJ[A-Z0-9_-]{6,96})(?![A-Za-z0-9])",
                    spec.raw_query or spec.normalized_query, flags=re.I,
                )
                if embedded_id is None:
                    raise
                fallback = await self.execute(replace(spec, normalized_query=embedded_id.group(), category=None))
                fallback["degraded_from"] = "cj_qdrant_unavailable"
                return fallback
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
