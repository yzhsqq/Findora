"""ebay localization parameters; shared projection preserves platform facts."""
from pathlib import Path
from app.infrastructure.persistence.snapshot_localization import (
    LANGUAGE_FIELDS, SnapshotLocalization, init_db, snapshot_fingerprint,
)

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


def fingerprint(card: dict) -> str:
    return snapshot_fingerprint(card, VERSION)


class EbayLocalization(SnapshotLocalization):
    def __init__(self, path: Path):
        super().__init__(path, categories=CATEGORY_ZH, fingerprint=fingerprint)
