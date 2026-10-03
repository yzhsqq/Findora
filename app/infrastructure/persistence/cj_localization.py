"""Read-only Chinese projection and local search for the immutable CJ snapshot."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path


LOCALIZATION_VERSION = "cj-zh-v1"
FIRST_CATEGORY_ZH = {
    "Bags & Shoes": "箱包鞋履",
    "Sports & Outdoors": "户外运动",
    "Consumer Electronics": "消费电子",
    "Phones & Accessories": "手机配件",
    "Home, Garden & Furniture": "家居园艺",
    "Health, Beauty & Hair": "美妆个护",
    "Pet Supplies": "宠物用品",
    "Computer & Office": "电脑办公",
    "Toys, Kids & Babies": "玩具母婴",
}
_HAN = re.compile(r"[\u3400-\u9fff]+")
_ASCII = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", re.I)
_IDENTIFIER = re.compile(r"(?:[0-9]{16,24}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|CJ[A-Z0-9_-]{6,96})", re.I)


def source_fingerprint(row: sqlite3.Row | dict) -> str:
    """Only language inputs participate; price, inventory and fetch time do not."""
    listing = json.loads(row["list_json"])
    values = [
        LOCALIZATION_VERSION, str(listing.get("nameEn") or ""),
        str(listing.get("description") or ""),
        str(row["first_category"]), str(row["second_category"]), str(row["third_category"]),
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def search_grams(text: str) -> list[str]:
    """Character bigrams make two-character Chinese terms searchable with FTS5."""
    grams: list[str] = []
    for segment in _HAN.findall(text):
        grams.extend(segment[i:i + 2] for i in range(len(segment) - 1))
        if len(segment) == 1:
            grams.append(segment)
    grams.extend(word.lower() for word in _ASCII.findall(text))
    return list(dict.fromkeys(grams))


def init_localization_db(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS localized_products (
            pid TEXT PRIMARY KEY,
            source_fingerprint TEXT NOT NULL,
            version TEXT NOT NULL,
            first_category TEXT NOT NULL,
            title_zh TEXT NOT NULL,
            summary_zh TEXT NOT NULL,
            keywords_zh TEXT NOT NULL,
            first_category_zh TEXT NOT NULL,
            second_category_zh TEXT NOT NULL,
            third_category_zh TEXT NOT NULL,
            search_text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS category_translations (
            path_key TEXT PRIMARY KEY,
            second_zh TEXT NOT NULL,
            third_zh TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS localized_fts USING fts5(
            pid UNINDEXED, grams, tokenize='unicode61'
        );
    """)


def upsert_localization(db: sqlite3.Connection, row: dict) -> None:
    fields = (
        "pid", "source_fingerprint", "version", "first_category", "title_zh",
        "summary_zh", "keywords_zh", "first_category_zh", "second_category_zh",
        "third_category_zh", "search_text",
    )
    values = [row[field] for field in fields]
    db.execute(
        "INSERT OR REPLACE INTO localized_products(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")",
        values,
    )
    db.execute("DELETE FROM localized_fts WHERE pid=?", (row["pid"],))
    db.execute("INSERT INTO localized_fts(pid,grams) VALUES(?,?)", (
        row["pid"], " ".join(search_grams(row["search_text"])),
    ))


class CJLocalization:
    def __init__(self, path: Path):
        self.path = path

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def available(self) -> bool:
        if not self.path.is_file():
            return False
        with closing(self._db()) as db:
            return db.execute("SELECT EXISTS(SELECT 1 FROM localized_products)").fetchone()[0] == 1

    def lookup_many(self, pids: list[str]) -> dict[str, sqlite3.Row]:
        if not pids or not self.path.is_file():
            return {}
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM localized_products WHERE pid IN (" + ",".join("?" for _ in pids) + ")", pids,
            ).fetchall()
        return {row["pid"]: row for row in rows}

    @staticmethod
    def apply_card(source: sqlite3.Row, card: dict, localized: sqlite3.Row | None) -> dict:
        if localized is None or localized["source_fingerprint"] != source_fingerprint(source):
            return card
        return {
            **card,
            "title": localized["title_zh"], "image_alt": localized["title_zh"],
            "category": localized["first_category_zh"],
            "highlights": [localized["second_category_zh"], localized["third_category_zh"]],
            "description": localized["summary_zh"] or f"{localized['third_category_zh']}。具体规格请查看下方选项。",
        }

    def search(self, query: str, categories: tuple[str, ...]) -> list[str]:
        """Return IDs ranked by term coverage, then title/alias phrase matches."""
        if not self.path.is_file():
            return []
        clean = _IDENTIFIER.sub(" ", query.strip()).replace("的", " ")
        segments = _HAN.findall(clean)
        groups = [search_grams(segment) for segment in segments]
        if not groups:
            groups = [search_grams(clean)]
        grams = list(dict.fromkeys(gram for group in groups for gram in group))[:24]
        if not grams:
            return []
        expression = " OR ".join(f'"{gram}"' for gram in grams)
        where = ""
        args: list[object] = [expression]
        if categories:
            where = " AND p.first_category IN (" + ",".join("?" for _ in categories) + ")"
            args.extend(categories)
        with closing(self._db()) as db:
            matches = db.execute(
                "SELECT p.pid,p.title_zh,p.keywords_zh,p.third_category_zh,p.search_text "
                "FROM localized_fts f JOIN localized_products p ON p.pid=f.pid "
                "WHERE localized_fts MATCH ?" + where + " LIMIT 10000", args,
            ).fetchall()
        ranked: list[tuple[float, str]] = []
        for row in matches:
            text = row["search_text"]
            found = set(search_grams(text))
            matched = len(found.intersection(grams))
            if matched < math.ceil(len(grams) * 0.6):
                continue
            if any(group and not found.intersection(group) for group in groups):
                continue
            title = row["title_zh"]
            keywords = json.loads(row["keywords_zh"])
            compact = re.sub(r"\s+", "", clean)
            score = matched / len(grams) * 100
            if compact and compact in title:
                score += 60
            elif compact and any(compact in word for word in keywords):
                score += 45
            elif compact and compact in text:
                score += 20
            if compact and compact in row["third_category_zh"]:
                score += 30
            score += sum(6 for segment in segments if segment in title)
            ranked.append((score, row["pid"]))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [pid for _, pid in ranked]
