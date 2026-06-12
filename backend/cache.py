"""In-memory cache for raw WB API data.

All dashboard endpoints share a single fetch of (sales, orders, stocks).
WB API is called at most once per TTL window regardless of how many
endpoints are hit simultaneously.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger(__name__)

TTL = 300  # seconds — 5 minutes


@dataclass
class _CacheEntry:
    sales: list
    orders: list
    stocks: list
    fetched_at: float
    days: int


_cache: Optional[_CacheEntry] = None
_lock = asyncio.Lock()


async def get_raw_data(days: int) -> tuple[list, list, list]:
    """Return (sales, orders, stocks), fetching from WB only when stale."""
    global _cache

    # Fast path — no lock needed for a read
    if _cache is not None and _cache.days == days and (time.monotonic() - _cache.fetched_at) < TTL:
        _log.debug("Cache hit (age %.0fs)", time.monotonic() - _cache.fetched_at)
        return _cache.sales, _cache.orders, _cache.stocks

    async with _lock:
        # Re-check after acquiring the lock (another coroutine may have refreshed)
        if _cache is not None and _cache.days == days and (time.monotonic() - _cache.fetched_at) < TTL:
            return _cache.sales, _cache.orders, _cache.stocks

        _log.info("Cache miss — fetching from WB API (days=%d)", days)

        import wb_client
        from datetime import datetime, timedelta

        date_to = datetime.utcnow()
        date_from = date_to - timedelta(days=days)

        sales, orders, stocks = await asyncio.gather(
            wb_client.get_sales(date_from, date_to),
            wb_client.get_orders(date_from, date_to),
            wb_client.get_stocks(),
        )

        _cache = _CacheEntry(
            sales=sales,
            orders=orders,
            stocks=stocks,
            fetched_at=time.monotonic(),
            days=days,
        )
        _log.info("Cache refreshed: %d sales, %d orders, %d stock lines",
                  len(sales), len(orders), len(stocks))

    return _cache.sales, _cache.orders, _cache.stocks


def invalidate() -> None:
    """Force next request to re-fetch from WB."""
    global _cache
    _cache = None
