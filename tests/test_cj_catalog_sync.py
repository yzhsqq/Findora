from scripts.sync_cj_catalog import (
    CJClient,
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


def test_implausible_cj_points_counter_is_rechecked_before_pausing(tmp_path):
    db = open_db(tmp_path / "cj.sqlite3")

    class Response:
        status_code = 200

        def __init__(self, used):
            self.used = used

        def json(self):
            return {"code": 200, "result": True, "data": {},
                    "pointsInfo": {"usedToday": self.used, "remaining": 50000}}

    class Http:
        def __init__(self):
            self.calls = []

        def get(self, endpoint, **kwargs):
            self.calls.append(endpoint)
            return Response(60370 if len(self.calls) == 1 else 650)

    client = CJClient.__new__(CJClient)
    client.db = db
    client.http = Http()
    client.token = "test"
    client.max_points = 10000
    client.used_today = 640
    client._pace = lambda: None

    client.get("/product/stock/getInventoryByPid", params={"pid": "p1"}, cost=10)
    assert client.used_today == 650
    assert client.http.calls == ["/product/stock/getInventoryByPid", "/product/getCategory"]
    db.close()
