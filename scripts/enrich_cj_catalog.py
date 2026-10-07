"""Resume detail enrichment of existing products; expose only observed CJ URLs.

No listings, inventory or freight calls. Account and local daily ceilings apply.
Use --follow to wait for point replenishment and UTC daily reset. A STOP file
next to the progress report requests a clean stop. Credentials stay in memory.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.infrastructure.cj_product_links import candidate_product_url, valid_product_url
from scripts.sync_cj_catalog import open_db, utc_now

API_BASES = ("https://developers.cjdropshipping.com/api2.0/v1",
             "https://developers.cjdropshipping.cn/api2.0/v1")
DEFAULT_REPORT = ROOT / "data/cj_enrichment/progress.json"
DEFAULT_PUBLISH = ROOT / "data/cj_published/cj_catalog.sqlite3"
RATE_CODES = {429, 1600200, 1600201, 16900500}
UNAVAILABLE_CODES = {1602001, 1602002}


class CJTransientError(RuntimeError):
    """A network interruption may be retried after reserving its possible cost."""


def migrate(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(products)")}
    for name in ("source_url", "source_url_status", "source_url_checked_at", "source_url_evidence",
                 "source_url_page_status", "source_url_page_checked_at", "enrichment_attempted_at"):
        if name not in columns:
            db.execute(f"ALTER TABLE products ADD COLUMN {name} TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS cj_enrichment_calls(
        id INTEGER PRIMARY KEY,at TEXT NOT NULL,pid TEXT,endpoint TEXT NOT NULL,
        points_reserved INTEGER NOT NULL,result_code TEXT,points_json TEXT)""")
    db.commit()


def seed_links(db: sqlite3.Connection, seeds: list[dict]) -> int:
    count = 0
    for seed in seeds:
        row = db.execute("SELECT list_json FROM products WHERE pid=?", (seed["pid"],)).fetchone()
        if row is None or not valid_product_url(seed["url"], seed["pid"]):
            continue
        listing = json.loads(row[0])
        if seed["spu"] not in {listing.get("sku"), listing.get("spu"), listing.get("productSku")}:
            raise ValueError(f"Observed page SPU mismatch for {seed['pid']}")
        db.execute("""UPDATE products SET source_url=?,source_url_status='observed',
            source_url_checked_at=?,source_url_evidence=? WHERE pid=? AND
            (source_url_status IS NULL OR source_url_status <> 'page_verified')""",
            (seed["url"], seed["observed_at"], json.dumps(seed, ensure_ascii=False), seed["pid"]))
        count += 1
    db.commit()
    return count


def seed_candidates(db: sqlite3.Connection) -> None:
    for pid, listed in db.execute("SELECT pid,list_json FROM products WHERE source_url IS NULL").fetchall():
        candidate = candidate_product_url(str(json.loads(listed).get("nameEn") or ""), pid)
        if candidate:
            db.execute("UPDATE products SET source_url=?,source_url_status='derived' WHERE pid=?", (candidate, pid))
    db.commit()


def store_detail(db: sqlite3.Connection, pid: str, payload: dict) -> str:
    code = payload.get("code")
    if code in UNAVAILABLE_CODES:
        db.execute("UPDATE products SET detail_status=?,enrichment_attempted_at=? WHERE pid=?",
                   (f"unavailable:{code}", utc_now(), pid))
        db.commit()
        return "unavailable"
    data = payload.get("data")
    if payload.get("result") is not True or not isinstance(data, dict):
        raise RuntimeError(f"CJ detail failed: code={code}")
    if str(data.get("pid", "")).casefold() != pid.casefold():
        raise RuntimeError(f"CJ detail identity mismatch for {pid}")
    db.execute("""UPDATE products SET detail_json=?,detail_fetched_at=?,detail_status='ok',
        enrichment_attempted_at=? WHERE pid=?""", (json.dumps(data, ensure_ascii=False), utc_now(), utc_now(), pid))
    db.commit()
    return "ok"


def page_status(response: httpx.Response, pid: str, spu: str) -> str:
    """HTTP 200 from a challenge/login/error page is not product verification."""
    body = response.text
    lower = body.lower()
    if "validation.html" in str(response.url) or "captcha" in lower or "verify you are human" in lower:
        return "verification_required"
    if response.status_code == 404:
        return "not_found"
    if response.status_code != 200 or not valid_product_url(str(response.url), pid):
        return "unconfirmed"
    # Require source SKU on rendered product markup, not merely in script state.
    if (spu and re_search_visible_sku(body, spu)
            and ("description" in lower) and ("product" in lower)):
        return "page_verified"
    return "unconfirmed"


def re_search_visible_sku(body: str, spu: str) -> bool:
    import re
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", body, flags=re.I | re.S)
    visible = re.sub(r"<[^>]*>", " ", visible)
    return spu.casefold() in visible.casefold()


class API:
    def __init__(self, key: str, db: sqlite3.Connection, ceiling: int):
        self.db, self.ceiling = db, ceiling
        self.http = None
        self.next_request = 0.0
        self.points: dict = {}
        self.point_day: str | None = None
        for base in API_BASES:
            client = httpx.Client(base_url=base, timeout=30)
            try:
                auth = client.post("/authentication/getAccessToken", json={"apiKey": key}).json()
            except (httpx.TransportError, ValueError):
                client.close()
                continue
            token = (auth.get("data") or {}).get("accessToken")
            if auth.get("result") is not True or not token:
                client.close()
                raise RuntimeError(f"CJ authentication failed: code={auth.get('code')}")
            self.http, self.token = client, token
            self.next_request = time.monotonic() + 1.5
            break
        if self.http is None:
            raise RuntimeError("CJ authentication endpoints unreachable")
        self.refresh_points()

    def request(self, endpoint: str, pid: str | None = None, cost: int = 0) -> tuple[int, dict]:
        # Reserve points before sending: a timeout may still consume CJ points.
        at = utc_now()
        with self.db:
            cursor = self.db.execute("""INSERT INTO cj_enrichment_calls(at,pid,endpoint,points_reserved)
                VALUES(?,?,?,?)""", (at, pid, endpoint, cost))
        time.sleep(max(0, self.next_request - time.monotonic()))
        self.next_request = time.monotonic() + 1.5
        try:
            response = self.http.get(endpoint, params={"pid": pid} if pid else None,
                                     headers={"CJ-Access-Token": self.token})
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            with self.db:
                self.db.execute("UPDATE cj_enrichment_calls SET result_code=? WHERE id=?",
                                (type(error).__name__, cursor.lastrowid))
            raise CJTransientError(f"CJ request interrupted: {type(error).__name__}") from None
        points = payload.get("pointsInfo") or {}
        if points:
            if not all(type(points.get(k)) in (int, float) and points[k] >= 0
                       for k in ("usedToday", "remaining", "total")):
                raise RuntimeError("CJ points counter invalid; collector stopped")
            today = datetime.now(timezone.utc).date().isoformat()
            if self.point_day == today:
                points["usedToday"] = max(points["usedToday"], self.points.get("usedToday", 0))
            self.points = points
            self.point_day = today
        with self.db:
            self.db.execute("UPDATE cj_enrichment_calls SET result_code=?,points_json=? WHERE id=?",
                            (str(payload.get("code")), json.dumps(points), cursor.lastrowid))
            self.db.execute("INSERT INTO run_log(at,endpoint,points_used_today,points_remaining,result_code) VALUES(?,?,?,?,?)",
                            (at, endpoint, points.get("usedToday"), points.get("remaining"), str(payload.get("code"))))
        return response.status_code, payload

    def refresh_points(self) -> None:
        status, payload = self.request("/product/getCategory")
        if status != 200 or payload.get("result") is not True:
            raise RuntimeError(f"CJ points check failed: code={payload.get('code')}")
        if not all(type(self.points.get(k)) in (int, float) and self.points[k] >= 0
                   for k in ("usedToday", "remaining", "total")):
            raise RuntimeError("CJ points counter missing; collector stopped")

    def wait_seconds(self) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        if self.point_day != day:
            self.refresh_points()
        reserved = self.db.execute("SELECT COALESCE(SUM(points_reserved),0) FROM cj_enrichment_calls WHERE substr(at,1,10)=?", (day,)).fetchone()[0]
        if reserved + 10 > self.ceiling or self.points["usedToday"] + 10 > self.ceiling:
            now = datetime.now(timezone.utc)
            reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=1, microsecond=0)
            return max(1, int((reset - now).total_seconds()))
        return 65 if self.points["remaining"] < 10 else 0


def write_report(db: sqlite3.Connection, path: Path, state: str, **extra) -> None:
    counts = db.execute("SELECT count(*),sum(detail_json IS NOT NULL),sum(detail_status LIKE 'unavailable:%'),sum(detail_json IS NULL AND COALESCE(detail_status,'') NOT LIKE 'unavailable:%') FROM products").fetchone()
    report = {"updated_at": utc_now(), "state": state, "pid": os.getpid(),
              "total": counts[0], "details": counts[1] or 0, "unavailable": counts[2] or 0,
              "pending_details": counts[3] or 0,
              "links": dict(db.execute("SELECT COALESCE(source_url_status,'missing'),count(*) FROM products GROUP BY source_url_status")),
              "page_checks": dict(db.execute("SELECT COALESCE(source_url_page_status,'unchecked'),count(*) FROM products GROUP BY source_url_page_status")),
              "categories": [{"category": category, "total": total, "details": details or 0}
                             for category,total,details in db.execute("SELECT first_category,count(*),sum(detail_json IS NOT NULL) FROM products GROUP BY first_category")], **extra}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def publish_snapshot(db: sqlite3.Connection, path: Path) -> Path:
    """Publish a standalone SQLite file; readers never need the writer's WAL.

    Versioned filenames avoid Windows denying replacement of a Docker-open file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.time_ns():020d}"
    published = path.with_name(f"{path.stem}.snapshot.{stamp}{path.suffix}")
    temporary = published.with_suffix(".publishing")
    with closing(sqlite3.connect(temporary)) as target:
        db.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    temporary.replace(published)
    versions = sorted(path.parent.glob(f"{path.stem}.snapshot.*{path.suffix}"), key=lambda item: item.name)
    for old in versions[:-3]:
        try:
            old.unlink(missing_ok=True)
        except PermissionError:
            pass  # An active reader can finish; retry cleanup on the next publication.
    if len(list(path.parent.glob(f"{path.stem}.snapshot.*{path.suffix}"))) > 10:
        raise RuntimeError("Published snapshots still locked by readers; stopped before disk growth")
    return published


@contextmanager
def single_worker(path: Path):
    # Windows releases the OS lock if the process dies; a stale file is harmless.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            if not lock.read(1):
                lock.write(b"0"); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def run(args) -> int:
    db = open_db(args.db)
    migrate(db)
    seed_links(db, json.loads((ROOT / "scripts/cj_observed_product_links.json").read_text(encoding="utf-8")))
    seed_candidates(db)
    publish_snapshot(db, args.publish)
    write_report(db, args.report, "ready")
    if args.prepare_only:
        db.close()
        return 0
    api = None
    try:
        key = dotenv_values(ROOT / ".env").get("CJdropshipping_key")
        if not key:
            raise RuntimeError("CJ API Key not configured")
        api = API(key, db, args.daily_points)
        done, failures, pages_blocked = 0, 0, False
        last_publish = time.monotonic()
        # Round-robin categories instead of exhausting one category first.
        rows = db.execute("""SELECT pid,list_json,source_url FROM (
            SELECT *,row_number() OVER(PARTITION BY first_category ORDER BY
                CASE WHEN source_url_status='observed' THEN 0 ELSE 1 END,pid) AS rank
            FROM products WHERE (detail_json IS NULL OR (source_url_status='observed' AND ?))
            AND COALESCE(detail_status,'') NOT LIKE 'unavailable:%'
            ) ORDER BY CASE WHEN source_url_status='observed' THEN 0 ELSE 1 END,rank,first_category""", (args.refresh_observed,)).fetchall()
        for pid, listed, source_url in rows:
            if args.limit is not None and done >= args.limit:
                break
            while True:
                if args.report.with_name("STOP").exists():
                    publish_snapshot(db, args.publish)
                    write_report(db, args.report, "stopped", points=api.points, processed_this_run=done)
                    return 0
                wait = api.wait_seconds()
                if wait:
                    if time.monotonic() - last_publish >= 120:
                        publish_snapshot(db, args.publish)
                        last_publish = time.monotonic()
                    write_report(db, args.report, "waiting_for_points", points=api.points,
                                 retry_after_seconds=wait, processed_this_run=done)
                    if not args.follow:
                        return 0
                    # Keep STOP responsive, and refresh a free endpoint once per minute.
                    time.sleep(min(wait, 55))
                    api.refresh_points()
                    continue
                try:
                    status, payload = api.request("/product/query", pid, 10)
                except CJTransientError as error:
                    failures += 1
                    write_report(db, args.report, "network_retry", points=api.points,
                                 processed_this_run=done, error=str(error), retry_attempt=failures)
                    if not args.follow or failures >= 5:
                        raise
                    time.sleep(55)
                    continue
                if status == 429 or payload.get("code") in RATE_CODES:
                    write_report(db, args.report, "rate_limited", points=api.points, processed_this_run=done)
                    if not args.follow:
                        return 0
                    time.sleep(55)
                    api.refresh_points()
                    continue
                if status >= 400:
                    raise RuntimeError(f"CJ detail HTTP error: {status}")
                try:
                    outcome = store_detail(db, pid, payload)
                    failures = 0
                except RuntimeError as error:
                    failures += 1
                    with db:
                        db.execute("UPDATE products SET detail_status='error',enrichment_attempted_at=? WHERE pid=?", (utc_now(), pid))
                    outcome = str(error)
                    if failures >= 3:
                        raise
                done += 1
                if done == 5 or time.monotonic() - last_publish >= 120:
                    publish_snapshot(db, args.publish)
                    last_publish = time.monotonic()
                # Page checks are opt-in, low rate, and cease on the first challenge.
                if args.verify_pages and not pages_blocked and source_url:
                    listing = json.loads(listed)
                    with httpx.Client(timeout=30, follow_redirects=True) as web:
                        try:
                            page = web.get(source_url)
                            checked = page_status(page, pid, str(listing.get("spu") or listing.get("sku") or ""))
                        except httpx.HTTPError:
                            checked = "unconfirmed"
                    with db:
                        db.execute("UPDATE products SET source_url_page_status=?,source_url_page_checked_at=? WHERE pid=?", (checked, utc_now(), pid))
                        if checked == "page_verified":
                            db.execute("UPDATE products SET source_url_status='page_verified',source_url_checked_at=?,source_url_evidence=? WHERE pid=?",
                                       (utc_now(), json.dumps({"kind":"live_page_sku_match","url":source_url}), pid))
                    pages_blocked = checked == "verification_required"
                write_report(db, args.report, "running", points=api.points, processed_this_run=done,
                             page_verification_blocked=pages_blocked)
                print(f"processed={done} pid={pid} detail={outcome} used_today={api.points.get('usedToday')}", flush=True)
                break
        pending = db.execute("SELECT count(*) FROM products WHERE detail_json IS NULL AND COALESCE(detail_status,'') NOT LIKE 'unavailable:%'").fetchone()[0]
        state = "batch_finished" if args.limit is not None else "details_finished" if not pending else "finished_with_errors"
        publish_snapshot(db, args.publish)
        write_report(db, args.report, state,
                     points=api.points, processed_this_run=done, page_verification_blocked=pages_blocked)
        return 0
    except (RuntimeError, httpx.HTTPError, ValueError, OSError) as error:
        # Log only safe error messages/types; never auth bodies or request headers.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        write_report(db, args.report, "failed", error=message, points=api.points if api else {})
        print(f"collector stopped: {message}", flush=True)
        return 1
    finally:
        if api is not None:
            api.http.close()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/cj_catalog.sqlite3")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--publish", type=Path, default=DEFAULT_PUBLISH)
    parser.add_argument("--daily-points", type=int, default=45000)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--verify-pages", action="store_true")
    parser.add_argument("--refresh-observed", action="store_true", help="refresh existing details for the observed product-page links first")
    args = parser.parse_args()
    if args.publish.resolve() == args.db.resolve():
        parser.error("publish path must differ from the writable database")
    if not 10 <= args.daily_points <= 45000 or args.limit is not None and args.limit < 1:
        parser.error("daily points must be 10..45000; limit must be positive")
    with single_worker(args.db.with_suffix(".enrichment.lock")):
        return run(args)


if __name__ == "__main__":
    sys.exit(main())
