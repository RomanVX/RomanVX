"""Async client for Wildberries Statistics API."""
import logging
from datetime import datetime, timedelta

import httpx

from config import WB_API_KEY, USE_MOCK
import mock_data

_log = logging.getLogger(__name__)

STATS_BASE    = "https://statistics-api.wildberries.ru/api/v1"
REPORT_BASE   = "https://statistics-api.wildberries.ru"
ANALYTICS_BASE = "https://seller-analytics-api.wildberries.ru"

# Актуальные пути nm-report (WB периодически меняет версии)
_NM_REPORT_PATHS = [
    "/api/v2/nm-report/detail",
    "/api/v1/nm-report/detail",
]


def _headers() -> dict:
    return {"Authorization": WB_API_KEY}


async def _get(url: str, params: dict) -> list[dict]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        if not resp.is_success:
            _log.error("WB API %s → %s %s", url, resp.status_code, resp.text[:300])
            resp.raise_for_status()
        return resp.json()


async def get_sales(date_from: datetime, date_to: datetime) -> list[dict]:
    if USE_MOCK:
        return mock_data.generate_sales(date_from, date_to)
    return await _get(
        f"{STATS_BASE}/supplier/sales",
        {"dateFrom": date_from.strftime("%Y-%m-%dT00:00:00"), "flag": 0},
    )


async def get_orders(date_from: datetime, date_to: datetime) -> list[dict]:
    if USE_MOCK:
        return mock_data.generate_orders(date_from, date_to)
    return await _get(
        f"{STATS_BASE}/supplier/orders",
        {"dateFrom": date_from.strftime("%Y-%m-%dT00:00:00"), "flag": 0},
    )


async def get_stocks() -> list[dict]:
    if USE_MOCK:
        return mock_data.generate_stocks()
    date_from = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    return await _get(
        f"{STATS_BASE}/supplier/stocks",
        {"dateFrom": date_from},
    )


async def _nm_report_week_single(date_from: str, date_to: str) -> dict:
    """Один запрос к /api/analytics/v3/sales-funnel/products за период."""
    FUNNEL_URL = f"{ANALYTICS_BASE}/api/analytics/v3/sales-funnel/products"
    body_base = {
        "selectedPeriod": {"start": date_from, "end": date_to},
        "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [],
        "skipDeletedNm": False,
        "orderBy": {"field": "ordersSumRub", "mode": "desc"},
        "limit": 1000,
    }
    orders_rub = orders_qty = buyouts_rub = buyouts_qty = 0
    offset = 0
    logged = False
    while True:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(FUNNEL_URL, headers=_headers(), json={**body_base, "offset": offset})
        if not resp.is_success:
            _log.error("WB funnel %s–%s → %s %s", date_from, date_to, resp.status_code, resp.text[:300])
            break
        data   = resp.json().get("data") or {}
        prods  = data.get("products") or []
        if not logged and prods:
            _log.info("[WB funnel] sample product keys=%s", list(prods[0].keys()))
            logged = True
        for p in prods:
            sp = p.get("selectedPeriod") or (p.get("statistics") or {}).get("selectedPeriod") or {}
            orders_rub  += float(sp.get("ordersSumRub",  0) or 0)
            orders_qty  += int(sp.get("ordersCount",     0) or 0)
            buyouts_rub += float(sp.get("buyoutsSumRub", 0) or 0)
            buyouts_qty += int(sp.get("buyoutsCount",    0) or 0)
        if len(prods) < 1000:
            break
        offset += 1000
    return {"orders_rub": orders_rub, "buyouts_rub": buyouts_rub,
            "orders_qty": orders_qty,  "buyouts_qty": buyouts_qty}


async def get_nm_report_weeks(week_ranges: list[tuple[str, str]]) -> list[dict]:
    """Воронка продаж для списка недель с соблюдением лимита WB (3 запроса/мин, интервал 20 сек).

    Запрашивает батчами по 3, между батчами пауза 21 сек.
    Возвращает точные цифры кабинета WB: «Заказали на сумму / Выкупили на сумму».
    """
    if USE_MOCK:
        return [{"orders_rub": 0, "buyouts_rub": 0, "orders_qty": 0, "buyouts_qty": 0}] * len(week_ranges)

    import asyncio as _aio
    results: list[dict] = []
    batch_size = 3
    for i in range(0, len(week_ranges), batch_size):
        batch = week_ranges[i:i + batch_size]
        batch_res = await _aio.gather(*[_nm_report_week_single(s, e) for s, e in batch])
        results.extend(batch_res)
        if i + batch_size < len(week_ranges):
            await _aio.sleep(21)   # пауза между батчами чтобы не превысить лимит
    return results


async def get_report_detail(date_from: datetime, date_to: datetime) -> list[dict]:
    """GET /api/v5/supplier/reportDetailByPeriod with auto-pagination via rrdid.

    Returns the full financial report: per-item commission, logistics,
    storage, deductions, penalties, acquiring, for_pay etc.
    """
    if USE_MOCK:
        return []

    all_records: list[dict] = []
    rrdid = 0
    df_str = date_from.strftime("%Y-%m-%d")
    dt_str = date_to.strftime("%Y-%m-%d")

    while True:
        data = await _get(
            f"{REPORT_BASE}/api/v5/supplier/reportDetailByPeriod",
            {"dateFrom": df_str, "dateto": dt_str, "limit": 100_000, "rrdid": rrdid},
        )
        if not data:
            break
        all_records.extend(data)
        _log.info("reportDetailByPeriod: got %d records (total %d)", len(data), len(all_records))
        if len(data) < 100_000:
            break
        rrdid = data[-1]["rrd_id"]

    return all_records
