"""Run the CJ quote pilot with an isolated prompt registry and session store.

Copy data/cj_catalog.sqlite3 to data/cj_pilot/cj_catalog.sqlite3 first. The
existing data/prompts registry keeps its pinned production tool contract.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PILOT_DATA = ROOT / "data" / "cj_pilot"


def main() -> None:
    if not (PILOT_DATA / "cj_catalog.sqlite3").is_file():
        raise SystemExit("先将现有 CJ 快照复制到 data/cj_pilot/cj_catalog.sqlite3")
    os.environ["DATA_DIR"] = str(PILOT_DATA)
    os.environ["PROMPT_PIN_VERSION"] = ""
    sys.path.insert(0, str(ROOT))
    import uvicorn

    uvicorn.run("app.presentation.server:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
