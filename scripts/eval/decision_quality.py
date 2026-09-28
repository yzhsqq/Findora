# -*- coding: utf-8 -*-
"""Offline contract checks for version 2 shopping decision reports.

The cases are deliberately small, versioned examples. This runner checks a
generated report against the frozen catalog and its source-hit order; it does
not retrieve products, call a model, or estimate recommendation quality.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "eval" / "v2" / "decision_cases.jsonl"
DEFAULT_CATALOG = ROOT / "data" / "catalog-v1.jsonl"
_LIVE_CLAIM = re.compile(
    r"(?<!非)(?<!非 )实时(?:价格|库存|报价|运费|关税|汇率|评分|政策|数据)"
    r"|官方(?:政策|关税|运费|报价|保证)"
    r"|(?:保证|承诺)(?:到手价|价格|时效|包税)"
    r"|real[ -]time (?:price|stock|quote|shipping)|official (?:tariff|policy)",
    re.IGNORECASE,
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: expected JSON object")
            rows.append(value)
    return rows


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _texts(report: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for candidate in report.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for key in ("reasons", "tradeoffs", "unknowns"):
            values.extend(value for value in candidate.get(key) or [] if isinstance(value, str))
        values.extend(
            check.get("detail") for check in candidate.get("checks") or []
            if isinstance(check, dict) and isinstance(check.get("detail"), str)
        )
    values.extend(
        item.get("reason") for item in report.get("excluded") or []
        if isinstance(item, dict) and isinstance(item.get("reason"), str)
    )
    return values


def _has_evidence_field(fields: set[str], name: str) -> bool:
    if name == "stock":
        return any(re.search(r"(?:^|[.\[\]_])stock(?:$|[.\]])", field) for field in fields)
    if name == "landed_total_major":
        return any("landed_total_major" in field for field in fields)
    return any(field == name or field.endswith(f".{name}") for field in fields)


def _evidence_field_exists(field: str, product: dict[str, Any], sku: dict[str, Any] | None) -> bool:
    field = field.removeprefix("product.")
    if field in {"skus.stock", "sku.stock", "stock"}:
        return sku is not None and "stock" in sku
    if field in {"skus.price_major", "sku.price_major"}:
        return sku is not None and "price_major" in sku
    if field.startswith("landed_price."):
        return isinstance(product.get("landed_price"), dict) and field.split(".", 1)[1] in product["landed_price"]
    return field in product


def evaluate_case(case: dict[str, Any], report: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Return actionable findings; an empty list is a passing case."""
    findings: list[dict[str, str]] = []

    def issue(code: str, message: str) -> None:
        findings.append({"code": code, "message": message})

    if report.get("version") != 2:
        issue("version", "report.version must be 2")
    expected_request = case["request"]
    request = report.get("request")
    if not isinstance(request, dict):
        request = {}
        issue("request", "report.request must be an object")
    for key, expected in expected_request.items():
        if request.get(key) != expected:
            issue("request", f"request.{key}: expected {expected!r}, got {request.get(key)!r}")
    if report.get("status") != case["expected_status"]:
        issue("status", f"expected {case['expected_status']!r}, got {report.get('status')!r}")

    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
        issue("shape", "report.candidates must be a list")
    if len(candidates) > 5:
        issue("candidate_cap", f"{len(candidates)} candidates exceeds the maximum of 5")
    candidate_ids = [
        candidate.get("product", {}).get("product_id")
        if isinstance(candidate, dict) and isinstance(candidate.get("product"), dict) else None
        for candidate in candidates
    ]
    if candidate_ids != case["expected_candidate_ids"]:
        issue("candidate_order", f"expected {case['expected_candidate_ids']!r}, got {candidate_ids!r}")
    if len(candidate_ids) != len(set(candidate_ids)):
        issue("duplicate_candidate", "a product appears more than once")

    excluded = report.get("excluded")
    if not isinstance(excluded, list):
        excluded = []
        issue("shape", "report.excluded must be a list")
    excluded_ids = [entry.get("product_id") if isinstance(entry, dict) else None for entry in excluded]
    if excluded_ids != case["expected_excluded_ids"]:
        issue("excluded", f"expected exclusions {case['expected_excluded_ids']!r}, got {excluded_ids!r}")
    if set(candidate_ids) & set(excluded_ids):
        issue("excluded", "the same product is both selected and excluded")

    source_ids = case["source_hit_ids"]
    refs = report.get("evidence_refs")
    if not isinstance(refs, list):
        refs = []
        issue("evidence_refs", "report.evidence_refs must be a list")
    if candidates and case["result_ref"] not in refs:
        issue("evidence_refs", f"missing source result ref {case['result_ref']!r}")

    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("product"), dict):
            issue("shape", f"candidate[{index}] must contain a product object")
            continue
        product = candidate["product"]
        product_id = product.get("product_id")
        prefix = f"candidate[{index}] {product_id}"
        if product_id not in source_ids:
            issue("source_hit", f"{prefix} was not in the source hits")
        original = catalog.get(product_id)
        if original is None:
            issue("catalog", f"{prefix} is absent from frozen catalog")
            continue
        for field in ("title", "category", "ships_to", "material_tags", "updated_at"):
            if product.get(field) != original.get(field):
                issue("snapshot_mismatch", f"{prefix}.{field} differs from catalog-v1")

        if expected_request.get("category") and original["category"] != expected_request["category"]:
            issue("hard_constraint", f"{prefix} violates category")
        if expected_request.get("ship_to") and expected_request["ship_to"] not in original["ships_to"]:
            issue("hard_constraint", f"{prefix} cannot ship to {expected_request['ship_to']}")
        tags = set(original["material_tags"])
        if tags & set(expected_request.get("excluded_material_tags") or []):
            issue("hard_constraint", f"{prefix} contains an excluded material")
        if set(expected_request.get("required_material_tags") or []) - tags:
            issue("hard_constraint", f"{prefix} lacks a required material")

        sku_id = candidate.get("sku_id")
        sku = next((item for item in original["skus"] if item["sku_id"] == sku_id), None)
        if sku is None:
            issue("sku", f"{prefix} has no matching catalog SKU {sku_id!r}")
        elif sku["stock"] <= 0:
            issue("hard_constraint", f"{prefix} selects an out-of-stock SKU")
        else:
            displayed_sku = next((item for item in product.get("skus", []) if isinstance(item, dict) and item.get("sku_id") == sku_id), None)
            if displayed_sku is None or any(displayed_sku.get(key) != sku.get(key) for key in ("stock", "price_major", "currency")):
                issue("snapshot_mismatch", f"{prefix} displayed SKU differs from catalog-v1")
            if sku["currency"] == expected_request["target_currency"]:
                price = _number(product.get("price_major"))
                if price is None or product.get("currency") != sku["currency"] or abs(price - sku["price_major"]) > 0.001:
                    issue("snapshot_mismatch", f"{prefix} displayed product price differs from selected SKU")

        cap = expected_request.get("price_max_major")
        if cap is not None:
            if expected_request["budget_basis"] == "product":
                price = _number(product.get("price_major"))
                if price is None or product.get("currency") != expected_request["target_currency"]:
                    issue("budget_basis", f"{prefix} lacks comparable product price")
                elif price > cap + 0.001:
                    issue("hard_constraint", f"{prefix} product price {price} exceeds {cap}")
            elif expected_request["budget_basis"] == "landed":
                quote = product.get("landed_price")
                source_quote = (case.get("quote_by_product") or {}).get(product_id)
                if not isinstance(quote, dict) or source_quote is None:
                    issue("budget_basis", f"{prefix} must have a source landed quote")
                else:
                    total = _number(quote.get("landed_total_major"))
                    if total is None or quote.get("currency") != expected_request["target_currency"] or quote.get("ship_to") != expected_request["ship_to"]:
                        issue("budget_basis", f"{prefix} has no comparable landed total")
                    elif total > cap + 0.001:
                        issue("hard_constraint", f"{prefix} landed total {total} exceeds {cap}")
                    for field in ("ship_to", "currency", "subtotal_major", "freight_major", "tariff_major", "landed_total_major"):
                        if quote.get(field) != source_quote.get(field):
                            issue("quote_mismatch", f"{prefix} landed_price.{field} differs from source quote")
                    parts = [_number(quote.get(field)) for field in ("subtotal_major", "freight_major", "tariff_major")]
                    if total is not None and all(part is not None for part in parts) and abs(total - sum(parts)) > 0.011:
                        issue("quote_arithmetic", f"{prefix} landed total does not equal its components")

        checks = candidate.get("checks")
        if not isinstance(checks, list) or not checks:
            issue("evidence", f"{prefix} needs field-level checks")
            continue
        evidenced_fields: set[str] = set()
        for check_index, check in enumerate(checks):
            label = f"{prefix}.checks[{check_index}]"
            if not isinstance(check, dict):
                issue("evidence", f"{label} must be an object")
                continue
            if check.get("status") not in {"pass", "unknown"}:
                issue("evidence", f"{label} has invalid status")
            evidence = check.get("evidence")
            if not isinstance(evidence, dict):
                issue("evidence", f"{label} lacks evidence object")
                continue
            if not isinstance(evidence.get("kind"), str) or not evidence["kind"]:
                issue("evidence", f"{label} lacks evidence.kind")
            if evidence.get("ref") != case["result_ref"] or evidence.get("ref") not in refs:
                issue("evidence", f"{label} has an untraceable evidence.ref")
            field = evidence.get("field")
            if not isinstance(field, str) or not field.strip():
                issue("evidence", f"{label} lacks evidence.field")
            else:
                evidenced_fields.add(field)
                if check.get("field") != field:
                    issue("evidence", f"{label} check.field and evidence.field differ")
                expected_kind = "rule_estimate" if field.startswith("landed_price.") else "catalog_snapshot"
                if evidence.get("kind") != expected_kind:
                    issue("evidence", f"{label} evidence.kind must be {expected_kind}")
                if check.get("status") == "pass" and not _evidence_field_exists(field, product, sku):
                    issue("evidence", f"{label} evidence.field does not identify a product fact")
            if evidence.get("observed_at") != case["observed_at"]:
                issue("evidence", f"{label} has wrong evidence.observed_at")
        needed = ["stock"]
        if expected_request.get("category"):
            needed.append("category")
        if expected_request.get("ship_to"):
            needed.append("ships_to")
        if expected_request.get("excluded_material_tags") or expected_request.get("required_material_tags"):
            needed.append("material_tags")
        if cap is not None:
            needed.append("price_major" if expected_request["budget_basis"] == "product" else "landed_total_major")
        for field in needed:
            if not _has_evidence_field(evidenced_fields, field):
                issue("evidence_coverage", f"{prefix} has no field-level evidence for {field}")

    for statement in _texts(report):
        if _LIVE_CLAIM.search(statement):
            issue("unsupported_live_claim", f"unsupported live or official claim: {statement[:100]!r}")
    return findings


def evaluate_reports(cases: list[dict[str, Any]], reports: list[dict[str, Any]], catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for row in reports:
        case_id = row.get("case_id") or row.get("id")
        if case_id in by_id:
            duplicate_ids.add(str(case_id))
        by_id[str(case_id)] = row.get("report", row)
    results = []
    for case in cases:
        case_id = case["id"]
        if case_id not in by_id:
            findings = [{"code": "missing_report", "message": f"no report for {case_id}"}]
        elif case_id in duplicate_ids:
            findings = [{"code": "duplicate_report", "message": f"multiple reports for {case_id}"}]
        elif not isinstance(by_id[case_id], dict):
            findings = [{"code": "shape", "message": f"report for {case_id} must be an object"}]
        else:
            findings = evaluate_case(case, by_id[case_id], catalog)
        results.append({"id": case_id, "scenario": case["scenario"], "passed": not findings, "findings": findings})
    known = {case["id"] for case in cases}
    for case_id in sorted(by_id.keys() - known):
        results.append({"id": case_id, "scenario": "unknown", "passed": False,
                        "findings": [{"code": "unknown_case", "message": f"no fixture for {case_id}"}]})
    counts = Counter(finding["code"] for result in results for finding in result["findings"])
    return {"fixture_version": 2, "summary": {"total": len(results), "passed": sum(r["passed"] for r in results),
            "failed": sum(not r["passed"] for r in results), "findings_by_code": dict(sorted(counts.items()))},
            "cases": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True, help="JSONL of {case_id, report} objects")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args(argv)
    try:
        cases = load_jsonl(args.cases)
        reports = load_jsonl(args.reports)
        catalog = {row["product_id"]: row for row in load_jsonl(args.catalog)}
        if not cases or any(case.get("fixture_version") != 2 for case in cases):
            raise ValueError("cases must contain version 2 fixtures")
        if len({case["id"] for case in cases}) != len(cases):
            raise ValueError("duplicate case IDs")
        result = evaluate_reports(cases, reports, catalog)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
