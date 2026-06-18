"""Async client for Yandex Market Partner API — stocks and orders."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx

from config import YM_API_KEY, YM_CAMPAIGN_ID, YM_BUSINESS_ID

_log = logging.getLogger(__name__)
_BASE = "https://api.partner.market.yandex.ru"

_CACHE_TTL = 3600  # 1 hour — YM rate limit is 10000 pts/hr

_stocks_cache: dict[str, int] = {}
_stocks_ts: float = 0.0
_sales_cache: dict[str, float] = {}
_sales_ts: float = 0.0
_lock = asyncio.Lock()


def _headers() -> dict:
    return {
        "Api-Key": YM_API_KEY,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{_BASE}{path}", headers=_headers(), json=body)
        if not r.is_success:
            _log.warning("[YM] POST %s → %s | resp[:300]=%s", path, r.status_code, r.text[:300])
            r.raise_for_status()
        return r.json()


async def _get(path: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{_BASE}{path}", headers=_headers(), params=params)
        _log.info("[YM] GET %s params=%s → %s | resp[:400]=%s",
                  path, params, r.status_code, r.text[:400])
        if not r.is_success:
            r.raise_for_status()
        return r.json()


async def _try_stats_orders_post(date_from: str, date_to: str) -> dict[str, int]:
    """POST /campaigns/{id}/stats/orders — lightweight stats endpoint."""
    try:
        data = await _post(
            f"/campaigns/{YM_CAMPAIGN_ID}/stats/orders",
            {"dateFrom": date_from, "dateTo": date_to, "groupBy": "DAY"},
        )
        totals: dict[str, int] = {}
        for order in (data.get("result") or {}).get("orders") or []:
            if (order.get("status") or "") == "CANCELLED":
                continue
            for item in order.get("items") or []:
                oid = item.get("shopSku") or item.get("offerId") or ""
                qty = item.get("count") or item.get("initialCount") or 0
                if oid and qty:
                    totals[oid] = totals.get(oid, 0) + qty
        _log.info("[YM] POST stats/orders → %d articles", len(totals))
        return totals
    except Exception as e:
        _log.warning("[YM] POST stats/orders failed: %s", e)
        return {}


async def _try_campaign_orders_stats(date_from: str, date_to: str) -> dict[str, int]:
    """GET /campaigns/{id}/orders/stats — order statistics by period."""
    try:
        data = await _get(
            f"/campaigns/{YM_CAMPAIGN_ID}/orders/stats",
            {"dateFrom": date_from, "dateTo": date_to, "groupBy": "DAY"},
        )
        totals: dict[str, int] = {}
        for order in (data.get("result") or {}).get("orders") or []:
            if (order.get("status") or "") == "CANCELLED":
                continue
            for item in order.get("items") or []:
                oid = item.get("shopSku") or item.get("offerId") or ""
                qty = item.get("count") or item.get("initialCount") or 0
                if oid and qty:
                    totals[oid] = totals.get(oid, 0) + qty
        _log.info("[YM] campaign/orders/stats → %d articles", len(totals))
        return totals
    except Exception as e:
        _log.warning("[YM] campaign/orders/stats failed: %s", e)
        return {}


async def _try_business_orders_post(date_from: str, date_to: str) -> dict[str, int]:
    """POST /businesses/{id}/orders — paginated, cached so rate limit OK."""
    try:
        totals: dict[str, int] = {}
        page_token: str | None = None
        while True:
            body: dict = {
                "dateFrom": date_from,
                "dateTo": date_to,
                "status": ["DELIVERED"],
                "limit": 50,
            }
            if page_token:
                body["pageToken"] = page_token
            data = await _post(f"/businesses/{YM_BUSINESS_ID}/orders", body)
            orders = (data.get("result") or {}).get("orders") or []
            for order in orders:
                for item in order.get("items") or []:
                    oid = item.get("offerId") or (item.get("offer") or {}).get("offerId", "")
                    qty = item.get("initialCount") or item.get("count") or 0
                    if oid and qty:
                        totals[oid] = totals.get(oid, 0) + qty
            paging = (data.get("result") or {}).get("paging") or {}
            page_token = paging.get("nextPageToken")
            if not orders or not page_token:
                break
        _log.info("[YM] POST business/orders → %d articles", len(totals))
        return totals
    except Exception as e:
        _log.warning("[YM] POST business/orders failed: %s", e)
        return {}


def _parse_orders(orders: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for order in orders:
        status   = order.get("status") or ""
        raw_date = (order.get("creationDate") or "")[:10]
        try:
            date_str = datetime.strptime(raw_date, "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            date_str = raw_date
        for item in order.get("items") or []:
            oid = item.get("offerId") or (item.get("offer") or {}).get("offerId", "")
            qty = item.get("count") or item.get("initialCount") or 0
            buyer_before = float(item.get("buyerPriceBeforeDiscount") or 0)
            buyer_price  = float(item.get("buyerPrice") or item.get("price") or 0)
            unit_price   = buyer_before or buyer_price
            revenue      = unit_price * (qty or 1)
            if oid and qty:
                rows.append({"date": date_str, "shop_sku": oid, "qty": qty,
                             "revenue": revenue, "status": status})
    return rows


async def _fetch_day(sem: asyncio.Semaphore, day: datetime) -> list[dict]:
    """Fetch all orders for a single day (pagination broken → 1 request per day)."""
    fmt = day.strftime("%d-%m-%Y")
    async with sem:
        try:
            data = await _get(
                f"/campaigns/{YM_CAMPAIGN_ID}/orders",
                {"fromDate": fmt, "toDate": fmt, "limit": 50, "page": 1},
            )
        except Exception as e:
            _log.warning("[YM] day %s failed: %s", fmt, e)
            return []
    return _parse_orders(data.get("orders") or [])


async def get_sales_detail(date_from: str, date_to: str) -> list[dict]:
    """Return [{date, shop_sku, qty, revenue, status}] for given range — no cache.

    YM campaigns pagination is broken (always returns same 50 orders regardless
    of page). Workaround: 1-day requests, all fired concurrently (semaphore=10).
    revenue = buyerPriceBeforeDiscount × qty (matches Excel "Заказано на сумму").
    """
    if not YM_API_KEY or not YM_CAMPAIGN_ID:
        return []

    dt_from = datetime.strptime(date_from, "%Y-%m-%d")
    dt_to   = datetime.strptime(date_to,   "%Y-%m-%d")

    days: list[datetime] = []
    cur = dt_from
    while cur <= dt_to:
        days.append(cur)
        cur += timedelta(days=1)

    sem = asyncio.Semaphore(10)
    results = await asyncio.gather(*[_fetch_day(sem, d) for d in days])

    rows: list[dict] = [r for day_rows in results for r in day_rows]
    delivered = sum(1 for r in rows if r["status"] == "DELIVERED")
    _log.info("[YM] sales_detail done: days=%d rows=%d delivered=%d for %s–%s",
              len(days), len(rows), delivered, date_from, date_to)
    return rows


async def get_stocks() -> dict[str, int]:
    """Return {offer_id: FIT count} — cached 1 hour."""
    global _stocks_cache, _stocks_ts
    if not YM_API_KEY or not YM_CAMPAIGN_ID:
        return {}
    if time.monotonic() - _stocks_ts < _CACHE_TTL and _stocks_cache:
        _log.info("YM stocks: cache hit (%d articles)", len(_stocks_cache))
        return dict(_stocks_cache)
    async with _lock:
        if time.monotonic() - _stocks_ts < _CACHE_TTL and _stocks_cache:
            return dict(_stocks_cache)
        try:
            data = await _post(
                f"/campaigns/{YM_CAMPAIGN_ID}/offers/stocks",
                {"limit": 100},
            )
            result: dict[str, int] = {}
            for wh in (data.get("result") or {}).get("warehouses") or []:
                for offer in wh.get("offers") or []:
                    offer_id = offer.get("offerId", "")
                    if not offer_id:
                        continue
                    for stock in offer.get("stocks") or []:
                        if stock.get("type") == "FIT":
                            result[offer_id] = result.get(offer_id, 0) + (stock.get("count") or 0)
            _stocks_cache = result
            _stocks_ts = time.monotonic()
            _log.info("YM stocks: %d articles (cached): %s", len(result), sorted(result.keys()))
            return dict(result)
        except Exception as e:
            _log.warning("YM get_stocks error: %s — returning stale cache (%d)", e, len(_stocks_cache))
            return dict(_stocks_cache)


async def get_sales_28d() -> dict[str, float]:
    """Return {offer_id: avg_daily_qty} — cached 1 hour."""
    global _sales_cache, _sales_ts
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return {}
    if time.monotonic() - _sales_ts < _CACHE_TTL and _sales_cache:
        _log.info("YM sales: cache hit (%d articles)", len(_sales_cache))
        return dict(_sales_cache)
    async with _lock:
        if time.monotonic() - _sales_ts < _CACHE_TTL and _sales_cache:
            return dict(_sales_cache)
        try:
            dt_to   = datetime.utcnow()
            dt_from = dt_to - timedelta(days=28)
            date_from = dt_from.strftime("%Y-%m-%d")
            date_to   = dt_to.strftime("%Y-%m-%d")

            totals: dict[str, int] = {}

            # Try POST /campaigns/{id}/stats/orders (cheapest on rate limit)
            totals = await _try_stats_orders_post(date_from, date_to)
            if not totals:
                # Try GET /campaigns/{id}/orders/stats
                totals = await _try_campaign_orders_stats(date_from, date_to)
            if not totals:
                # Fallback: POST /businesses/{id}/orders paginated
                totals = await _try_business_orders_post(date_from, date_to)

            _sales_cache = {k: round(v / 28, 2) for k, v in totals.items()}
            _sales_ts = time.monotonic()
            _log.info("YM sales 28d: %d articles (cached)", len(_sales_cache))
            return dict(_sales_cache)
        except Exception as e:
            _log.warning("YM get_sales_28d error: %s — returning stale cache (%d)", e, len(_sales_cache))
            return dict(_sales_cache)
