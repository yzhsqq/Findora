"""Offline, read-only known-item benchmark for the CJ snapshot search path.

Usage: python -m scripts.eval.run_cj_baseline --split all

The evaluator never instantiates CJLiveQuoteService, an LLM, or an embedding
client. A frozen SQLite backup and its expected SHA-256 are required so the
same cases can be rerun against the same catalog after a code change.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import sqlite3
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.cj_catalog import CJCatalog
from scripts.eval.run_manifest import select_cases, worktree_fingerprint


ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "eval/cj_v1/retrieval_cases.jsonl"
SNAPSHOT_MANIFEST = ROOT / "eval/cj_v1/snapshot.json"
SNAPSHOT = ROOT / "data/cj_eval/v1/catalog.sqlite3"
OUTPUT = ROOT / "eval/verification/cj-v1"
KINDS = {"lexical", "paraphrase", "exact", "empty"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_cases(path: Path, snapshot: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 40:
        raise ValueError(f"CJ v1 requires exactly 40 retrieval cases; found {len(rows)}")
    ids = [row.get("id") for row in rows]
    if len(ids) != len(set(ids)) or any(not isinstance(case_id, str) for case_id in ids):
        raise ValueError("case IDs must be unique strings")
    with sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True) as db:
        for row in rows:
            kind = row.get("kind")
            relevant = row.get("relevant")
            if kind not in KINDS or row.get("split") not in {"dev", "release"}:
                raise ValueError(f"{row['id']}: invalid kind or split")
            if not isinstance(row.get("query"), str) or not row["query"].strip():
                raise ValueError(f"{row['id']}: query is required")
            if kind == "empty":
                if relevant != [] or row.get("expected_empty") is not True:
                    raise ValueError(f"{row['id']}: empty case must have no gold item")
                continue
            if not isinstance(relevant, list) or len(relevant) != 1:
                raise ValueError(f"{row['id']}: known-item case must have one anchor")
            record = db.execute("SELECT json_extract(list_json,'$.nameEn') FROM products WHERE pid=?", relevant).fetchone()
            if record is None or not record[0]:
                raise ValueError(f"{row['id']}: anchor missing from frozen snapshot")
            terms = row.get("gold_terms")
            if not isinstance(terms, list) or not terms or any(
                not isinstance(term, str) or term.casefold() not in record[0].casefold() for term in terms
            ):
                raise ValueError(f"{row['id']}: anchor title does not support gold_terms")
    return rows


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))], 2)


def summarize(cases: list[dict]) -> dict:
    positive = [row for row in cases if not row["expected_empty"]]
    empty = [row for row in cases if row["expected_empty"]]
    by_kind = {}
    for kind in sorted(KINDS):
        bucket = [row for row in cases if row["kind"] == kind]
        if bucket:
            by_kind[kind] = {
                "count": len(bucket),
                "pass_count": sum(row["pass"] for row in bucket),
                "pass_rate": round(sum(row["pass"] for row in bucket) / len(bucket), 4),
            }
    return {
        "case_count": len(cases),
        "positive_count": len(positive),
        "known_item_hit_at_5": round(sum(row["pass"] for row in positive) / len(positive), 4) if positive else None,
        "known_item_mrr": round(statistics.mean(1 / row["rank"] if row["rank"] else 0 for row in positive), 4) if positive else None,
        "empty_count": len(empty),
        "empty_accuracy": round(sum(row["pass"] for row in empty) / len(empty), 4) if empty else None,
        "by_kind": by_kind,
        "failure_types": dict(sorted(Counter(row["failure_type"] for row in cases if row["failure_type"]).items())),
        "local_latency_ms_p50": percentile([row["latency_ms"] for row in cases], 0.50),
        "local_latency_ms_p95": percentile([row["latency_ms"] for row in cases], 0.95),
    }


def markdown(report: dict) -> str:
    result = report["result"]
    lines = [
        "# CJ 商品检索 v1 基线", "",
        f"运行时间（UTC）：{report['run_at']}",
        f"选集：`{report['split']}`；商品快照 SHA-256：`{report['snapshot']['sha256']}`；标注 SHA-256：`{report['cases_sha256']}`。", "",
        "这是**已知商品能否在前 5 位找回**的基线。每道正例只标注一个可核验的锚点商品；",
        "没有穷举其他相关商品，因此不报告完整 Recall、Precision 或 NDCG。", "",
        f"已知商品 Hit@5：**{result['known_item_hit_at_5']:.1%}**（{result['positive_count']} 题）；",
        f"锚点 MRR：**{result['known_item_mrr']:.3f}**；无结果准确率：**{result['empty_accuracy']:.1%}**（{result['empty_count']} 题）。", "",
        "| 类型 | 通过/总数 | 通过率 |", "| --- | ---: | ---: |",
    ]
    for kind, item in result["by_kind"].items():
        lines.append(f"| {kind} | {item['pass_count']}/{item['count']} | {item['pass_rate']:.1%} |")
    lines += [
        "", f"本地检索耗时 P50/P95：{result['local_latency_ms_p50']}/{result['local_latency_ms_p95']} ms。",
        "本轮仅调用本地 SQLite：CJ 计点 **0**，LLM token **0**；这些不是完整 Agent 的运行成本或延迟。", "",
        "## 逐题结果", "", "| 用例 | 类型 | 结果 | 锚点排名 | 返回商品 ID | 诊断 |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for row in report["observations"]:
        lines.append(
            f"| {row['id']} | {row['kind']} | {'通过' if row['pass'] else '失败'} | "
            f"{row['rank'] or '-'} | {', '.join(row['retrieved']) or '-'} | {row['failure_type'] or '-'} |"
        )
    lines += ["", "失败类型仅用于定位；`candidate_but_anchor_missing` 还需人工检查是否为排序或匹配问题。",
              "报价质量、Agent 回答真实性、模型成本与 CJ 在线失败率尚未由本轮检索基线测量。", ""]
    return "\n".join(lines)


async def run(snapshot: Path, cases: list[dict]) -> list[dict]:
    catalog = CJCatalog(snapshot)
    observations = []
    for row in cases:
        started = time.perf_counter()
        payload = await catalog.execute(ProductSearchSpec(
            normalized_query=row["query"], top_k=5, target_currency="USD"
        ))
        elapsed = (time.perf_counter() - started) * 1000
        retrieved = [str(item["product_id"]) for item in payload["hits"]]
        anchor = row["relevant"][0] if row["relevant"] else None
        rank = retrieved.index(anchor) + 1 if anchor in retrieved else None
        passed = not retrieved if row.get("expected_empty") else rank is not None
        failure_type = None
        if not passed:
            if row.get("expected_empty"):
                failure_type = "unexpected_candidates"
            elif row["kind"] == "exact":
                failure_type = "exact_id_gap"
            elif not retrieved:
                failure_type = "no_candidates"
            else:
                failure_type = "candidate_but_anchor_missing"
        observations.append({
            "id": row["id"], "split": row["split"], "kind": row["kind"], "query": row["query"],
            "anchor_id": anchor, "retrieved": retrieved, "rank": rank, "pass": passed,
            "failure_type": failure_type, "total_candidates": payload["total_candidates"],
            "actual_strategy": payload["recall_strategy"], "latency_ms": round(elapsed, 2),
            "expected_empty": bool(row.get("expected_empty")),
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
        raise SystemExit("冻结 CJ 快照 SHA-256 不匹配；拒绝用变化的数据生成可比基线")
    all_cases = load_cases(args.cases, args.snapshot)
    cases, selection = select_cases(all_cases, args.split)
    source = worktree_fingerprint(ROOT)
    observations = asyncio.run(run(args.snapshot, cases))
    if sha256(args.snapshot) != before or worktree_fingerprint(ROOT)["sha256"] != source["sha256"]:
        raise SystemExit("运行期间代码或 CJ 快照发生变化；结果不写入基线")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False)
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(), "split": args.split, "selection": selection,
        "code": {"git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
                 "worktree_sha256": source["sha256"]},
        "snapshot": {**expected, "sha256": before},
        "cases_sha256": sha256(args.cases), "top_k": 5,
        "runtime": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
        "result": summarize(observations),
        "cost_scope": {"cj_points": 0, "llm_tokens": 0, "scope": "offline_sqlite_retrieval_only"},
        "observations": observations,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"baseline-{args.split}.json"
    md_path = args.output_dir / f"baseline-{args.split}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"report": str(json_path), "result": report["result"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
