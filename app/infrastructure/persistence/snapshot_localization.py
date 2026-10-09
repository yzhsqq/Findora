"""Shared Chinese projection; original identity, pricing and snapshots remain intact."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable

LANGUAGE_FIELDS = ("title", "image_alt", "category", "description", "highlights", "price_conditions",
                   "source_title", "source_category", "source_description", "source_highlights", "source_price_conditions")


def snapshot_fingerprint(card: dict, version: str) -> str:
    inputs = [version, card["title"], card["brand"], card["category"],
              card.get("description", ""), card["highlights"]]
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()


def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS localized_products (
            product_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS localized_strings (
            kind TEXT NOT NULL, source TEXT NOT NULL, translated TEXT NOT NULL,
            PRIMARY KEY(kind, source)
        );
    """)


class SnapshotLocalization:
    def __init__(self, path: Path, *, categories: dict[str, str], fingerprint: Callable[[dict], str]):
        self.path = path.resolve()
        self.categories = categories
        self.fingerprint = fingerprint

    def project(self, cards: list[dict]) -> list[dict]:
        if not cards:
            return []
        products, strings = {}, {}
        if self.path.is_file():
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                products = {pid: (digest, json.loads(payload)) for pid, digest, payload in db.execute(
                    "SELECT product_id,fingerprint,payload FROM localized_products")}
                strings = {(kind, source): translated for kind, source, translated in db.execute(
                    "SELECT kind,source,translated FROM localized_strings")}
        result = []
        for source in cards:
            card = deepcopy(source)
            card["source_category"] = source["category"]
            card["category"] = self.categories.get(source["category"], source["category"])
            row = products.get(source["product_id"])
            if row and row[0] == self.fingerprint(source):
                localized = row[1]
                card.update(source_title=source["title"], source_description=source.get("description", ""),
                            source_highlights=source["highlights"], title=localized["title"],
                            image_alt=localized["title"], description=localized["description"],
                            highlights=localized["highlights"])
            for sku in card["skus"]:
                translated = strings.get(("spec", sku["spec"]))
                if translated:
                    sku["source_spec"], sku["spec"] = sku["spec"], translated
            card["source_price_conditions"] = source.get("price_conditions", [])
            card["price_conditions"] = [strings.get(("condition", text), text) for text in card["source_price_conditions"]]
            result.append(card)
        return result
