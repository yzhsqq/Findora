"""Render the frozen paired CJ v2 evaluation reports as reviewable Markdown."""
from __future__ import annotations

import html
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "eval/cj_crosslang_v2"
VERIFICATION = ROOT / "eval/verification/cj-crosslang-v2"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def metrics_table(report: dict) -> list[str]:
    result = []
    for label, title in (("first_hit5", "首次工具 Hit@5"), ("any_hit5", "任一轮 Hit@5"),
                         ("final_anchor", "最终答案包含锚点 ID")):
        values = report["metrics"][label]
        result.append(f"| {title} | {values['baseline']}/{values['denominator']} | "
                      f"{values['candidate']}/{values['denominator']} | {values['net']:+d} |")
    return result


def attempts_summary(report: dict) -> dict:
    return {"attempts": len(report["attempts"]), "invalid": len(report["invalid_attempts"]),
            "fallbacks": sum(len(item.get("model_fallbacks") or []) for item in report["attempts"]),
            "cj_detail_tool": sum(item.get("cj_tool_calls", []).count("cj_product_detail_tool") for item in report["attempts"]),
            "cj_freight_tool": sum(item.get("cj_tool_calls", []).count("cj_freight_quote_tool") for item in report["attempts"])}


def all_attempt_usage(report: dict, side: str) -> dict:
    rows = [item for item in report["attempts"] if item["variant"] == side]
    keys = ("model_calls", "input_tokens", "output_tokens")
    if any(not isinstance(item.get("usage"), dict) or any(item["usage"].get(key) is None for key in keys)
           for item in rows):
        return {key: None for key in keys}
    return {key: sum(item["usage"][key] for item in rows) for key in keys}


def pilot(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT COUNT(*),COALESCE(SUM(points),0) FROM cj_pilot_calls").fetchone()
        return int(row[0]), int(row[1])


def case_table(report: dict, diagnosis: dict) -> list[str]:
    lines = ["| 用例 | 首次排名 B→C | 首次 query B→C | 任一轮 B→C | 答案 ID B→C | 首个故障层 |",
             "| --- | ---: | --- | --- | --- | --- |"]
    for pair in report["pairs"]:
        case_id = pair["id"]
        base, cand = pair["baseline"], pair["candidate"]
        bfirst, cfirst = base["searches"][0], cand["searches"][0]
        link = f"../verification/cj-crosslang-v2/{report['suite']}/traces/{case_id}.{pair['attempt']}.candidate.jsonl.gz"
        rank = lambda value: str(value) if value is not None else "—"
        safe = lambda value: html.escape(str(value or ""), quote=False).replace("|", "&#124;")
        fault = diagnosis.get(case_id, "—")
        lines.append(f"| [{case_id}]({link}) | {rank(bfirst['anchor_rank'])}→{rank(cfirst['anchor_rank'])} | "
                     f"{safe(bfirst['normalized_query'])}<br>→ {safe(cfirst['normalized_query'])} | "
                     f"{int(base['any_hit5'])}→{int(cand['any_hit5'])} | "
                     f"{int(base['final_anchor'])}→{int(cand['final_anchor'])} | {fault} |")
    return lines


def main() -> None:
    known = read(VERIFICATION / "known/report.json")
    holdout = read(VERIFICATION / "holdout/report.json")
    exploratory = read(VERIFICATION / "known.c357b76/report.json")
    diagnosis = read(VERIFICATION / "diagnosis.json")
    safeguard = read(VERIFICATION / "default-off40/comparison.json")
    if known["manifests"] != holdout["manifests"] or known["frozen"] != holdout["frozen"]:
        raise ValueError("known and holdout were not run on the same source and inputs")
    if known["valid_pairs"] != 18 or holdout["valid_pairs"] != 10:
        raise ValueError("incomplete paired evaluation")
    if not known["all_attempt_cost_complete"] or not holdout["all_attempt_cost_complete"]:
        raise ValueError("formal evaluation contains unknown attempt cost")
    if safeguard["changed_cases"] or safeguard["known_item_hit_at_5"]["after_count"] != 31:
        raise ValueError("default-off guard does not match the pinned baseline")
    faults = {row["id"]: row["first_fault"] for row in diagnosis["rows"]}
    snapshot = ROOT / "data/cj_eval/v1/catalog.sqlite3"
    pilot_counts = {"frozen": pilot(snapshot),
                    "baseline": pilot(ROOT / "data/cj_eval/v2_runs/baseline/cj_catalog.sqlite3"),
                    "candidate": pilot(ROOT / "data/cj_eval/v2_runs/candidate2/cj_catalog.sqlite3")}
    if pilot_counts["frozen"] != pilot_counts["baseline"] or pilot_counts["frozen"] != pilot_counts["candidate"]:
        raise ValueError("CJ point ledger changed during evaluation")
    first = known["workers"]["baseline"]["prompt"]["effective_version"]["version_id"]
    second = known["workers"]["candidate"]["prompt"]["effective_version"]["version_id"]
    output = ["# CJ 中文需求首次检索：第二次评测", "",
              "## 结论", "",
              "提示词候选改善了首次召回，但**未通过预注册的留出集发布门槛，保持未发布**。",
              "留出集 `CJ-V2H003` 的候选检索把目标商品排第 1，最终回答也列出该商品，但漏写 `product_id`；",
              "基线回答写出了该 ID，因此按预先定义的最终锚点 ID 退化判失败。不能在已打开的 10 题上修补后",
              "把它们再次当独立留出集。", "",
              "## 版本与有效性", "",
              f"- 基线 commit：`{known['manifests']['baseline']['commit']}`；候选 commit：`{known['manifests']['candidate']['commit']}`。",
              f"- 基线提示词版本：`{first}`；候选提示词版本：`{second}`。两个工作树的代码差异仅 `app/application/prompts/findora.yml`。",
              f"- CJ 快照 SHA-256：`{known['frozen']['snapshot_sha256']}`；已知题/留出题哈希均在 [freeze.json](freeze.json)。",
              "- 两侧为独立进程、数据目录、会话与 prompt registry；固定 `deepseek-v4-flash`，关闭语义缓存、队列与 Redis；成对顺序按固定种子随机。",
              f"- 已知集 {known['valid_pairs']}/18 对、留出集 {holdout['valid_pairs']}/10 对有效；已知集 `CJ-H003` 在前两对尝试中分别出现基线和候选的详情工具错误，均保留 trace，第三对有效；留出集无效尝试为 0。正式集合无网关错误、模型回退或 usage 缺失。",
              "  [工具错误审计](../verification/cj-crosslang-v2/tool_error_audit.json)保留了首次误判报告的哈希；完整重试见已知集 JSON。",
              "- 上一版提示词 `c357b76` 在已知集仅净增 3/18，且运行器出现一次传输缓冲错误；该探索版报告单独保存在",
              "  [known.c357b76](../verification/cj-crosslang-v2/known.c357b76/report.json)，未接触 v2 留出集。",
              "", "## 预注册指标", "",
              "**已知集（8 道原开发题 + 10 道已公开 v1 留出题）：**", "",
              "| 指标 | 基线 | 候选 | 净增 |", "| --- | ---: | ---: | ---: |",
              *metrics_table(known), "",
              "首次 Hit@5 净增 11/18 ≥ 预注册门槛 5/18；三项指标均无逐题退化。原 40 题组件回归",
              f"{known['component40']['baseline_pass']}/40 → {known['component40']['candidate_pass']}/40，逐题候选和排名相同。",
              "", "**v2 封存留出集：**", "",
              "| 指标 | 基线 | 候选 | 净增 |", "| --- | ---: | ---: | ---: |",
              *metrics_table(holdout), "",
              "首次 Hit@5 净增 5/10 ≥ 门槛 2/10；最终答案 ID 退化 `CJ-V2H003` 违反零退化条件，",
              "即使最终命中总数持平也不算通过。", "",
              "## 调用与成本", "",
              "| 集合 | 侧别 | 模型调用 | 输入 token | 输出 token | 合计 token |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    for label, report in (("已知 18", known), ("留出 10", holdout)):
        for side in ("baseline", "candidate"):
            usage = report["usage"][side]
            output.append(f"| {label} | {side} | {usage['model_calls']} | {usage['input_tokens']} | "
                          f"{usage['output_tokens']} | {usage['input_tokens'] + usage['output_tokens']} |")
    for side in ("baseline", "candidate"):
        calls = known["usage"][side]["model_calls"] + holdout["usage"][side]["model_calls"]
        inputs = known["usage"][side]["input_tokens"] + holdout["usage"][side]["input_tokens"]
        outputs = known["usage"][side]["output_tokens"] + holdout["usage"][side]["output_tokens"]
        output.append(f"| 两集合 | {side} | {calls} | {inputs} | {outputs} | {inputs + outputs} |")
    known_tools, holdout_tools = attempts_summary(known), attempts_summary(holdout)
    output += ["", "上表仅列最终有效配对。实际执行还包含 `CJ-H003` 的两次无效配对；以下是**全部尝试**的真实用量：", "",
               "| 集合 | 侧别 | 尝试次数 | 模型调用 | 输入 token | 输出 token |",
               "| --- | --- | ---: | ---: | ---: | ---: |"]
    for label, report in (("已知 18", known), ("留出 10", holdout)):
        for side in ("baseline", "candidate"):
            usage = all_attempt_usage(report, side)
            attempt_count = sum(item["variant"] == side for item in report["attempts"])
            output.append(f"| {label} | {side} | {attempt_count} | {usage['model_calls']} | "
                          f"{usage['input_tokens']} | {usage['output_tokens']} |")
    output += ["", "两表的 token 均来自完整 `usage.summary`，没有把工具错误或 usage 缺失记为零。模型网关未给出可确认的价格，",
               "所以不换算美元费用；品类知识库启动时的 embedding 请求不在逐题 usage 中，端到端启动成本尚未完整计量。",
               f"已知集 CJ 详情工具调用 {known_tools['cj_detail_tool']} 次、物流试算工具 {known_tools['cj_freight_tool']} 次；",
               f"留出集分别 {holdout_tools['cj_detail_tool']} / {holdout_tools['cj_freight_tool']} 次。两侧 `cj_pilot_calls`",
               f"账本仍与冻结快照同为 {pilot_counts['frozen'][0]} 条、{pilot_counts['frozen'][1]} 点，",
               "因此本轮新增 CJ 点数为 0；这是实测结果，不是验收门槛。", "",
               "## 失败归因与下一轮", "",
               "首次仍未命中的 4 题（已知 `CJ-H001`、`CJ-H003`；留出 `CJ-V2H004`、`CJ-V2H007`）",
               "在**保持候选英文 query 不变、只关 category** 的本地快照回放里都将目标排第 1；",
               "首个故障层为 `category`。`CJ-V2H003` 首次排名第 1，答案未写商品 ID，故障层为 `answer`。",
               "这两类问题应分开实验。下一轮可先在已知集测 category 处理；把这 10 道留出题升级为已知集，",
               "另建新的封存留出集后才能再作泛化判断。当前不得发布这版提示词。",
               "旧词典候选 `693963a` 在主工作树现由默认关闭的 `CJ_EXPERIMENTAL_LEXICON` 控制（防护 commit `6a5cf82`）；",
               "默认关闭后原 40 题与 `30b9a4f` 逐题候选完全相同，见",
               "[对照](../verification/cj-crosslang-v2/default-off40/comparison.json)。当前运行中的本机服务未被本次评测重启。",
               "", "## 逐题证据", "",
               "逐题首次改写、category、候选 ID、排名、完整 usage 与 Agent trace 见",
               "[已知集 JSON](../verification/cj-crosslang-v2/known/report.json)、",
               "[留出集 JSON](../verification/cj-crosslang-v2/holdout/report.json) 和",
               "[只读归因回放](../verification/cj-crosslang-v2/diagnosis.json)。",
               "下面每题可点开候选侧完整压缩 trace；基线 trace 路径在 JSON 中。", "",
               "### 已知集", "", *case_table(known, faults), "", "### v2 留出集", "",
               *case_table(holdout, faults), "",
               "`—` 表示锚点未进入首次前五；`任一轮` 与 `答案 ID` 以 1/0 表示真/假。",
               "每题只核验一个人工指定的锚点，这些数字是已知商品命中率，不是完整 Recall。", ""]
    (SUITE / "RESULTS.md").write_text("\n".join(output), encoding="utf-8")
    print(SUITE / "RESULTS.md")


if __name__ == "__main__":
    main()
