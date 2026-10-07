"""Federated snapshots. Rank positions are comparable; provider scores are not."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import math
import re

from app.domain.catalog.product_search_spec import ProductSearchSpec
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
        self.cj.reranker = None  # One rerank after all sources have recalled candidates.
        self._sources = [("cj", cj)] + [(name, catalog) for name, catalog in
                                        (("amazon", amazon), ("ebay", ebay)) if catalog is not None]

    def extra_sources(self) -> list[tuple[str, object]]:
        """Non-CJ snapshots, each owning its own vector collection."""
        return [(name, catalog) for name, catalog in self._sources if name != "cj"]

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
        results = await asyncio.gather(*(catalog.browse(query, category, 1, page * page_size) for _, catalog in sources))
        products = []
        for rank in range(max((len(r["products"]) for r in results), default=0)):
            products.extend(r["products"][rank] for r in results if rank < len(r["products"]))
        return {"source": "multi", "platform": platform,
                **{key: sum(r[key] for r in results) for key in ("total", "all_count", "detail_count", "inventory_count")},
                "source_counts": {name: r["all_count"] for (name, _), r in zip(sources, results)},
                "categories": sorted({c for r in results for c in r["categories"]}),
                "page": page, "page_size": page_size,
                "products": [{**c, "match_status": "unverified"} for c in products[(page - 1) * page_size:page * page_size]],
                "data_scope": self._scope(())}

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
        results = await asyncio.gather(*(dict(self._sources)[name].cards_by_ids(grouped[name]) for name in grouped))
        return [card for cards in results for card in cards]

    async def localize_saved_cards(self, cards):
        by_platform = {"CJdropshipping": ("cj", self.cj), "Amazon": ("amazon", self.amazon), "eBay": ("ebay", self.ebay)}
        tasks, order = [], []
        for name, catalog in self._sources:
            if catalog is None:
                continue
            batch = [c for c in cards if by_platform.get(c.get("source_platform"), ("", None))[0] == name]
            order.append(name)
            tasks.append(catalog.localize_saved_cards(batch))
        results = await asyncio.gather(*tasks)
        localized = {c["product_id"]: c for batch in results for c in batch}
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
        if amazon_id or exact.startswith(AMAZON_PREFIX):
            direct = self.amazon
        elif ebay_id or exact.startswith(EBAY_PREFIX):
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
        results = await asyncio.gather(*(catalog.execute(expanded) for _, catalog in self._sources), return_exceptions=True)
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
        if spec.price_max_major is not None and spec.target_currency == "USD" and spec.budget_basis == "product":
            candidates = [c for c in candidates if c.get("price_kind") == "unknown" or c["price_major"] <= spec.price_max_major]
        # A comparison needs representatives from each recalled source. Retain
        # their relevance scores/order; never substitute the cheapest item.
        selected = candidates[:spec.top_k]
        if spec.top_k >= 2:
            representatives = {}
            for card in candidates:
                representatives.setdefault(card["source_platform"], card["product_id"])
            required = set(representatives.values())
            # Reserve one slot per available platform, then fill by global rank.
            remainder = [c["product_id"] for c in candidates if c["product_id"] not in required]
            chosen = required | set(remainder[:max(0, spec.top_k - len(required))])
            selected = [c for c in candidates if c["product_id"] in chosen]
        return {"source": "multi", "hits": selected, "total_candidates": total,
                "recall_strategy": strategy, "rerank_applied": applied, "filtered_out": [],
                "selection_policy": "platform_coverage",
                "source_candidate_counts": {name: len(r["hits"]) if not isinstance(r, Exception) else 0
                                            for name, r in zip(names, results)},
                "source_status": {name: "unavailable" if name in failures else "ok" for name in names},
                "partial_results": bool(failures), "data_scope": self._scope(failures)}
