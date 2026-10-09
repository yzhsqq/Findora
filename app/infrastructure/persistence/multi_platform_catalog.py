"""Federated snapshots. Rank positions are comparable; provider scores are not."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import math
import re

from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.application.ports.catalog import CatalogCapabilities
from app.infrastructure.context import ShoppingContext
from app.infrastructure.persistence.amazon_catalog import PREFIX as AMAZON_PREFIX, SCOPE as AMAZON_SCOPE, is_asin_query
from app.infrastructure.persistence.cj_catalog import CJCatalog
from app.infrastructure.persistence.ebay_catalog import PREFIX as EBAY_PREFIX, SCOPE as EBAY_SCOPE, is_item_id_query

logger = logging.getLogger(__name__)

# Source name -> (display label, id prefix, scope sentence, id matcher).
LABELS = {"cj": "CJ", "amazon": "Amazon", "ebay": "eBay"}
PREFIXES = {"cj": "", "amazon": AMAZON_PREFIX, "ebay": EBAY_PREFIX}
SCOPES = {"amazon": AMAZON_SCOPE, "ebay": EBAY_SCOPE}
CJ_ID = re.compile(r"(?:[0-9]{16,24}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|CJ[A-Z0-9_-]{6,96})", re.I)


class MultiPlatformCatalog:
    """Federates CJ with any number of marketplace snapshots.

    Each snapshot keeps its own Qdrant collection and its own id prefix, so a
    query is answered by every available source and merged by rank.
    """

    def __init__(self, cj: CJCatalog, amazon=None, reranker=None, ebay=None):
        self.cj, self.amazon, self.ebay, self.reranker = cj, amazon, ebay, reranker
        self._sources = [("cj", cj)] + [(name, catalog) for name, catalog in
                                        (("amazon", amazon), ("ebay", ebay)) if catalog is not None]

    def extra_sources(self) -> list[tuple[str, object]]:
        """Non-CJ snapshots, each owning its own vector collection."""
        return [(name, catalog) for name, catalog in self._sources if name != "cj"]

    @property
    def capabilities(self) -> CatalogCapabilities:
        return CatalogCapabilities(source="multi", platforms=tuple(name for name, _ in self._sources),
                                   local_orders=False, purchase_records=True)

    async def list_all(self):
        # CJ stays the bootstrap entry point for its own collection; every other
        # snapshot is bootstrapped through extra_sources().
        return await self.cj.list_all()

    def set_vector_available(self, available: bool):
        for _, catalog in self._sources:
            catalog.set_vector_available(available)

    async def browse(self, query="", category="", page=1, page_size=24, platform=""):
        allowed = {"", *(name for name, _ in self._sources)}
        if platform not in allowed:
            raise ValueError("平台仅支持 " + "、".join(LABELS[name] for name in allowed if name))
        sources = self._sources if not platform else [(n, c) for n, c in self._sources if n == platform]
        gathered = await asyncio.gather(*(catalog.browse(query, category, 1, page * page_size) for _, catalog in sources),
                                        return_exceptions=True)
        failures = tuple(name for (name, _), result in zip(sources, gathered) if isinstance(result, Exception))
        healthy = [(name, result) for (name, _), result in zip(sources, gathered) if not isinstance(result, Exception)]
        if not healthy:
            raise ValueError("商品目录暂不可用，请稍后重试")
        results = [result for _, result in healthy]
        products = []
        for rank in range(max((len(r["products"]) for r in results), default=0)):
            products.extend(r["products"][rank] for r in results if rank < len(r["products"]))
        return {"source": "multi", "platform": platform,
                **{key: sum(r[key] for r in results) for key in ("total", "all_count", "detail_count", "inventory_count")},
                "source_counts": {name: r["all_count"] for name, r in healthy},
                "source_status": {name: "unavailable" if name in failures else "ok" for name, _ in sources},
                "partial_results": bool(failures),
                "categories": sorted({c for r in results for c in r["categories"]}),
                "page": page, "page_size": page_size,
                "products": [{**c, "match_status": "unverified"} for c in products[(page - 1) * page_size:page * page_size]],
                "data_scope": self._scope(failures)}

    def _route(self, ids: list[str]) -> dict[str, list[str]]:
        """Split ids by platform prefix; CJ takes whatever carries no prefix."""
        routed: dict[str, list[str]] = {name: [] for name, _ in self._sources}
        for value in ids:
            for name, _ in self._sources:
                prefix = PREFIXES[name]
                if prefix and value.startswith(prefix):
                    routed[name].append(value)
                    break
            else:
                routed.setdefault("cj", []).append(value)
        return routed

    async def cards_by_ids(self, ids):
        routed = self._route(ids)
        grouped = {name: ids_for for name, ids_for in routed.items() if ids_for}
        results = await asyncio.gather(*(dict(self._sources)[name].cards_by_ids(grouped[name]) for name in grouped),
                                       return_exceptions=True)
        healthy = [cards for cards in results if not isinstance(cards, Exception)]
        for name, result in zip(grouped, results):
            if isinstance(result, Exception):
                logger.warning("%s 商品身份读取暂不可用", name)
        if results and not healthy:
            raise ValueError("商品身份读取暂不可用，请稍后重试")
        return [card for cards in healthy for card in cards]

    async def localize_saved_cards(self, cards):
        by_platform = {"CJdropshipping": ("cj", self.cj), "Amazon": ("amazon", self.amazon), "eBay": ("ebay", self.ebay)}
        tasks, order = [], []
        for name, catalog in self._sources:
            if catalog is None:
                continue
            batch = [c for c in cards if by_platform.get(c.get("source_platform"), ("", None))[0] == name]
            order.append(name)
            tasks.append(catalog.localize_saved_cards(batch))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(order, results):
            if isinstance(result, Exception):
                logger.warning("%s 收藏中文投影暂不可用，保留原收藏", name)
        localized = {c["product_id"]: c for batch in results if not isinstance(batch, Exception) for c in batch}
        return [localized.get(c["product_id"], c) for c in cards]

    def _scope(self, failures: tuple[str, ...]) -> str:
        extras = [name for name, _ in self.extra_sources()]
        head = "CJ 与 " + "、".join(LABELS[name] for name in extras) if extras else "CJ"
        return (head + " 同类候选联合搜索，未确认同款。") + " ".join(SCOPES[name] for name in extras) + (
            " 当前无法检索的平台：" + ", ".join(failures) if failures else "")

    async def execute(self, spec: ProductSearchSpec) -> dict:
        exact = spec.normalized_query.strip()
        cj_id = CJ_ID.fullmatch(exact)
        amazon_id = self.amazon is not None and is_asin_query(exact.removeprefix(AMAZON_PREFIX)) and not cj_id
        ebay_id = self.ebay is not None and is_item_id_query(exact.removeprefix(EBAY_PREFIX)) and not cj_id
        opaque = re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{11,}", exact) and re.search(r"\d", exact)
        direct = None
        if exact.startswith(AMAZON_PREFIX):
            if self.amazon is None:
                raise ValueError("Amazon 快照未启用")
            direct = self.amazon
        elif exact.startswith(EBAY_PREFIX):
            if self.ebay is None:
                raise ValueError("eBay 快照未启用")
            direct = self.ebay
        elif amazon_id and ebay_id:
            # Ten-digit numeric ids match both schemas. Check actual snapshot
            # identity rather than silently treating them as an ASIN.
            found = await asyncio.gather(
                self.amazon.cards_by_ids([AMAZON_PREFIX + exact]),
                self.ebay.cards_by_ids([EBAY_PREFIX + exact]),
                return_exceptions=True,
            )
            if any(isinstance(result, Exception) for result in found):
                raise ValueError("编号所属平台暂无法核验，请使用 amazon:us: 或 ebay:us: 前缀")
            if all(found):
                raise ValueError("编号同时存在于 Amazon 和 eBay，请添加平台前缀")
            if not any(found):
                return {"source": "multi", "hits": [], "total_candidates": 0,
                        "recall_strategy": "exact_id_lookup", "rerank_applied": False,
                        "filtered_out": [], "existence_checked": True,
                        "missing_identifiers": [exact], "partial_results": False,
                        "source_status": {"amazon": "ok", "ebay": "ok"}, "data_scope": self._scope(())}
            direct = self.amazon if found[0] else self.ebay
        elif amazon_id:
            direct = self.amazon
        elif ebay_id:
            direct = self.ebay
        elif cj_id:
            direct = self.cj
        elif opaque:
            direct = self.amazon or self.ebay or self.cj
        if direct is not None:
            result = await direct.execute(replace(spec, normalized_query=exact))
            return {**result, "source": "multi", "rerank_applied": False}
        expanded = replace(spec, top_k=50, price_max_major=None)
        names = [name for name, _ in self._sources]
        results = await asyncio.gather(*(getattr(catalog, "recall", catalog.execute)(expanded)
                                         for _, catalog in self._sources), return_exceptions=True)
        failures = tuple(name for name, r in zip(names, results) if isinstance(r, Exception))
        if len(failures) == len(names):
            raise ValueError("、".join(LABELS[name] for name in names) + " 商品检索均暂不可用")
        candidates = []
        for result in results:
            if isinstance(result, Exception):
                continue
            for rank, card in enumerate(result["hits"], 1):
                candidates.append({**card, "score": 1.0 / (60 + rank), "match_status": "unverified"})
        candidates.sort(key=lambda c: (-c["score"], c["product_id"]))
        strategy, applied = "multi_snapshot_rank_fusion", False
        if self.reranker and candidates:
            context = ShoppingContext.current()
            query = spec.raw_query.strip() or (context.raw_query.strip() if context else "") or spec.normalized_query
            texts = [" | ".join((c["title"], c["brand"], c["category"], " ".join(c["highlights"])))[:1400] for c in candidates]
            try:
                scores = await asyncio.wait_for(self.reranker.rerank(query, texts), timeout=3.0)
                if len(scores) != len(candidates) or not all(not isinstance(s, bool) and math.isfinite(float(s)) for s in scores):
                    raise ValueError("精排分数无效")
                candidates = [{**card, "score": float(score)} for card, score in zip(candidates, scores)]
                candidates.sort(key=lambda c: (-c["score"], c["product_id"]))
                strategy, applied = "multi_snapshot_rerank", True
            except Exception:
                logger.warning("多平台精排暂不可用，保留各平台召回顺序合并")
        total = len(candidates)
        rejected = [entry for result in results if not isinstance(result, Exception)
                    for entry in result.get("filtered_out", [])]
        available = []
        for card in candidates:
            if card.get("source_platform") in {"Amazon", "eBay"} and card.get("snapshot_available") is False:
                rejected.append({"product_id": card["product_id"], "title": card["title"],
                                 "reason": card["source_platform"] + " 采集时页面标记不可购买"})
            else:
                available.append(card)
        candidates = available
        if spec.price_max_major is not None and spec.target_currency == "USD" and spec.budget_basis == "product":
            candidates = [c for c in candidates if c.get("price_kind") == "unknown" or c["price_major"] <= spec.price_max_major]
        # A comparison needs representatives from each recalled source. Retain
        # their relevance scores/order; never substitute the cheapest item.
        selected = candidates[:spec.top_k]
        if spec.top_k >= 2:
            representatives = {}
            for card in candidates:
                representatives.setdefault(card["source_platform"], card["product_id"])
            # When K is smaller than the platform count, the best-ranked
            # platform representatives get the slots. Never exceed K.
            required = set(list(representatives.values())[:spec.top_k])
            # Reserve one slot per available platform, then fill by global rank.
            remainder = [c["product_id"] for c in candidates if c["product_id"] not in required]
            chosen = required | set(remainder[:max(0, spec.top_k - len(required))])
            selected = [c for c in candidates if c["product_id"] in chosen]
        return {"source": "multi", "hits": selected, "total_candidates": total,
                "recall_strategy": strategy, "rerank_applied": applied, "filtered_out": rejected,
                "selection_policy": "platform_coverage",
                "source_candidate_counts": {name: len(r["hits"]) if not isinstance(r, Exception) else 0
                                            for name, r in zip(names, results)},
                "source_status": {name: "unavailable" if name in failures else "ok" for name in names},
                "partial_results": bool(failures), "data_scope": self._scope(failures)}
