"""Chinese presentation of eBay snapshots; prices and identities stay unchanged."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

VERSION = "ebay-zh-v1"
CATEGORY_ZH = {"Toys & Hobbies": "玩具与爱好", "Collectibles & Art": "收藏品与艺术",
               "Electronics": "电子产品", "Home & Garden": "家居园艺",
               "Health & Beauty": "健康与美容", "Fashion": "服饰",
               "Pet Supplies": "宠物用品", "Cell Phones & Accessories": "手机与配件",
               "Computers/Tablets & Networking": "电脑与网络", "Cameras & Photo": "相机摄影",
               "Sporting Goods": "运动用品", "Musical Instruments & Gear": "乐器",
               "Business & Industrial": "工商用品", "Video Games & Consoles": "游戏与主机",
               "Baby": "母婴用品", "Jewelry & Watches": "珠宝手表", "Crafts": "手工艺",
               "Books": "图书", "Movies & TV": "影视", "Music": "音乐",
               "Motors": "汽车摩托", "Art": "艺术品", "Antiques": "古董",
               "Coins & Paper Money": "钱币", "Stamps": "邮票",
               "Sports Memorabilia": "体育纪念品", "Dolls & Bears": "娃娃与毛绒",
               "Pottery & Glass": "陶瓷玻璃", "Travel": "旅行",
               "Everything Else": "其他", "Home Audio": "家用音响", "未分类": "未分类"}
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


class EbayLocalization:
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
