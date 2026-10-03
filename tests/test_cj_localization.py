import json
import sqlite3

import pytest

from app.infrastructure.persistence.cj_catalog import CJCatalog
from app.infrastructure.persistence.cj_localization import (
    CJLocalization, LOCALIZATION_VERSION, init_localization_db,
    source_fingerprint, upsert_localization,
)
from scripts.sync_cj_catalog import open_db, store_list_page


@pytest.mark.asyncio
async def test_localized_short_chinese_search_and_exact_id_keep_source_intact(tmp_path):
    source_path = tmp_path / "cj_catalog.sqlite3"
    with open_db(source_path) as source:
        store_list_page(source, ("phone", "Phones & Accessories", "Cases", "Phone Cases"), 1, [
            {"id": "2502090644421612600", "nameEn": "Transparent Phone Case", "sku": "CJPHONECASE01", "sellPrice": "3.50"},
            {"id": "2502090644421612601", "nameEn": "Wireless Phone Charger", "sellPrice": "8.50"},
        ])
        source.row_factory = sqlite3.Row
        rows = source.execute("SELECT * FROM products ORDER BY pid").fetchall()
        original = {row["pid"]: row["list_json"] for row in rows}
    localized_path = tmp_path / "cj_localization.sqlite3"
    with sqlite3.connect(localized_path) as db:
        init_localization_db(db)
        for row, title, keywords in zip(rows, ["透明手机壳", "无线手机充电器"], [["手机壳", "保护壳"], ["手机充电器"]]):
            upsert_localization(db, {
                "pid": row["pid"], "source_fingerprint": source_fingerprint(row),
                "version": LOCALIZATION_VERSION, "first_category": row["first_category"],
                "title_zh": title, "summary_zh": "", "keywords_zh": json.dumps(keywords, ensure_ascii=False),
                "first_category_zh": "手机配件", "second_category_zh": "保护配件",
                "third_category_zh": "手机壳", "search_text": " ".join([title, *keywords, "手机配件"]),
            })
    catalog = CJCatalog(source_path, localization=CJLocalization(localized_path))
    page = await catalog.browse("手机壳")
    assert [card["product_id"] for card in page["products"]] == [rows[0]["pid"]]
    assert page["products"][0]["title"] == "透明手机壳"
    exact = await catalog.browse(rows[0]["pid"])
    assert exact["products"][0]["title"] == "透明手机壳"
    assert (await catalog.browse("CJPHONECASE01"))["products"][0]["product_id"] == rows[0]["pid"]
    saved = (await CJCatalog(source_path).browse(rows[0]["pid"]))["products"]
    assert (await catalog.localize_saved_cards(saved))[0]["title"] == "透明手机壳"
    with sqlite3.connect(source_path) as source:
        assert dict(source.execute("SELECT pid,list_json FROM products")) == original
