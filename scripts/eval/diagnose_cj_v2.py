"""Read-only replay for first-query misses after the sealed v2 run."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "data/cj_eval/v2_baseline"
sys.path.insert(0, str(BASE))

from app.domain.catalog.product_search_spec import ProductSearchSpec  # noqa: E402
from app.infrastructure.persistence.cj_catalog import CJCatalog  # noqa: E402

SNAPSHOT = ROOT / "data/cj_eval/v1/catalog.sqlite3"
REPORTS = ROOT / "eval/verification/cj-crosslang-v2"
CASES = [ROOT / "eval/cj_crosslang_v1/dev_cases.jsonl",
         ROOT / "eval/cj_crosslang_v2/known_cases.jsonl",
         ROOT / "eval/cj_crosslang_v2/holdout_cases.jsonl"]


async def search(catalog: CJCatalog, query: str, category: str | None, anchor: str, top_k: int) -> dict:
    payload = await catalog.execute(ProductSearchSpec(normalized_query=query, category=category,
                                                       top_k=top_k, target_currency="USD"))
    ids = [str(hit["product_id"]) for hit in payload["hits"]]
    return {"query": query, "category": category, "top_k": top_k,
            "anchor_rank": ids.index(anchor) + 1 if anchor in ids else None,
            "candidate_ids": ids, "total_candidates": payload.get("total_candidates")}


async def main() -> None:
    frozen = json.loads((ROOT / "eval/cj_crosslang_v2/freeze.json").read_text(encoding="utf-8"))
    if hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest() != frozen["snapshot_sha256"]:
        raise ValueError("snapshot changed")
    cases = {row["id"]: row for path in CASES for line in path.read_text(encoding="utf-8").splitlines()
             if (row := json.loads(line))}
    catalog = CJCatalog(SNAPSHOT)
    result = {"snapshot_sha256": frozen["snapshot_sha256"], "baseline_commit": "30b9a4f7e22cfe42c234ba0dc0449d717e870aac",
              "rule": "first miss: category-off rescue, ideal-title rescue, ideal top50 ranking, otherwise retrieval; final ID omission with first hit: answer",
              "rows": []}
    for suite in ("known", "holdout"):
        report = json.loads((REPORTS / suite / "report.json").read_text(encoding="utf-8"))
        for pair in report["pairs"]:
            side = pair["candidate"]
            if side["first_hit5"] and side["final_anchor"]:
                continue
            case = cases[pair["id"]]
            anchor = case["anchor_id"]
            first = side["searches"][0]
            query = first["normalized_query"]
            category = first["effective_category"]
            ideal = case.get("ideal_query") or " ".join(case["anchor_terms"])
            off = await search(catalog, query, None, anchor, 5)
            on = await search(catalog, query, category, anchor, 5) if category else None
            ideal5 = await search(catalog, ideal, None, anchor, 5)
            ideal50 = await search(catalog, ideal, None, anchor, 50) if ideal5["anchor_rank"] is None else None
            if not side["first_hit5"]:
                if category and off["anchor_rank"] is not None:
                    fault = "category"
                elif ideal5["anchor_rank"] is not None:
                    fault = "rewrite"
                elif ideal50 and ideal50["anchor_rank"] is not None:
                    fault = "ranking"
                else:
                    fault = "retrieval"
            else:
                fault = "answer" if side["any_hit5"] and not side["final_anchor"] else "selection"
            result["rows"].append({"suite": suite, "id": pair["id"], "anchor_id": anchor,
                                   "first_fault": fault, "first_query": query, "first_category": category,
                                   "first_actual_rank": first["anchor_rank"], "any_hit5": side["any_hit5"],
                                   "final_anchor_id_present": side["final_anchor"],
                                   "query_category_off": off, "query_category_on": on,
                                   "ideal_title_query": ideal, "ideal_off_top5": ideal5,
                                   "ideal_off_top50": ideal50})
    target = REPORTS / "diagnosis.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{"id": row["id"], "fault": row["first_fault"],
                       "off_rank": row["query_category_off"]["anchor_rank"],
                       "ideal_rank": row["ideal_off_top5"]["anchor_rank"]} for row in result["rows"]],
                     ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
