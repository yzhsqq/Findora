"""Rejected input must not replace the last usable snapshot."""
import json
import sqlite3

import pytest

from app.infrastructure.persistence.amazon_catalog import import_snapshot as amazon_import
from app.infrastructure.persistence.ebay_catalog import import_snapshot as ebay_import


@pytest.fixture(params=[
    (amazon_import, "amazon_products", {"asin": "B000MD58UM", "title": "Dog toy", "currency": "USD",
                                      "timestamp": "2026-10-06T00:00:00Z", "final_price": 5}),
    (ebay_import, "ebay_products", {"product_id": "123456789012", "title": "Dog toy", "currency": "USD",
                                  "price": "$5.00"}),
])
def platform(request):
    return request.param


def test_invalid_replacement_preserves_previous_rows(tmp_path, platform):
    importer, table, record = platform
    source, output = tmp_path / "batch.json", tmp_path / "catalog.sqlite3"
    source.write_text(json.dumps([record]), encoding="utf-8")
    importer(source, output)
    before = output.read_bytes()
    source.write_text(json.dumps([{**record, "title": ""}]), encoding="utf-8")
    with pytest.raises(ValueError, match="没有有效商品"):
        importer(source, output, skip_invalid=True)
    assert output.read_bytes() == before
    merged = importer(source, output, skip_invalid=True, merge=True)
    assert merged["products"] == 1 and merged["skipped_invalid"] == 1
    with sqlite3.connect(output) as db:
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


def test_empty_normalized_batch_does_not_create_database(tmp_path, platform):
    importer, _, record = platform
    source, output = tmp_path / "batch.json", tmp_path / "new" / "catalog.sqlite3"
    source.write_text(json.dumps([{**record, "title": ""}]), encoding="utf-8")
    with pytest.raises(ValueError, match="没有有效商品"):
        importer(source, output, skip_invalid=True)
    assert not output.exists()


def test_valid_rows_in_mixed_replacement_are_imported(tmp_path, platform):
    importer, _, record = platform
    source, output = tmp_path / "batch.json", tmp_path / "catalog.sqlite3"
    source.write_text(json.dumps([{**record, "title": ""}, record]), encoding="utf-8")
    result = importer(source, output, skip_invalid=True)
    assert result["products"] == 1 and result["skipped_invalid"] == 1
