"""Mutation checks for the offline V2 decision-report gate."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.application.usecases.shopping_decision import build_decision_report
from scripts.eval.decision_quality import DEFAULT_CASES, DEFAULT_CATALOG, evaluate_case, evaluate_reports, load_jsonl, main


@pytest.fixture
def sample():
    cases = load_jsonl(DEFAULT_CASES)
    catalog = {row["product_id"]: row for row in load_jsonl(DEFAULT_CATALOG)}
    reports = {}
    for case in cases:
        hits = []
        for product_id in case["source_hit_ids"]:
            card = deepcopy(catalog[product_id])
            sku = next(s for s in card["skus"] if s["stock"] > 0)
            card.update({"default_sku_id": sku["sku_id"], "price_major": sku["price_major"], "currency": sku["currency"]})
            if product_id in case.get("quote_by_product", {}):
                card["landed_price"] = case["quote_by_product"][product_id]
            hits.append(card)
        source = {"query_conditions": case["request"], "hits": hits,
                  "result_ref": case["result_ref"], "observed_at": case["observed_at"]}
        reports[case["id"]] = build_decision_report(source, budget_basis=case["request"]["budget_basis"])
    return cases, reports, catalog


def test_frozen_scenarios_pass_with_pure_decision_builder(sample):
    cases, reports, catalog = sample
    result = evaluate_reports(cases, [{"case_id": case["id"], "report": reports[case["id"]]} for case in cases], catalog)
    assert result["summary"] == {"total": 5, "passed": 5, "failed": 0, "findings_by_code": {}}


@pytest.mark.parametrize(
    ("case_id", "mutation", "expected"),
    [
        ("DWB-001", "price_leak", "hard_constraint"),
        ("DWB-001", "reorder", "candidate_order"),
        ("DWB-003", "missing_quote", "budget_basis"),
        ("DWB-003", "altered_quote", "quote_mismatch"),
        ("DWB-002", "bad_ref", "evidence"),
        ("DWB-001", "missing_stock_check", "evidence_coverage"),
        ("DWB-001", "live_claim", "unsupported_live_claim"),
        ("DWB-005", "six_candidates", "candidate_cap"),
    ],
)
def test_injected_decision_defects_are_reported(sample, case_id, mutation, expected):
    cases, reports, catalog = sample
    case = next(case for case in cases if case["id"] == case_id)
    report = deepcopy(reports[case_id])
    if mutation == "price_leak":
        report["candidates"][0]["product"]["price_major"] = 999
    elif mutation == "reorder":
        report["candidates"][:2] = reversed(report["candidates"][:2])
    elif mutation == "missing_quote":
        del report["candidates"][0]["product"]["landed_price"]
    elif mutation == "altered_quote":
        report["candidates"][0]["product"]["landed_price"]["landed_total_major"] = 1
    elif mutation == "bad_ref":
        report["candidates"][0]["checks"][0]["evidence"]["ref"] = "ctx_fabricated"
    elif mutation == "missing_stock_check":
        report["candidates"][0]["checks"] = [check for check in report["candidates"][0]["checks"] if check["field"] != "skus.stock"]
    elif mutation == "live_claim":
        report["candidates"][0]["reasons"].append("官方政策保证到手价")
    elif mutation == "six_candidates":
        report["candidates"].append(deepcopy(report["candidates"][0]))
    findings = evaluate_case(case, report, catalog)
    assert expected in {finding["code"] for finding in findings}


def test_cli_returns_failure_code_and_aggregate_findings(sample, tmp_path, capsys):
    cases, reports, _ = sample
    reports["DWB-001"]["candidates"][0]["reasons"].append("实时库存已确认")
    path = tmp_path / "reports.jsonl"
    path.write_text("\n".join(json.dumps({"case_id": case["id"], "report": reports[case["id"]]}, ensure_ascii=False)
                              for case in cases) + "\n", encoding="utf-8")
    assert main(["--reports", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["failed"] == 1
    assert result["summary"]["findings_by_code"]["unsupported_live_claim"] == 1
