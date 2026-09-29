"""Replay already cached CJ trial quotes without a live CJ API call.

This is a cache-path contract check, not an online delivery or final-price eval.
Historical quote timestamps are deliberately treated as fresh for replay only;
the CJ client is replaced with a function that always raises.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.infrastructure.cj_live_quote import CJLiveQuoteService
from scripts.eval.run_cj_baseline import ROOT, SNAPSHOT, SNAPSHOT_MANIFEST, OUTPUT, sha256
from scripts.eval.run_manifest import select_cases, worktree_fingerprint


CASES = ROOT / "eval/cj_v1/quote_cases.jsonl"


async def replay(snapshot: Path, cases: list[dict]) -> list[dict]:
    service = CJLiveQuoteService(snapshot)
    observations = []
    with patch("app.infrastructure.cj_live_quote._fresh", return_value=True), patch.object(
        service, "_client", side_effect=AssertionError("live CJ call forbidden during quote replay")
    ):
        for case in cases:
            actual = await service.quote(case["product_id"], case["sku_id"], case["ship_to"], case["quantity"])
            subtotal = Decimal(str(actual["product_subtotal_usd"]))
            fees = Decimal(str(actual["shipping_and_cj_fees_usd"]))
            total = Decimal(str(actual["cj_trial_total_usd"]))
            expected = Decimal(case["expected_total_usd"])
            checks = {
                "cache_only": actual.get("cache_hit") is True,
                "quoted_status": actual.get("status") == "quoted",
                "sku_and_destination": actual.get("sku_id") == case["sku_id"] and actual.get("ship_to") == case["ship_to"],
                "total_matches_frozen_evidence": total == expected,
                "amount_adds_up": subtotal + fees == total,
                "route_scope": actual.get("route_scope") == case["expected_route_scope"],
                "inventory_kind": actual.get("origin_inventory_kind") == case["expected_origin_inventory_kind"],
                "legacy_warehouse_label_removed": "ship_from_warehouse" not in actual,
            }
            observations.append({
                "id": case["id"], "product_id": case["product_id"], "sku_id": case["sku_id"],
                "ship_to": case["ship_to"], "quote_origin_country": actual.get("quote_origin_country"),
                "route_scope": actual.get("route_scope"), "origin_inventory_kind": actual.get("origin_inventory_kind"),
                "origin_inventory_verified": actual.get("origin_inventory_verified"),
                "actual_total_usd": str(total), "expected_total_usd": str(expected),
                "checks": checks, "pass": all(checks.values()),
            })
    return observations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("all", "dev", "release"), default="all")
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    parser.add_argument("--snapshot-manifest", type=Path, default=SNAPSHOT_MANIFEST)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    expected = json.loads(args.snapshot_manifest.read_text(encoding="utf-8"))
    before = sha256(args.snapshot)
    if before != expected["sha256"]:
        raise SystemExit("冻结 CJ 快照 SHA-256 不匹配")
    source = worktree_fingerprint(ROOT)["sha256"]
    all_cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(all_cases) != 8 or len({case.get("id") for case in all_cases}) != 8:
        raise SystemExit("CJ quote v1 requires eight unique replay cases")
    cases, selection = select_cases(all_cases, args.split)
    observations = asyncio.run(replay(args.snapshot, cases))
    if sha256(args.snapshot) != before or worktree_fingerprint(ROOT)["sha256"] != source:
        raise SystemExit("运行期间代码或 CJ 快照变化；拒绝写入可比结果")
    result = {
        "run_at": datetime.now(timezone.utc).isoformat(), "split": args.split,
        "selection": selection, "snapshot_sha256": before, "cases_sha256": sha256(args.cases),
        "code_sha256": source, "runtime": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
        "count": len(observations),
        "passed": sum(item["pass"] for item in observations),
        "route_scopes": dict(Counter(item["route_scope"] for item in observations)),
        "origin_inventory_kinds": dict(Counter(item["origin_inventory_kind"] for item in observations)),
        "cost_scope": {"cj_points": 0, "llm_tokens": 0, "scope": "cached_quote_replay_only"},
        "limitations": [
            "Cached responses do not prove current price, stock, or route availability.",
            "A CJ trial total does not establish final landed or checkout price.",
            "No-route and live API failure paths are not covered by these eight successful historical quotes.",
        ],
        "observations": observations,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"quote-replay-{args.split}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(path), "passed": result["passed"], "count": result["count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
