"""Attach catalog IDs only for CJ products whose title is visibly in the reply."""
from __future__ import annotations

import re


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def attach_visible_product_ids(text: str, search_result: dict | None) -> str:
    """Add grounded IDs without turning unseen search hits into recommendations."""
    if not text or not isinstance(search_result, dict) or search_result.get("recall_strategy") != "cj_snapshot_keyword":
        return text
    hits = search_result.get("hits")
    if not isinstance(hits, list):
        return text
    reply_words = " ".join(_words(text))
    matches: list[tuple[str, str]] = []
    prefixes = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        product_id = hit.get("product_id")
        title = hit.get("title")
        if not isinstance(product_id, str) or not isinstance(title, str):
            continue
        words = _words(title)
        if len(words) < 6:
            continue
        prefix = " ".join(words[: min(8, len(words))])
        prefixes.append((prefix, product_id, title))
    for prefix, product_id, title in prefixes:
        if text.casefold().find(product_id.casefold()) >= 0 or reply_words.find(prefix) < 0:
            continue
        if sum(other == prefix for other, _, _ in prefixes) != 1:
            continue
        matches.append((title, product_id))
    if not matches:
        return text
    appendix = ["本轮已展示商品的 product_id（供核对）："]
    for title, product_id in matches:
        short_title = title[:72].replace("\n", " ").strip()
        appendix.append(f"- {short_title}：`{product_id}`")
    return text.rstrip() + "\n\n" + "\n".join(appendix)
