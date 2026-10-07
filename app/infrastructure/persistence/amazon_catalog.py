"""Local Amazon US snapshot. Listing prices are not cross-border landed costs."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient, ProductVectorIndex, Reranker
from app.infrastructure.context import ShoppingContext
from app.infrastructure.persistence.amazon_localization import AmazonLocalization, LANGUAGE_FIELDS


ASIN = re.compile(r"[A-Z0-9]{10}")
PREFIX = "amazon:us:"
logger = logging.getLogger(__name__)
_HYBRID_CANDIDATES = 40
_CATEGORY_BOOST = 0.005
_RERANK_TIMEOUT_SECONDS = 3.0


def is_asin_query(value: str) -> bool:
    return bool(ASIN.fullmatch(value.upper()) and re.search(r"\d", value))


WORDS = {"狗玩具": "dog toy", "宠物玩具": "pet toy", "家居装饰": "home decor",
         "装饰": "decor", "灯泡": "light bulb", "宠物": "pet", "狗": "dog",
         "猫": "cat", "玩具": "toy", "家居": "home", "灯": "light", "花瓶": "vase",
         "地毯": "rug", "抱枕": "pillow", "圣诞": "christmas", "咀嚼": "chew"}
CATEGORIES = {"Home, Garden & Furniture": ("Home & Kitchen", "Patio, Lawn & Garden", "Tools & Home Improvement",
                                           "Kitchen & Dining", "Appliances"),
              "Consumer Electronics": ("Electronics",),
              "Phones & Accessories": ("Cell Phones & Accessories",),
              "Bags & Shoes": ("Shoe, Jewelry & Watch Accessories",),
              "Health, Beauty & Hair": ("Health & Household", "Beauty & Personal Care"),
              "Toys, Kids & Babies": ("Baby", "Baby Products", "Toys & Games"),
              "Computer & Office": ("Office Products",),
              "家居生活": ("Home & Kitchen", "Tools & Home Improvement"),
              "母婴宠物": ("Pet Supplies",), "数码配件": ("Electronics",)}
SCOPE = "Amazon 美国站本地快照；USD 商品报价，配送报价地区为美国邮编。未核实寄往中国等跨境目的地的配送、库存、运费、税费和到手价；与 CJ 仅作同类候选比较，未确认同款。"


@dataclass(frozen=True)
class AmazonSearchDocument:
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


def _text(value: object, limit: int = 1400) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def amazon_url(value: object, asin: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in {"amazon.com", "www.amazon.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", parsed.path)
        return value if match and match[1] == asin else None
    except ValueError:
        return None


def normalize(record: dict) -> dict:
    asin = _text(record.get("asin")).upper()
    title = _text(record.get("title"), 500)
    stamp = _text(record.get("timestamp"))
    if not ASIN.fullmatch(asin) or not title:
        raise ValueError("Amazon 记录缺少有效 ASIN 或标题")
    try:
        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("采集时间缺少时区")
    except ValueError as error:
        raise ValueError("Amazon 记录缺少带时区的采集时间") from error
    if record.get("currency") != "USD":
        raise ValueError("当前导入器仅支持 Amazon 美国站 USD 快照")
    price = _amount(record.get("final_price"))
    variants = record.get("variations") or []
    current = next((v for v in variants if isinstance(v, dict) and v.get("asin") == asin), {})
    skus = []
    seen = set()
    # The selected listing's final_price takes precedence over variation prices.
    for variant in [{**current, "asin": asin, "price": price}, *variants]:
        if not isinstance(variant, dict):
            continue
        vid = _text(variant.get("asin")).upper()
        amount = _amount(variant.get("price"))
        if not ASIN.fullmatch(vid) or vid in seen or amount is None or variant.get("currency", "USD") != "USD":
            continue
        if vid == asin and price is None:
            continue
        seen.add(vid)
        skus.append({"sku_id": PREFIX + vid, "variant_id": vid,
                     "spec": _text(variant.get("name"), 180) or _text(variant.get("size"), 180) or "页面规格未提供",
                     "price_major": amount, "currency": "USD", "stock": 0, "stock_known": False})
        if len(skus) >= 40:
            break
    categories = record.get("categories") or []
    category = next((_text(c) for c in categories if isinstance(c, str)), "未分类")
    features = record.get("features") or []
    features = [_text(x, 240) for x in features if isinstance(x, str)][:6]
    conditions = []
    breakdown = record.get("prices_breakdown")
    if isinstance(breakdown, dict) and breakdown.get("deal_type"):
        conditions.append("页面促销：" + _text(breakdown["deal_type"], 200))
    for key in ("coupon_description", "coupon"):
        if record.get(key):
            conditions.append("优惠券条件待核实：" + _text(str(record[key]), 200))
    conditions.append("会员、优惠资格及结算价格需在 Amazon 确认")
    details = record.get("product_details") or []
    identifiers = {str(v.get("type")): _text(v.get("value"), 200) for v in details
                   if isinstance(v, dict) and re.search(r"UPC|GTIN|EAN|Global Trade|model", str(v.get("type")), re.I)}
    available = record.get("is_available")
    card = {"product_id": PREFIX + asin, "external_product_id": asin,
            "canonical_product_id": PREFIX + asin, "source_platform": "Amazon",
            "title": title, "brand": _text(record.get("brand"), 100), "category": category,
            "origin_country": _text(record.get("country_of_origin"), 100),
            "price_major": price if price is not None else 0.0, "currency": "USD",
            "price_kind": "listing" if price is not None else "unknown",
            "price_text": f"US${price:.2f}" if price is not None else "报价待核实",
            "highlights": features, "description": _text(record.get("description")),
            "score": 1.0, "skus": skus, "stock_known": False,
            "detail_available": bool(details or variants), "updated_at": stamp,
            "seller_name": _text(record.get("seller_name") or record.get("buybox_seller"), 180),
            "source_region": "US", "delivery_zipcode": _text(str(record.get("zipcode") or ""), 20),
            "availability_text": "采集时页面标记可购买；实时库存未核实" if available is True else
                                 "采集时页面标记不可购买" if available is False else "可购买状态未提供",
            "price_conditions": list(dict.fromkeys(conditions)), "match_status": "unverified",
            "identifiers": identifiers, "rating_is_live": False,
            "landed_price": {"unavailable_reason": "仅有美国站商品报价；跨境配送、运费、税费及到手价待核实"}}
    if type(available) is bool:
        card["snapshot_available"] = available
    if price is not None and skus and skus[0]["variant_id"] == asin:
        card["default_sku_id"] = skus[0]["sku_id"]
    link = amazon_url(record.get("url"), asin)
    if link:
        card.update(source_url=link, source_url_status="observed", source_url_checked_at=stamp)
    image = record.get("image_url") or record.get("image")
    if isinstance(image, str) and image.startswith("https://m.media-amazon.com/"):
        card.update(image_url=image, image_kind="source", image_alt=title)
    rating, reviews = _amount(record.get("rating")), _amount(record.get("reviews_count"))
    if rating is not None and rating <= 5 and reviews is not None and reviews.is_integer():
        card["rating_summary"] = {"average": rating, "review_count": int(reviews)}
    return card


def import_snapshot(input_path: Path, output_path: Path, *, merge: bool = False, skip_invalid: bool = False) -> dict:
    records = json.loads(input_path.read_text(encoding="utf-8-sig"))
    if not isinstance(records, list) or not records:
        raise ValueError("输入必须为非空 Amazon JSON 数组")
    reused = 0
    invalid = 0
    if merge and output_path.is_file():
        with closing(sqlite3.connect(output_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            try:
                previous = [json.loads(row[0]) for row in db.execute("SELECT raw_json FROM amazon_products")]
            except sqlite3.OperationalError:
                previous = []
        reused = len(previous)
        records = previous + records
    normalized = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Amazon 记录必须为对象")
        try:
            card = normalize(record)
        except ValueError:
            if not skip_invalid:
                raise
            invalid += 1
            continue
        key = card["product_id"]
        previous = normalized.get(key)
        if previous and datetime.fromisoformat(previous[0]["updated_at"].replace("Z", "+00:00")) > datetime.fromisoformat(card["updated_at"].replace("Z", "+00:00")):
            continue
        normalized[key] = (card, record)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(output_path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS amazon_products (product_id TEXT PRIMARY KEY, card_json TEXT NOT NULL, raw_json TEXT NOT NULL)")
        db.execute("DELETE FROM amazon_products")
        db.executemany("INSERT INTO amazon_products VALUES (?,?,?)", [
            (key, json.dumps(card, ensure_ascii=False), json.dumps(raw, ensure_ascii=False))
            for key, (card, raw) in normalized.items()])
    return {"products": len(normalized), "priced": sum(c[0]["price_kind"] != "unknown" for c in normalized.values()),
            "duplicates": len(records) - len(normalized) - invalid, "reused_existing": reused,
            "skipped_invalid": invalid, "output": str(output_path.resolve())}


class AmazonCatalog:
    def __init__(self, path: Path, *, embedder: EmbeddingClient | None = None,
                 vector_index: ProductVectorIndex | None = None, reranker: Reranker | None = None,
                 hybrid_enabled: bool = False):
        self.path = path.resolve()
        if not self.path.is_file():
            raise ValueError("Amazon 快照不存在，请先运行 scripts/import_amazon_catalog.py")
        self.localization = AmazonLocalization(self.path.with_name("amazon_localization.sqlite3"))
        self.embedder = embedder
        self.vector_index = vector_index
        self.reranker = reranker
        self.hybrid_enabled = hybrid_enabled
        self.vector_available = False
        self._documents: list[AmazonSearchDocument] | None = None
        self._documents_by_id: dict[str, AmazonSearchDocument] = {}

    def _cards(self) -> list[dict]:
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT card_json FROM amazon_products ORDER BY product_id")]

    def _browse(self, query: str, category: str, page: int, page_size: int) -> dict:
        cards = self._cards()
        displayed = {c["product_id"]: c for c in self.localization.project(cards)}
        categories = sorted({c["category"] for c in cards})
        all_count = len(cards)
        exact = query.removeprefix(PREFIX).upper()
        direct = is_asin_query(exact) or query.startswith(PREFIX)
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
                body = terms(" ".join(card["highlights"]) + " " + card["category"])
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
        return {"source": "amazon", "total": len(cards), "all_count": all_count,
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
        parts = [card["title"], card["brand"], card["category"], *card["highlights"], card.get("description", "")[:600]]
        return " ".join(dict.fromkeys(part.strip() for part in parts if isinstance(part, str) and part.strip()))[:1200]

    def _load_documents(self) -> list[AmazonSearchDocument]:
        documents = [
            AmazonSearchDocument(product_id=card["product_id"], title=card["title"],
                                 category=card["category"], text=self._index_text(card))
            for card in self._cards()
        ]
        self._documents = documents
        self._documents_by_id = {item.product_id: item for item in documents}
        return documents

    async def list_all(self) -> list[AmazonSearchDocument]:
        """Stable search projection consumed by index_bootstrap."""
        if self._documents is None:
            return await asyncio.to_thread(self._load_documents)
        return self._documents

    def set_vector_available(self, available: bool) -> None:
        self.vector_available = bool(available)

    @staticmethod
    def _category_match(document: AmazonSearchDocument, category: str | None) -> bool:
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
        strategy = "amazon_qdrant_rrf" if english_query else "amazon_qdrant_dense"
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
                logger.warning("Amazon rerank 不可用，保留召回排序：%s", error)
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
        return {"source": "amazon", "hits": hits, "total_candidates": len(rescored),
                "recall_strategy": strategy, "retrieval_variant": "amazon_qdrant_dense_bm25_rrf_v1",
                "vector_available": True, "category_mode": "rerank" if rerank_applied else "soft_boost",
                "query_variants": {"dense": dense_texts, "bm25": english_query},
                "rerank_applied": rerank_applied, "data_scope": SCOPE, "filtered_out": []}

    async def _keyword_search(self, spec: ProductSearchSpec) -> dict:
        page = await self.browse(spec.normalized_query, spec.category or "", 1, 200)
        hits = page["products"]
        if spec.price_max_major is not None and spec.target_currency == "USD" and spec.budget_basis == "product":
            hits = [c for c in hits if c["price_kind"] == "unknown" or c["price_major"] <= spec.price_max_major]
        result = {"source": "amazon", "hits": [{**c, "skus": c["skus"][:8]} for c in hits[:spec.top_k]], "total_candidates": page["total"],
                "recall_strategy": "amazon_snapshot_keyword", "rerank_applied": False,
                "data_scope": SCOPE, "filtered_out": []}
        if is_asin_query(spec.normalized_query.removeprefix(PREFIX)) or spec.normalized_query.startswith(PREFIX):
            result.update(existence_checked=True, missing_identifiers=[] if page["total"] else [spec.normalized_query])
        return result

    async def execute(self, spec: ProductSearchSpec) -> dict:
        exact = spec.normalized_query.strip()
        direct = exact.startswith(PREFIX) or is_asin_query(exact.removeprefix(PREFIX))
        if (self.hybrid_enabled and self.vector_available and not direct
                and self.embedder is not None and self.vector_index is not None):
            try:
                return await self._hybrid_search(spec)
            except Exception as error:  # noqa: BLE001 - the local snapshot can still serve keyword recall.
                logger.warning("Amazon 向量检索不可用，回退本地关键词召回：%s", error)
                return {**await self._keyword_search(spec), "degraded_from": "amazon_qdrant_unavailable"}
        return await self._keyword_search(spec)
