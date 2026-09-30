"""One sealed holdout pass for the CJ bilingual lookup experiment.

Baseline behavior is reconstructed from the exact old _WORDS literal. The
runner checks that no other executable CJCatalog AST changed between commits.
Each case is run once under each dictionary using the same main orchestrator.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence import cj_catalog
from app.infrastructure.persistence.cj_catalog import CJCatalog
from scripts.eval.run_cj_crosslang import ROOT, SUITE, SNAPSHOT, direct_agent_trace, sha256
from scripts.eval.run_manifest import worktree_fingerprint


def old_words_and_verify() -> dict[str, str]:
    old = subprocess.check_output(
        ["git", "show", "30b9a4f:app/infrastructure/persistence/cj_catalog.py"],
        cwd=ROOT, text=True, encoding="utf-8")
    new = (ROOT / "app/infrastructure/persistence/cj_catalog.py").read_text(encoding="utf-8")
    def parse(source):
        tree = ast.parse(source)
        words = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_WORDS" for target in node.targets):
                words = ast.literal_eval(node.value)
                node.value = ast.Constant(value=None)
        if words is None:
            raise ValueError("CJ translation map missing")
        return words, ast.dump(tree, include_attributes=False)
    baseline_words, baseline_ast = parse(old)
    current_words, current_ast = parse(new)
    if baseline_ast != current_ast or current_words != cj_catalog._WORDS:
        raise ValueError("CJCatalog changed beyond the translation map; baseline simulation is invalid")
    return baseline_words


async def search(catalog: CJCatalog, query: str, anchor: str) -> dict:
    result = await catalog.execute(ProductSearchSpec(normalized_query=query, top_k=5, target_currency="USD"))
    ids = [str(item["product_id"]) for item in result["hits"]]
    return {"ids": ids, "rank": ids.index(anchor) + 1 if anchor in ids else None,
            "total_candidates": result["total_candidates"]}


def summary(rows: list[dict], variant: str) -> dict:
    component = [row[variant]["component"]["rank"] for row in rows]
    agent = [row[variant]["agent"]["first_rank"] for row in rows]
    usages = [row[variant]["agent"]["usage"] for row in rows]
    usage_complete = all(usage.get("usage_complete") is True for usage in usages)
    return {"n": len(rows),
            "component_hit_at_1": sum(rank == 1 for rank in component),
            "component_hit_at_3": sum(rank is not None and rank <= 3 for rank in component),
            "component_hit_at_5": sum(rank is not None for rank in component),
            "component_mrr": round(sum(1 / rank if rank else 0 for rank in component) / len(rows), 4) if rows else None,
            "agent_first_hit_at_1": sum(rank == 1 for rank in agent),
            "agent_first_hit_at_3": sum(rank is not None and rank <= 3 for rank in agent),
            "agent_first_hit_at_5": sum(rank is not None for rank in agent),
            "agent_first_mrr": round(sum(1 / rank if rank else 0 for rank in agent) / len(rows), 4) if rows else None,
            "agent_any_hit_at_5": sum(row[variant]["agent"]["any_hit_at_5"] for row in rows),
            "answer_mentions_anchor": sum(row[variant]["agent"]["answer_mentions_anchor"] for row in rows),
            "model_calls": sum(usage.get("model_calls") or 0 for usage in usages),
            "usage_complete": usage_complete,
            "unknown_usage_calls": sum(usage.get("unknown_usage_calls") or 0 for usage in usages),
            "input_tokens": sum(usage["input_tokens"] for usage in usages) if usage_complete else None,
            "output_tokens": sum(usage["output_tokens"] for usage in usages) if usage_complete else None,
            "observed_input_tokens": sum(usage.get("observed_input_tokens") or 0 for usage in usages),
            "observed_output_tokens": sum(usage.get("observed_output_tokens") or 0 for usage in usages),
            "search_calls": sum(row[variant]["agent"]["search_calls"] for row in rows)}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "eval/verification/cj-crosslang-v1/holdout")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    frozen = json.loads((SUITE / "freeze.json").read_text(encoding="utf-8"))
    if sha256(SNAPSHOT) != frozen["snapshot_sha256"] or sha256(SUITE / "holdout_cases.jsonl") != frozen["holdout_cases_sha256"]:
        raise ValueError("sealed holdout input changed")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if commit != "693963a78671e928cd398529f890c0f74853d3eb":
        raise ValueError("fix commit changed after freeze")
    baseline_words = old_words_and_verify()
    fix_words = dict(cj_catalog._WORDS)
    cases = [json.loads(line) for line in (SUITE / "holdout_cases.jsonl").read_text(encoding="utf-8").splitlines()]
    if len(cases) != 10 or len({row["id"] for row in cases}) != 10:
        raise ValueError("expected ten distinct holdout cases")
    source = worktree_fingerprint(ROOT)
    old_anchors = {anchor for line in (ROOT / "eval/cj_v1/retrieval_cases.jsonl").read_text(encoding="utf-8").splitlines()
                   for anchor in json.loads(line)["relevant"]}
    import sqlite3
    with sqlite3.connect(f"file:{SNAPSHOT.as_posix()}?mode=ro", uri=True) as db:
        for case in cases:
            title = db.execute("SELECT json_extract(list_json,'$.nameEn') FROM products WHERE pid=?",
                               (case["anchor_id"],)).fetchone()
            if case["anchor_id"] in old_anchors or not title or not all(term.casefold() in title[0].casefold() for term in case["anchor_terms"]):
                raise ValueError(f"invalid sealed anchor: {case['id']}")
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("fix_commit") != commit or report.get("frozen") != frozen:
            raise ValueError("existing holdout report belongs to another freeze")
        rows = report["observations"]
    else:
        rows = []
    from app.composition import build_container
    container = await build_container()
    await container.startup()
    if container.settings.catalog_source != "cj":
        raise ValueError("Agent is not using CJ")
    data_catalog = container.settings.data_dir / "cj_catalog.sqlite3"
    if sha256(data_catalog) != frozen["snapshot_sha256"]:
        raise ValueError("Agent catalog is not the frozen snapshot")
    catalog = CJCatalog(SNAPSHOT)
    try:
        for case in cases:
            if any(row["id"] == case["id"] for row in rows):
                continue
            pair = {"id": case["id"], "stratum": case["stratum"], "anchor_id": case["anchor_id"]}
            for variant, words in (("baseline", baseline_words), ("fix", fix_words)):
                cj_catalog._WORDS.clear()
                cj_catalog._WORDS.update(words)
                component = await search(catalog, case["query"], case["anchor_id"])
                trace_path = output / "traces" / variant / f"{case['id']}.jsonl.gz"
                prior_trace = str(trace_path.relative_to(ROOT)) if trace_path.exists() else None
                if prior_trace:
                    trace_path = trace_path.with_name(f"{case['id']}.retry1.jsonl.gz")
                trace = await asyncio.wait_for(direct_agent_trace(container, case, trace_path), timeout=240)
                searches = [call for call in trace["calls"] if call["tool"] == "product_search_tool"]
                all_searches = []
                for call in searches:
                    result = call["parsed_result"] if isinstance(call["parsed_result"], dict) else {}
                    ids = [str(hit.get("product_id")) for hit in result.get("hits", [])]
                    all_searches.append({"query": (call["args"] or {}).get("normalized_query"),
                                         "category": result.get("query_conditions", {}).get("category"),
                                         "ids": ids, "rank": ids.index(case["anchor_id"]) + 1 if case["anchor_id"] in ids else None})
                first_rank = all_searches[0]["rank"] if all_searches else None
                pair[variant] = {"component": component,
                                 "agent": {"terminal": trace["terminal"], "trace_file": trace["trace_file"],
                                           "first_rank": first_rank, "searches": all_searches,
                                           "any_hit_at_5": any(item["rank"] is not None for item in all_searches),
                                           "answer_mentions_anchor": case["anchor_id"] in trace["final_answer"],
                                           "answer": trace["final_answer"], "search_calls": len(searches),
                                           "tool_names": [call["tool"] for call in trace["calls"]],
                                           "usage": trace["usage"] or {}, "prior_interrupted_trace": prior_trace}}
                if trace["terminal"] != "RUN_FINISHED":
                    raise RuntimeError(f"{case['id']} {variant}: Agent did not finish; trace saved")
            rows.append(pair)
            report = {"run_at": datetime.now(timezone.utc).isoformat(), "baseline_commit": frozen["baseline_commit"],
                      "fix_commit": commit, "frozen": frozen, "worktree_sha256": source["sha256"],
                      "model": container.settings.llm_model, "prompt": container.prompt_registry.describe(),
                      "observations": rows, "metrics": {variant: summary(rows, variant) for variant in ("baseline", "fix")}}
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(case["id"], "baseline", pair["baseline"]["agent"]["first_rank"],
                  "fix", pair["fix"]["agent"]["first_rank"], flush=True)
    finally:
        cj_catalog._WORDS.clear()
        cj_catalog._WORDS.update(fix_words)
        await container.shutdown()
    if sha256(SNAPSHOT) != frozen["snapshot_sha256"] or sha256(data_catalog) != frozen["snapshot_sha256"]:
        raise ValueError("catalog snapshot changed during holdout")
    if worktree_fingerprint(ROOT)["sha256"] != source["sha256"]:
        raise ValueError("source changed during holdout")


if __name__ == "__main__":
    asyncio.run(main())
