"""In-memory cache for raw WB API data.

Keyed by (date_from, date_to) strings. All dashboard endpoints share
one fetch per unique date range; WB API is hit at most once per TTL.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

_log = logging.getLogger(__name__)

TTL = 300  # 5 minutes


@dataclass
class _CacheEntry:
    sales: list
    orders: list
    stocks: list
    fetched_at: float


# {(date_from_str, date_to_str): _CacheEntry}
_cache: dict[tuple[str, str], _CacheEntry] = {}
_lock = asyncio.Lock()


async def get_raw_data(date_from: datetime, date_to: datetime) -> tuple[list, list, list]:
    """Return (sales, orders, stocks), fetching from WB only when stale."""
    key = (date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d"))

    entry = _cache.get(key)
    if entry and (time.monotonic() - entry.fetched_at) < TTL:
        _log.debug("Cache hit %s→%s (age %.0fs)", key[0], key[1],
                   time.monotonic() - entry.fetched_at)
        return entry.sales, entry.orders, entry.stocks

    async with _lock:
        entry = _cache.get(key)
        if entry and (time.monotonic() - entry.fetched_at) < TTL:
            return entry.sales, entry.orders, entry.stocks

        _log.info("Cache miss — fetching WB API for %s → %s", key[0], key[1])

        import wb_client
        sales, orders, stocks = await asyncio.gather(
            wb_client.get_sales(date_from, date_to),
            wb_client.get_orders(date_from, date_to),
            wb_client.get_stocks(),
        )

        _cache[key] = _CacheEntry(
            sales=sales, orders=orders, stocks=stocks,
            fetched_at=time.monotonic(),
        )
        _log.info("Cached: %d sales, %d orders, %d stock lines",
                  len(sales), len(orders), len(stocks))

    return _cache[key].sales, _cache[key].orders, _cache[key].stocks


def invalidate() -> None:
    _cache.clear()
