# -*- coding: utf-8 -*-
"""Decision reports only promote checked catalog facts into candidates."""
from copy import deepcopy

from app.application.usecases.shopping_decision import build_decision_report


def card(number: int = 1, **overrides) -> dict:
    product_id = f"P{number:04d}"
    original = {
        "product_id": product_id,
        "title": f"旅行背包 {number}",
        "category": "旅行装备",
        "default_sku_id": f"{product_id}-S1",
        "skus": [{"sku_id": f"{product_id}-S1", "stock": 3}],
        "ships_to": ["CN"],
        "material_tags": ["尼龙"],
        "price_major": 90,
        "currency": "CNY",
        "landed_price": {
            "ship_to": "CN", "currency": "CNY", "subtotal_major": 90,
            "freight_major": 25, "tariff_major": 0, "landed_total_major": 115,
        },
    }
    original.update(overrides)
    return original


def search(hits: list[dict], **conditions) -> dict:
    return {
        "hits": hits,
        "query_conditions": {
            "normalized_query": "轻便旅行背包", "category": "旅行装备",
            "ship_to": "CN", "target_currency": "CNY",
            "price_max_major": 120,
            "excluded_material_tags": [], "required_material_tags": [],
            **conditions,
        },
        "observed_at": "2026-09-27T00:00:00+00:00",
        "result_ref": "ctx-search-1",
    }


def test_at_most_five_candidates_preserve_retrieval_order_and_input():
    result = search([card(index) for index in range(1, 8)])
    before = deepcopy(result)
    report = build_decision_report(result)

    assert report["version"] == 2
    assert report["status"] == "ready"
    assert [item["product"]["product_id"] for item in report["candidates"]] == [f"P{i:04d}" for i in range(1, 6)]
    assert result == before
    report["candidates"][0]["product"]["price_major"] = 0
    assert result["hits"][0]["price_major"] == 90


def test_hard_constraints_are_rechecked_and_upstream_rejections_are_kept():
    hits = [
        card(1, category="数码配件"),
        card(2, ships_to=["US"]),
        card(3, material_tags=["尼龙", "合成聚合物"]),
        card(4, material_tags=["棉"]),
        card(5, skus=[{"sku_id": "P0005-S1", "stock": 0}]),
        card(6, price_major=130, landed_price={}),
        card(7, material_tags=[""]),
        card(8),
    ]
    result = search(hits, excluded_material_tags=["合成聚合物"], required_material_tags=["尼龙"])
    result["filtered_out"] = [{"product_id": "P9999", "title": "另一商品", "reason": "over_price_cap"}]

    report = build_decision_report(result)

    assert [item["product"]["product_id"] for item in report["candidates"]] == ["P0008"]
    assert [item["product_id"] for item in report["excluded"]] == [
        "P0001", "P0002", "P0003", "P0004", "P0005", "P0006", "P0007", "P9999",
    ]
    assert "超出商品价预算" in report["excluded"][-1]["reason"]
    checked_fields = {check["field"] for check in report["candidates"][0]["checks"]}
    assert {"skus.stock", "category", "ships_to", "material_tags", "price_major"} <= checked_fields


def test_product_and_landed_budget_are_distinct():
    result = search([card()], price_max_major=100)

    product = build_decision_report(result, budget_basis="product")
    landed = build_decision_report(result, budget_basis="landed")

    assert product["status"] == "ready"
    assert product["request"]["budget_basis"] == "product"
    assert "商品价在预算内" in product["candidates"][0]["reasons"][-1]
    assert landed["status"] == "no_match"
    assert "估算到手价" in landed["excluded"][0]["reason"]


def test_landed_budget_requires_matching_quote_and_unknown_is_explicit():
    result = search([card(1, landed_price={}), card(2, landed_price={
        "ship_to": "US", "currency": "CNY", "subtotal_major": 90, "landed_total_major": 95,
    })], price_max_major=120)

    landed = build_decision_report(result, budget_basis="landed")
    product = build_decision_report(result, budget_basis="product")

    assert landed["status"] == "no_match"
    assert len(landed["excluded"]) == 2
    assert all("缺少可核验的到手价" in item["reason"] for item in landed["excluded"])
    assert product["status"] == "ready"
    assert all("到手价报价缺失" in item["unknowns"][0] for item in product["candidates"])
    assert all(any(check["status"] == "unknown" and check["field"] == "landed_price.landed_total_major"
                   for check in item["checks"]) for item in product["candidates"])


def test_field_evidence_uses_snapshot_ref_and_marks_quote_as_estimate():
    result = search([card()])
    report = build_decision_report(result, budget_basis="landed")
    checks = report["candidates"][0]["checks"]

    assert report["evidence_refs"] == ["ctx-search-1"]
    assert all(check["evidence"]["ref"] == "ctx-search-1" for check in checks)
    assert all(check["evidence"]["field"] == check["field"] for check in checks)
    assert all(check["evidence"]["observed_at"] == result["observed_at"] for check in checks)
    assert next(check for check in checks if check["label"] == "估算到手价")["evidence"]["kind"] == "rule_estimate"
    assert next(check for check in checks if check["label"] == "商品价")["evidence"]["kind"] == "catalog_snapshot"


def test_multiple_unmapped_refs_do_not_claim_a_specific_snapshot():
    result = search([card()])
    result.pop("result_ref")
    result["result_refs"] = ["ctx-a", "ctx-b"]

    report = build_decision_report(result)

    assert report["evidence_refs"] == ["ctx-a", "ctx-b"]
    assert all(check["evidence"]["ref"] is None for check in report["candidates"][0]["checks"])
    assert "检索证据引用缺失" in report["candidates"][0]["unknowns"][-1]


def test_merged_exact_id_results_keep_each_products_own_evidence_ref():
    result = search([card(1), card(2)])
    result.pop("result_ref")
    result["result_refs_by_product"] = {"P0001": ["ctx-a"], "P0002": ["ctx-b"]}

    report = build_decision_report(result)

    assert report["evidence_refs"] == ["ctx-a", "ctx-b"]
    assert [candidate["checks"][0]["evidence"]["ref"] for candidate in report["candidates"]] == ["ctx-a", "ctx-b"]


def test_empty_or_invalid_input_never_invents_candidates():
    assert build_decision_report({"hits": []})["status"] == "no_match"
    assert build_decision_report(None)["candidates"] == []
    invalid = build_decision_report(search([card()], price_max_major="unknown"))
    assert invalid["status"] == "no_match"
    assert "预算上限无效" in invalid["excluded"][0]["reason"]
    invalid_basis = build_decision_report(search([card()]), budget_basis="other")
    assert invalid_basis["status"] == "no_match"
