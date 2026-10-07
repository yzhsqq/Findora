"""Shared offline Chinese presentation builder for platform snapshots.

Amazon and eBay snapshots have their own SQLite database and prompts, but share
the same resumable, validated translation pipeline.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass
from importlib import import_module
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.infrastructure.settings import load_settings

HAN = re.compile(r"[\u3400-\u9fff]")
NUMBERS = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Profile:
    """Per-platform configuration of the localization pipeline."""

    platform: str  # e.g. "Amazon" / "eBay"
    table: str  # raw snapshot table holding card_json
    product_prompt: str
    string_prompt: str
    # Brand/platform names that may stay English inside translated conditions.
    allowed_english: frozenset[str]


def validate_text(source: str, translated: object, *, require_chinese: bool = True, exact_numbers: bool = True) -> str:
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError("缺少翻译文本")
    translated = translated.strip()
    if require_chinese and not HAN.search(translated):
        raise ValueError("翻译文本缺少中文")
    before, after = set(NUMBERS.findall(source)), set(NUMBERS.findall(translated))
    if after - before or (exact_numbers and before - after):
        raise ValueError(f"原文数字必须保留且不得新增：原文={sorted(before)}，译文={sorted(after)}")
    if exact_numbers:
        for code in re.findall(r"\b[A-Z0-9_-]*[A-Z][A-Z0-9_-]*\d[A-Z0-9_-]*\b", source):
            if code not in translated:
                raise ValueError("翻译改变了型号或优惠码")
    return translated


def validate_string(profile: Profile, kind: str, source: str, translated: object) -> str:
    result = validate_text(source, translated, require_chinese=bool(re.search(r"[A-Za-z]{3,}", source)))
    if kind == "condition":
        words = re.findall(r"\b[A-Za-z][A-Za-z0-9_-]*\b", result)
        if any(word not in profile.allowed_english and not any(c.isdigit() for c in word) for word in words):
            raise ValueError(f"优惠条件中的英文正文必须翻译，保留 {'、'.join(sorted(profile.allowed_english))} 和优惠码")
        promotion = re.search(r"Get (\d+) for the price of (\d+)", source, re.I)
        if promotion and f"{promotion[1]}件按{promotion[2]}件价格" not in result:
            raise ValueError(f"数量优惠必须写为：{promotion[1]}件按{promotion[2]}件价格，不能颠倒购买数量和计费数量")
    return result


class Translator:
    def __init__(self, settings, profile: Profile):
        self.url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        self.key, self.model = settings.llm_api_key, settings.llm_model
        self.deepseek = "deepseek.com" in settings.llm_base_url
        self.profile = profile

    async def call(self, client, prompt, items):
        payload = {"model": self.model, "temperature": 0, "max_tokens": 7000,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": prompt},
                                {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)}]}
        if self.deepseek:
            payload["thinking"] = {"type": "disabled"}
        for attempt in range(3):
            try:
                response = await client.post(self.url, headers={"Authorization": "Bearer " + self.key}, json=payload)
                response.raise_for_status()
                text = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])["items"]
                if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
                    raise ValueError("翻译返回格式错误")
                found = {str(item.get("id")): item for item in parsed}
                if len(found) != len(parsed) or set(found) != {str(item["id"]) for item in items}:
                    raise ValueError("翻译返回编号不匹配")
                return [found[str(item["id"])] for item in items]
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError, KeyError, IndexError):
                if attempt == 2:
                    raise RuntimeError(f"{self.profile.platform} 中文生成失败；已完成批次保存在 .build 文件，可重新执行继续") from None
                await asyncio.sleep(1 + attempt)


async def build(args, profile: Profile) -> None:
    source, output = args.source.resolve(), args.output.resolve()
    if source == output:
        raise ValueError("翻译数据库不能覆盖商品快照")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        cards = [json.loads(r[0]) for r in db.execute(f"SELECT card_json FROM {profile.table} ORDER BY product_id")]
    output.parent.mkdir(parents=True, exist_ok=True)
    building = output.with_name(output.name + ".build")
    if not building.exists() and output.exists():
        shutil.copy2(output, building)
    fingerprint, init_db = _hooks(profile)
    translator = Translator(load_settings(), profile)
    with closing(sqlite3.connect(building)) as db:
        init_db(db)
        cached = dict(db.execute("SELECT product_id,fingerprint FROM localized_products"))
        pending = [c for c in cards if cached.get(c["product_id"]) != fingerprint(c)]
        skipped: list[str] = []  # 反复校验失败、保留英文原文的条目
        print(f"products total={len(cards)} pending={len(pending)}", flush=True)
        async with httpx.AsyncClient(timeout=120) as client:
            async def products(batch):
                validated = []
                def validate_product(card, item):
                    title = validate_text(card["title"], item.get("title"))
                    if card["brand"] and card["brand"].casefold() in card["title"].casefold() and card["brand"].casefold() not in title.casefold():
                        raise ValueError("翻译改变了标题中的品牌")
                    evidence = " ".join((card["title"], card.get("description", "")[:800], *card["highlights"]))
                    description = item.get("description", "")
                    if description:
                        description = validate_text(evidence, description, exact_numbers=False)
                    highlights = item.get("highlights")
                    if not isinstance(highlights, list):
                        raise ValueError("中文特点格式错误")
                    highlights = [validate_text(evidence, text, exact_numbers=False) for text in highlights[:3]]
                    localized = {"title": title, "description": description, "highlights": highlights}
                    return (card["product_id"], fingerprint(card), json.dumps(localized, ensure_ascii=False))
                pending_batch = list(batch)
                feedback = {}
                for attempt in range(3):
                    items = [{"id": c["product_id"], "title": c["title"], "brand": c["brand"],
                              "description": c.get("description", "")[:800], "highlights": c["highlights"],
                              "validation_feedback": feedback.get(c["product_id"], "")} for c in pending_batch]
                    translated = await translator.call(client, profile.product_prompt, items)
                    failed = []
                    good = []
                    for card, item in zip(pending_batch, translated):
                        try:
                            good.append(validate_product(card, item))
                        except ValueError as error:
                            feedback[card["product_id"]] = str(error)
                            failed.append(card)
                    with db:
                        db.executemany("INSERT OR REPLACE INTO localized_products VALUES (?,?,?)", good)
                    validated.extend(good)
                    if not failed:
                        return validated
                    pending_batch = failed
                    print(f"retry product validation count={len(failed)} attempt={attempt + 1}", flush=True)
                for card in pending_batch:  # 单条重试；仍失败则保留英文原文，不中断整批
                    items = [{"id": c["product_id"], "title": c["title"], "brand": c["brand"],
                              "description": c.get("description", "")[:800], "highlights": c["highlights"],
                              "validation_feedback": feedback.get(c["product_id"], "")} for c in (card,)]
                    for _ in range(2):
                        try:
                            row = validate_product(card, (await translator.call(client, profile.product_prompt, items))[0])
                        except (ValueError, RuntimeError) as error:
                            feedback[card["product_id"]] = str(error)
                            continue
                        with db:
                            db.execute("INSERT OR REPLACE INTO localized_products VALUES (?,?,?)", row)
                        validated.append(row)
                        break
                    else:
                        skipped.append(card["product_id"])
                        print(f"skip product {card['product_id']}: {feedback.get(card['product_id'], '')}", flush=True)
                return validated

            for start in range(0, len(pending), args.batch_size * 2):
                wave = pending[start:start + args.batch_size * 2]
                batches = [wave[i:i + args.batch_size] for i in range(0, len(wave), args.batch_size)]
                results = await asyncio.gather(*(products(batch) for batch in batches))
                with db:
                    for rows in results:
                        db.executemany("INSERT OR REPLACE INTO localized_products VALUES (?,?,?)", rows)
                print(f"products completed={min(start + len(wave), len(pending))}/{len(pending)}", flush=True)

            sources = {("spec", s["spec"]) for c in cards for s in c["skus"]} | {
                ("condition", text) for c in cards for text in c.get("price_conditions", [])}
            cached_strings = set()
            for kind, source_text, translated in db.execute("SELECT kind,source,translated FROM localized_strings"):
                try:
                    validate_string(profile, kind, source_text, translated)
                    cached_strings.add((kind, source_text))
                except ValueError:
                    pass
            pending_strings = sorted((kind, text) for kind, text in sources - cached_strings
                                     if re.search(r"[A-Za-z]", text.replace(profile.platform, "")))
            print(f"strings pending={len(pending_strings)}", flush=True)
            async def strings(batch):
                validated, feedback = [], {}
                pending_batch = list(batch)
                for attempt in range(3):
                    items = [{"id": str(i), "kind": kind, "text": text, "validation_feedback": feedback.get(text, "")}
                             for i, (kind, text) in enumerate(pending_batch)]
                    translated = await translator.call(client, profile.string_prompt, items)
                    failed, good = [], []
                    for (kind, text), item in zip(pending_batch, translated):
                        try:
                            good.append((kind, text, validate_string(profile, kind, text, item.get("text"))))
                        except ValueError as error:
                            feedback[text] = str(error)
                            failed.append((kind, text))
                    with db:
                        db.executemany("INSERT OR REPLACE INTO localized_strings VALUES (?,?,?)", good)
                    validated.extend(good)
                    if not failed:
                        return validated
                    pending_batch = failed
                    print(f"retry string validation count={len(failed)} attempt={attempt + 1}", flush=True)
                for kind, text in pending_batch:  # 单条重试；仍失败则保留英文原文，不中断整批
                    items = [{"id": "0", "kind": kind, "text": text, "validation_feedback": feedback.get(text, "")}]
                    for _ in range(2):
                        try:
                            row = (kind, text, validate_string(profile, kind, text,
                                   (await translator.call(client, profile.string_prompt, items))[0].get("text")))
                        except (ValueError, RuntimeError) as error:
                            feedback[text] = str(error)
                            continue
                        with db:
                            db.execute("INSERT OR REPLACE INTO localized_strings VALUES (?,?,?)", row)
                        validated.append(row)
                        break
                    else:
                        skipped.append(f"{kind}:{text}")
                        print(f"skip string {kind}: {text[:40]}", flush=True)
                return validated
            for start in range(0, len(pending_strings), 100):
                wave = pending_strings[start:start + 100]
                results = await asyncio.gather(*(strings(wave[i:i + 50]) for i in range(0, len(wave), 50)))
                with db:
                    for rows in results:
                        db.executemany("INSERT OR REPLACE INTO localized_strings VALUES (?,?,?)", rows)
                print(f"strings completed={min(start + len(wave), len(pending_strings))}/{len(pending_strings)}", flush=True)
        ids = {c["product_id"] for c in cards}
        with db:
            for (pid,) in db.execute("SELECT product_id FROM localized_products").fetchall():
                if pid not in ids:
                    db.execute("DELETE FROM localized_products WHERE product_id=?", (pid,))
    os.replace(building, output)
    print(f"published products={len(cards)} skipped={len(skipped)}", flush=True)


def _hooks(profile: Profile):
    """Resolve the platform's fingerprint/init_db pair from its localization module."""
    module = import_module(f"app.infrastructure.persistence.{profile.platform.lower()}_localization")
    return module.fingerprint, module.init_db


def run(profile: Profile, default_source: str, default_output: str, description: str) -> None:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--source", type=Path, default=ROOT / default_source)
    parser.add_argument("--output", type=Path, default=ROOT / default_output)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 20:
        parser.error("batch-size must be between 1 and 20")
    asyncio.run(build(args, profile))
