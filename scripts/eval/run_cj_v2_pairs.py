"""Paired, isolated CJ Agent evaluation. Run known first; holdout is gated."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "eval/cj_crosslang_v2"
BASE = ROOT / "data/cj_eval/v2_baseline"
CANDIDATE = ROOT / "data/cj_eval/v2_prompt"
RUNS = ROOT / "data/cj_eval/v2_runs"
RUN_DATA = {"baseline": RUNS / "baseline", "candidate": RUNS / "candidate2"}
OUTPUT = ROOT / "eval/verification/cj-crosslang-v2"
SNAPSHOT = ROOT / "data/cj_eval/v1/catalog.sqlite3"
BASE_COMMIT = "30b9a4f7e22cfe42c234ba0dc0449d717e870aac"
PREFIX = "CJ_V2_JSON "
SEED = 20260930


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(worktree: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=worktree, check=True,
                          capture_output=True, text=True).stdout.strip()


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def inputs(suite: str) -> tuple[list[dict], dict]:
    freeze = json.loads((SUITE / "freeze.json").read_text(encoding="utf-8"))
    if sha(SNAPSHOT) != freeze["snapshot_sha256"]:
        raise ValueError("CJ snapshot hash mismatch")
    known = SUITE / "known_cases.jsonl"
    holdout = SUITE / "holdout_cases.jsonl"
    if sha(known) != freeze["known_cases_sha256"] or sha(holdout) != freeze["holdout_cases_sha256"]:
        raise ValueError("v2 case hash mismatch")
    dev = ROOT / "eval/cj_crosslang_v1/dev_cases.jsonl"
    old = json.loads((ROOT / "eval/cj_crosslang_v1/freeze.json").read_text(encoding="utf-8"))
    if sha(dev) != old["dev_cases_sha256"]:
        raise ValueError("v1 dev hash mismatch")
    cases = lines(dev) + lines(known) if suite == "known" else lines(holdout)
    if len(cases) != (18 if suite == "known" else 10):
        raise ValueError("case count mismatch")
    return cases, freeze


def prepare() -> dict:
    if git(BASE, "rev-parse", "HEAD") != BASE_COMMIT:
        raise ValueError("baseline commit mismatch")
    candidate_commit = git(CANDIDATE, "rev-parse", "HEAD")
    if git(CANDIDATE, "merge-base", BASE_COMMIT, candidate_commit) != BASE_COMMIT:
        raise ValueError("candidate does not descend from baseline")
    if git(BASE, "status", "--porcelain") or git(CANDIDATE, "status", "--porcelain"):
        raise ValueError("evaluation worktrees must be clean")
    changes = git(CANDIDATE, "diff", "--name-only", BASE_COMMIT, candidate_commit).splitlines()
    if changes != ["app/application/prompts/globex.yml"]:
        raise ValueError(f"unexpected candidate changes: {changes}")
    if sha(BASE / "app/application/prompts/globex.yml") == sha(CANDIDATE / "app/application/prompts/globex.yml"):
        raise ValueError("prompt variant absent")
    frozen = json.loads((SUITE / "freeze.json").read_text(encoding="utf-8"))
    if sha(SNAPSHOT) != frozen["snapshot_sha256"]:
        raise ValueError("snapshot changed")
    manifests = {}
    for label, path in (("baseline", BASE), ("candidate", CANDIDATE)):
        data = RUN_DATA[label]
        data.mkdir(parents=True, exist_ok=True)
        catalog = data / "cj_catalog.sqlite3"
        if not catalog.exists():
            shutil.copy2(SNAPSHOT, catalog)
        if sha(catalog) != frozen["snapshot_sha256"]:
            raise ValueError(f"{label} catalog hash mismatch")
        manifests[label] = {"commit": git(path, "rev-parse", "HEAD"),
                            "branch": git(path, "branch", "--show-current"),
                            "prompt_sha256": sha(path / "app/application/prompts/globex.yml"),
                            "catalog_sha256": sha(catalog), "data_dir": str(data)}
    return manifests


def env_for(label: str) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in dotenv_values(ROOT / ".env").items():
        if value is not None:
            env.setdefault(key, value)
    env.update({"DATA_DIR": str(RUN_DATA[label]), "CATALOG_SOURCE": "cj",
                "PROMPT_PIN_VERSION": "", "QUEUE_ENABLED": "0", "REDIS_URL": "",
                "SEMANTIC_CACHE_ENABLED": "0", "QDRANT_URL": "", "DATABASE_URL": "file",
                "HYBRID_RECALL_ENABLED": "0", "DRIFT_DETECT_ENABLED": "0",
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    env.pop("CJdropshipping_key", None)
    return env


async def worker(label: str) -> None:
    path = BASE if label == "baseline" else CANDIDATE
    sys.path.insert(0, str(path))
    # Import the old worktree's application code, and the common read-only trace adapter.
    spec = importlib.util.spec_from_file_location("cj_v1_trace_adapter", ROOT / "scripts/eval/run_cj_crosslang.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    from app.composition import build_container

    container = await build_container()
    await container.startup()
    settings = container.settings
    info = {"status": "ready", "label": label, "commit": git(path, "rev-parse", "HEAD"),
            "runtime": container.runtime, "prompt": container.prompt_registry.describe(),
            "model": settings.llm_model, "fallback_model": settings.llm_fallback_model,
            "catalog_source": settings.catalog_source, "semantic_cache": settings.semantic_cache_enabled,
            "queue": settings.queue_enabled, "redis": bool(settings.redis_url),
            "data_dir": str(settings.data_dir)}
    print(PREFIX + json.dumps(info, ensure_ascii=True), flush=True)
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            request = json.loads(line)
            case, attempt, suite = request["case"], request["attempt"], request["suite"]
            trace_path = OUTPUT / suite / "traces" / f"{case['id']}.{attempt}.{label}.jsonl.gz"
            try:
                trace = await module.direct_agent_trace(container, case, trace_path)
                result = {"status": "ok", "trace": trace}
            except BaseException as error:
                result = {"status": "exception", "error_type": type(error).__name__,
                          "error": str(error), "traceback": traceback.format_exc()[-6000:]}
            print(PREFIX + json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        await container.shutdown()


class Child:
    def __init__(self, label: str, process: asyncio.subprocess.Process, log):
        self.label, self.process, self.log = label, process, log

    async def receive(self, timeout: float = 300) -> dict:
        while True:
            raw = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not raw:
                raise RuntimeError(f"{self.label} worker exited with {self.process.returncode}")
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith(PREFIX):
                return json.loads(line[len(PREFIX):])
            self.log.write(line + "\n")
            self.log.flush()

    async def run(self, case: dict, attempt: int, suite: str) -> dict:
        self.process.stdin.write((json.dumps({"case": case, "attempt": attempt, "suite": suite},
                                             ensure_ascii=True) + "\n").encode("ascii"))
        await self.process.stdin.drain()
        return await self.receive()


def summarize(case: dict, result: dict) -> dict:
    row = {"id": case["id"], "raw_query": case["query"], "anchor_id": case["anchor_id"],
           "status": result["status"]}
    if result["status"] != "ok":
        row["invalid_reason"] = result.get("error_type", "worker_exception")
        row["exception"] = result.get("error")
        return row
    trace = result["trace"]
    row.update({k: trace.get(k) for k in ("trace_file", "terminal", "usage", "model_fallbacks",
                                            "elapsed_ms", "event_counts")})
    searches = []
    cj_calls = []
    tool_errors = []
    for call in trace["calls"]:
        tool = call.get("tool", "")
        if tool.startswith("cj_"):
            cj_calls.append(tool)
        parsed = call.get("parsed_result")
        if (isinstance(parsed, dict) and parsed.get("error")) or str(call.get("result") or "").lstrip().startswith("[error]"):
            tool_errors.append(tool)
        if tool != "product_search_tool":
            continue
        args = call.get("args") or {}
        result_data = parsed if isinstance(parsed, dict) else {}
        hits = result_data.get("hits")
        ids = [str(hit.get("product_id")) for hit in hits] if isinstance(hits, list) else []
        anchor = case["anchor_id"]
        searches.append({"raw_query": case["query"], "normalized_query": args.get("normalized_query"),
                         "raw_category": args.get("category"),
                         "effective_category": result_data.get("query_conditions", {}).get("category"),
                         "candidate_ids": ids, "anchor_rank": ids.index(anchor) + 1 if anchor in ids else None,
                         "result_complete": isinstance(hits, list)})
    row["searches"] = searches
    row["cj_tool_calls"] = cj_calls
    row["tool_errors"] = tool_errors
    row["first_hit5"] = bool(searches and searches[0]["anchor_rank"] is not None)
    row["any_hit5"] = any(search["anchor_rank"] is not None for search in searches)
    row["final_anchor"] = case["anchor_id"] in trace.get("final_answer", "")
    row["final_answer"] = trace.get("final_answer", "")
    usage = trace.get("usage")
    if trace.get("terminal") != "RUN_FINISHED":
        row["invalid_reason"] = "run_not_finished"
    elif trace.get("model_fallbacks"):
        row["invalid_reason"] = "model_fallback"
    elif not searches or not searches[0]["normalized_query"] or not searches[0]["result_complete"]:
        row["invalid_reason"] = "search_trace_incomplete"
    elif tool_errors:
        row["invalid_reason"] = "tool_error"
    elif not isinstance(usage, dict) or any(usage.get(key) is None for key in
                                            ("model_calls", "input_tokens", "output_tokens")):
        row["invalid_reason"] = "usage_missing"
    elif usage.get("unknown_usage_calls", 0):
        row["invalid_reason"] = "usage_missing"
    else:
        row["valid"] = True
    return row


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def component40() -> dict:
    reports = {}
    for label, path in (("baseline", BASE), ("candidate", CANDIDATE)):
        target = OUTPUT / "known" / "component40" / label
        report_path = target / "baseline-all.json"
        if not report_path.exists():
            command = [sys.executable, "-m", "scripts.eval.run_cj_baseline", "--split", "all",
                       "--snapshot", str(RUN_DATA[label] / "cj_catalog.sqlite3"),
                       "--snapshot-manifest", str(ROOT / "eval/cj_v1/snapshot.json"),
                       "--cases", str(ROOT / "eval/cj_v1/retrieval_cases.jsonl"),
                       "--output-dir", str(target)]
            subprocess.run(command, cwd=path, env=env_for(label), check=True, capture_output=True,
                           text=True, encoding="utf-8")
        reports[label] = json.loads(report_path.read_text(encoding="utf-8"))
        if reports[label]["code"]["git_commit"] != git(path, "rev-parse", "HEAD"):
            raise ValueError(f"{label} component40 report commit mismatch")
    before, after = reports["baseline"], reports["candidate"]
    if before["cases_sha256"] != after["cases_sha256"] or before["snapshot"]["sha256"] != after["snapshot"]["sha256"]:
        raise ValueError("component40 inputs mismatch")
    changes = [{"id": left["id"], "baseline_pass": left["pass"], "candidate_pass": right["pass"],
                "baseline_rank": left["rank"], "candidate_rank": right["rank"],
                "baseline_ids": left["retrieved"], "candidate_ids": right["retrieved"]}
               for left, right in zip(before["observations"], after["observations"])
               if left["id"] == right["id"] and (left["pass"] != right["pass"] or left["retrieved"] != right["retrieved"])]
    if [row["id"] for row in before["observations"]] != [row["id"] for row in after["observations"]]:
        raise ValueError("component40 case order mismatch")
    result = {"baseline_pass": sum(row["pass"] for row in before["observations"]),
              "candidate_pass": sum(row["pass"] for row in after["observations"]),
              "changed_cases": changes,
              "regressions": [row["id"] for row in changes if row["baseline_pass"] and not row["candidate_pass"]]}
    write(OUTPUT / "known" / "component40" / "comparison.json", result)
    return result


async def parent(suite: str) -> None:
    cases, frozen = inputs(suite)
    manifests = prepare()
    known_report = OUTPUT / "known" / "report.json"
    if suite == "holdout":
        if not known_report.exists() or not json.loads(known_report.read_text(encoding="utf-8")).get("gate_passed"):
            raise ValueError("known-set gate has not passed; holdout remains sealed")
    output = OUTPUT / suite
    report_path = output / "report.json"
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED + (0 if suite == "known" else 1))
    order = {case["id"]: (["baseline", "candidate"] if rng.randrange(2) == 0
                           else ["candidate", "baseline"]) for case in cases}
    report = (json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else
              {"suite": suite, "started_at": datetime.now(timezone.utc).isoformat(),
               "seed": SEED + (0 if suite == "known" else 1), "order": order,
               "frozen": frozen, "manifests": manifests, "attempts": [], "pairs": []})
    if report["suite"] != suite or report["frozen"] != frozen or report["manifests"] != manifests or report["order"] != order:
        raise ValueError("cannot resume against different frozen inputs or source")
    if report.get("finished_at"):
        raise ValueError("evaluation already finished")
    completed_ids = {pair["id"] for pair in report["pairs"]}
    if report_path.exists():
        report.setdefault("interrupted_traces", [])
        recorded_traces = {item.get("trace_file") for item in report["attempts"]}
        for trace in sorted((output / "traces").glob("*.jsonl.gz")):
            if trace.name.split(".")[0] not in completed_ids and str(trace.relative_to(ROOT)) not in recorded_traces:
                relative = str(trace.relative_to(ROOT))
                if relative not in report["interrupted_traces"]:
                    report["interrupted_traces"].append(relative)
    write(report_path, report)
    if suite == "known":
        report["component40"] = component40()
        write(report_path, report)
    workers = {}
    logs = {}
    try:
        for label in ("baseline", "candidate"):
            log = (output / f"{label}.worker.log").open("w", encoding="utf-8")
            logs[label] = log
            err = (output / f"{label}.stderr.log").open("wb")
            logs[label + "_err"] = err
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), "--worker", label,
                cwd=BASE if label == "baseline" else CANDIDATE, env=env_for(label),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=err,
                limit=16 * 1024 * 1024)
            workers[label] = Child(label, process, log)
            ready = await workers[label].receive(300)
            report.setdefault("workers", {})[label] = ready
            if ready.get("status") != "ready" or ready["commit"] != manifests[label]["commit"]:
                raise ValueError(f"{label} worker failed startup identity check")
            if ready["catalog_source"] != "cj" or ready["semantic_cache"] or ready["queue"] or ready["redis"]:
                raise ValueError(f"{label} worker configuration mismatch")
            if ready["prompt"]["effective_version"]["yaml_sha256"] != manifests[label]["prompt_sha256"]:
                raise ValueError(f"{label} prompt registry version mismatch")
            write(report_path, report)
        if report["workers"]["baseline"]["model"] != report["workers"]["candidate"]["model"]:
            raise ValueError("model mismatch")
        for case in cases:
            if case["id"] in completed_ids:
                continue
            pair = None
            prior = [item["attempt"] for item in report["attempts"] if item["id"] == case["id"]]
            prior += [int(trace.name.split(".")[1]) for trace in (output / "traces").glob(
                f"{case['id']}.*.jsonl.gz") if trace.name.split(".")[1].isdigit()]
            for attempt in range(max(prior, default=0) + 1, 4):
                sides = {}
                for label in order[case["id"]]:
                    started = datetime.now(timezone.utc).isoformat()
                    result = await workers[label].run(case, attempt, suite)
                    side = summarize(case, result)
                    side["started_at"] = started
                    side["ended_at"] = datetime.now(timezone.utc).isoformat()
                    sides[label] = side
                    report["attempts"].append({"id": case["id"], "attempt": attempt,
                                               "variant": label, **side})
                    write(report_path, report)
                if all(side.get("valid") for side in sides.values()):
                    pair = {"id": case["id"], "attempt": attempt, "valid": True, **sides}
                    break
                pair = {"id": case["id"], "attempt": attempt, "valid": False, **sides}
            report["pairs"].append(pair)
            write(report_path, report)
            print(f"{case['id']}: {pair['valid']} first={pair['baseline'].get('first_hit5')}/{pair['candidate'].get('first_hit5')} "+
                  f"reason={pair['baseline'].get('invalid_reason')}/{pair['candidate'].get('invalid_reason')}", flush=True)
    finally:
        for child in workers.values():
            if child.process.stdin:
                child.process.stdin.close()
            try:
                await asyncio.wait_for(child.process.wait(), 30)
            except asyncio.TimeoutError:
                child.process.kill()
                await child.process.wait()
        for log in logs.values():
            log.close()
    scored = [pair for pair in report["pairs"] if pair["valid"]]
    report["valid_pairs"] = len(scored)
    report["metrics"] = {}
    report["regressions"] = {}
    for metric in ("first_hit5", "any_hit5", "final_anchor"):
        base = sum(pair["baseline"][metric] for pair in scored)
        cand = sum(pair["candidate"][metric] for pair in scored)
        report["metrics"][metric] = {"baseline": base, "candidate": cand, "net": cand - base,
                                      "denominator": len(scored)}
        report["regressions"][metric] = [pair["id"] for pair in scored
                                           if pair["baseline"][metric] and not pair["candidate"][metric]]
    report["usage"] = {label: {key: sum(pair[label]["usage"][key] for pair in scored)
                                for key in ("model_calls", "input_tokens", "output_tokens")}
                       for label in ("baseline", "candidate")}
    report["invalid_attempts"] = [{"id": item["id"], "variant": item["variant"],
                                    "attempt": item["attempt"], "reason": item.get("invalid_reason")}
                                   for item in report["attempts"] if not item.get("valid")]
    report["all_attempt_cost_complete"] = (not report.get("interrupted_traces") and not any(
        item.get("invalid_reason") == "usage_missing" for item in report["attempts"]))
    required = 18 if suite == "known" else 10
    threshold = 5 if suite == "known" else 2
    component_ok = suite != "known" or (report["component40"]["candidate_pass"] >=
                                         report["component40"]["baseline_pass"] and
                                         not report["component40"]["regressions"])
    report["gate_passed"] = (len(scored) == required and report["metrics"]["first_hit5"]["net"] >= threshold
                             and not report["regressions"]["final_anchor"] and component_ok)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    for label, path in (("baseline", BASE), ("candidate", CANDIDATE)):
        if git(path, "rev-parse", "HEAD") != manifests[label]["commit"] or sha(
                path / "app/application/prompts/globex.yml") != manifests[label]["prompt_sha256"]:
            raise ValueError(f"{label} changed during run")
        if sha(RUN_DATA[label] / "cj_catalog.sqlite3") != frozen["snapshot_sha256"]:
            raise ValueError(f"{label} snapshot changed during run")
    write(report_path, report)
    print(json.dumps({"valid_pairs": len(scored), "metrics": report["metrics"],
                      "regressions": report["regressions"], "gate_passed": report["gate_passed"]},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", nargs="?", choices=["known", "holdout"])
    parser.add_argument("--worker", choices=["baseline", "candidate"])
    args = parser.parse_args()
    if args.worker:
        asyncio.run(worker(args.worker))
    elif args.suite:
        asyncio.run(parent(args.suite))
    else:
        parser.error("suite or --worker is required")
