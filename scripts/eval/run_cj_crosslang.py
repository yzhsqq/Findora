"""Frozen CJ cross-language diagnostic: live Agent trace plus offline replay.

The dev set is the only supported input in this runner. Holdout execution is a
separate, explicitly gated step after the implementation has been frozen.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets

from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.persistence.cj_catalog import CJCatalog
from scripts.eval.run_manifest import worktree_fingerprint


ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "eval/cj_crosslang_v1"
SNAPSHOT = ROOT / "data/cj_eval/v1/catalog.sqlite3"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def frozen_inputs() -> tuple[list[dict], dict]:
    frozen = json.loads((SUITE / "freeze.json").read_text(encoding="utf-8"))
    for path, key in (
        (SNAPSHOT, "snapshot_sha256"),
        (SUITE / "dev_cases.jsonl", "dev_cases_sha256"),
        (SUITE / "holdout_cases.jsonl", "holdout_cases_sha256"),
    ):
        if sha256(path) != frozen[key]:
            raise ValueError(f"frozen input changed: {path}")
    cases = [json.loads(line) for line in (SUITE / "dev_cases.jsonl").read_text(encoding="utf-8").splitlines()]
    if len(cases) != 8:
        raise ValueError("expected eight development cases")
    old = {row["id"]: row for line in (ROOT / "eval/cj_v1/retrieval_cases.jsonl").read_text(encoding="utf-8").splitlines()
           if (row := json.loads(line))["id"] in {case["id"] for case in cases}}
    for case in cases:
        original = old[case["id"]]
        if original["query"] != case["query"] or original["relevant"] != [case["anchor_id"]]:
            raise ValueError(f"old annotation mismatch: {case['id']}")
    return cases, frozen


async def agent_trace(client: httpx.AsyncClient, base_url: str, case: dict, trace_path: Path) -> dict:
    thread_id, run_id = f"cjcross-{uuid.uuid4().hex}", f"run-{uuid.uuid4().hex}"
    request = {
        "threadId": thread_id, "runId": run_id, "state": {}, "tools": [], "context": [],
        "forwardedProps": {"buyerId": "cj-crosslang-eval", "locale": "zh-CN", "currency": "CNY"},
        "messages": [{"id": f"user-{uuid.uuid4().hex}", "role": "user",
                      "content": f"仅做商品检索并展示候选，不执行交易、报价或偏好写入。需求：{case['query']}"}],
    }
    started = time.perf_counter()
    calls: dict[str, dict] = {}
    call_order: list[str] = []
    final_answer = ""
    terminal = None
    fallback = []
    event_counts: dict[str, int] = {}
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    async def usage_stream(ws):
        while True:
            event = json.loads(await ws.recv())
            if event.get("type") == "usage.summary":
                return event.get("payload")

    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/commerce/events"
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"buyer_id": "cj-crosslang-eval", "shopping_session_id": thread_id}))
        usage_task = asyncio.create_task(usage_stream(ws))
        await asyncio.sleep(0.1)
        with gzip.open(trace_path, "wt", encoding="utf-8") as trace:
            async with client.stream("POST", base_url.rstrip("/") + "/commerce/ag-ui/run", json=request) as response:
                response.raise_for_status()
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line:
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip(" "))
                        continue
                    if not data_lines:
                        continue
                    event = json.loads("\n".join(data_lines))
                    data_lines.clear()
                    trace.write(json.dumps(event, ensure_ascii=False) + "\n")
                    kind = event.get("type", "")
                    event_counts[kind] = event_counts.get(kind, 0) + 1
                    call_id = event.get("toolCallId")
                    if kind == "TOOL_CALL_START":
                        calls[call_id] = {"tool": event.get("toolCallName"), "argument_chunks": [], "result": None}
                        call_order.append(call_id)
                    elif kind == "TOOL_CALL_ARGS" and call_id in calls:
                        calls[call_id]["argument_chunks"].append(event.get("delta", ""))
                    elif kind == "TOOL_CALL_RESULT" and call_id in calls:
                        calls[call_id]["result"] = event.get("content")
                    elif kind == "MESSAGES_SNAPSHOT":
                        assistants = [m for m in event.get("messages", []) if m.get("role") == "assistant"]
                        if assistants:
                            final_answer = str(assistants[-1].get("content") or "")
                    elif kind in {"RUN_FINISHED", "RUN_ERROR"}:
                        terminal = kind
                    elif kind == "CUSTOM" and event.get("name") == "model.fallback":
                        fallback.append(event.get("value"))
                if data_lines:
                    raise ValueError("incomplete SSE event")
        try:
            usage = await asyncio.wait_for(usage_task, 3)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            usage = None
            usage_task.cancel()
    ordered = []
    for call_id in call_order:
        call = calls[call_id]
        raw_args = "".join(call.pop("argument_chunks"))
        try:
            call["args"] = json.loads(raw_args)
        except ValueError:
            call["args"] = None
        try:
            call["parsed_result"] = json.loads(call["result"]) if call["result"] else None
        except ValueError:
            call["parsed_result"] = None
        call["tool_call_id"] = call_id
        ordered.append(call)
    return {
        "thread_id": thread_id, "run_id": run_id, "trace_file": str(trace_path.relative_to(ROOT)),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "terminal": terminal, "event_counts": event_counts, "model_fallbacks": fallback,
        "calls": ordered, "final_answer": final_answer, "usage": usage,
    }


async def direct_agent_trace(container, case: dict, trace_path: Path) -> dict:
    """Run the same main orchestrator without the AG-UI journal transport."""
    from ag_ui.core import RunAgentInput
    from app.infrastructure.eventbus import observe_run_events
    from app.presentation.ag_ui import parse_intent, stream_run

    thread_id, run_id = f"cjcross-{uuid.uuid4().hex}", f"run-{uuid.uuid4().hex}"
    request = {
        "threadId": thread_id, "runId": run_id, "state": {}, "tools": [], "context": [],
        "forwardedProps": {"buyerId": "cj-crosslang-eval", "locale": "zh-CN", "currency": "CNY"},
        "messages": [{"id": f"user-{uuid.uuid4().hex}", "role": "user",
                      "content": f"仅做商品检索并展示候选，不执行交易、报价或偏好写入。需求：{case['query']}"}],
    }
    body = RunAgentInput.model_validate(request)
    intent = parse_intent(body)
    usage = None
    def on_trade(event):
        nonlocal usage
        if event.type == "usage.summary":
            usage = event.payload
    started = time.perf_counter()
    events = []
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(trace_path, "wt", encoding="utf-8") as trace:
        with observe_run_events(on_trade):
            async for packet in stream_run(container.orchestrator, body, intent):
                for line in packet.splitlines():
                    if line.startswith("data:"):
                        event = json.loads(line[5:].lstrip(" "))
                        events.append(event)
                        trace.write(json.dumps(event, ensure_ascii=False) + "\n")
    calls: dict[str, dict] = {}
    call_order = []
    answer = ""
    terminal = None
    counts: dict[str, int] = {}
    fallbacks = []
    for event in events:
        kind = event.get("type", "")
        counts[kind] = counts.get(kind, 0) + 1
        call_id = event.get("toolCallId")
        if kind == "TOOL_CALL_START":
            calls[call_id] = {"tool": event.get("toolCallName"), "argument_chunks": [], "result": None}
            call_order.append(call_id)
        elif kind == "TOOL_CALL_ARGS" and call_id in calls:
            calls[call_id]["argument_chunks"].append(event.get("delta", ""))
        elif kind == "TOOL_CALL_RESULT" and call_id in calls:
            calls[call_id]["result"] = event.get("content")
        elif kind == "MESSAGES_SNAPSHOT":
            assistants = [m for m in event.get("messages", []) if m.get("role") == "assistant"]
            if assistants:
                answer = str(assistants[-1].get("content") or "")
        elif kind in {"RUN_FINISHED", "RUN_ERROR"}:
            terminal = kind
        elif kind == "CUSTOM" and event.get("name") == "model.fallback":
            fallbacks.append(event.get("value"))
    ordered = []
    for call_id in call_order:
        call = calls[call_id]
        try:
            call["args"] = json.loads("".join(call.pop("argument_chunks")))
        except ValueError:
            call["args"] = None
        try:
            call["parsed_result"] = json.loads(call["result"]) if call["result"] else None
        except ValueError:
            call["parsed_result"] = None
        call["tool_call_id"] = call_id
        ordered.append(call)
    return {"thread_id": thread_id, "run_id": run_id, "trace_file": str(trace_path.relative_to(ROOT)),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2), "terminal": terminal,
            "event_counts": counts, "model_fallbacks": fallbacks, "calls": ordered,
            "final_answer": answer, "usage": usage}


async def replay(catalog: CJCatalog, query: str, category: str | None, anchor: str) -> dict:
    result = await catalog.execute(ProductSearchSpec(
        normalized_query=query, category=category, top_k=5, target_currency="USD"))
    ids = [item["product_id"] for item in result["hits"]]
    return {"query": query, "category": category, "ids": ids,
            "rank": ids.index(anchor) + 1 if anchor in ids else None,
            "total_candidates": result["total_candidates"]}


def metrics(observations: list[dict], key: str) -> dict:
    rows = [row["replays"][key] for row in observations if row["replays"].get(key)]
    ranks = [row["rank"] for row in rows]
    return {"n": len(rows), "hit_at_1": sum(rank == 1 for rank in ranks),
            "hit_at_3": sum(rank is not None and rank <= 3 for rank in ranks),
            "hit_at_5": sum(rank is not None and rank <= 5 for rank in ranks),
            "mrr": round(sum(1 / rank if rank else 0 for rank in ranks) / len(rows), 4) if rows else None}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "eval/verification/cj-crosslang-v1/baseline")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--resume", action="store_true", help="Reuse successful cases in an existing report")
    parser.add_argument("--direct", action="store_true", help="Use the main orchestrator directly, bypassing AG-UI journal")
    args = parser.parse_args()
    cases, frozen = frozen_inputs()
    if not 1 <= args.limit <= len(cases):
        parser.error("--limit must be 1..8")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = worktree_fingerprint(ROOT)
    catalog = CJCatalog(SNAPSHOT)
    report_path = output / "report.json"
    previous = json.loads(report_path.read_text(encoding="utf-8")) if args.resume and report_path.exists() else {}
    if previous and previous.get("frozen") != frozen:
        raise ValueError("cannot resume against different frozen inputs")
    observations = [row for row in previous.get("observations", []) if row["agent"]["terminal"] == "RUN_FINISHED"]
    if args.direct:
        from app.composition import build_container
        container = await build_container()
        await container.startup()
        health = {"runtime": container.runtime, "catalog_source": container.settings.catalog_source,
                  "transport": "direct_main_orchestrator"}
    else:
        container = None
    try:
      async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=10)) as client:
        if not args.direct:
            health = (await client.get(args.base_url.rstrip("/") + "/health")).json()
        if health.get("runtime", {}).get("catalog_source") != "cj":
            raise ValueError("Agent server does not use CJ catalog")
        for case in cases[:args.limit]:
            if any(row["id"] == case["id"] for row in observations):
                continue
            run_one = (lambda path: direct_agent_trace(container, case, path)) if args.direct else (
                lambda path: agent_trace(client, args.base_url, case, path))
            trace = await run_one(output / "traces" / f"{case['id']}.jsonl.gz")
            if trace["terminal"] != "RUN_FINISHED":
                failed_trace = trace
                trace = await run_one(output / "traces" / f"{case['id']}.retry.jsonl.gz")
                trace["prior_failed_attempt"] = {"terminal": failed_trace["terminal"],
                                                 "trace_file": failed_trace["trace_file"],
                                                 "usage": failed_trace["usage"]}
            searches = [call for call in trace["calls"] if call["tool"] == "product_search_tool"]
            first = searches[0] if searches else None
            conditions = first["parsed_result"].get("query_conditions", {}) if first and isinstance(first["parsed_result"], dict) else {}
            query = conditions.get("normalized_query") or (first["args"] or {}).get("normalized_query") if first else None
            category = conditions.get("category") if first else None
            replays = {}
            for label, text in (("original", case["query"]), ("ideal", case["ideal_query"]), ("agent", query)):
                if not text:
                    continue
                replays[f"{label}_off"] = await replay(catalog, text, None, case["anchor_id"])
                if category:
                    replays[f"{label}_on"] = await replay(catalog, text, category, case["anchor_id"])
            agent_ids = []
            if first and isinstance(first["parsed_result"], dict):
                agent_ids = [str(hit.get("product_id")) for hit in first["parsed_result"].get("hits", [])]
            all_searches = []
            for search in searches:
                result = search["parsed_result"] if isinstance(search["parsed_result"], dict) else {}
                ids = [str(hit.get("product_id")) for hit in result.get("hits", [])]
                all_searches.append({"query": (search["args"] or {}).get("normalized_query"),
                                     "category": result.get("query_conditions", {}).get("category"),
                                     "ids": ids, "rank": ids.index(case["anchor_id"]) + 1 if case["anchor_id"] in ids else None})
            usage = trace["usage"] or {}
            observations.append({
                "id": case["id"], "stratum": case["stratum"], "anchor_id": case["anchor_id"],
                "agent": {"trace_file": trace["trace_file"], "terminal": trace["terminal"],
                          "transport": "direct_main_orchestrator" if args.direct else "ag_ui_http",
                          "elapsed_ms": trace["elapsed_ms"], "event_counts": trace["event_counts"],
                          "model_fallbacks": trace["model_fallbacks"], "call_count": len(trace["calls"]),
                          "prior_failed_attempt": trace.get("prior_failed_attempt"),
                          "tool_names": [call["tool"] for call in trace["calls"]],
                          "search_call_count": len(searches), "first_query": query,
                          "first_category": category, "first_ids": agent_ids,
                          "first_rank": agent_ids.index(case["anchor_id"]) + 1 if case["anchor_id"] in agent_ids else None,
                          "all_searches": all_searches,
                          "any_search_hit_at_5": any(search["rank"] is not None for search in all_searches),
                          "answer": trace["final_answer"],
                          "model_calls": usage.get("model_calls"), "input_tokens": usage.get("input_tokens"),
                          "output_tokens": usage.get("output_tokens"), "usage": trace["usage"]},
                "replays": replays,
            })
            print(f"{case['id']}: agent query={query!r} category={category!r} rank={observations[-1]['agent']['first_rank']}", flush=True)
            write_report(report_path, observations, frozen, source, health)
    finally:
        if container is not None:
            await container.shutdown()
    if sha256(SNAPSHOT) != frozen["snapshot_sha256"] or worktree_fingerprint(ROOT)["sha256"] != source["sha256"]:
        raise ValueError("snapshot or source changed during run")
    write_report(report_path, observations, frozen, source, health)


def write_report(path: Path, observations: list[dict], frozen: dict, source: dict, health: dict) -> None:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
    labels = ("original_off", "original_on", "agent_off", "agent_on", "ideal_off", "ideal_on")
    report = {"run_at": datetime.now(timezone.utc).isoformat(), "code": {"commit": commit, "worktree": source},
              "frozen": frozen, "server_health": health, "observations": observations,
              "metrics": {label: metrics(observations, label) for label in labels},
              "agent_first": {"n": len(observations),
                              "hit_at_1": sum(row["agent"]["first_rank"] == 1 for row in observations),
                              "hit_at_3": sum((row["agent"]["first_rank"] or 99) <= 3 for row in observations),
                              "hit_at_5": sum(row["agent"]["first_rank"] is not None for row in observations),
                              "mrr": round(sum(1 / row["agent"]["first_rank"] if row["agent"]["first_rank"] else 0 for row in observations) / len(observations), 4)}}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
