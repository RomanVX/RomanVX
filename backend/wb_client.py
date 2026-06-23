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


async def get_nm_report_week(date_from: str, date_to: str) -> dict:
    """Воронка продаж по всем артикулам за период (Analytics API).

    Возвращает точные цифры кабинета WB:
      orders_rub / orders_qty  — «Заказали на сумму / шт»
      buyouts_rub / buyouts_qty — «Выкупили на сумму / шт»
    """
    if USE_MOCK:
        return {"orders_rub": 0, "buyouts_rub": 0, "orders_qty": 0, "buyouts_qty": 0}

    all_cards: list[dict] = []
    body_base = {
        "brandNames": [], "objectIDs": [], "tagIDs": [], "nmIDs": [],
        "timezone": "Europe/Moscow",
        "period": {"begin": f"{date_from} 00:00:00", "end": f"{date_to} 23:59:59"},
        "orderBy": {"field": "ordersSumRub", "mode": "desc"},
    }
    # Перебираем известные пути — WB периодически меняет версию
    working_path: str | None = None
    for path in _NM_REPORT_PATHS:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{ANALYTICS_BASE}{path}", headers=_headers(), json={**body_base, "page": 1})
        if r.status_code == 404:
            continue
        if r.is_success:
            working_path = path
            first_data = r.json().get("data") or {}
            all_cards.extend(first_data.get("cards") or [])
            if first_data.get("isNextPage"):
                page = 2
                while True:
                    async with httpx.AsyncClient(timeout=30) as client:
                        r2 = await client.post(f"{ANALYTICS_BASE}{path}", headers=_headers(), json={**body_base, "page": page})
                    if not r2.is_success:
                        break
                    d2 = r2.json().get("data") or {}
                    all_cards.extend(d2.get("cards") or [])
                    if not d2.get("isNextPage"):
                        break
                    page += 1
        else:
            _log.error("WB nm-report %s–%s path=%s → %s %s", date_from, date_to, path, r.status_code, r.text[:200])
        break

    if working_path is None:
        _log.error("WB nm-report: все пути вернули 404 (%s–%s), данные недоступны", date_from, date_to)
        return {"orders_rub": 0, "buyouts_rub": 0, "orders_qty": 0, "buyouts_qty": 0}

    def _stat(card: dict) -> dict:
        return (card.get("statistics") or {}).get("selectedPeriod") or {}

    return {
        "orders_rub":  sum(_stat(c).get("ordersSumRub",  0) for c in all_cards),
        "buyouts_rub": sum(_stat(c).get("buyoutsSumRub", 0) for c in all_cards),
        "orders_qty":  sum(_stat(c).get("ordersCount",   0) for c in all_cards),
        "buyouts_qty": sum(_stat(c).get("buyoutsCount",  0) for c in all_cards),
    }


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
