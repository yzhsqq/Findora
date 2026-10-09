"""Cross-platform contracts use isolated providers and the actual UI projection."""
from ag_ui.core import RunAgentInput, UserMessage
import pytest

from app.application.agents.ag_ui_adapter import AGUIRunAdapter
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.usecases.shopping_decision import build_decision_report
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEvent
from app.infrastructure.persistence.multi_platform_catalog import MultiPlatformCatalog


def card(pid, platform, **extra):
    return dict(product_id=pid, title="Dog toy", brand="", category="Pet Supplies",
                source_platform=platform, price_major=4.0, currency="USD", price_kind="listing",
                price_text="US$4.00", highlights=[], skus=[], score=1, **extra)


class Source:
    def __init__(self, cards=(), *, broken=False):
        self.cards, self.broken = list(cards), broken

    async def execute(self, spec):
        if self.broken:
            raise ValueError("isolated source failure")
        return dict(hits=self.cards[:spec.top_k], total_candidates=len(self.cards),
                    recall_strategy="snapshot_keyword", rerank_applied=False, filtered_out=[])

    async def browse(self, query="", category="", page=1, page_size=24):
        if self.broken:
            raise ValueError("isolated source failure")
        return dict(products=self.cards[:page_size], total=len(self.cards), all_count=len(self.cards),
                    detail_count=0, inventory_count=0, categories=["Pet Supplies"])

    async def cards_by_ids(self, ids):
        if self.broken:
            raise ValueError("isolated source failure")
        return [c for c in self.cards if c["product_id"] in ids]

    async def localize_saved_cards(self, cards):
        if self.broken:
            raise ValueError("isolated source failure")
        return [{**c, "title": "Localized toy"} for c in cards]


async def test_partial_search_survives_tool_adapter_and_persistent_restore(tmp_path):
    catalog = MultiPlatformCatalog(Source([card("CJTEST001", "CJdropshipping")]), Source(broken=True))
    request = RunAgentInput(thread_id="session", run_id="run", state={}, tools=[], context=[],
                            forwarded_props={"buyerId": "buyer"}, messages=[UserMessage(id="u1", content="dog toy")])
    adapter = AGUIRunAdapter(request, lambda event: None)
    class Bus:
        def publish(self, session, kind, payload):
            adapter.on_trade_event(TradeEvent(session, kind, payload, "2026-10-08"))
    token = ShoppingContext.set(ShoppingContextSnapshot("session", "buyer", "zh-CN", "CNY"))
    try:
        await build_product_search_tool(catalog, Bus())("dog toy")
    finally:
        ShoppingContext.reset(token)
    report = adapter.state["decisionReport"]
    assert report["catalog_source"] == "multi"
    assert report["partial_results"] and report["source_status"]["amazon"] == "unavailable"
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(request.model_dump(by_alias=True), "buyer", "owner")
    await journal.append("run", "owner", [{"type": "STATE_SNAPSHOT", "snapshot": adapter.state}])
    restored = await AGUIJournal(tmp_path / "runs.db").session("session", "buyer")
    assert restored["state"]["decisionReport"] == report


@pytest.mark.parametrize("top_k", [1, 2, 3, 5])
async def test_platform_coverage_never_exceeds_requested_slots(top_k):
    cj = Source([card(f"CJTEST00{i}", "CJdropshipping") for i in range(3)])
    amazon = Source([card(f"amazon:us:B00000000{i}", "Amazon") for i in range(3)])
    ebay = Source([card(f"ebay:us:1234567890{i}", "eBay") for i in range(3)])
    result = await MultiPlatformCatalog(cj, amazon, ebay=ebay).execute(ProductSearchSpec("dog toy", top_k=top_k))
    assert len(result["hits"]) == top_k
    assert len({c["product_id"] for c in result["hits"]}) == top_k
    assert len({c["source_platform"] for c in result["hits"]}) == min(top_k, 3)
    assert [c["score"] for c in result["hits"]] == sorted([c["score"] for c in result["hits"]], reverse=True)


async def test_known_unavailable_candidates_do_not_hide_later_usable_candidate():
    cards = [card(f"amazon:us:B00000000{i}", "Amazon", snapshot_available=False) for i in range(5)]
    cards += [card("amazon:us:B000000009", "Amazon", snapshot_available=True)]
    result = await MultiPlatformCatalog(Source(), Source(cards)).execute(ProductSearchSpec("dog toy", top_k=5))
    report = build_decision_report(result)
    assert report["status"] == "ready"
    assert [c["product_id"] for c in result["hits"]] == ["amazon:us:B000000009"]
    assert len(report["excluded"]) == 5 and result["total_candidates"] == 6


async def test_unknown_availability_is_not_filtered_out():
    unknown = card("ebay:us:123456789012", "eBay")
    result = await MultiPlatformCatalog(Source(), ebay=Source([unknown])).execute(ProductSearchSpec("dog toy"))
    assert result["hits"] and result["filtered_out"] == []


@pytest.mark.parametrize("platform", ["amazon", "ebay"])
async def test_ambiguous_numeric_id_routes_by_snapshot_existence(platform):
    pid = f"{platform}:us:1234567890"
    amazon = Source([card(pid, "Amazon")]) if platform == "amazon" else Source()
    ebay = Source([card(pid, "eBay")]) if platform == "ebay" else Source()
    result = await MultiPlatformCatalog(Source(), amazon, ebay=ebay).execute(ProductSearchSpec("1234567890"))
    assert result["hits"][0]["product_id"] == pid


async def test_numeric_id_present_in_two_platforms_requires_prefix():
    amazon = Source([card("amazon:us:1234567890", "Amazon")])
    ebay = Source([card("ebay:us:1234567890", "eBay")])
    catalog = MultiPlatformCatalog(Source(), amazon, ebay=ebay)
    with pytest.raises(ValueError, match="平台前缀"):
        await catalog.execute(ProductSearchSpec("1234567890"))
    for platform in ("amazon", "ebay"):
        result = await catalog.execute(ProductSearchSpec(f"{platform}:us:1234567890"))
        assert result["hits"][0]["product_id"].startswith(platform + ":")


async def test_unknown_numeric_id_remains_exact_no_match():
    catalog = MultiPlatformCatalog(Source(), Source(), ebay=Source())
    result = await catalog.execute(ProductSearchSpec("1234567890"))
    assert result["hits"] == [] and result["missing_identifiers"] == ["1234567890"]


async def test_failed_identity_check_is_not_reported_as_missing():
    catalog = MultiPlatformCatalog(Source(), Source(broken=True), ebay=Source())
    with pytest.raises(ValueError, match="暂无法核验"):
        await catalog.execute(ProductSearchSpec("1234567890"))


async def test_disabled_platform_prefix_does_not_search_another_catalog():
    with pytest.raises(ValueError, match="未启用"):
        await MultiPlatformCatalog(Source()).execute(ProductSearchSpec("ebay:us:1234567890"))


async def test_browse_isolates_failure_and_marks_partial_totals():
    catalog = MultiPlatformCatalog(Source([card("CJTEST001", "CJdropshipping")]), Source(broken=True),
                                   ebay=Source([card("ebay:us:123456789012", "eBay")]))
    result = await catalog.browse(page_size=1)
    assert result["source_status"] == {"cj": "ok", "amazon": "unavailable", "ebay": "ok"}
    assert result["partial_results"] and result["total"] == 2
    assert result["source_counts"] == {"cj": 1, "ebay": 1}
    assert len(result["products"]) == 1
    assert "amazon" in result["data_scope"]
    with pytest.raises(ValueError, match="暂不可用"):
        await catalog.browse(platform="amazon")


async def test_all_browse_failures_are_an_error_not_empty_inventory():
    with pytest.raises(ValueError, match="暂不可用"):
        await MultiPlatformCatalog(Source(broken=True), Source(broken=True)).browse()


async def test_card_lookup_isolates_one_platform_but_reports_total_outage():
    cj_card = card("CJTEST001", "CJdropshipping")
    catalog = MultiPlatformCatalog(Source([cj_card]), Source(broken=True))
    assert await catalog.cards_by_ids(["CJTEST001", "amazon:us:B000MD58UM"]) == [cj_card]
    with pytest.raises(ValueError, match="暂不可用"):
        await catalog.cards_by_ids(["amazon:us:B000MD58UM"])


async def test_saved_card_localization_keeps_failed_platform_originals():
    original = [card("amazon:us:B000MD58UM", "Amazon"), card("CJTEST001", "CJdropshipping")]
    catalog = MultiPlatformCatalog(Source(), Source(broken=True))
    localized = await catalog.localize_saved_cards(original)
    assert localized[0] == original[0] and localized[1]["title"] == "Localized toy"
    assert original[1]["title"] == "Dog toy"
