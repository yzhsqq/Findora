# -*- coding: utf-8 -*-
"""Build a traceable shopping decision from a completed catalog search.

The search tool supplies candidate facts. This module only checks those facts;
it does not ask a model to infer stock, delivery, materials, or price.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math


_MAX_CANDIDATES = 5
_REJECTION_LABELS = {
    "requested_sku_out_of_stock": "指定规格无库存",
    "out_of_stock": "无可售库存",
    "category_mismatch": "品类不符合要求",
    "material_excluded": "含有排除的材质",
    "material_required_missing": "缺少要求的材质",
    "ship_to_unavailable": "无法配送至指定目的地",
    "over_price_cap": "超出商品价预算",
    "over_landed_price_cap": "超出估算到手价预算",
}


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _tags(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(tag for item in value if (tag := _string(item))))


def _references(result: dict) -> list[str]:
    values = [result.get("result_ref")]
    if isinstance(result.get("result_refs"), list):
        values.extend(result["result_refs"])
    by_product = result.get("result_refs_by_product")
    if isinstance(by_product, dict):
        for item in by_product.values():
            if isinstance(item, list):
                values.extend(item)
    for hit in result.get("hits", []) if isinstance(result.get("hits"), list) else []:
        if isinstance(hit, dict):
            values.append(hit.get("result_ref"))
    return list(dict.fromkeys(ref for value in values if (ref := _string(value))))


def build_decision_report(result: dict, *, budget_basis: str = "product") -> dict:
    """Return a version 2 decision report, preserving retrieval order.

    ``product`` compares the selected SKU's converted product price. ``landed``
    compares a matching rule estimate including freight and tariff. A missing or
    inconsistent estimate cannot establish that a landed budget is met.
    """
    result = result if isinstance(result, dict) else {}
    conditions = result.get("query_conditions")
    conditions = conditions if isinstance(conditions, dict) else {}
    raw_cap = conditions.get("price_max_major")
    cap = _amount(raw_cap)
    invalid_cap = raw_cap is not None and cap is None
    basis = _string(budget_basis).lower()
    invalid_basis = basis not in {"product", "landed"}
    request = {
        "normalized_query": _string(conditions.get("normalized_query")),
        "category": _string(conditions.get("category")) or None,
        "ship_to": _string(conditions.get("ship_to")).upper() or None,
        "target_currency": _string(conditions.get("target_currency")).upper() or "CNY",
        "price_max_major": cap,
        "budget_basis": basis,
        "excluded_material_tags": _tags(conditions.get("excluded_material_tags")),
        "required_material_tags": _tags(conditions.get("required_material_tags")),
    }
    observed_at = _string(result.get("observed_at")) or None
    refs = _references(result)
    common_ref = _string(result.get("result_ref")) or (refs[0] if len(refs) == 1 else None)
    refs_by_product = result.get("result_refs_by_product")
    refs_by_product = refs_by_product if isinstance(refs_by_product, dict) else {}

    candidates: list[dict] = []
    excluded: list[dict] = []
    hits = result.get("hits") if isinstance(result.get("hits"), list) else []

    for card in hits:
        if not isinstance(card, dict):
            excluded.append({"product_id": "", "title": "", "reason": "商品卡无效"})
            continue
        product_id = _string(card.get("product_id"))
        title = _string(card.get("title"))

        def reject(reason: str) -> None:
            excluded.append({"product_id": product_id, "title": title, "reason": reason})

        if not product_id or not title:
            reject("商品身份信息缺失")
            continue
        if invalid_basis:
            reject("预算口径无效，仅支持 product 或 landed")
            continue
        if invalid_cap:
            reject("预算上限无效，无法核验")
            continue

        product_refs = refs_by_product.get(product_id)
        product_refs = product_refs if isinstance(product_refs, list) else []
        mapped_ref = _string(product_refs[0]) if len(product_refs) == 1 else ""
        ref = _string(card.get("result_ref")) or mapped_ref or common_ref
        checks: list[dict] = []
        reasons: list[str] = []
        tradeoffs: list[str] = []
        unknowns: list[str] = []

        def check(field: str, label: str, detail: str, *, kind: str = "catalog_snapshot", known: bool = True) -> None:
            checks.append({
                "field": field, "label": label, "status": "pass" if known else "unknown",
                "detail": detail,
                "evidence": {"kind": kind, "ref": ref, "field": field, "observed_at": observed_at},
            })

        if card.get("source_platform") == "CJdropshipping":
            # CJ list data identifies a product and quotes a price. It does not
            # establish a sellable SKU, destination shipping or landed cost.
            check("product_id", "商品来源", "CJdropshipping 商品快照", kind="cj_snapshot")
            price = _amount(card.get("price_major"))
            price_kind = _string(card.get("price_kind"))
            if price is not None and price_kind != "unknown":
                check("price_text", "列表报价", _string(card.get("price_text")) + " USD；非最终结算价", kind="cj_snapshot")
            else:
                unknowns.append("CJ 列表报价缺失")
            check("skus.stock", "可售库存", "库存快照不能替代下单时的实时确认", kind="cj_snapshot", known=False)
            check("ships_to", "配送目的地", "尚未查询目的地运费与可配送范围", kind="cj_snapshot", known=False)
            unknowns.extend(["实时可售库存未确认", "目的地配送和运费未确认", "到手价未确认"])
            if request["required_material_tags"] or request["excluded_material_tags"]:
                unknowns.append("材质条件尚未核验")
            if cap is not None:
                if (request["target_currency"] == "USD" and basis == "product"
                        and price is not None and price_kind != "unknown" and price > cap):
                    reject(f"CJ 列表起价 {price:g} USD 已超出预算 {cap:g} USD")
                    continue
                check("price_max_major", "预算", "报价币种、规格或到手价条件不足，不能核验预算", kind="cj_snapshot", known=False)
                unknowns.append("预算是否满足尚未核验")
            if request["category"] and _string(card.get("category")) not in {request["category"], ""}:
                unknowns.append("CJ 品类与请求品类采用不同分类体系，需人工核对")
            if not ref:
                unknowns.append("检索证据引用缺失")
            if len(candidates) < _MAX_CANDIDATES:
                candidates.append({
                    "product": deepcopy(card), "sku_id": _string(card.get("default_sku_id")),
                    "checks": checks, "reasons": ["来自 CJdropshipping 商品快照"],
                    "tradeoffs": ["列表报价与具体规格售价可能不同"], "unknowns": unknowns,
                })
            continue

        sku_id = _string(card.get("default_sku_id"))
        skus = card.get("skus")
        sku = next((item for item in skus if isinstance(item, dict) and item.get("sku_id") == sku_id), None) if isinstance(skus, list) else None
        if sku is None or type(sku.get("stock")) is not int or sku["stock"] <= 0:
            reject("默认规格或可售库存无法核验")
            continue
        check("skus.stock", "可售库存", f"规格 {sku_id} 的快照库存为 {sku['stock']}")
        reasons.append(f"规格 {sku_id} 在目录快照中有库存")

        if request["category"]:
            if _string(card.get("category")) != request["category"]:
                reject("品类不符合要求或目录字段缺失")
                continue
            check("category", "品类", f"目录品类为 {request['category']}")

        if request["ship_to"]:
            ships_to = card.get("ships_to")
            if not isinstance(ships_to, list) or request["ship_to"] not in ships_to:
                reject("无法核验配送至指定目的地")
                continue
            check("ships_to", "配送范围", f"目录列有目的地 {request['ship_to']}")
            reasons.append(f"目录快照列有配送至 {request['ship_to']}")
        else:
            unknowns.append("未指定配送目的地，无法估算到手价")

        excluded_tags = set(request["excluded_material_tags"])
        required_tags = set(request["required_material_tags"])
        material_tags = card.get("material_tags")
        if excluded_tags or required_tags:
            if (not isinstance(material_tags, list) or not material_tags
                    or any(not _string(tag) for tag in material_tags)):
                reject("材质标签缺失，无法核验材质要求")
                continue
            material_set = set(_tags(material_tags))
            if material_set & excluded_tags:
                reject("含有排除的材质：" + "、".join(sorted(material_set & excluded_tags)))
                continue
            if required_tags - material_set:
                reject("缺少要求的材质：" + "、".join(sorted(required_tags - material_set)))
                continue
            check("material_tags", "材质要求", "目录材质标签符合指定要求")
            reasons.append("目录材质标签符合指定要求")
        elif not material_tags:
            unknowns.append("目录未提供材质标签")

        price = _amount(card.get("price_major"))
        if price is None or _string(card.get("currency")).upper() != request["target_currency"]:
            reject("商品价格或目标币种无法核验")
            continue
        check("price_major", "商品价", f"所选规格商品价 {price:g} {request['target_currency']}")

        quote = card.get("landed_price") if isinstance(card.get("landed_price"), dict) else {}
        landed = _amount(quote.get("landed_total_major"))
        subtotal = _amount(quote.get("subtotal_major"))
        freight = _amount(quote.get("freight_major"))
        tariff = _amount(quote.get("tariff_major"))
        quote_valid = bool(
            request["ship_to"] and landed is not None and subtotal is not None
            and freight is not None and tariff is not None
            and _string(quote.get("ship_to")).upper() == request["ship_to"]
            and _string(quote.get("currency")).upper() == request["target_currency"]
            and math.isclose(subtotal, price, abs_tol=0.02, rel_tol=0)
            and math.isclose(landed, subtotal + freight + tariff, abs_tol=0.02, rel_tol=0)
        )
        if request["ship_to"]:
            if quote_valid:
                check("landed_price.landed_total_major", "估算到手价", f"规则估算 {landed:g} {request['target_currency']}（含商品、运费和关税）", kind="rule_estimate")
                tradeoffs.append("到手价为规则估算，实际结算金额可能变化")
            else:
                check("landed_price.landed_total_major", "估算到手价", "无可核验的同目的地、同币种报价", kind="rule_estimate", known=False)
                unknowns.append("到手价报价缺失或与所选规格不一致")

        if cap is not None:
            if basis == "landed":
                if not quote_valid:
                    reject("缺少可核验的到手价，无法判断到手价预算")
                    continue
                if landed > cap:
                    reject(f"估算到手价 {landed:g} {request['target_currency']} 超出预算 {cap:g}")
                    continue
                check("landed_price.landed_total_major", "到手价预算", f"估算到手价 {landed:g} {request['target_currency']} 不超过预算 {cap:g}", kind="rule_estimate")
                reasons.append("规则估算到手价在预算内")
            else:
                if price > cap:
                    reject(f"商品价 {price:g} {request['target_currency']} 超出预算 {cap:g}")
                    continue
                check("price_major", "商品价预算", f"商品价 {price:g} {request['target_currency']} 不超过预算 {cap:g}")
                reasons.append("商品价在预算内；运费和关税不计入此预算")

        if not ref:
            unknowns.append("检索证据引用缺失，无法回溯商品快照")
        if len(candidates) < _MAX_CANDIDATES:
            candidates.append({
                "product": deepcopy(card), "sku_id": sku_id, "checks": checks,
                "reasons": reasons, "tradeoffs": tradeoffs, "unknowns": unknowns,
            })

    filtered_out = result.get("filtered_out")
    for rejected in filtered_out if isinstance(filtered_out, list) else []:
        if isinstance(rejected, dict):
            code = _string(rejected.get("reason"))
            excluded.append({
                "product_id": _string(rejected.get("product_id")),
                "title": _string(rejected.get("title")),
                "reason": _REJECTION_LABELS.get(code, code or "未满足检索硬条件"),
            })

    return {
        "version": 2, "request": request,
        "catalog_source": "cj" if result.get("source") == "cj" or any(
            isinstance(hit, dict) and hit.get("source_platform") == "CJdropshipping" for hit in hits
        ) else "fixture",
        "status": "ready" if candidates else "no_match",
        "candidates": candidates, "excluded": excluded,
        "evidence_refs": refs,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
