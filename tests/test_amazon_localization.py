"""Language projections must never change source identity, quotes or selections."""
import json
import sqlite3

import pytest

from app.infrastructure.persistence.amazon_catalog import AmazonCatalog, import_snapshot
from app.infrastructure.persistence.amazon_localization import AmazonLocalization, fingerprint, init_db
from scripts.localize_amazon_catalog import validate_string, validate_text


@pytest.fixture
def localized(tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps([{
        "asin": "B000MD58UM", "title": "KONG Wubba XL Dog Toy Pack of 1", "brand": "KONG",
        "currency": "USD", "final_price": 17.95, "timestamp": "2026-10-06T08:54:40Z",
        "categories": ["Pet Supplies"], "description": "Reinforced nylon tug and fetch toy.",
        "features": ["Long tails for tugging"], "is_available": True, "zipcode": "11001",
        "coupon_description": "Get 4 for the price of 3. Enter code EA55D022.",
        "variations": [{"asin": "B000MD58UM", "name": "XL Pack of 1", "price": 17.95},
                       {"asin": "B000MD57ZI", "name": "Large Pack of 1", "price": 12.99}],
    }]), encoding="utf-8")
    path = tmp_path / "amazon_catalog.sqlite3"
    import_snapshot(source, path)
    catalog = AmazonCatalog(path)
    raw = catalog._cards()[0]
    translated = {"title": "KONG Wubba 加大号狗狗玩具 1件装", "description": "尼龙拉扯与抛接玩具。",
                  "highlights": ["长尾设计，适合拉扯互动"]}
    with sqlite3.connect(tmp_path / "amazon_localization.sqlite3") as db:
        init_db(db)
        db.execute("INSERT INTO localized_products VALUES (?,?,?)", (
            raw["product_id"], fingerprint(raw), json.dumps(translated, ensure_ascii=False)))
        db.executemany("INSERT INTO localized_strings VALUES (?,?,?)", [
            ("spec", "XL Pack of 1", "加大号 1件装"), ("spec", "Large Pack of 1", "大号 1件装"),
            ("condition", raw["price_conditions"][0], "优惠券条件待核实：4件按3件价格购买，输入优惠码 EA55D022。")])
    return catalog, raw


@pytest.mark.asyncio
async def test_chinese_browse_english_recall_and_exact_identity_keep_all_quote_facts(localized):
    catalog, raw = localized
    original = catalog.path.read_bytes()
    for query in ("", "长尾", "long tails", "KONG", "B000MD58UM", "amazon:us:B000MD58UM"):
        # English title/feature search remains on the original snapshot.
        page = await catalog.browse(query, "Pet Supplies")
        assert page["total"] == 1
        card = page["products"][0]
        assert card["title"] == "KONG Wubba 加大号狗狗玩具 1件装"
        assert card["category"] == "宠物用品" and card["source_category"] == "Pet Supplies"
        assert card["source_title"] == raw["title"]
        assert card["source_description"] == raw["description"]
        assert card["skus"][0]["spec"] == "加大号 1件装" and card["skus"][0]["source_spec"] == "XL Pack of 1"
        assert "EA55D022" in card["price_conditions"][0]
        assert card["source_price_conditions"] == raw["price_conditions"]
        for field in ("product_id", "external_product_id", "canonical_product_id", "default_sku_id", "price_major", "currency",
                      "price_kind", "price_text", "updated_at", "delivery_zipcode", "stock_known", "snapshot_available", "landed_price"):
            assert card[field] == raw[field]
        for actual, expected in zip(card["skus"], raw["skus"]):
            assert {k: v for k, v in actual.items() if k not in {"spec", "source_spec"}} == {k: v for k, v in expected.items() if k != "spec"}
    assert catalog.path.read_bytes() == original


@pytest.mark.asyncio
async def test_saved_selected_variant_and_quote_survive_chinese_refresh(localized):
    catalog, raw = localized
    saved = {**raw, "price_major": 12.99, "price_text": "US$12.99", "default_sku_id": raw["skus"][1]["sku_id"],
             "quote_sku_id": raw["skus"][1]["sku_id"]}
    result = (await catalog.localize_saved_cards([saved]))[0]
    assert result["title"] != raw["title"] and result["skus"][1]["spec"] == "大号 1件装"
    assert result["default_sku_id"] == saved["default_sku_id"]
    assert result["quote_sku_id"] == saved["quote_sku_id"]
    assert result["price_major"] == 12.99 and result["price_text"] == "US$12.99"
    assert saved["title"] == raw["title"] and saved["skus"][0]["spec"] == "XL Pack of 1"


def test_source_text_change_invalidates_title_but_price_timestamp_changes_do_not(localized):
    catalog, raw = localized
    source_changed = {**raw, "title": "KONG Completely Different Product"}
    assert catalog.localization.project([source_changed])[0]["title"] == source_changed["title"]
    price_changed = {**raw, "price_major": 99, "updated_at": "2026-10-07T00:00:00Z"}
    projected = catalog.localization.project([price_changed])[0]
    assert "狗狗" in projected["title"] and projected["price_major"] == 99
    assert projected["updated_at"] == price_changed["updated_at"]


def test_absent_localization_keeps_original_title_with_known_category_translation(tmp_path):
    card = {"product_id": "amazon:us:B000MD58UM", "title": "Original", "brand": "KONG", "category": "Pet Supplies",
            "highlights": [], "skus": [], "price_major": 17.95, "currency": "USD"}
    result = AmazonLocalization(tmp_path / "absent.sqlite3").project([card])[0]
    assert result["title"] == "Original" and result["category"] == "宠物用品"
    assert result["price_major"] == 17.95 and "source_title" not in result


@pytest.mark.parametrize("translated", ["2件装", "规格 1件装 20cm", "Pack of 1"])
def test_translation_rejects_changed_numbers_invented_dimensions_or_missing_chinese(translated):
    with pytest.raises(ValueError):
        validate_text("Pack of 1", translated)


def test_coupon_code_cannot_be_changed_or_omitted():
    source = "Get 4 for the price of 3. Code EA55D022"
    with pytest.raises(ValueError):
        validate_text(source, "4件按3件价格购买，优惠码 EA55D023")
    assert validate_text(source, "4件按3件价格购买，优惠码 EA55D022")


def test_chinese_prefix_does_not_hide_untranslated_coupon_prose():
    source = "优惠券条件待核实：Prime Savings Save 10% on 4 select item(s)"
    with pytest.raises(ValueError):
        validate_string("condition", source, source)
    assert validate_string("condition", source, "优惠券条件待核实：Prime 优惠，购买4件指定商品可省10%")


def test_quantity_discount_cannot_reverse_purchase_and_payment_counts():
    source = "Get 4 for the price of 3. Enter code EA55D022."
    with pytest.raises(ValueError):
        validate_string("condition", source, "买3件享4件价格，优惠码 EA55D022")
    assert validate_string("condition", source, "4件按3件价格购买，优惠码 EA55D022")
