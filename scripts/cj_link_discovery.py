"""Import CJ links observed in search results, matching both PID and SKU.

Search results are evidence, not a claim of current availability or checkout.
This command never fabricates URLs and never bypasses CJ verification pages.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.infrastructure.cj_product_links import valid_product_url
from scripts.enrich_cj_catalog import DEFAULT_PUBLISH, migrate, publish_snapshot
from scripts.sync_cj_catalog import open_db


def import_results(db: sqlite3.Connection, results: list[dict], ledger: Path) -> dict:
    """Validate the entire batch before writing. Rejected results stay unpublished."""
    observed_at = datetime.now(timezone.utc).isoformat()
    accepted, rejected, duplicates = [], [], []
    existing = json.loads(ledger.read_text(encoding="utf-8")) if ledger.is_file() else []
    by_pid = {item["pid"]: item for item in existing}
    selected: set[str] = set()
    for result in results:
        pid = str(result.get("pid") or "")
        url = valid_product_url(result.get("url"), pid)
        row = db.execute("SELECT list_json,detail_status,source_url_status FROM products WHERE pid=?", (pid,)).fetchone()
        reason = None
        if not url:
            reason = "invalid_product_url"
        elif row is None:
            reason = "product_not_in_catalog"
        elif str(row[1] or "").startswith("unavailable:"):
            reason = "api_unavailable"
        if reason:
            rejected.append({"pid": pid, "reason": reason})
            continue
        listed = json.loads(row[0])
        spu = listed.get("spu") or listed.get("sku") or listed.get("productSku")
        observed_spus = result.get("spus")
        if (not isinstance(observed_spus, list) or not spu or spu not in observed_spus
                or result.get("query_evidence") != "official_page_index"):
            rejected.append({"pid": pid, "reason": "missing_matching_index_sku"})
            continue
        if pid in selected or row[2] == "page_verified":
            duplicates.append(pid)
            continue
        selected.add(pid)
        seed = {"pid": pid, "spu": spu, "title": listed.get("nameEn") or "",
                "url": url, "evidence_kind": "official_page_index", "observed_at": observed_at,
                "crawl_recency": result.get("crawl_recency") or "not_reported",
                "search_query": f'site:cjdropshipping.com/product "{spu}"',
                "evidence_excerpt": f"SKU: {spu}"}
        accepted.append(seed)
        by_pid[pid] = seed
    # Save reusable evidence before DB changes, so restart can re-import safely.
    ledger.parent.mkdir(parents=True, exist_ok=True)
    temporary = ledger.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(list(by_pid.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(ledger)
    inserted = 0
    with db:
        for seed in accepted:
            previous = db.execute("SELECT source_url_status FROM products WHERE pid=?", (seed["pid"],)).fetchone()[0]
            if previous != "observed":
                inserted += 1
            db.execute("""UPDATE products SET source_url=?,source_url_status='observed',
                source_url_checked_at=?,source_url_evidence=? WHERE pid=?""",
                (seed["url"], seed["observed_at"], json.dumps(seed, ensure_ascii=False), seed["pid"]))
    return {"imported_at": observed_at, "new_links": inserted, "matched_products": len(accepted),
            "duplicates": len(duplicates), "rejected": rejected,
            "total_observed": db.execute("SELECT count(*) FROM products WHERE source_url_status IN ('observed','page_verified')").fetchone()[0]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data/cj_catalog.sqlite3")
    parser.add_argument("--publish", type=Path, default=DEFAULT_PUBLISH)
    parser.add_argument("--ledger", type=Path, default=ROOT / "scripts/cj_observed_product_links.json")
    parser.add_argument("--report", type=Path, default=ROOT / "data/cj_link_discovery/progress.json")
    args = parser.parse_args()
    if args.publish.resolve() == args.db.resolve():
        parser.error("publish path must differ from the writable database")
    results = json.loads(args.results.read_text(encoding="utf-8"))
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        parser.error("results must be a JSON array of indexed page records")
    with closing(open_db(args.db)) as db:
        migrate(db)
        report = import_results(db, results, args.ledger)
        report["published_snapshot"] = str(publish_snapshot(db, args.publish))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
