"""Local eBay US snapshot. Listing prices are not cross-border landed costs."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from app.infrastructure.persistence.snapshot_import import import_records
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient, ProductVectorIndex, Reranker
from app.infrastructure.context import ShoppingContext
from app.infrastructure.persistence.ebay_localization import EbayLocalization, LANGUAGE_FIELDS
from app.infrastructure.persistence.sql.readonly_mysql import ReadonlyMySQLConnection


# eBay item ids are numeric listing ids; CJ ids are longer, so the ranges do not overlap.
ITEM_ID = re.compile(r"\d{9,15}")
PREFIX = "ebay:us:"
logger = logging.getLogger(__name__)
_HYBRID_CANDIDATES = 40
_CATEGORY_BOOST = 0.005
_RERANK_TIMEOUT_SECONDS = 3.0


def is_item_id_query(value: str) -> bool:
    return bool(ITEM_ID.fullmatch(value.strip()))


WORDS = {"狗玩具": "dog toy", "宠物玩具": "pet toy", "家居装饰": "home decor",
         "装饰": "decor", "灯泡": "light bulb", "宠物": "pet", "狗": "dog",
         "猫": "cat", "玩具": "toy", "家居": "home", "灯": "light", "花瓶": "vase",
         "地毯": "rug", "抱枕": "pillow", "圣诞": "christmas", "咀嚼": "chew",
         "卡牌": "card", "收藏卡": "trading card", "宝可梦": "pokemon",
         "手办": "figure", "模型": "model", "贴纸": "sticker"}
CATEGORIES = {"Toys, Kids & Babies": ("Toys & Hobbies", "Dolls & Bears", "Baby"),
              "Consumer Electronics": ("Electronics", "Cameras & Photo", "Home Audio",
                                       "Computers/Tablets & Networking", "Video Games & Consoles"),
              "Phones & Accessories": ("Cell Phones & Accessories",),
              "Bags & Shoes": ("Fashion", "Jewelry & Watches"),
              "Health, Beauty & Hair": ("Health & Beauty",),
              "Computer & Office": ("Computers/Tablets & Networking", "Business & Industrial"),
              "Home, Garden & Furniture": ("Home & Garden", "Crafts"),
              "家居生活": ("Home & Garden",), "母婴宠物": ("Pet Supplies",),
              "数码配件": ("Electronics",)}
SCOPE = "eBay 美国站本地快照；USD 商品报价，页面配送估算基于美国地址。未核实寄往中国等跨境目的地的配送、库存、运费、税费和到手价；与 CJ、Amazon 仅作同类候选比较，未确认同款。"


@dataclass(frozen=True)
class EbaySearchDocument:
    """Stable projection shared by the vector index and hybrid recall."""

    product_id: str
    title: str
    category: str
    text: str

    def searchable_text(self) -> str:
        return self.text


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _price(value: object) -> float | None:
    """eBay quotes prices as strings such as "$4.99" or "US $1,299.99"."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
        return _amount(match[0]) if match else None
    return _amount(value)


def _text(value: object, limit: int = 1400) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def ebay_url(value: object, item_id: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in {"ebay.com", "www.ebay.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
        match = re.search(r"/itm/([^/?#]+)(?:/|$)", parsed.path)
        # The listing must identify the same item; variation query strings are allowed.
        return value if match and re.fullmatch(r"\d{9,15}", match[1]) and match[1] == item_id else None
    except ValueError:
        return None


def _observed_at(record: dict, fallback_timestamp: str | None) -> str:
    """Prefer the crawler timestamp; collected files without one fall back to file mtime."""
    stamp = _text(record.get("timestamp"))
    if stamp:
        try:
            observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("eBay 记录采集时间格式无效") from error
        if observed.tzinfo is None:
            raise ValueError("eBay 记录采集时间缺少时区")
        return stamp
    if not fallback_timestamp:
        raise ValueError("eBay 记录缺少采集时间，且未提供文件时间兜底")
    return fallback_timestamp


def _specifications(record: dict) -> tuple[list[str], dict[str, str]]:
    """Item specifics become highlights; identifier-like ones are kept separately."""
    highlights: list[str] = []
    identifiers: dict[str, str] = {}
    for spec in record.get("product_specifications") or []:
        if not isinstance(spec, dict):
            continue
        name, value = _text(spec.get("specification_name"), 80), _text(spec.get("specification_value"), 200)
        if not name or not value:
            continue
        if re.search(r"UPC|GTIN|EAN|ISBN|MPN|Model|Brand", name, re.I):
            identifiers[name] = value
        if len(highlights) < 6:
            highlights.append(f"{name}: {value}")
    for key, label in (("gtin", "GTIN"), ("mpn", "MPN")):
        value = _text(record.get(key), 200)
        if value:
            identifiers[label] = value
    return highlights, identifiers


def _skus(record: dict, item_id: str, price: float | None) -> list[dict]:
    skus: list[dict] = []
    seen: set[str] = set()
    for group in record.get("variants") or []:
        if not isinstance(group, dict):
            continue
        kind = _text(group.get("variant_type"), 40)
        for option in group.get("variant_options") or []:
            if not isinstance(option, dict):
                continue
            option_id = _text(option.get("option_id"))
            amount = _price(option.get("option_price"))
            # A snapshot never proves live stock, so variant stock stays unknown.
            if not option_id or option_id in seen or amount is None:
                continue
            seen.add(option_id)
            name = _text(option.get("option_name"), 180)
            skus.append({"sku_id": PREFIX + option_id, "variant_id": option_id,
                         "spec": f"{kind}: {name}" if kind and name else name or "页面规格未提供",
                         "price_major": amount, "currency": "USD", "stock": 0, "stock_known": False})
            if len(skus) >= 40:
                return skus
    if price is not None:
        skus.insert(0, {"sku_id": PREFIX + item_id, "variant_id": item_id,
                        "spec": "页面规格未提供" if skus else "页面规格未提供",
                        "price_major": price, "currency": "USD", "stock": 0, "stock_known": False})
    return skus


def normalize(record: dict, *, fallback_timestamp: str | None = None) -> dict:
    if not isinstance(record, dict):
        raise ValueError("eBay 记录必须为对象")
    item_id = _text(record.get("product_id"))
    title = _text(record.get("title"), 500)
    if not ITEM_ID.fullmatch(item_id) or not title:
        raise ValueError("eBay 记录缺少有效商品编号或标题")
    currency = _text(record.get("currency"), 8).upper()
    if currency not in {"", "USD"}:
        raise ValueError("当前导入器仅支持 eBay 美国站 USD 快照")
    stamp = _observed_at(record, fallback_timestamp)
    price = _price(record.get("price"))
    sale_price = _price(record.get("sale_price"))
    highlights, identifiers = _specifications(record)
    category = _text(record.get("root_category"), 120) or "未分类"
    category_path = _text(record.get("product_category"), 240)
    conditions = []
    if price is not None and sale_price is not None and sale_price < price:
        conditions.append(f"页面促销价：原价 US${price:.2f}，促销价 US${sale_price:.2f}；优惠条件待核实")
    conditions.append("运费、卖家优惠资格及结算价格需在 eBay 确认")
    location = _text(record.get("item_location"), 160)
    origin = location.rsplit(",", 1)[-1].strip() if location else ""
    available = record.get("availability") == "in_stock" or record.get("is_sold") is False
    card = {"product_id": PREFIX + item_id, "external_product_id": item_id,
            "canonical_product_id": PREFIX + item_id, "source_platform": "eBay",
            "title": title, "brand": _text(record.get("brand"), 100), "category": category,
            "origin_country": origin, "price_major": price if price is not None else 0.0,
            "currency": "USD", "price_kind": "listing" if price is not None else "unknown",
            "price_text": f"US${price:.2f}" if price is not None else "报价待核实",
            "highlights": highlights, "description": _text(record.get("description")),
            "score": 1.0, "skus": _skus(record, item_id, price), "stock_known": False,
            "detail_available": bool(record.get("product_specifications") or record.get("variants")),
            "updated_at": stamp, "seller_name": _text(record.get("seller_name"), 180),
            "source_region": _text(record.get("store_country"), 8).upper() or "US",
            "condition": _text(record.get("condition"), 160),
            "availability_text": "采集时页面标记在售；实时库存未核实" if available else "可购买状态未提供",
            "price_conditions": list(dict.fromkeys(conditions)), "match_status": "unverified",
            "identifiers": identifiers, "rating_is_live": False,
            "landed_price": {"unavailable_reason": "仅有美国站商品报价；跨境配送、运费、税费及到手价待核实"}}
    if type(available) is bool:
        card["snapshot_available"] = available
    if price is not None and card["skus"] and card["skus"][0]["variant_id"] == item_id:
        card["default_sku_id"] = card["skus"][0]["sku_id"]
    if category_path:
        card["category_path"] = category_path
    link = ebay_url(record.get("url"), item_id)
    if link:
        card.update(source_url=link, source_url_status="observed", source_url_checked_at=stamp)
    images = record.get("images")
    image = record.get("image_url") or (images[0] if isinstance(images, list) and images else None)
    if isinstance(image, str) and image.startswith("https://i.ebayimg.com/"):
        card.update(image_url=image, image_kind="source", image_alt=title)
    return card


def import_snapshot(input_path: Path, output_path: Path, *, merge: bool = False, skip_invalid: bool = False) -> dict:
    fallback = datetime.fromtimestamp(input_path.stat().st_mtime, timezone.utc).isoformat()
    return import_records(input_path, output_path, table="ebay_products", label="eBay",
                          normalize=lambda record: normalize(record, fallback_timestamp=fallback), merge=merge, skip_invalid=skip_invalid)


class EbayCatalog:
    def __init__(self, path: Path, *, mysql_dsn: str | None = None,
                 embedder: EmbeddingClient | None = None,
                 vector_index: ProductVectorIndex | None = None, reranker: Reranker | None = None,
                 hybrid_enabled: bool = False):
        self.path = path.resolve()
        self.mysql_dsn = mysql_dsn
        if not mysql_dsn and not self.path.is_file():
            raise ValueError("eBay 快照不存在，请先运行 scripts/import_ebay_catalog.py")
        self.localization = EbayLocalization(self.path.with_name("ebay_localization.sqlite3"))
        self.embedder = embedder
        self.vector_index = vector_index
        self.reranker = reranker
        self.hybrid_enabled = hybrid_enabled
        self.vector_available = False
        self._documents: list[EbaySearchDocument] | None = None
        self._documents_by_id: dict[str, EbaySearchDocument] = {}

    def _cards(self) -> list[dict]:
        if self.mysql_dsn:
            with closing(ReadonlyMySQLConnection(self.mysql_dsn)) as db:
                return [json.loads(row["card_json"]) for row in
                        db.execute("SELECT card_json FROM ebay_products ORDER BY product_id")]
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT card_json FROM ebay_products ORDER BY product_id")]

    def _browse(self, query: str, category: str, page: int, page_size: int) -> dict:
        cards = self._cards()
        displayed = {c["product_id"]: c for c in self.localization.project(cards)}
        categories = sorted({c["category"] for c in cards})
        all_count = len(cards)
        exact = query.removeprefix(PREFIX).strip()
        direct = is_item_id_query(exact) or query.startswith(PREFIX)
        if direct:
            cards = [c for c in cards if c["external_product_id"] == exact]
        else:
            translated = query
            for cn, en in WORDS.items():
                translated = translated.replace(cn, " " + en + " ")
            tokens = set(re.findall(r"[a-z0-9]+", translated.casefold())) - {"a", "the", "for", "and", "with"}
            def terms(text: str) -> set[str]:
                return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text.casefold())}
            wanted = {w.rstrip("s") for w in tokens}
            ranked = []
            for card in cards:
                title = terms(card["title"] + " " + card["brand"])
                body = terms(" ".join(card["highlights"]) + " " + card["category"] + " "
                             + card.get("category_path", "") + " " + card.get("condition", ""))
                score = 4 * len(wanted & title) + len(wanted & body)
                localized = displayed[card["product_id"]]
                chinese = re.findall(r"[\u3400-\u9fff]+", query)
                local_text = " ".join((localized["title"], localized.get("description", ""), *localized["highlights"],
                                       *(s["spec"] for s in localized["skus"])))
                score += sum(4 if text in localized["title"] else 1 if text in local_text else 0 for text in chinese)
                if not query or score:
                    ranked.append((score, card))
            cards = [c for _, c in sorted(ranked, key=lambda v: (-v[0], v[1]["product_id"]))]
        if category and not direct:
            cards = [c for c in cards if c["category"] in CATEGORIES.get(category, (category,))]
        return {"source": "ebay", "total": len(cards), "all_count": all_count,
                "detail_count": sum(c["detail_available"] for c in cards), "inventory_count": 0,
                "page": page, "page_size": page_size, "categories": categories,
                "products": [displayed[c["product_id"]] for c in cards[(page - 1) * page_size:page * page_size]], "data_scope": SCOPE}

    async def browse(self, query: str = "", category: str = "", page: int = 1, page_size: int = 24) -> dict:
        return await asyncio.to_thread(self._browse, query, category, page, page_size)

    async def cards_by_ids(self, product_ids: list[str]) -> list[dict]:
        ids = set(product_ids)
        def read():
            return self.localization.project([c for c in self._cards() if c["product_id"] in ids])
        return await asyncio.to_thread(read)

    async def localize_saved_cards(self, cards: list[dict]) -> list[dict]:
        latest = {c["product_id"]: c for c in await self.cards_by_ids([c["product_id"] for c in cards])}
        result = []
        for saved in cards:
            current = latest.get(saved["product_id"])
            if current is None:
                result.append(saved)
                continue
            specs = {s["sku_id"]: s for s in current["skus"]}
            result.append({**saved, **{key: current[key] for key in LANGUAGE_FIELDS if key in current},
                           "skus": [{**sku, **{key: specs[sku["sku_id"]][key] for key in ("spec", "source_spec")
                                               if key in specs[sku["sku_id"]]}} if sku["sku_id"] in specs else sku
                                    for sku in saved.get("skus", [])]})
        return result

    @staticmethod
    def _index_text(card: dict) -> str:
        """English retrieval text; Chinese is a display projection only."""
        parts = [card["title"], card["brand"], card["category"], card.get("category_path", ""),
                 card.get("condition", ""), *card["highlights"], card.get("description", "")[:600]]
        return " ".join(dict.fromkeys(part.strip() for part in parts if isinstance(part, str) and part.strip()))[:1200]

    def _load_documents(self) -> list[EbaySearchDocument]:
        documents = [
            EbaySearchDocument(product_id=card["product_id"], title=card["title"],
                               category=card["category"], text=self._index_text(card))
            for card in self._cards()
        ]
        self._documents = documents
        self._documents_by_id = {item.product_id: item for item in documents}
        return documents

    async def list_all(self) -> list[EbaySearchDocument]:
        """Stable search projection consumed by index_bootstrap."""
        if self._documents is None:
            return await asyncio.to_thread(self._load_documents)
        return self._documents

    def set_vector_available(self, available: bool) -> None:
        self.vector_available = bool(available)

    @staticmethod
    def _category_match(document: EbaySearchDocument, category: str | None) -> bool:
        return bool(category) and document.category.casefold() in {
            value.casefold() for value in CATEGORIES.get(category, (category,))}

    async def _hybrid_search(self, spec: ProductSearchSpec) -> dict:
        await self.list_all()
        snapshot = ShoppingContext.current()
        raw_query = spec.raw_query.strip() or (snapshot.raw_query.strip() if snapshot else "") or spec.normalized_query.strip()
        normalized_query = spec.normalized_query.strip()
        english_query = " ".join(re.findall(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", normalized_query))[:240]
        dense_texts = [raw_query]
        if normalized_query and normalized_query.casefold() != raw_query.casefold():
            dense_texts.append(normalized_query)
        vectors = await asyncio.wait_for(self.embedder.embed_batch(dense_texts), timeout=7.0)
        fused_hits = await asyncio.wait_for(
            self.vector_index.hybrid_search(vectors, english_query, top_n=_HYBRID_CANDIDATES * 2),
            timeout=3.0,
        )
        fused = [(hit.score, self._documents_by_id[hit.product_id])
                 for hit in fused_hits if hit.product_id in self._documents_by_id]
        rescored = [(score + (_CATEGORY_BOOST if self._category_match(document, spec.category) else 0.0), document)
                    for score, document in fused]
        rescored.sort(key=lambda item: (-item[0], item[1].product_id))
        strategy = "ebay_qdrant_rrf" if english_query else "ebay_qdrant_dense"
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
                rescored = sorted(zip(scores, (document for _, document in rescored)),
                                  key=lambda item: (-item[0], item[1].product_id))
                strategy += "_rerank"
                rerank_applied = True
            except Exception as error:  # noqa: BLE001 - keep the successful recall on rerank failure.
                logger.warning("eBay rerank 不可用，保留召回排序：%s", error)
        found = {card["product_id"]: card for card in await self.cards_by_ids([d.product_id for _, d in rescored])}
        hits = []
        for score, document in rescored:
            card = found.get(document.product_id)
            if card is None:
                continue
            if spec.price_max_major is not None and spec.target_currency == "USD" and spec.budget_basis == "product":
                if card["price_kind"] != "unknown" and card["price_major"] > spec.price_max_major:
                    continue
            hits.append({**card, "score": score, "skus": card["skus"][:8]})
            if len(hits) >= spec.top_k:
                break
        return {"source": "ebay", "hits": hits, "total_candidates": len(rescored),
                "recall_strategy": strategy, "retrieval_variant": "ebay_qdrant_dense_bm25_rrf_v1",
                "vector_available": True, "category_mode": "rerank" if rerank_applied else "soft_boost",
                "query_variants": {"dense": dense_texts, "bm25": english_query},
                "rerank_applied": rerank_applied, "data_scope": SCOPE, "filtered_out": []}

    async def _keyword_search(self, spec: ProductSearchSpec) -> dict:
        page = await self.browse(spec.normalized_query, spec.category or "", 1, 200)
        hits = page["products"]
        if spec.price_max_major is not None and spec.target_currency == "USD" and spec.budget_basis == "product":
            hits = [c for c in hits if c["price_kind"] == "unknown" or c["price_major"] <= spec.price_max_major]
        result = {"source": "ebay", "hits": [{**c, "skus": c["skus"][:8]} for c in hits[:spec.top_k]], "total_candidates": page["total"],
                "recall_strategy": "ebay_snapshot_keyword", "rerank_applied": False,
                "data_scope": SCOPE, "filtered_out": []}
        if is_item_id_query(spec.normalized_query.removeprefix(PREFIX)) or spec.normalized_query.startswith(PREFIX):
            result.update(existence_checked=True, missing_identifiers=[] if page["total"] else [spec.normalized_query])
        return result

    async def execute(self, spec: ProductSearchSpec) -> dict:
        exact = spec.normalized_query.strip()
        direct = exact.startswith(PREFIX) or is_item_id_query(exact.removeprefix(PREFIX))
        if (self.hybrid_enabled and self.vector_available and not direct
                and self.embedder is not None and self.vector_index is not None):
            try:
                return await self._hybrid_search(spec)
            except Exception as error:  # noqa: BLE001 - the local snapshot can still serve keyword recall.
                logger.warning("eBay 向量检索不可用，回退本地关键词召回：%s", error)
                return {**await self._keyword_search(spec), "degraded_from": "ebay_qdrant_unavailable"}
        return await self._keyword_search(spec)
