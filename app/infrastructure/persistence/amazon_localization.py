"""Chinese presentation of Amazon snapshots; prices and identities stay unchanged."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

VERSION = "amazon-zh-v1"
CATEGORY_ZH = {"Pet Supplies": "宠物用品", "Home & Kitchen": "家居厨房",
               "Tools & Home Improvement": "工具与家装", "Electronics": "电子产品",
               "Industrial & Scientific": "工业与科研", "Arts, Crafts & Sewing": "艺术手工与缝纫",
               "Patio, Lawn & Garden": "庭院园艺", "Health & Household": "健康与家居",
               "Beauty & Personal Care": "美妆个护", "Baby": "母婴用品", "Baby Products": "母婴用品",
               "Office Products": "办公用品", "Clothing, Shoes & Jewelry": "服饰鞋包",
               "Grocery & Gourmet Food": "食品与杂货", "Toys & Games": "玩具与游戏",
               "Collectibles & Fine Art": "收藏品与艺术品", "Musical Instruments": "乐器",
               "Software": "软件", "Automotive": "汽车用品", "Video Games": "电子游戏",
               "Cell Phones & Accessories": "手机与配件", "Kindle Store": "Kindle 电子书",
               "Books": "图书", "Sports & Outdoors": "运动与户外", "Movies & TV": "影视",
               "CDs & Vinyl": "音乐唱片", "Appliances": "家电",
               "Small Appliance Parts & Accessories": "小家电配件", "Kitchen & Dining": "厨房与餐桌",
               "Shoe, Jewelry & Watch Accessories": "鞋履珠宝手表配件", "Pantry Staples": "食品储藏"}
LANGUAGE_FIELDS = ("title", "image_alt", "category", "description", "highlights", "price_conditions",
                   "source_title", "source_category", "source_description", "source_highlights", "source_price_conditions")


def fingerprint(card: dict) -> str:
    inputs = [VERSION, card["title"], card["brand"], card["category"],
              card.get("description", ""), card["highlights"]]
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()


def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS localized_products (
            product_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS localized_strings (
            kind TEXT NOT NULL, source TEXT NOT NULL, translated TEXT NOT NULL,
            PRIMARY KEY(kind, source)
        );
    """)


class AmazonLocalization:
    def __init__(self, path: Path):
        self.path = path.resolve()

    def project(self, cards: list[dict]) -> list[dict]:
        if not cards:
            return []
        products, strings = {}, {}
        if self.path.is_file():
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                products = {pid: (digest, json.loads(payload)) for pid, digest, payload in db.execute(
                    "SELECT product_id,fingerprint,payload FROM localized_products")}
                strings = {(kind, source): translated for kind, source, translated in db.execute(
                    "SELECT kind,source,translated FROM localized_strings")}
        result = []
        for source in cards:
            card = deepcopy(source)
            card["source_category"] = source["category"]
            card["category"] = CATEGORY_ZH.get(source["category"], source["category"])
            row = products.get(source["product_id"])
            if row and row[0] == fingerprint(source):
                localized = row[1]
                card.update(source_title=source["title"], source_description=source.get("description", ""),
                            source_highlights=source["highlights"], title=localized["title"],
                            image_alt=localized["title"], description=localized["description"],
                            highlights=localized["highlights"])
            for sku in card["skus"]:
                translated = strings.get(("spec", sku["spec"]))
                if translated:
                    sku["source_spec"], sku["spec"] = sku["spec"], translated
            card["source_price_conditions"] = source.get("price_conditions", [])
            card["price_conditions"] = [strings.get(("condition", text), text) for text in card["source_price_conditions"]]
            result.append(card)
        return result
