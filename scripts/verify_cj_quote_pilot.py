"""Small, point-capped live acceptance of list-only CJ products to CN.

This deliberately selects a SKU for each test product. It does not claim that
the selected SKU is the customer's choice or that a trial quote is final price.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.cj_live_quote import CJLiveQuoteService, CJQuoteError  # noqa: E402

DB_PATH = ROOT / "data" / "cj_pilot" / "cj_catalog.sqlite3"


def spent_today(db: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return int(db.execute(
        "SELECT COALESCE(SUM(points),0) FROM cj_pilot_calls WHERE substr(called_at,1,10)=?",
        (today,),
    ).fetchone()[0])


def candidates(db: sqlite3.Connection, sample_size: int) -> list[tuple[str, str, str]]:
    """Take recent products whose listings report inventory, including factory stock."""
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for pid, category, raw in db.execute(
        """SELECT pid,first_category,list_json FROM products
           WHERE detail_json IS NULL AND detail_status IS NULL
           ORDER BY list_fetched_at DESC,pid"""
    ):
        listed = json.loads(raw)
        if int(listed.get("warehouseInventoryNum") or 0) <= 0:
            continue
        groups.setdefault(category, []).append((pid, category, str(listed.get("nameEn") or "")))
    selected = []
    for index in range(max(map(len, groups.values()), default=0)):
        for category in sorted(groups):
            if index < len(groups[category]):
                selected.append(groups[category][index])
                if len(selected) == sample_size:
                    return selected
    return selected


async def run(sample_size: int, point_budget: int) -> dict:
    with sqlite3.connect(DB_PATH) as db:
        before = spent_today(db)
        chosen = candidates(db, sample_size)
    if before + point_budget > 1000:
        raise SystemExit(f"Pilot cap 1000 exceeded: already {before}, requested {point_budget}")
    service = CJLiveQuoteService(DB_PATH, daily_point_limit=before + point_budget)
    rows = []
    for pid, category, title in chosen:
        with sqlite3.connect(DB_PATH) as db:
            if spent_today(db) - before + 30 > point_budget:
                break
        item = {"product_id": pid, "category": category, "title": title}
        try:
            card = await service.detail(pid)
            skus = card.get("skus") or []
            item["sku_count"] = len(skus)
            if not skus:
                item["status"] = "no_quotable_sku"
            else:
                sku_id = str(skus[0]["sku_id"])
                item["selected_test_sku"] = sku_id
                quote = await service.quote(pid, sku_id, "CN", 1)
                item.update(status="quoted", amount_usd=quote["cj_trial_total_usd"],
                            quote_origin_country=quote["quote_origin_country"],
                            route=quote["shipping_method"], fee_status=quote["fee_status"])
        except CJQuoteError as error:
            item.update(status="unavailable", reason=str(error))
            if "上限" in str(error) or "429" in str(error) or "授权" in str(error):
                rows.append(item)
                print(f"{len(rows)}/{sample_size} {pid} {item['status']}: {item['reason']}", flush=True)
                break
        rows.append(item)
        print(f"{len(rows)}/{sample_size} {pid} {item['status']}: {item.get('reason', item.get('amount_usd', ''))}", flush=True)
    with sqlite3.connect(DB_PATH) as db:
        after = spent_today(db)
        counts = db.execute(
            "SELECT COUNT(*),SUM(detail_json IS NOT NULL),SUM(inventory_json IS NOT NULL) FROM products"
        ).fetchone()
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "sample_target": sample_size,
            "tested": len(rows), "pilot_points_before": before, "pilot_points_after": after,
            "new_points": after - before, "catalog_counts": {"list": counts[0], "detail": counts[1], "inventory": counts[2]},
            "results": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--point-budget", type=int, default=650)
    args = parser.parse_args()
    if not 1 <= args.sample_size <= 20 or not 30 <= args.point_budget <= 650:
        parser.error("sample size must be 1..20 and new point budget 30..650")
    report = asyncio.run(run(args.sample_size, args.point_budget))
    path = ROOT / "data" / "cj_pilot" / "quote_acceptance.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"summary: tested={report['tested']} points={report['new_points']} "
          f"detail={report['catalog_counts']['detail']} stock={report['catalog_counts']['inventory']} "
          f"report={path}", flush=True)


if __name__ == "__main__":
    main()
