"""Identity, quota, restart and challenge checks for catalog enrichment."""
import json
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest

from app.infrastructure.cj_product_links import product_link_fields, valid_product_url
from app.infrastructure.cj_live_quote import CJLiveQuoteService
from app.infrastructure.cj_catalog_snapshot import resolve_catalog_snapshot
from scripts.enrich_cj_catalog import API, migrate, page_status, publish_snapshot, seed_links, store_detail
from scripts.sync_cj_catalog import open_db, store_list_page
from scripts.cj_link_discovery import import_results

PID = "05B050F6-9DF5-4488-9218-B1D919650ADE"
URL = f"https://cjdropshipping.com/product/green-sandalwood-hair-comb-p-{PID}.html"


def catalog(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    db = open_db(path)
    store_list_page(db, ("c", "Beauty", "Hair", "Combs"), 1,
                    [{"id": PID, "spu": "CJBJJFTF00034", "nameEn": "Comb", "sellPrice": "3.00"}])
    migrate(db)
    db.row_factory = sqlite3.Row
    return path, db


def test_restart_preserves_data_and_candidate_url_is_not_published(tmp_path):
    _, db = catalog(tmp_path)
    db.execute("UPDATE products SET source_url=?,source_url_status='derived'", (URL,))
    db.commit()
    store_detail(db, PID, {"result": True, "data": {"pid": PID.lower(), "variants": []}})
    migrate(db)
    row = db.execute("SELECT * FROM products").fetchone()
    assert row["detail_status"] == "ok"
    assert "source_url" not in product_link_fields(row)
    before = row["detail_json"]
    with pytest.raises(RuntimeError, match="identity mismatch"):
        store_detail(db, PID, {"result": True, "data": {"pid": "999999"}})
    assert db.execute("SELECT detail_json FROM products").fetchone()[0] == before
    db.close()


def test_observed_page_is_bound_to_listing_spu(tmp_path):
    _, db = catalog(tmp_path)
    seed = {"pid": PID, "url": URL, "spu": "CJBJJFTF00034", "observed_at": "2026-10-03"}
    assert seed_links(db, [seed]) == 1
    assert product_link_fields(db.execute("SELECT * FROM products").fetchone())["source_url"] == URL
    with pytest.raises(ValueError, match="SPU mismatch"):
        seed_links(db, [{**seed, "spu": "OTHER"}])
    db.close()


@pytest.mark.parametrize("url", [URL.replace("https:", "http:"), URL.replace("cjdropshipping.com", "cjdropshipping.com.evil.test"),
                                   URL + "?redirect=evil", URL.replace(PID, "999999"),
                                   URL.replace("https://", "https://user:pass@")])
def test_rejects_wrong_identity_and_unsafe_url(url):
    assert valid_product_url(url, PID) is None


def test_http_200_verification_page_does_not_verify_url():
    response = httpx.Response(200, text="Human verification", request=httpx.Request("GET", "https://frontend.cjdropshipping.com/egg/cj/validation.html"))
    assert page_status(response, PID, "CJBJJFTF00034") == "verification_required"
    page = httpx.Response(200, text='<h1>Comb</h1><p>SKU: CJBJJFTF00034</p><p>Product Description</p>', request=httpx.Request("GET", URL))
    assert page_status(page, PID, "CJBJJFTF00034") == "page_verified"
    script_only = httpx.Response(200, text='<script>"CJBJJFTF00034"</script>Product Description', request=httpx.Request("GET", URL))
    assert page_status(script_only, PID, "CJBJJFTF00034") == "unconfirmed"


def test_ceiling_accounts_for_timeouts_and_low_minute_balance(tmp_path):
    _, db = catalog(tmp_path)
    api = API.__new__(API)
    api.db, api.ceiling = db, 45000
    api.point_day = datetime.now(timezone.utc).date().isoformat()
    api.points = {"usedToday": 0, "remaining": 9, "total": 50000}
    assert api.wait_seconds() == 65
    api.points["remaining"] = 50000
    db.execute("INSERT INTO cj_enrichment_calls(at,endpoint,points_reserved) VALUES(?,?,?)", (datetime.now(timezone.utc).isoformat(), "/product/query", 45000))
    assert api.wait_seconds() > 0
    db.close()


@pytest.mark.asyncio
async def test_live_details_use_new_enrichment_and_link_without_paid_api(tmp_path):
    path, db = catalog(tmp_path)
    service = CJLiveQuoteService.from_snapshot(path, tmp_path / "live.sqlite3")
    store_detail(db, PID, {"result": True, "data": {"pid": PID, "description": "<p>Real description</p>", "variants": []}})
    seed_links(db, [{"pid": PID, "url": URL, "spu": "CJBJJFTF00034", "observed_at": "2026-10-03"}])
    card = await service.detail(PID)
    assert card["source_url"] == URL
    assert card["source_description"] == "Real description。"
    assert card["detail_available"] is True
    db.close()


def test_published_snapshot_includes_wal_and_can_be_atomically_updated(tmp_path):
    from contextlib import closing
    _, db = catalog(tmp_path)
    published = tmp_path / "published/catalog.sqlite3"
    first = publish_snapshot(db, published)
    store_detail(db, PID, {"result": True, "data": {"pid": PID, "description": "New details"}})
    second = publish_snapshot(db, published)
    assert first != second
    assert resolve_catalog_snapshot(published) == second
    with closing(sqlite3.connect(f"file:{second.as_posix()}?mode=ro", uri=True)) as snapshot:
        assert snapshot.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert json.loads(snapshot.execute("SELECT detail_json FROM products").fetchone()[0])["description"] == "New details"
    assert not second.with_name(second.name + "-wal").exists()
    db.close()


def test_index_link_import_requires_catalog_pid_and_source_sku_and_is_idempotent(tmp_path):
    _, db = catalog(tmp_path)
    result = {"pid": PID, "url": URL, "spus": ["CJBJJFTF00034"], "query_evidence": "official_page_index"}
    ledger = tmp_path / "observed.json"
    report = import_results(db, [result, {**result, "spus": ["WRONG"]},
                                 {**result, "url": URL.replace("cjdropshipping.com", "sp-test.cjdropshipping.com")}], ledger)
    assert report["new_links"] == 1
    assert {r["reason"] for r in report["rejected"]} == {"missing_matching_index_sku", "invalid_product_url"}
    assert import_results(db, [result], ledger)["new_links"] == 0
    assert len(json.loads(ledger.read_text(encoding="utf-8"))) == 1
    row = db.execute("SELECT * FROM products").fetchone()
    assert product_link_fields(row)["source_url"] == URL
    assert row["source_url_status"] == "observed"
    assert row["source_url_page_status"] is None
    db.close()
