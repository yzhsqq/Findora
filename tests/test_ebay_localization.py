"""eBay Chinese projection may restate text but never identity, quotes or stock."""
import json
import sqlite3

import pytest

from app.infrastructure.persistence.ebay_catalog import import_snapshot
from app.infrastructure.persistence.ebay_localization import EbayLocalization, fingerprint, init_db
from scripts.localize_ebay_catalog import validate_string, validate_text


def record(**overrides):
    return {"product_id": "960005457882", "title": "Dog Toy Holo Card Lot 10x", "brand": "Pokemon",
            "price": "$4.99", "currency": "USD", "condition": "Ungraded - Near mint or better",
            "root_category": "Toys & Hobbies", "availability": "in_stock",
            "url": "https://www.ebay.com/itm/960005457882",
            "product_specifications": [{"specification_name": "Game", "specification_value": "Pokémon TCG"}],
            "variants": [{"variant_type": "Card", "variant_options": [
                {"option_id": "459441505389", "option_name": "STR-01 Yor Forger", "option_price": 4.99}]}],
            **overrides}


@pytest.fixture
def snapshot(tmp_path):
    source, path = tmp_path / "ebay.json", tmp_path / "ebay.sqlite3"
    source.write_text(json.dumps([record()]))
    import_snapshot(source, path)
    return path


def test_absent_localization_keeps_english_title_and_translates_category(snapshot):
    card = EbayLocalization(snapshot.with_name("missing.sqlite3")).project(
        [json.loads(row[0]) for row in sqlite3.connect(snapshot).execute("SELECT card_json FROM ebay_products")])[0]
    assert card["title"] == "Dog Toy Holo Card Lot 10x"
    assert card["category"] == "玩具与爱好" and card["source_category"] == "Toys & Hobbies"
    assert card["price_text"] == "US$4.99" and card["condition"] == "Ungraded - Near mint or better"


def test_stored_localization_replaces_text_and_keeps_price_and_identity(snapshot):
    localization = EbayLocalization(snapshot.with_name("ebay_localization.sqlite3"))
    cards = [json.loads(row[0]) for row in sqlite3.connect(snapshot).execute("SELECT card_json FROM ebay_products")]
    with sqlite3.connect(localization.path) as db:
        init_db(db)
        db.execute("INSERT INTO localized_products VALUES (?,?,?)", (
            cards[0]["product_id"], fingerprint(cards[0]),
            json.dumps({"title": "狗狗玩具 全息卡牌 10x", "description": "卡牌套装",
                        "highlights": ["全息稀有卡"]}, ensure_ascii=False)))
        db.execute("INSERT INTO localized_strings VALUES (?,?,?)", ("spec", "Card: STR-01 Yor Forger", "卡牌：STR-01 约尔"))
        db.execute("INSERT INTO localized_strings VALUES (?,?,?)", (
            "condition", "运费、卖家优惠资格及结算价格需在 eBay 确认", "运费、卖家优惠资格及结算价格需在 eBay 确认"))
    card = localization.project(cards)[0]
    assert card["title"] == "狗狗玩具 全息卡牌 10x" and card["source_title"] == cards[0]["title"]
    assert card["skus"][-1]["spec"] == "卡牌：STR-01 约尔" and card["skus"][-1]["source_spec"] == "Card: STR-01 Yor Forger"
    assert card["price_major"] == 4.99 and card["product_id"] == "ebay:us:960005457882"


def test_source_text_change_invalidates_stored_title(snapshot):
    localization = EbayLocalization(snapshot.with_name("ebay_localization.sqlite3"))
    old = record()
    cards = [json.loads(row[0]) for row in sqlite3.connect(snapshot).execute("SELECT card_json FROM ebay_products")]
    with sqlite3.connect(localization.path) as db:
        init_db(db)
        db.execute("INSERT INTO localized_products VALUES (?,?,?)", (
            cards[0]["product_id"], fingerprint({**cards[0], "title": "Old Title"}),
            json.dumps({"title": "旧标题", "description": "", "highlights": []}, ensure_ascii=False)))
    card = localization.project(cards)[0]
    assert card["title"] == old["title"]


def test_condition_translation_may_keep_ebay_but_not_other_english():
    assert validate_string("condition", "Free shipping on 2 items", "2件包邮，以商品页为准") == "2件包邮，以商品页为准"
    assert validate_string("condition", "结算价格需在 eBay 确认", "结算价格需在 eBay 确认") == "结算价格需在 eBay 确认"
    with pytest.raises(ValueError):
        validate_string("condition", "结算价格需在 eBay 确认", "结算价格需在 Store 确认")
    with pytest.raises(ValueError):
        validate_text("10x Holo Lot", "全息卡组")
