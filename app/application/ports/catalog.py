"""Catalog contracts describe business capabilities without adapter imports."""
from dataclasses import dataclass
from typing import Literal, NotRequired, Protocol, TypedDict

from app.domain.catalog.product_search_spec import ProductSearchSpec


class SearchResult(TypedDict):
    hits: list[dict]
    total_candidates: int
    recall_strategy: str
    rerank_applied: bool
    source: NotRequired[str]
    filtered_out: NotRequired[list[dict]]
    data_scope: NotRequired[str]
    partial_results: NotRequired[bool]
    source_status: NotRequired[dict[str, str]]


class CatalogSearch(Protocol):
    async def execute(self, spec: ProductSearchSpec) -> SearchResult: ...


@dataclass(frozen=True)
class CatalogCapabilities:
    source: Literal["fixture", "cj", "multi"] = "fixture"
    platforms: tuple[str, ...] = ()
    local_orders: bool = True
    purchase_records: bool = False


def catalog_capabilities(catalog: object) -> CatalogCapabilities:
    return getattr(catalog, "capabilities", CatalogCapabilities())


class SnapshotCatalog(CatalogSearch, Protocol):
    @property
    def capabilities(self) -> CatalogCapabilities: ...

    async def browse(self, query: str = "", category: str = "", page: int = 1,
                     page_size: int = 24, platform: str = "") -> dict: ...

    async def cards_by_ids(self, ids: list[str]) -> list[dict]: ...

    async def localize_saved_cards(self, cards: list[dict]) -> list[dict]: ...
