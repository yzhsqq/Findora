"""Collect CJ product snapshots without changing the project's frozen fixture.

Official flow: getAccessToken -> getCategory -> listV2 -> product/query.
The SQLite file is a resumable, local snapshot. It never stores the API key or token.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "cj_catalog.sqlite3"
API_BASE = "https://developers.cjdropshipping.com/api2.0/v1"
FIRST_CATEGORIES = (
    "Bags & Shoes",
    "Sports & Outdoors",
    "Consumer Electronics",
    "Phones & Accessories",
    "Home, Garden & Furniture",
    "Health, Beauty & Hair",
    "Pet Supplies",
    "Computer & Office",
    "Toys, Kids & Babies",
)
LIST_COST = 50
DETAIL_COST = 10
STOCK_COST = 10
FREE_DAILY_POINTS = 50_000


class CJQuotaReached(Exception):
    """A resumable pause after reaching CJ or local free-tier limits."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            pid TEXT PRIMARY KEY,
            first_category TEXT NOT NULL,
            second_category TEXT NOT NULL,
            third_category TEXT NOT NULL,
            list_json TEXT NOT NULL,
            list_fetched_at TEXT NOT NULL,
            detail_json TEXT,
            detail_fetched_at TEXT,
            detail_status TEXT,
            inventory_json TEXT,
            inventory_fetched_at TEXT,
            inventory_status TEXT
        );
        CREATE TABLE IF NOT EXISTS list_pages (
            category_id TEXT NOT NULL,
            page INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            product_count INTEGER NOT NULL,
            PRIMARY KEY (category_id, page)
        );
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            points_used_today INTEGER,
            points_remaining INTEGER,
            result_code TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(products)")}
    for name in ("inventory_json", "inventory_fetched_at", "inventory_status"):
        if name not in columns:
            db.execute(f"ALTER TABLE products ADD COLUMN {name} TEXT")
    return db


def interleave_categories(groups: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    """Distribute page requests across relevant top-level CJ categories."""
    by_first: dict[str, list[tuple[str, str, str, str]]] = {}
    for group in groups:
        first = str(group.get("categoryFirstName") or "")
        if first not in FIRST_CATEGORIES:
            continue
        rows: list[tuple[str, str, str, str]] = []
        for second_group in group.get("categoryFirstList") or []:
            second = str(second_group.get("categorySecondName") or "")
            for third_group in second_group.get("categorySecondList") or []:
                category_id = str(third_group.get("categoryId") or "")
                third = str(third_group.get("categoryName") or "")
                if category_id and third:
                    rows.append((category_id, first, second, third))
        by_first[first] = rows
    ordered: list[tuple[str, str, str, str]] = []
    for index in range(max((len(rows) for rows in by_first.values()), default=0)):
        for first in FIRST_CATEGORIES:
            rows = by_first.get(first) or []
            if index < len(rows):
                ordered.append(rows[index])
    return ordered


def flatten_list_page(data: dict[str, Any]) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for group in data.get("content") or []:
        if not isinstance(group, dict):
            continue
        for product in group.get("productList") or []:
            if isinstance(product, dict) and product.get("id"):
                products.append(product)
    return products


class CJClient:
    def __init__(self, api_key: str, db: sqlite3.Connection, max_points: int) -> None:
        self.http = httpx.Client(base_url=API_BASE, timeout=30)
        self.db = db
        self.max_points = min(max_points, FREE_DAILY_POINTS)
        self.used_today: int | None = None
        self.next_call_at = 0.0
        self._pace()
        auth = self.http.post("/authentication/getAccessToken", json={"apiKey": api_key})
        payload = auth.json()
        if payload.get("result") is not True or not (payload.get("data") or {}).get("accessToken"):
            raise RuntimeError(f"CJ authentication failed: code={payload.get('code')}")
        self.token = payload["data"]["accessToken"]

    def close(self) -> None:
        self.http.close()

    def _pace(self) -> None:
        delay = self.next_call_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_call_at = time.monotonic() + 1.2

    def can_spend(self, points: int) -> bool:
        return self.used_today is None or self.used_today + points <= self.max_points

    def get(self, endpoint: str, *, params: dict[str, Any] | None = None, cost: int = 0) -> dict[str, Any]:
        if not self.can_spend(cost):
            raise CJQuotaReached("free point budget reached")
        for attempt in range(4):
            self._pace()
            response = self.http.get(endpoint, params=params, headers={"CJ-Access-Token": self.token})
            payload = response.json()
            points = payload.get("pointsInfo") or {}
            if isinstance(points.get("usedToday"), (int, float)):
                reported = int(points["usedToday"])
                previous = self.used_today
                if previous is not None and (reported < previous or reported > previous + cost + 100):
                    # CJ occasionally reports another, implausibly large counter on an
                    # otherwise successful stock response. Recheck on a free endpoint;
                    # fail closed if it cannot establish the account's real usage.
                    self._pace()
                    check = self.http.get("/product/getCategory", headers={"CJ-Access-Token": self.token})
                    check_payload = check.json()
                    checked = (check_payload.get("pointsInfo") or {}).get("usedToday")
                    if (check.status_code >= 400 or check_payload.get("result") is not True
                            or not isinstance(checked, (int, float))):
                        raise CJQuotaReached("CJ points counter inconsistent; paused for safety")
                    self.used_today = int(checked)
                else:
                    self.used_today = reported
            with self.db:
                self.db.execute(
                    "INSERT INTO run_log(at,endpoint,points_used_today,points_remaining,result_code) VALUES(?,?,?,?,?)",
                    (utc_now(), endpoint, self.used_today, points.get("remaining"), str(payload.get("code"))),
                )
            if payload.get("code") == 1600200 and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            if response.status_code == 429 or payload.get("code") in (429, 1600200, 1600201, 16900500):
                raise CJQuotaReached("CJ point or rate limit reached; resume later")
            if response.status_code >= 400 or (payload.get("result") is not True and payload.get("success") is not True):
                raise RuntimeError(f"CJ {endpoint} failed: http={response.status_code}, code={payload.get('code')}")
            return payload
        raise AssertionError("unreachable")


def store_list_page(
    db: sqlite3.Connection, category: tuple[str, str, str, str], page: int,
    products: list[dict[str, Any]],
) -> None:
    category_id, first, second, third = category
    now = utc_now()
    with db:
        for product in products:
            db.execute(
                """INSERT INTO products(pid,first_category,second_category,third_category,list_json,list_fetched_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(pid) DO UPDATE SET
                     first_category=excluded.first_category,
                     second_category=excluded.second_category,
                     third_category=excluded.third_category,
                     list_json=excluded.list_json,
                     list_fetched_at=excluded.list_fetched_at""",
                (str(product["id"]), first, second, third, json.dumps(product, ensure_ascii=False), now),
            )
        db.execute(
            "INSERT OR REPLACE INTO list_pages(category_id,page,fetched_at,product_count) VALUES(?,?,?,?)",
            (category_id, page, now, len(products)),
        )


def list_count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM products").fetchone()[0])


def detail_count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM products WHERE detail_json IS NOT NULL").fetchone()[0])


def stock_count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM products WHERE inventory_json IS NOT NULL").fetchone()[0])


def collect_lists(client: CJClient, db: sqlite3.Connection, target: int) -> None:
    categories_payload = client.get("/product/getCategory")
    categories = interleave_categories(categories_payload.get("data") or [])
    if not categories:
        raise RuntimeError("CJ category response had no selected categories")
    print(f"list phase: categories={len(categories)} target={target} existing={list_count(db)}", flush=True)
    for page in range(1, 61):  # listV2 exposes at most 6,000 records per query
        for category in categories:
            if list_count(db) >= target:
                return
            category_id = category[0]
            if db.execute("SELECT 1 FROM list_pages WHERE category_id=? AND page=?", (category_id, page)).fetchone():
                continue
            payload = client.get(
                "/product/listV2",
                params={"categoryId": category_id, "page": page, "size": 100,
                        "features": "enable_category,enable_description", "orderBy": 3, "sort": "desc"},
                cost=LIST_COST,
            )
            data = payload.get("data") or {}
            products = flatten_list_page(data)
            store_list_page(db, category, page, products)
            print(
                f"list page: category={category[3]} page={page} returned={len(products)} "
                f"unique={list_count(db)} used_today={client.used_today}", flush=True,
            )
        # No further pages can add products if every category is already exhausted.
        if all(
            (db.execute(
                "SELECT product_count FROM list_pages WHERE category_id=? AND page=?", (cat[0], page),
            ).fetchone() or [100])[0] < 100
            for cat in categories
        ):
            return


async def collect_details(client: CJClient, db: sqlite3.Connection, max_details: int | None) -> None:
    print(f"detail phase: existing={detail_count(db)}", flush=True)
    pids = [row[0] for row in db.execute(
        """SELECT pid FROM products WHERE detail_json IS NULL AND detail_status IS NULL
           ORDER BY list_fetched_at DESC, pid""",
    )]
    pending: dict[asyncio.Task[tuple[int, dict[str, Any]]], str] = {}
    scheduled = 0
    successful = detail_count(db)
    failure: Exception | None = None

    async with httpx.AsyncClient(base_url=API_BASE, timeout=30) as http:
        async def fetch(pid: str) -> tuple[int, dict[str, Any]]:
            for attempt in range(5):
                try:
                    response = await http.get(
                        "/product/query", params={"pid": pid},
                        headers={"CJ-Access-Token": client.token},
                    )
                    payload = response.json()
                except (httpx.TransportError, ValueError):
                    if attempt == 4:
                        raise
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if (payload.get("code") not in (1600200, 1600000, 1600301)
                        and response.status_code < 500) or attempt == 4:
                    return response.status_code, payload
                await asyncio.sleep(3 * (attempt + 1))
            raise AssertionError("unreachable")

        while scheduled < len(pids) or pending:
            may_start = (
                scheduled < len(pids)
                and len(pending) < 2
                and (max_details is None or successful + len(pending) < max_details)
                and (client.used_today is None or client.used_today + DETAIL_COST * (len(pending) + 1) <= client.max_points)
                and failure is None
            )
            if may_start:
                pid = pids[scheduled]
                scheduled += 1
                pending[asyncio.create_task(fetch(pid))] = pid
                await asyncio.sleep(2.0)  # start at most 0.5 QPS; two in flight
            elif not pending:
                break
            else:
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    pid = pending.pop(task)
                    try:
                        status, payload = task.result()
                    except (httpx.HTTPError, ValueError) as exc:
                        failure = RuntimeError(f"CJ detail request interrupted: {type(exc).__name__}")
                        continue
                    points = payload.get("pointsInfo") or {}
                    if isinstance(points.get("usedToday"), (int, float)):
                        client.used_today = max(client.used_today or 0, int(points["usedToday"]))
                    with db:
                        db.execute(
                            "INSERT INTO run_log(at,endpoint,points_used_today,points_remaining,result_code) VALUES(?,?,?,?,?)",
                            (utc_now(), "/product/query", client.used_today, points.get("remaining"), str(payload.get("code"))),
                        )
                    if status == 429 or payload.get("code") in (429, 1600200, 1600201, 16900500):
                        failure = CJQuotaReached("CJ point or rate limit reached; resume later")
                        continue
                    if payload.get("code") in (1602001, 1602002):
                        with db:
                            db.execute(
                                "UPDATE products SET detail_status=? WHERE pid=?",
                                (f"unavailable:{payload.get('code')}", pid),
                            )
                        continue
                    if status >= 400 or payload.get("result") is not True:
                        failure = RuntimeError(f"CJ product/query failed: http={status}, code={payload.get('code')}")
                        continue
                    detail = payload.get("data")
                    with db:
                        if isinstance(detail, dict) and str(detail.get("pid")) == pid:
                            db.execute(
                                "UPDATE products SET detail_json=?,detail_fetched_at=?,detail_status='ok' WHERE pid=?",
                                (json.dumps(detail, ensure_ascii=False), utc_now(), pid),
                            )
                            successful += 1
                        else:
                            db.execute("UPDATE products SET detail_status='empty' WHERE pid=?", (pid,))
                    if successful % 25 == 0:
                        print(f"details={successful} used_today={client.used_today}", flush=True)
    if failure:
        raise failure


def collect_stock(client: CJClient, db: sqlite3.Connection, max_stock: int) -> None:
    print(f"stock phase: existing={stock_count(db)} target={max_stock}", flush=True)
    while stock_count(db) < max_stock:
        row = db.execute(
            """SELECT pid FROM products WHERE detail_json IS NOT NULL AND inventory_status IS NULL
               ORDER BY detail_fetched_at DESC, pid LIMIT 1""",
        ).fetchone()
        if row is None:
            return
        pid = row[0]
        payload = client.get("/product/stock/getInventoryByPid", params={"pid": pid}, cost=STOCK_COST)
        inventory = payload.get("data")
        with db:
            if isinstance(inventory, dict):
                db.execute(
                    "UPDATE products SET inventory_json=?,inventory_fetched_at=?,inventory_status='ok' WHERE pid=?",
                    (json.dumps(inventory, ensure_ascii=False), utc_now(), pid),
                )
            else:
                db.execute("UPDATE products SET inventory_status='empty' WHERE pid=?", (pid,))
        count = stock_count(db)
        if count % 25 == 0:
            print(f"stock={count} used_today={client.used_today}", flush=True)


def export_jsonl(db: sqlite3.Connection, path: Path) -> None:
    """Export source records with provenance; no invented destination or SKU data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        for pid, first, second, third, listed, listed_at, detail, detail_at, inventory, inventory_at in db.execute(
            """SELECT pid,first_category,second_category,third_category,list_json,
                      list_fetched_at,detail_json,detail_fetched_at,inventory_json,
                      inventory_fetched_at FROM products ORDER BY pid""",
        ):
            record = {
                "source_platform": "CJdropshipping",
                "external_product_id": pid,
                "category_path": [first, second, third],
                "list_fetched_at": listed_at,
                "detail_fetched_at": detail_at,
                "inventory_fetched_at": inventory_at,
                "list": json.loads(listed),
                "detail": json.loads(detail) if detail else None,
                "inventory": json.loads(inventory) if inventory else None,
            }
            output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--export", type=Path, default=ROOT / "data" / "cj-products.jsonl")
    parser.add_argument("--target-list", type=int, default=10_000)
    parser.add_argument("--max-details", type=int, default=None)
    parser.add_argument("--max-stock", type=int, default=500)
    parser.add_argument("--max-points", type=int, default=FREE_DAILY_POINTS)
    parser.add_argument("--phase", choices=("all", "list", "detail", "stock", "export"), default="all")
    args = parser.parse_args()
    if args.target_list < 1 or args.max_points < 1 or args.max_stock < 0 or (args.max_details is not None and args.max_details < 0):
        parser.error("targets and point budget must be positive")
    db = open_db(args.db)
    client: CJClient | None = None
    try:
        if args.phase != "export":
            api_key = dotenv_values(ROOT / ".env").get("CJdropshipping_key")
            if not api_key:
                raise RuntimeError("CJdropshipping_key missing from local .env")
            client = CJClient(api_key, db, args.max_points)
            if args.phase in ("detail", "stock"):
                client.get("/product/getCategory")  # free, obtains current usedToday before budgeted calls
            if args.phase in ("all", "list"):
                collect_lists(client, db, args.target_list)
            if args.phase in ("all", "detail"):
                if args.phase == "all":
                    daily_budget = min(args.max_points, FREE_DAILY_POINTS)
                    client.max_points = max(1, daily_budget - args.max_stock * STOCK_COST)
                asyncio.run(collect_details(client, db, args.max_details))
            if args.phase in ("all", "stock"):
                client.max_points = min(args.max_points, FREE_DAILY_POINTS)
                collect_stock(client, db, args.max_stock)
    except CJQuotaReached as exc:
        print(f"paused: {exc}", flush=True)
    finally:
        if client:
            client.close()
        export_jsonl(db, args.export)
        print(f"snapshot: list={list_count(db)} detail={detail_count(db)} stock={stock_count(db)} db={args.db} export={args.export}", flush=True)
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
