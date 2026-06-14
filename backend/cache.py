"""In-memory cache for raw WB API data.

Strategy: fetch the last MAX_DAYS days ONCE, cache for TTL seconds.
All endpoints filter records from this single dataset — the WB Sales API
(/supplier/sales) is called at most once per TTL, regardless of how many
date sub-ranges the dashboard queries (e.g. current + previous period).
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

_log = logging.getLogger(__name__)

TTL      = 600   # 10 minutes
MAX_DAYS = 90    # how far back to fetch from WB API


class _Store:
    __slots__ = ("sales", "orders", "stocks", "fetched_at")

    def __init__(self):
        self.sales: list      = []
        self.orders: list     = []
        self.stocks: list     = []
        self.fetched_at: float = 0.0


_store = _Store()
_lock  = asyncio.Lock()


def _filter(records: list, date_from: datetime, date_to: datetime) -> list:
    """Keep records whose 'date' field falls within [date_from, date_to]."""
    lo = date_from.strftime("%Y-%m-%d")
    hi = date_to.strftime("%Y-%m-%d")
    return [r for r in records if lo <= r.get("date", "")[:10] <= hi]


async def get_raw_data(date_from: datetime, date_to: datetime) -> tuple[list, list, list]:
    """Return (sales, orders, stocks) for the requested sub-period.

    Data comes from the in-memory 90-day cache; WB API is hit only on a
    cache miss (first request or after TTL expires).
    """
    global _store

    age = time.monotonic() - _store.fetched_at
    if age >= TTL:
        async with _lock:
            # re-check inside the lock (another coroutine may have refreshed)
            age = time.monotonic() - _store.fetched_at
            if age >= TTL:
                await _refresh()

    _log.debug("Serving from cache (age %.0fs): filtering %s → %s",
               time.monotonic() - _store.fetched_at,
               date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d"))

    sales  = _filter(_store.sales,  date_from, date_to)
    orders = _filter(_store.orders, date_from, date_to)
    return sales, orders, _store.stocks   # stocks are point-in-time, no date filter


async def _refresh() -> None:
    """Fetch the last MAX_DAYS days from WB API and store in _store."""
    import wb_client

    fetch_from = datetime.utcnow() - timedelta(days=MAX_DAYS)
    fetch_to   = datetime.utcnow()

    _log.info("Fetching WB API: last %d days (%s → %s)",
              MAX_DAYS, fetch_from.strftime("%Y-%m-%d"), fetch_to.strftime("%Y-%m-%d"))

    # Sequential: avoid parallel Sales/Orders calls triggering 429 on Sales
    sales  = await wb_client.get_sales(fetch_from, fetch_to)
    orders = await wb_client.get_orders(fetch_from, fetch_to)
    stocks = await wb_client.get_stocks()

    _store.sales      = sales
    _store.orders     = orders
    _store.stocks     = stocks
    _store.fetched_at = time.monotonic()

    _log.info("Cache refreshed: %d sales, %d orders, %d stock lines",
              len(sales), len(orders), len(stocks))


def invalidate() -> None:
    _store.fetched_at = 0.0
