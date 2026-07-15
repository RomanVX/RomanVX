"""In-memory cache for raw WB API data.

Two caches:
  1. _store  — sales/orders/stocks, 90-day rolling window, 600s TTL
  2. _report — reportDetailByPeriod, per requested date range, 600s TTL
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta

_log = logging.getLogger(__name__)

TTL      = 600   # 10 minutes
MAX_DAYS = 90


# ── Sales / Orders / Stocks cache ────────────────────────────────────────────

class _Store:
    __slots__ = ("sales", "orders", "stocks", "fetched_at")

    def __init__(self):
        self.sales: list       = []
        self.orders: list      = []
        self.stocks: list      = []
        self.fetched_at: float = 0.0


_store = _Store()
_lock  = asyncio.Lock()

# фоновые задачи (держим ссылки, иначе GC их убьёт)
_bg_tasks: set = set()


def _spawn(coro) -> None:
    import heavy
    t = asyncio.get_event_loop().create_task(heavy.guard(coro))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _filter(records: list, date_from: datetime, date_to: datetime) -> list:
    lo = date_from.strftime("%Y-%m-%d")
    hi = date_to.strftime("%Y-%m-%d")
    return [r for r in records if lo <= r.get("date", "")[:10] <= hi]


def _is_stale() -> bool:
    # fetched_at == 0.0 значит «ещё не загружали». Нельзя сравнивать только age:
    # monotonic() — это uptime системы, и на свежем контейнере (cold start Render)
    # age = monotonic() - 0 < TTL, из-за чего пустой кеш считался «свежим»
    # и WB-данные не грузились первые 10 минут после пробуждения.
    if _store.fetched_at == 0.0:
        return True
    return time.monotonic() - _store.fetched_at >= TTL


async def get_raw_data(date_from: datetime, date_to: datetime) -> tuple[list, list, list]:
    global _store
    if _is_stale():
        async with _lock:
            if _is_stale():
                # холодный старт: сначала пробуем снапшот из БД — отдаём его
                # сразу, свежие данные тянутся фоном (иначе синхронный поход
                # в WB API в request-path даёт 502 при rate-limit/таймауте)
                if _store.fetched_at == 0.0:
                    import snapshot
                    snap = await asyncio.to_thread(snapshot.load, "wb_raw", None)
                    if snap:
                        _store.sales  = snap.get("sales", [])
                        _store.orders = snap.get("orders", [])
                        _store.stocks = snap.get("stocks", [])
                        _store.fetched_at = time.monotonic()
                        _log.info("WB raw: снапшот из БД (%d sales, %d orders), фон-обновление",
                                  len(_store.sales), len(_store.orders))
                        _spawn(_refresh())
                    else:
                        await _refresh()
                else:
                    await _refresh()

    sales  = _filter(_store.sales,  date_from, date_to)
    orders = _filter(_store.orders, date_from, date_to)
    return sales, orders, _store.stocks


async def _refresh() -> None:
    import wb_client
    fetch_from = datetime.utcnow() - timedelta(days=MAX_DAYS)
    fetch_to   = datetime.utcnow()
    _log.info("Fetching WB API: last %d days", MAX_DAYS)
    try:
        sales, orders, stocks = await asyncio.gather(
            wb_client.get_sales(fetch_from, fetch_to),
            wb_client.get_orders(fetch_from, fetch_to),
            wb_client.get_stocks(),
        )
        _store.sales      = sales
        _store.orders     = orders
        _store.stocks     = stocks
        _store.fetched_at = time.monotonic()
        import heavy
        _log.info("Cache refreshed: %d sales, %d orders, %d stocks (rss %.0f МБ)",
                  len(sales), len(orders), len(stocks), heavy.rss_mb())
        try:
            import sales_history
            sales_history.persist_wb_bg(sales)
        except Exception as exc:
            _log.warning("sales_history persist (WB) failed: %s", exc)
        try:
            import snapshot
            await asyncio.to_thread(
                snapshot.save_parts, "wb_raw", {},
                {"sales": sales, "orders": orders, "stocks": stocks})
        except Exception as exc:
            _log.warning("wb_raw snapshot save failed: %s", exc)
    except Exception as exc:
        stale = bool(_store.sales or _store.orders)
        _log.warning("WB API refresh failed (%s) — %s", exc,
                     "using stale cache" if stale else "no stale data")
        if stale:
            # Keep stale data but reset timer so we retry in TTL seconds
            _store.fetched_at = time.monotonic()
        else:
            raise


def invalidate() -> None:
    _store.fetched_at = 0.0


# ── Report detail cache ───────────────────────────────────────────────────────

class _ReportStore:
    __slots__ = ("records", "date_from", "date_to", "fetched_at")

    def __init__(self):
        self.records: list       = []
        self.date_from: datetime | None = None
        self.date_to:   datetime | None = None
        self.fetched_at: float   = 0.0


_report_store = _ReportStore()
_report_lock  = asyncio.Lock()


def _filter_report(records: list, date_from: datetime, date_to: datetime) -> list:
    """Filter report records by sale_dt or rr_dt."""
    lo = date_from.strftime("%Y-%m-%d")
    hi = date_to.strftime("%Y-%m-%d")
    out = []
    for r in records:
        dt = (r.get("sale_dt") or r.get("rr_dt") or r.get("order_dt") or "")[:10]
        if lo <= dt <= hi:
            out.append(r)
    return out


async def get_report_data(date_from: datetime, date_to: datetime) -> list:
    """Return reportDetailByPeriod records for the requested date range.

    Fetches fresh from WB API if the cached range doesn't cover the request
    or the cache is older than TTL.
    """
    rs = _report_store
    age = time.monotonic() - rs.fetched_at

    # Cache hit: cached range covers requested range and still fresh
    if (age < TTL
            and rs.date_from is not None
            and rs.date_from <= date_from
            and rs.date_to   >= date_to):
        return _filter_report(rs.records, date_from, date_to)

    async with _report_lock:
        # Re-check inside lock
        age = time.monotonic() - rs.fetched_at
        if not (age < TTL
                and rs.date_from is not None
                and rs.date_from <= date_from
                and rs.date_to   >= date_to):
            # холодный старт: снапшот из БД, если он покрывает запрошенный
            # диапазон — отдаём сразу, свежий отчёт тянется фоном
            if rs.fetched_at == 0.0:
                import snapshot
                snap = await asyncio.to_thread(snapshot.load, "wb_report", None)
                try:
                    sf = datetime.fromisoformat(snap["date_from"]) if snap else None
                    st = datetime.fromisoformat(snap["date_to"])   if snap else None
                except (KeyError, ValueError, TypeError):
                    sf = st = None
                if sf is not None and sf <= date_from and st >= date_to:
                    rs.records, rs.date_from, rs.date_to = snap.get("records", []), sf, st
                    rs.fetched_at = time.monotonic()
                    _log.info("WB report: снапшот из БД (%d строк), фон-обновление", len(rs.records))
                    _spawn(_refresh_report(date_from, date_to))
                else:
                    await _refresh_report(date_from, date_to)
            else:
                await _refresh_report(date_from, date_to)

    return _filter_report(rs.records, date_from, date_to)


async def _refresh_report(date_from: datetime, date_to: datetime) -> None:
    import wb_client
    _log.info("Fetching reportDetailByPeriod %s → %s",
              date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d"))
    records = await wb_client.get_report_detail(date_from, date_to)
    _report_store.records    = records
    _report_store.date_from  = date_from
    _report_store.date_to    = date_to
    _report_store.fetched_at = time.monotonic()
    _log.info("Report cache: %d records", len(records))
    try:
        import snapshot
        await asyncio.to_thread(
            snapshot.save_parts, "wb_report",
            {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
            {"records": records})
    except Exception as exc:
        _log.warning("wb_report snapshot save failed: %s", exc)


def invalidate_report() -> None:
    _report_store.fetched_at = 0.0
