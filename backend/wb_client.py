"""Async client for Wildberries Statistics API."""
import asyncio
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


# Общий клиент с keep-alive: новый AsyncClient на каждый запрос платит
# ~100-300мс за TCP+TLS handshake, переиспользуемый — нет.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60)
    return _client


async def _get(url: str, params: dict) -> list[dict]:
    resp = await _http().get(url, headers=_headers(), params=params)
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


_funnel_lock = asyncio.Lock()  # одна загрузка воронки одновременно (prefetch vs прямой запрос)


async def _nm_report_week_single(date_from: str, date_to: str) -> dict:
    """Один запрос к /api/analytics/v3/sales-funnel/products за период.

    При 429 повторяет до 3 раз с паузой 21 сек (лимит WB пополняется ~1 токен/20 сек).
    """
    FUNNEL_URL = f"{ANALYTICS_BASE}/api/analytics/v3/sales-funnel/products"
    body_base = {
        "selectedPeriod": {"start": date_from, "end": date_to},
        "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [],
        "skipDeletedNm": False,
        "orderBy": {"field": "orderSum", "mode": "desc"},
        "limit": 1000,
    }
    orders_rub = orders_qty = buyouts_rub = buyouts_qty = 0
    offset = 0
    while True:
        # запрос одной страницы с retry на 429
        resp = None
        for attempt in range(4):
            resp = await _http().post(FUNNEL_URL, headers=_headers(), json={**body_base, "offset": offset})
            if resp.status_code == 429:
                _log.warning("WB funnel %s–%s → 429, retry %d/3 через 21с", date_from, date_to, attempt + 1)
                await asyncio.sleep(21)
                continue
            break
        if resp is None or not resp.is_success:
            code = resp.status_code if resp is not None else "—"
            _log.error("WB funnel %s–%s → %s %s", date_from, date_to, code,
                       resp.text[:300] if resp is not None else "")
            break
        data   = resp.json().get("data") or {}
        prods  = data.get("products") or []
        for p in prods:
            stat = p.get("statistic") or {}
            sp   = stat.get("selected") or {}
            orders_rub  += float(sp.get("orderSum",   0) or 0)
            orders_qty  += int(sp.get("orderCount",   0) or 0)
            buyouts_rub += float(sp.get("buyoutSum",  0) or 0)
            buyouts_qty += int(sp.get("buyoutCount",  0) or 0)
        if len(prods) < 1000:
            break
        offset += 1000
    return {"orders_rub": orders_rub, "buyouts_rub": buyouts_rub,
            "orders_qty": orders_qty,  "buyouts_qty": buyouts_qty}


async def get_nm_report_weeks(week_ranges: list[tuple[str, str]]) -> list[dict]:
    """Воронка продаж для списка недель с соблюдением лимита WB (3 запроса/мин).

    Лимит — token bucket: burst 3, пополнение ~1 токен / 20 сек.
    Поэтому первые 3 запроса идут сразу, далее по одному с паузой 21 сек.
    Возвращает точные цифры кабинета WB: «Заказали на сумму / Выкупили на сумму».
    """
    if USE_MOCK:
        return [{"orders_rub": 0, "buyouts_rub": 0, "orders_qty": 0, "buyouts_qty": 0}] * len(week_ranges)

    async with _funnel_lock:
        results: list[dict] = []
        for i, (s, e) in enumerate(week_ranges):
            if i >= 3:                       # после burst-окна — пауза перед каждым запросом
                await asyncio.sleep(21)
            results.append(await _nm_report_week_single(s, e))
        return results
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
