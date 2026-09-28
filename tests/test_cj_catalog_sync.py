from scripts.sync_cj_catalog import (
    flatten_list_page,
    interleave_categories,
    list_count,
    open_db,
    store_list_page,
)


def test_category_round_robin_and_list_page_flatten():
    groups = [
        {"categoryFirstName": "Pet Supplies", "categoryFirstList": [
            {"categorySecondName": "Toys", "categorySecondList": [
                {"categoryId": "pet-1", "categoryName": "Cat Toys"},
                {"categoryId": "pet-2", "categoryName": "Dog Toys"},
            ]},
        ]},
        {"categoryFirstName": "Sports & Outdoors", "categoryFirstList": [
            {"categorySecondName": "Camping", "categorySecondList": [
                {"categoryId": "sport-1", "categoryName": "Camping Gear"},
            ]},
        ]},
    ]
    assert [row[0] for row in interleave_categories(groups)] == ["sport-1", "pet-1", "pet-2"]
    data = {"content": [
        {"productList": [{"id": "a"}, {"id": "b"}]},
        {"productList": [{"id": "c"}]},
    ]}
    assert [row["id"] for row in flatten_list_page(data)] == ["a", "b", "c"]


def test_page_store_is_idempotent_and_preserves_detail(tmp_path):
    db = open_db(tmp_path / "cj.sqlite3")
    category = ("cat", "Pet Supplies", "Pet Toys", "Cat Toys")
    store_list_page(db, category, 1, [{"id": "p1", "sellPrice": "1.00"}])
    db.execute("UPDATE products SET detail_json='{}',detail_status='ok' WHERE pid='p1'")
    db.commit()
    store_list_page(db, category, 1, [{"id": "p1", "sellPrice": "2.00"}])
    assert list_count(db) == 1
    assert db.execute("SELECT detail_json FROM products WHERE pid='p1'").fetchone()[0] == "{}"
    assert db.execute("SELECT product_count FROM list_pages WHERE category_id='cat'").fetchone()[0] == 1
    db.close()
