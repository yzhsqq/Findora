"""Compare two CJ retrieval reports only when they used the same frozen inputs."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def compare(before: dict, after: dict) -> dict:
    for key in ("split", "cases_sha256", "top_k"):
        if before.get(key) != after.get(key):
            raise ValueError(f"input mismatch: {key}")
    if before.get("snapshot", {}).get("sha256") != after.get("snapshot", {}).get("sha256"):
        raise ValueError("input mismatch: frozen CJ snapshot")
    original = before.get("observations") or []
    candidate = after.get("observations") or []
    ids_before = [item["id"] for item in original]
    ids_after = [item["id"] for item in candidate]
    if ids_before != ids_after or len(ids_before) != len(set(ids_before)):
        raise ValueError("input mismatch: case order or IDs")
    changed = []
    for left, right in zip(original, candidate):
        if left["expected_empty"] != right["expected_empty"] or left["anchor_id"] != right["anchor_id"]:
            raise ValueError(f"input mismatch: gold label for {left['id']}")
        if left["pass"] != right["pass"] or left["retrieved"] != right["retrieved"]:
            changed.append({
                "id": left["id"], "kind": left["kind"],
                "before_pass": left["pass"], "after_pass": right["pass"],
                "before_rank": left["rank"], "after_rank": right["rank"],
                "before_retrieved": left["retrieved"], "after_retrieved": right["retrieved"],
            })
    first = before["result"]
    second = after["result"]
    return {
        "compared_at": datetime.now(timezone.utc).isoformat(),
        "split": before["split"], "snapshot_sha256": before["snapshot"]["sha256"],
        "cases_sha256": before["cases_sha256"], "case_count": len(original),
        "code_sha256_before": before.get("code", {}).get("worktree_sha256"),
        "code_sha256_after": after.get("code", {}).get("worktree_sha256"),
        "known_item_hit_at_5": {"before": first["known_item_hit_at_5"], "after": second["known_item_hit_at_5"],
                                "before_count": sum(item["pass"] for item in original if not item["expected_empty"]),
                                "after_count": sum(item["pass"] for item in candidate if not item["expected_empty"]),
                                "denominator": first["positive_count"]},
        "empty_accuracy": {"before": first["empty_accuracy"], "after": second["empty_accuracy"],
                           "denominator": first["empty_count"]},
        "case_failures": {
            "before": sum(not item["pass"] for item in original),
            "after": sum(not item["pass"] for item in candidate),
            "denominator": len(original),
        },
        "failure_types_before": first["failure_types"],
        "failure_types_after": second["failure_types"],
        "by_kind_before": first["by_kind"], "by_kind_after": second["by_kind"],
        "cost_before": before["cost_scope"], "cost_after": after["cost_scope"],
        "local_latency_ms": {
            "before_p50": first["local_latency_ms_p50"], "after_p50": second["local_latency_ms_p50"],
            "before_p95": first["local_latency_ms_p95"], "after_p95": second["local_latency_ms_p95"],
            "interpretation": "Single sequential runs on a shared local machine; no causal performance claim.",
        },
        "changed_cases": changed,
        "improved_ids": [item["id"] for item in changed if not item["before_pass"] and item["after_pass"]],
        "regressed_ids": [item["id"] for item in changed if item["before_pass"] and not item["after_pass"]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    result = compare(before, after)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hit = result["known_item_hit_at_5"]
    lines = [
        "# CJ 检索 UUID 精确查找：前后对比", "",
        "使用相同的 40 道题、相同的冻结 CJ 快照与相同的 K=5。该数据集只有一个人工核验锚点/正例，以下是已知商品 Hit@5。", "",
        f"- 命中：{hit['before_count']}/{hit['denominator']} → {hit['after_count']}/{hit['denominator']}（{hit['before']:.1%} → {hit['after']:.1%}）。",
        f"- 无结果题准确率：{result['empty_accuracy']['before']:.1%} → {result['empty_accuracy']['after']:.1%}。",
        f"- 本批次未通过用例：{result['case_failures']['before']}/{result['case_failures']['denominator']} → {result['case_failures']['after']}/{result['case_failures']['denominator']}；这不是在线接口故障率。",
        f"- 改善用例：{', '.join(result['improved_ids']) or '无'}；退化用例：{', '.join(result['regressed_ids']) or '无'}。",
        "- 本轮只读 SQLite，前后 CJ 计点与模型 token 均为 0。该成本不代表 Agent 端到端成本。", "",
        "本地检索耗时受共享机器负载影响，本次单次顺序跑测无法推出延迟变化。中文改写题仍需单独改进。", "",
        "| 类型 | 改进前 | 改进后 |", "| --- | ---: | ---: |",
    ]
    for kind, first_kind in sorted(result["by_kind_before"].items()):
        second_kind = result["by_kind_after"][kind]
        lines.append(f"| {kind} | {first_kind['pass_count']}/{first_kind['count']} | {second_kind['pass_count']}/{second_kind['count']} |")
    lines += ["", "运行报告内含输入哈希与逐题返回；如快照、题集、标注或顺序不一致，对比脚本会直接拒绝。", ""]
    args.output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"comparison": str(args.output), "improved": result["improved_ids"],
                      "regressed": result["regressed_ids"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
