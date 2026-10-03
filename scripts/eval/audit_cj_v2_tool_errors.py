"""Audit v2 raw traces for tool errors missed by the first report parser."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "eval/verification/cj-crosslang-v2"


def errors(trace_path: Path) -> list[str]:
    calls = {}
    found = []
    with gzip.open(trace_path, "rt", encoding="utf-8") as file:
        for line in file:
            event = json.loads(line)
            call_id = event.get("toolCallId")
            if event.get("type") == "TOOL_CALL_START":
                calls[call_id] = event.get("toolCallName")
            elif event.get("type") == "TOOL_CALL_RESULT":
                content = event.get("content")
                if str(content or "").lstrip().startswith("[error]") or isinstance(content, dict) and content.get("error"):
                    found.append(calls.get(call_id, "unknown_tool"))
    return found


def main() -> None:
    findings = []
    for suite in ("known", "holdout"):
        path = OUTPUT / suite / "report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        for attempt in report["attempts"]:
            trace = attempt.get("trace_file")
            if not trace:
                continue
            tool_errors = errors(ROOT / trace)
            if tool_errors:
                findings.append({"suite": suite, "id": attempt["id"], "attempt": attempt["attempt"],
                                 "variant": attempt["variant"], "tool_errors": tool_errors,
                                 "trace_file": trace, "old_valid": attempt.get("valid", False)})
                if not attempt.get("valid"):
                    continue
                attempt["valid"] = False
                attempt["invalid_reason"] = "tool_error"
                attempt["tool_errors"] = tool_errors
        affected = {item["id"] for item in findings if item["suite"] == suite and item["old_valid"]}
        if not affected:
            continue
        archive = path.with_name("report.pre_tool_error_audit.json")
        if archive.exists():
            raise ValueError(f"audit already applied: {archive}")
        raw = path.read_bytes()
        archive.write_bytes(raw)
        report["pairs"] = [pair for pair in report["pairs"] if pair["id"] not in affected]
        for key in ("finished_at", "gate_passed", "valid_pairs", "metrics", "regressions", "usage",
                    "invalid_attempts", "all_attempt_cost_complete"):
            report.pop(key, None)
        report["tool_error_audit"] = {"previous_report_sha256": hashlib.sha256(raw).hexdigest(),
                                      "affected_ids": sorted(affected)}
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "tool_error_audit.json").write_text(json.dumps({"findings": findings}, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
    print(json.dumps(findings, ensure_ascii=False))


if __name__ == "__main__":
    main()
