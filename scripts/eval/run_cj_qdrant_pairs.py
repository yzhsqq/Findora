"""Run frozen 40+30 CJ cases through isolated baseline/candidate Agent processes."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import random
import statistics
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "data/cj_eval/v3"
BASE = RUN / "baseline_src"
CASES = [ROOT / "eval/cj_v1/retrieval_cases.jsonl", ROOT / "eval/cj_hybrid_v3/new_cases.jsonl"]
PREFIX = "CJ_HYBRID_PAIR "
SEED = 20261001


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted((path / "app").rglob("*")):
        if file.suffix not in {".py", ".yml", ".yaml"}:
            continue
        digest.update(str(file.relative_to(path)).encode())
        digest.update(bytes.fromhex(sha(file)))
    return digest.hexdigest()


def env_for(label: str) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in dotenv_values(ROOT / ".env").items():
        if value is not None:
            env[key] = value
    env.update({
        "DATA_DIR": str(RUN / f"{label}_data"), "CATALOG_SOURCE": "cj",
        "PROMPT_PIN_VERSION": "", "QUEUE_ENABLED": "0", "REDIS_URL": "",
        "SEMANTIC_CACHE_ENABLED": "0", "DRIFT_DETECT_ENABLED": "0",
        "DATABASE_URL": "", "MYSQL_URL": "", "HYBRID_RECALL_ENABLED": "1",
        "QDRANT_URL": "" if label == "baseline" else "http://127.0.0.1:6333",
        "QDRANT_COLLECTION": "globex_products" if label == "baseline" else "globex_products_cj_hybrid",
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
    })
    env.pop("CJdropshipping_key", None)
    return env


def compact(case: dict, result: dict) -> dict:
    row = {"id": case["id"], "status": result["status"]}
    if result["status"] != "ok":
        return {**row, "invalid_reason": result.get("error_type", "exception"),
                "error": result.get("error")}
    trace = result["trace"]
    searches = []
    errors = []
    for call in trace["calls"]:
        parsed = call.get("parsed_result")
        if isinstance(parsed, dict) and parsed.get("error"):
            errors.append({"tool": call.get("tool"), "error": parsed["error"]})
        if call.get("tool") != "product_search_tool":
            continue
        hits = parsed.get("hits") if isinstance(parsed, dict) else None
        ids = [str(hit.get("product_id")) for hit in hits] if isinstance(hits, list) else []
        searches.append({"query": (call.get("args") or {}).get("normalized_query"),
                         "category": (call.get("args") or {}).get("category"),
                         "ids": ids, "complete": isinstance(hits, list)})
    anchor = str(case["relevant"][0]) if case["relevant"] else None
    ranks = [s["ids"].index(anchor) + 1 if anchor in s["ids"] else None
             for s in searches] if anchor else []
    usage = trace.get("usage")
    row.update({"terminal": trace.get("terminal"), "elapsed_ms": trace.get("elapsed_ms"),
                "searches": searches, "first_rank": ranks[0] if ranks else None,
                "any_hit5": any(rank is not None for rank in ranks),
                "final_anchor": bool(anchor and anchor in trace.get("final_answer", "")),
                "final_answer": trace.get("final_answer", ""), "usage": usage,
                "model_fallbacks": trace.get("model_fallbacks"), "tool_errors": errors,
                "trace_file": trace.get("trace_file")})
    if trace.get("terminal") != "RUN_FINISHED":
        row["invalid_reason"] = "run_not_finished"
    elif trace.get("model_fallbacks"):
        row["invalid_reason"] = "model_fallback"
    elif not searches or not searches[0]["complete"]:
        row["invalid_reason"] = "search_trace_incomplete"
    elif errors:
        row["invalid_reason"] = "tool_error"
    elif not isinstance(usage, dict) or any(usage.get(k) is None for k in
                                            ("model_calls", "input_tokens", "output_tokens")):
        row["invalid_reason"] = "usage_missing"
    elif usage.get("unknown_usage_calls"):
        row["invalid_reason"] = "usage_missing"
    else:
        row["valid"] = True
    return row


async def worker(label: str) -> None:
    source = BASE if label == "baseline" else ROOT
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location("cj_trace_adapter", ROOT / "scripts/eval/run_cj_crosslang.py")
    adapter = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(adapter)
    from app.composition import build_container

    container = await build_container()
    await container.startup()
    settings = container.settings
    print(PREFIX + json.dumps({"status": "ready", "label": label,
                               "source_hash": code_hash(source),
                               "catalog_hash": sha(settings.data_dir / "cj_catalog.sqlite3"),
                               "qdrant_url": settings.qdrant_url,
                               "collection": container.runtime.get("product_vector_collection"),
                               "prompt": container.prompt_registry.describe(),
                               "model": settings.llm_model,
                               "semantic_cache": settings.semantic_cache_enabled,
                               "queue": settings.queue_enabled}, ensure_ascii=True), flush=True)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            case, attempt = request["case"], request["attempt"]
            trace_path = RUN / "traces" / f"{case['id']}.{attempt}.{label}.jsonl.gz"
            try:
                trace = await adapter.direct_agent_trace(container, case, trace_path)
                result = {"status": "ok", "trace": trace}
            except BaseException as error:
                result = {"status": "exception", "error_type": type(error).__name__,
                          "error": str(error), "traceback": traceback.format_exc()[-4000:]}
            print(PREFIX + json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        await container.shutdown()


async def receive(process: asyncio.subprocess.Process, log, timeout: float = 300) -> dict:
    while True:
        raw = await asyncio.wait_for(process.stdout.readline(), timeout)
        if not raw:
            raise RuntimeError(f"worker exited with {process.returncode}")
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith(PREFIX):
            return json.loads(line[len(PREFIX):])
        log.write(line + "\n")
        log.flush()


async def run_side(process, log, case: dict, attempt: int) -> dict:
    process.stdin.write((json.dumps({"case": case, "attempt": attempt}, ensure_ascii=True) + "\n").encode())
    await process.stdin.drain()
    return compact(case, await receive(process, log))


def write(report: dict) -> None:
    target = RUN / "pairs.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summarize(report: dict) -> None:
    pairs = [p for p in report["pairs"] if p["valid"]]
    positives = [p for p in pairs if p["anchor"]]
    report["valid_pairs"] = len(pairs)
    report["metrics"] = {}
    report["regressions"] = {}
    for key in ("first_hit5", "any_hit5", "final_anchor"):
        get = (lambda row: row["first_rank"] is not None) if key == "first_hit5" else (lambda row: row[key])
        report["metrics"][key] = {"baseline": sum(get(p["baseline"]) for p in positives),
                                  "candidate": sum(get(p["candidate"]) for p in positives),
                                  "denominator": len(positives)}
        report["regressions"][key] = [p["id"] for p in positives if get(p["baseline"]) and not get(p["candidate"])]
    for key in ("model_calls", "input_tokens", "output_tokens"):
        report.setdefault("usage", {})[key] = {
            label: sum(p[label]["usage"][key] for p in pairs) for label in ("baseline", "candidate")}
    report["p95_ms"] = {label: round(sorted(p[label]["elapsed_ms"] for p in pairs)[
        max(0, int(0.95 * len(pairs) + 0.999999) - 1)], 2) if pairs else None
        for label in ("baseline", "candidate")}
    report["invalid_pairs"] = [{"id": p["id"], "baseline": p["baseline"].get("invalid_reason"),
                                "candidate": p["candidate"].get("invalid_reason")}
                               for p in report["pairs"] if not p["valid"]]


async def parent(limit: int | None) -> None:
    cases = [json.loads(line) for path in CASES for line in path.read_text(encoding="utf-8").splitlines() if line]
    if len(cases) != 70 or len({c["id"] for c in cases}) != 70:
        raise ValueError("expected 70 unique cases")
    if limit is not None:
        cases = cases[:limit]
    frozen = {"baseline_commit": "1f2a2a71bd948322047ef5c215631b28f0b9f55f",
              "baseline_zip_sha256": sha(RUN / "baseline.zip"),
              "snapshot_sha256": sha(RUN / "catalog.sqlite3"),
              "cases_sha256": [sha(path) for path in CASES],
              "source_hash": {"baseline": code_hash(BASE), "candidate": code_hash(ROOT)}}
    for label in ("baseline", "candidate"):
        if sha(RUN / f"{label}_data/cj_catalog.sqlite3") != frozen["snapshot_sha256"]:
            raise ValueError(f"{label} snapshot mismatch")
    order_rng = random.Random(SEED)
    order = {case["id"]: (["baseline", "candidate"] if order_rng.randrange(2) else
                          ["candidate", "baseline"]) for case in cases}
    path = RUN / "pairs.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "started_at": datetime.now(timezone.utc).isoformat(), "frozen": frozen,
        "order": order, "pairs": [], "attempts": []}
    if report["frozen"] != frozen or report["order"] != order:
        raise ValueError("cannot resume with changed inputs or code")
    workers, logs = {}, {}
    try:
        for label in ("baseline", "candidate"):
            out = (RUN / f"{label}.worker.log").open("a", encoding="utf-8")
            err = (RUN / f"{label}.stderr.log").open("ab")
            logs[label], logs[label + "_err"] = out, err
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), "--worker", label,
                cwd=BASE if label == "baseline" else ROOT, env=env_for(label),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=err, limit=16 * 1024 * 1024)
            workers[label] = process
            ready = await receive(process, out)
            if (ready.get("status") != "ready" or ready["source_hash"] != frozen["source_hash"][label]
                    or ready["catalog_hash"] != frozen["snapshot_sha256"]
                    or ready["semantic_cache"] or ready["queue"]):
                raise ValueError(f"{label} worker identity/config mismatch: {ready}")
            report.setdefault("workers", {})[label] = ready
            write(report)
        if report["workers"]["baseline"]["model"] != report["workers"]["candidate"]["model"]:
            raise ValueError("model mismatch")
        completed = {p["id"] for p in report["pairs"]}
        for case in cases:
            if case["id"] in completed:
                continue
            pair = None
            for attempt in range(1, 4):
                sides = {}
                for label in order[case["id"]]:
                    side = await run_side(workers[label], logs[label], case, attempt)
                    sides[label] = side
                    report["attempts"].append({"id": case["id"], "attempt": attempt,
                                               "variant": label, **side})
                    write(report)
                pair = {"id": case["id"], "anchor": case["relevant"][0] if case["relevant"] else None,
                        "attempt": attempt, "valid": all(s.get("valid") for s in sides.values()), **sides}
                if pair["valid"]:
                    break
            report["pairs"].append(pair)
            write(report)
            print(f"{len(report['pairs'])}/{len(cases)} {case['id']} valid={pair['valid']} "
                  f"first={pair['baseline'].get('first_rank')}/{pair['candidate'].get('first_rank')} "
                  f"reason={pair['baseline'].get('invalid_reason')}/{pair['candidate'].get('invalid_reason')}",
                  flush=True)
    finally:
        for process in workers.values():
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 30)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for log in logs.values():
            log.close()
    if frozen["source_hash"] != {"baseline": code_hash(BASE), "candidate": code_hash(ROOT)}:
        raise ValueError("source changed during evaluation")
    summarize(report)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write(report)
    print(json.dumps({"valid_pairs": report["valid_pairs"], "metrics": report["metrics"],
                      "regressions": report["regressions"], "p95_ms": report["p95_ms"]},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("baseline", "candidate"))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    asyncio.run(worker(args.worker) if args.worker else parent(args.limit))
