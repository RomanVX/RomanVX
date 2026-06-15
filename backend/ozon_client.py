"""Async client for Ozon Seller API — stocks and FBO sales."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx

from config import OZON_CLIENT_ID, OZON_API_KEY

_log = logging.getLogger(__name__)
_BASE = "https://api-seller.ozon.ru"

_CACHE_TTL = 3600  # 1 hour

_stocks_cache: dict[str, int] = {}
_stocks_ts: float = 0.0
_sales_cache: dict[str, float] = {}
_sales_ts: float = 0.0
_lock = asyncio.Lock()


def _headers() -> dict:
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{_BASE}{path}", headers=_headers(), json=body)
        _log.debug("OZON POST %s → %s", path, r.status_code)
        if not r.is_success:
            _log.error("OZON %s → %s %s", path, r.status_code, r.text[:300])
            r.raise_for_status()
        return r.json()


async def _get_all_skus() -> list[dict]:
    """Paginate /v4/product/info/attributes → list of {sku, offer_id}."""
    items: list[dict] = []
    last_id = ""
    while True:
        data = await _post("/v4/product/info/attributes", {
            "filter": {"visibility": "ALL"},
            "last_id": last_id,
            "limit": 1000,
        })
        result = data.get("result") or []
        if not result:
            break
        for r in result:
            items.append({"sku": str(r.get("sku", "")), "offer_id": r.get("offer_id", "")})
        last_id = data.get("last_id", "")
        if not last_id or len(result) < 1000:
            break
    _log.info("OZON: got %d SKUs", len(items))
    return items


async def _get_stocks_warehouses() -> dict[str, int]:
    """Paginate /v2/analytics/stock_on_warehouses → {offer_id: free_to_sell_amount}."""
    result: dict[str, int] = {}
    offset = 0
    while True:
        data = await _post("/v2/analytics/stock_on_warehouses", {
            "limit": 1000,
            "offset": offset,
            "warehouse_type": "ALL",
        })
        rows = data.get("result", {}).get("rows") or []
        if not rows:
            break
        for row in rows:
            oid = row.get("offer_id", "")
            if not oid:
                continue
            qty = row.get("free_to_sell_amount") or 0
            result[oid] = result.get(oid, 0) + qty
        offset += len(rows)
        if len(rows) < 1000:
            break
    _log.info("OZON stocks warehouses: %d articles", len(result))
    return result


async def get_stocks() -> dict[str, int]:
    """Return {offer_id: available_stock_count} — cached 1 hour."""
    global _stocks_cache, _stocks_ts
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return {}
    if time.monotonic() - _stocks_ts < _CACHE_TTL and _stocks_cache:
        _log.info("OZON stocks: cache hit (%d articles)", len(_stocks_cache))
        return dict(_stocks_cache)
    async with _lock:
        if time.monotonic() - _stocks_ts < _CACHE_TTL and _stocks_cache:
            return dict(_stocks_cache)
        try:
            result = await _get_stocks_warehouses()
            _stocks_cache = result
            _stocks_ts = time.monotonic()
            _log.info("OZON stocks: %d articles (cached)", len(result))
            return dict(result)
        except Exception as e:
            _log.warning("OZON get_stocks error: %s — returning stale cache (%d)", e, len(_stocks_cache))
            return dict(_stocks_cache)


async def get_sales_28d() -> dict[str, float]:
    """Return {offer_id: avg_daily_qty} — cached 1 hour."""
    global _sales_cache, _sales_ts
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return {}
    if time.monotonic() - _sales_ts < _CACHE_TTL and _sales_cache:
        _log.info("OZON sales: cache hit (%d articles)", len(_sales_cache))
        return dict(_sales_cache)
    async with _lock:
        if time.monotonic() - _sales_ts < _CACHE_TTL and _sales_cache:
            return dict(_sales_cache)
        try:
            dt_to   = datetime.utcnow()
            dt_from = dt_to - timedelta(days=28)
            since = dt_from.strftime("%Y-%m-%dT00:00:00.000Z")
            to    = dt_to.strftime("%Y-%m-%dT23:59:59.000Z")

            totals: dict[str, int] = {}
            offset = 0
            while True:
                data = await _post("/v2/posting/fbo/list", {
                    "dir": "ASC",
                    "filter": {"since": since, "to": to, "status": ""},
                    "limit": 1000,
                    "offset": offset,
                })
                result = data.get("result") or []
                if not result:
                    break
                for posting in result:
                    for prod in posting.get("products") or []:
                        oid = prod.get("offer_id", "")
                        qty = prod.get("quantity") or 0
                        if oid:
                            totals[oid] = totals.get(oid, 0) + qty
                offset += len(result)
                if len(result) < 1000:
                    break

            _sales_cache = {k: round(v / 28, 2) for k, v in totals.items()}
            _sales_ts = time.monotonic()
            _log.info("OZON sales 28d: %d articles (cached)", len(_sales_cache))
            return dict(_sales_cache)
        except Exception as e:
            _log.warning("OZON get_sales_28d error: %s — returning stale cache (%d)", e, len(_sales_cache))
            return dict(_sales_cache)
