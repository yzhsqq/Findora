"""Build a resumable Chinese SQLite projection without modifying CJ source facts.

Uses the configured OpenAI-compatible LLM only during this offline command.
The completed database is promoted atomically; an interrupted .build file resumes.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.persistence.cj_localization import (  # noqa: E402
    FIRST_CATEGORY_ZH, LOCALIZATION_VERSION, init_localization_db,
    source_fingerprint, upsert_localization,
)

_HAN = re.compile(r"[\u3400-\u9fff]")
_TAGS = re.compile(r"<[^>]+>")
_ATTRIBUTE_EVIDENCE = (
    (re.compile(r"防晒|防紫外"), re.compile(r"sun|uv|spf|shade", re.I)),
    (re.compile(r"保湿|补水"), re.compile(r"moistur|hydrat", re.I)),
    (re.compile(r"灰色"), re.compile(r"gray|grey", re.I)),
    (re.compile(r"白色"), re.compile(r"white", re.I)),
    (re.compile(r"加厚"), re.compile(r"thick|padded|fleece|thermal|insulat", re.I)),
    (re.compile(r"特步"), re.compile(r"xtep", re.I)),
)


def _plain(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub(" ", str(value or "")))).strip()[:210]


def _supported_keywords(keywords: list[str], listing: dict) -> list[str]:
    evidence = str(listing.get("nameEn") or "") + " " + _plain(listing.get("description"))
    return [word for word in keywords if all(
        not chinese.search(word) or english.search(evidence)
        for chinese, english in _ATTRIBUTE_EVIDENCE
    )]


def _json_object(content: str) -> dict:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM did not return a JSON object")
    value = json.loads(content[start:end + 1])
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("LLM returned an invalid items object")
    return value


class Translator:
    def __init__(self, base_url: str, api_key: str, model: str, concurrency: int):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key = api_key
        self.model = model
        self.deepseek = "deepseek.com" in base_url.lower()
        self.semaphore = asyncio.Semaphore(concurrency)

    async def _call(self, client: httpx.AsyncClient, instruction: str, items: list[dict]) -> list[dict]:
        payload = {
            "model": self.model, "temperature": 0,
            "max_tokens": min(7500, max(1000, len(items) * 180)),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        if self.deepseek:
            payload["thinking"] = {"type": "disabled"}
        for attempt in range(5):
            try:
                async with self.semaphore:
                    response = await client.post(
                        self.url, headers={"Authorization": "Bearer " + self.key}, json=payload,
                    )
                if response.status_code in (408, 429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=response.request, response=response)
                response.raise_for_status()
                result = _json_object(response.json()["choices"][0]["message"]["content"])["items"]
                found = {str(item.get("id")): item for item in result if isinstance(item, dict)}
                if set(found) != {str(item["id"]) for item in items}:
                    raise ValueError("LLM omitted or added item IDs")
                return [found[str(item["id"])] for item in items]
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError, KeyError, IndexError, json.JSONDecodeError):
                if 'response' in locals() and response.status_code in (401, 402, 403):
                    raise RuntimeError(f"translation provider rejected the request: HTTP {response.status_code}")
                if attempt == 4:
                    if len(items) > 1:
                        middle = len(items) // 2
                        left = await self._call(client, instruction, items[:middle])
                        right = await self._call(client, instruction, items[middle:])
                        return left + right
                    raise
                await asyncio.sleep(min(2 ** attempt, 12))
        raise AssertionError("unreachable")

    async def categories(self, client: httpx.AsyncClient, items: list[dict]) -> list[dict]:
        instruction = (
            "Translate CJ category paths into concise natural Simplified Chinese. "
            "Return JSON object {items:[{id,second_zh,third_zh}]}. "
            "Keep each path's meaning; do not invent a different category. No markdown."
        )
        result = await self._call(client, instruction, items)
        if any(not _HAN.search(str(x.get("second_zh", ""))) or not _HAN.search(str(x.get("third_zh", ""))) for x in result):
            raise ValueError("category response lacks Chinese")
        return result

    async def products(self, client: httpx.AsyncClient, items: list[dict]) -> list[dict]:
        instruction = (
            "Localize CJ product listings for Chinese shoppers. Return one JSON object "
            "{items:[{id,title_zh,summary_zh,keywords_zh}]}. "
            "title_zh is a concise natural Simplified Chinese product title. Preserve brand, "
            "model, quantity, color, material and measurements exactly when supplied; never "
            "invent a brand, feature or medical claim. summary_zh is one short Chinese sentence "
            "grounded in the input description, or an empty string if it adds no fact. "
            "keywords_zh is 2-5 common Chinese product names or important searchable "
            "attributes supported by input; include a common category name such as 帽子, "
            "手机壳, 洗面奶, 运动鞋, 睡袋 where applicable. Do not add unrelated broad categories. "
            "Keep IDs unchanged, return every item exactly once, no markdown."
        )
        result = await self._call(client, instruction, items)
        for item in result:
            if not isinstance(item.get("keywords_zh"), list):
                item["keywords_zh"] = []
        return result


def _path_key(row: sqlite3.Row) -> str:
    return json.dumps([row["first_category"], row["second_category"], row["third_category"]], ensure_ascii=False)


async def build(args: argparse.Namespace) -> None:
    env = dotenv_values(ROOT / ".env")
    base_url = os.getenv(args.base_url_env) or env.get(args.base_url_env)
    api_key = os.getenv(args.api_key_env) or env.get(args.api_key_env)
    model = args.model or os.getenv("LLM_MODEL") or env.get("LLM_MODEL")
    if not base_url or not api_key or not model:
        raise RuntimeError("LLM_BASE_URL, LLM_API_KEY and LLM_MODEL are required")
    source_path = args.source.resolve()
    output_path = args.output.resolve()
    if source_path == output_path:
        raise ValueError("source and output must differ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    building_path = output_path.with_name(output_path.name + ".build")
    if not building_path.is_file() and output_path.is_file():
        shutil.copy2(output_path, building_path)
    with sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        rows = source.execute(
            "SELECT pid,first_category,second_category,third_category,list_json FROM products ORDER BY pid"
        ).fetchall()
    # Every pid in the snapshot, captured before --limit truncates `rows`, so the
    # prune below still sees a product that left the source.
    source_ids = {str(row["pid"]) for row in rows}
    if args.limit:
        rows = rows[:args.limit]
    translator = Translator(base_url, api_key, model, args.concurrency)
    with sqlite3.connect(building_path) as db:
        db.row_factory = sqlite3.Row
        init_localization_db(db)
        category_rows = db.execute("SELECT path_key,second_zh,third_zh FROM category_translations").fetchall()
        categories = {r[0]: (r[1], r[2]) for r in category_rows}
        missing_paths = {}
        for row in rows:
            key = _path_key(row)
            if key not in categories:
                missing_paths[key] = row
        limits = httpx.Limits(max_connections=args.concurrency + 2)
        async with httpx.AsyncClient(timeout=120, limits=limits) as client:
            paths = list(missing_paths)
            for start in range(0, len(paths), 30):
                batch = paths[start:start + 30]
                items = [{"id": str(i), "first": missing_paths[key]["first_category"],
                          "second": missing_paths[key]["second_category"],
                          "third": missing_paths[key]["third_category"]}
                         for i, key in enumerate(batch)]
                result = await translator.categories(client, items)
                with db:
                    for key, item in zip(batch, result):
                        pair = (str(item["second_zh"]).strip(), str(item["third_zh"]).strip())
                        db.execute("INSERT OR REPLACE INTO category_translations VALUES(?,?,?)", (key, *pair))
                        categories[key] = pair
                print(f"categories {min(start + 30, len(paths))}/{len(paths)}", flush=True)

            versions = {r[0]: r[1] for r in db.execute("SELECT pid,source_fingerprint FROM localized_products")}
            pending = [row for row in rows if versions.get(row["pid"]) != source_fingerprint(row)]
            print(f"products total={len(rows)} pending={len(pending)}", flush=True)
            for start in range(0, len(pending), args.batch_size * args.concurrency):
                wave = pending[start:start + args.batch_size * args.concurrency]
                batches = [wave[i:i + args.batch_size] for i in range(0, len(wave), args.batch_size)]
                requests = []
                for batch in batches:
                    requests.append(translator.products(client, [
                        {"id": row["pid"], "title": json.loads(row["list_json"]).get("nameEn", ""),
                         "description": _plain(json.loads(row["list_json"]).get("description")),
                         "category": row["third_category"]}
                        for row in batch
                    ]))
                results = await asyncio.gather(*requests)
                with db:
                    for batch, translated in zip(batches, results):
                        for row, item in zip(batch, translated):
                            first = FIRST_CATEGORY_ZH.get(row["first_category"], row["first_category"])
                            second, third = categories[_path_key(row)]
                            listing = json.loads(row["list_json"])
                            title = str(item.get("title_zh") or "").strip()[:180]
                            if not _HAN.search(title):
                                title = f"{listing.get('nameEn') or row['pid']} 商品"[:180]
                            summary = str(item.get("summary_zh") or "").strip()[:260]
                            if summary and not _HAN.search(summary):
                                summary = ""
                            keywords = [str(word).strip() for word in item["keywords_zh"]
                                        if isinstance(word, str) and str(word).strip()][:8]
                            keywords = _supported_keywords(keywords, listing)
                            search_text = " ".join(dict.fromkeys([
                                title, first, second, third, *keywords,
                                str(listing.get("nameEn") or ""),
                            ]))
                            upsert_localization(db, {
                                "pid": row["pid"], "source_fingerprint": source_fingerprint(row),
                                "version": LOCALIZATION_VERSION, "first_category": row["first_category"],
                                "title_zh": title, "summary_zh": summary,
                                "keywords_zh": json.dumps(keywords, ensure_ascii=False),
                                "first_category_zh": first, "second_category_zh": second,
                                "third_category_zh": third, "search_text": search_text,
                            })
                print(f"localized {min(start + len(wave), len(pending))}/{len(pending)}", flush=True)
        # Refresh old resumed rows too, so a prompt/rule adjustment applies before promotion.
        with db:
            for row in rows:
                saved = db.execute("SELECT * FROM localized_products WHERE pid=?", (row["pid"],)).fetchone()
                if saved is None:
                    continue
                listing = json.loads(row["list_json"])
                keywords = _supported_keywords(json.loads(saved["keywords_zh"]), listing)
                search_text = " ".join(dict.fromkeys([
                    saved["title_zh"], saved["first_category_zh"], saved["second_category_zh"],
                    saved["third_category_zh"], *keywords, str(listing.get("nameEn") or ""),
                ]))
                if keywords != json.loads(saved["keywords_zh"]) or search_text != saved["search_text"]:
                    updated = dict(zip(saved.keys(), saved))
                    updated["keywords_zh"] = json.dumps(keywords, ensure_ascii=False)
                    updated["search_text"] = search_text
                    upsert_localization(db, updated)
        # A product removed from the snapshot must not keep a stale Chinese row:
        # nothing else ever deletes from the projection, so prune here.
        with db:
            orphans = [str(row[0]) for row in db.execute("SELECT pid FROM localized_products")
                       if str(row[0]) not in source_ids]
            for pid in orphans:
                db.execute("DELETE FROM localized_products WHERE pid=?", (pid,))
                db.execute("DELETE FROM localized_fts WHERE pid=?", (pid,))
            if orphans:
                print(f"pruned orphan pids={len(orphans)}", flush=True)
    db.close()
    source.close()
    for attempt in range(10):
        try:
            os.replace(building_path, output_path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2 * (attempt + 1))
    print(f"ready {output_path} products={len(rows)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "cj_catalog.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "cj_localization.sqlite3")
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url-env", default="LLM_BASE_URL")
    parser.add_argument("--api-key-env", default="LLM_API_KEY")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.batch_size < 1 or args.concurrency < 1 or args.limit < 0:
        parser.error("batch-size/concurrency must be positive and limit nonnegative")
    asyncio.run(build(args))


if __name__ == "__main__":
    main()
