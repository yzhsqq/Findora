"""amazon localization parameters; shared projection preserves platform facts."""
from pathlib import Path
from app.infrastructure.persistence.snapshot_localization import (
    LANGUAGE_FIELDS, SnapshotLocalization, init_db, snapshot_fingerprint,
)

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


def fingerprint(card: dict) -> str:
    return snapshot_fingerprint(card, VERSION)


class AmazonLocalization(SnapshotLocalization):
    def __init__(self, path: Path):
        super().__init__(path, categories=CATEGORY_ZH, fingerprint=fingerprint)
