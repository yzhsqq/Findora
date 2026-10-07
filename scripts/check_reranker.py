"""Small real-service preflight; never prints credentials or raw responses."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.infrastructure.rerank.http_reranker import HttpReranker
from app.infrastructure.settings import load_settings


async def main() -> int:
    settings = load_settings()
    if not settings.reranker_base_url or not settings.reranker_model:
        print(json.dumps({"status": "not_configured"}))
        return 1
    started = time.perf_counter()
    try:
        scores = await HttpReranker(settings).rerank(
            "轻便旅行背包",
            ["Lightweight travel backpack, 20L capacity", "Ceramic coffee mug for home use"],
        )
    except httpx.HTTPStatusError as error:
        print(json.dumps({"status": "http_error", "http_status": error.response.status_code}))
        return 1
    except Exception as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__}))
        return 1
    passed = len(scores) == 2 and scores[0] > scores[1]
    print(json.dumps({
        "status": "pass" if passed else "unexpected_order",
        "model": settings.reranker_model,
        "scores": scores,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "scope": "two-document connectivity check; not a catalog quality benchmark",
    }))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
