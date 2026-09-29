"""Small, explicitly requested CJ detail and freight trial.

The point ledger and short quote cache live beside the existing CJ snapshot. No
catalog browse or ordinary product search makes a paid CJ call.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from dotenv import dotenv_values

from app.infrastructure.persistence.cj_catalog import CJCatalog
from app.infrastructure.settings import PROJECT_ROOT


API_BASE = "https://developers.cjdropshipping.com/api2.0/v1"
API_MIRROR = "https://developers.cjdropshipping.cn/api2.0/v1"
QUOTE_TTL_SECONDS = 300
DETAIL_TTL_SECONDS = 600
STOCK_TTL_SECONDS = 600


class CJQuoteError(ValueError):
    pass


def _fresh(timestamp: str | None, seconds: int) -> bool:
    if not timestamp:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)
        return 0 <= age.total_seconds() < seconds
    except ValueError:
        return False


def _amount(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, TypeError):
        return None


class CJLiveQuoteService:
    """One API worker, serial CJ calls, with a persistent pilot point ceiling."""

    def __init__(self, db_path: Path, *, daily_point_limit: int = 1000):
        self.db_path = db_path
        self.daily_point_limit = daily_point_limit
        self._lock = asyncio.Lock()
        self._next_call_at = 0.0
        self._token: str | None = None
        self._token_cached_until = 0.0
        self._api_base = API_BASE

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS cj_pilot_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                points INTEGER NOT NULL,
                result_code TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cj_pilot_quotes (
                cache_key TEXT PRIMARY KEY,
                quoted_at TEXT NOT NULL,
                response_json TEXT NOT NULL
            );
        """)
        try:
            yield db
        finally:
            db.close()

    def _pace(self) -> None:
        wait = self._next_call_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_call_at = time.monotonic() + 1.2

    def _call(self, client: httpx.Client, token: str, db: sqlite3.Connection,
              endpoint: str, *, params: dict | None = None, body: dict | None = None) -> object:
        today = datetime.now(timezone.utc).date().isoformat()
        spent = db.execute("SELECT COALESCE(SUM(points),0) FROM cj_pilot_calls WHERE substr(called_at,1,10)=?", (today,)).fetchone()[0]
        if spent + 10 > self.daily_point_limit:
            raise CJQuoteError("本项目今日 CJ 试运行点数上限已到，请稍后再试")
        self._pace()
        try:
            headers = {"CJ-Access-Token": token}
            if body is None:
                response = client.get(endpoint, params=params, headers=headers)
            else:
                response = client.post(endpoint, json=body, headers=headers)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            # A timed-out request may already have reached CJ and consumed points.
            with db:
                db.execute("INSERT INTO cj_pilot_calls(called_at,endpoint,points,result_code) VALUES(?,?,?,?)",
                           (datetime.now(timezone.utc).isoformat(), endpoint, 10, type(error).__name__))
            raise CJQuoteError("CJ 接口暂时不可用，未生成报价") from error
        with db:
            db.execute("INSERT INTO cj_pilot_calls(called_at,endpoint,points,result_code) VALUES(?,?,?,?)",
                       (datetime.now(timezone.utc).isoformat(), endpoint, 10, str(payload.get("code"))))
        if response.status_code >= 400 or not (payload.get("result") is True or payload.get("success") is True):
            raise CJQuoteError(f"CJ 接口未返回有效结果（{payload.get('code')}）")
        return payload.get("data")

    def _client(self) -> tuple[httpx.Client, str]:
        client = httpx.Client(base_url=self._api_base, timeout=30)
        try:
            if self._token and time.monotonic() < self._token_cached_until:
                return client, self._token
            key = dotenv_values(PROJECT_ROOT / ".env").get("CJdropshipping_key")
            if not key:
                raise CJQuoteError("CJ API Key 未配置")
            self._pace()
            try:
                auth = client.post("/authentication/getAccessToken", json={"apiKey": key}).json()
            except httpx.TransportError:
                if self._api_base != API_BASE:
                    raise
                client.close()
                self._api_base = API_MIRROR
                client = httpx.Client(base_url=self._api_base, timeout=30)
                self._pace()
                auth = client.post("/authentication/getAccessToken", json={"apiKey": key}).json()
            token = (auth.get("data") or {}).get("accessToken")
            if not auth.get("result") or not token:
                raise CJQuoteError("CJ 授权失败，无法查询详情或物流")
            self._token = token
            self._token_cached_until = time.monotonic() + 3600
            return client, token
        except CJQuoteError:
            client.close()
            raise
        except (httpx.HTTPError, ValueError) as error:
            client.close()
            raise CJQuoteError("CJ 授权接口暂时不可用") from error

    def _product(self, db: sqlite3.Connection, product_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM products WHERE pid=?", (product_id,)).fetchone()
        if row is None:
            raise CJQuoteError("商品不在当前 CJ 快照中")
        return row

    def _ensure_detail(self, db: sqlite3.Connection, client: httpx.Client, token: str,
                       row: sqlite3.Row, *, refresh: bool) -> sqlite3.Row:
        if row["detail_json"] and (not refresh or _fresh(row["detail_fetched_at"], DETAIL_TTL_SECONDS)):
            return row
        detail = self._call(client, token, db, "/product/query", params={"pid": row["pid"]})
        if not isinstance(detail, dict) or str(detail.get("pid")) != row["pid"]:
            raise CJQuoteError("CJ 未返回这件商品的规格详情")
        with db:
            db.execute("UPDATE products SET detail_json=?,detail_fetched_at=?,detail_status='ok' WHERE pid=?",
                       (json.dumps(detail, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), row["pid"]))
        return self._product(db, row["pid"])

    def _ensure_stock(self, db: sqlite3.Connection, client: httpx.Client, token: str,
                      row: sqlite3.Row) -> sqlite3.Row:
        if row["inventory_json"] and _fresh(row["inventory_fetched_at"], STOCK_TTL_SECONDS):
            return row
        stock = self._call(client, token, db, "/product/stock/getInventoryByPid", params={"pid": row["pid"]})
        if not isinstance(stock, dict):
            raise CJQuoteError("CJ 未返回可核验的库存")
        with db:
            db.execute("UPDATE products SET inventory_json=?,inventory_fetched_at=?,inventory_status='ok' WHERE pid=?",
                       (json.dumps(stock, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), row["pid"]))
        return self._product(db, row["pid"])

    def _detail(self, product_id: str) -> dict:
        with self._db() as db:
            row = self._product(db, product_id)
            if not row["detail_json"]:
                client, token = self._client()
                try:
                    row = self._ensure_detail(db, client, token, row, refresh=False)
                finally:
                    client.close()
            return CJCatalog._card(row)

    async def detail(self, product_id: str) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._detail, product_id)

    def _quote(self, product_id: str, sku_id: str, ship_to: str, quantity: int) -> dict:
        destination = ship_to.strip().upper()
        if len(destination) != 2 or not destination.isalpha():
            raise CJQuoteError("目的国须为两位国家代码，如 CN")
        if not 1 <= quantity <= 10:
            raise CJQuoteError("试算数量须为 1 至 10")
        cache_key = f"{product_id}:{sku_id}:{destination}:{quantity}"
        with self._db() as db:
            cached = db.execute("SELECT quoted_at,response_json FROM cj_pilot_quotes WHERE cache_key=?", (cache_key,)).fetchone()
            if cached and _fresh(cached["quoted_at"], QUOTE_TTL_SECONDS):
                return {**json.loads(cached["response_json"]), "cache_hit": True}
            row = self._product(db, product_id)
            client, token = self._client()
            try:
                row = self._ensure_detail(db, client, token, row, refresh=True)
                detail = json.loads(row["detail_json"])
                variants = detail.get("variants") or []
                variant = (next((item for item in variants if sku_id in {str(item.get("variantSku")), str(item.get("vid"))}), None)
                           if sku_id else next(iter(variants), None))
                if not variant:
                    raise CJQuoteError("所选规格不在最新 CJ 商品详情中，请重新选择")
                price = _amount(variant.get("variantSellPrice"))
                if price is None or not variant.get("vid"):
                    raise CJQuoteError("CJ 未提供可核验的规格价格或编号")
                row = self._ensure_stock(db, client, token, row)
                inventory = json.loads(row["inventory_json"])
                variant_stock = next((item for item in inventory.get("variantInventories") or []
                                      if str(item.get("vid")) == str(variant["vid"])), None)
                origins = sorted((item for item in (variant_stock or {}).get("inventory") or []
                                  if item.get("countryCode") and (_amount(item.get("totalInventory")) or 0) > 0),
                                 key=lambda item: -float(item.get("cjInventory") or item.get("totalInventory") or 0))
                if not origins:
                    raise CJQuoteError("CJ 当前库存未提供该规格的可用发货仓，无法试算")
                origin = str(origins[0]["countryCode"])
                options = self._call(client, token, db, "/logistic/freightCalculate", body={
                    "startCountryCode": origin, "endCountryCode": destination,
                    "products": [{"quantity": quantity, "vid": str(variant["vid"])}],
                })
            finally:
                client.close()
            valid = []
            for item in options if isinstance(options, list) else []:
                if not isinstance(item, dict):
                    continue
                cost = _amount(item.get("totalPostageFee"))
                if cost is not None and item.get("logisticName"):
                    valid.append((cost, item))
            if not valid:
                raise CJQuoteError("CJ 本次未返回可用物流路线；不能据此断言永久不可配送")
            postage, option = min(valid, key=lambda pair: pair[0])
            subtotal = price * quantity
            taxes = _amount(option.get("taxesFee"))
            clearance = _amount(option.get("clearanceOperationFee"))
            result = {
                "status": "quoted", "source": "CJdropshipping", "quote_kind": "CJ 物流试算，非最终支付价",
                "product_id": product_id, "sku_id": str(variant.get("variantSku")),
                "variant_id": str(variant["vid"]), "quantity": quantity,
                "selection_mode": "selected_sku" if sku_id else "first_variant_assumed",
                "ship_from_warehouse": origin, "ship_to": destination,
                "shipping_method": str(option["logisticName"]), "route_count": len(valid),
                "product_unit_usd": float(price), "product_subtotal_usd": float(subtotal),
                "shipping_and_cj_fees_usd": float(postage), "cj_trial_total_usd": float(subtotal + postage),
                "cj_taxes_fee_usd": float(taxes) if taxes is not None else None,
                "cj_clearance_fee_usd": float(clearance) if clearance is not None else None,
                "fee_status": "cj_reported" if taxes is not None and clearance is not None else "tax_or_clearance_unknown",
                "quoted_at": datetime.now(timezone.utc).isoformat(),
                "price_checked_at": row["detail_fetched_at"], "stock_checked_at": row["inventory_fetched_at"],
                "cache_hit": False,
            }
            with db:
                db.execute("INSERT OR REPLACE INTO cj_pilot_quotes(cache_key,quoted_at,response_json) VALUES(?,?,?)",
                           (cache_key, result["quoted_at"], json.dumps(result, ensure_ascii=False)))
            return result

    async def quote(self, product_id: str, sku_id: str, ship_to: str, quantity: int = 1) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._quote, product_id, sku_id, ship_to, quantity)
