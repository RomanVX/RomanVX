"""Client for WB Finance API — sales reports list.

Rate limit: 1 req/min per seller account — ОБЩИЙ на все методы finance-api.
Data available from 2025-01-01.
"""
import asyncio
import logging
import time as _t
from datetime import datetime, timedelta

import httpx

from config import WB_API_KEY, USE_MOCK

_log = logging.getLogger(__name__)

FINANCE_BASE = "https://finance-api.wildberries.ru"

# Глобальный ограничитель: сводный список и детальный отчёт бьют в один
# rate-limit — без координации они выбивают друг другу 429.
_rate_lock = asyncio.Lock()
_last_call: float = -1e9
_MIN_INTERVAL = 62.0


async def _rate_limit() -> None:
    global _last_call
    async with _rate_lock:
        wait = _MIN_INTERVAL - (_t.monotonic() - _last_call)
        if wait > 0:
            _log.info("WB Finance rate-limit: пауза %.0fс", wait)
            await asyncio.sleep(wait)
        _last_call = _t.monotonic()


def _headers() -> dict:
    return {"Authorization": WB_API_KEY, "Content-Type": "application/json"}


async def _finance_post(url: str, body: dict, timeout: int = 60,
                        retries: int = 3) -> httpx.Response:
    """POST к finance-api с глобальным rate-limit и ретраями на 429."""
    for attempt in range(retries + 1):
        await _rate_limit()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=_headers(), json=body)
        if resp.status_code != 429:
            return resp
        _log.warning("WB Finance 429 (%s), попытка %d/%d — ждём %.0fс",
                     url.rsplit('/', 1)[-1], attempt + 1, retries, _MIN_INTERVAL)
        await asyncio.sleep(_MIN_INTERVAL)
    return resp


async def get_sales_reports(
    date_from: str,
    date_to: str,
    period: str = "weekly",
    limit: int = 1000,
) -> list[dict]:
    """POST /api/finance/v1/sales-reports/list — list of realisation reports."""
    if USE_MOCK:
        return _mock_reports(date_from, date_to)

    url = f"{FINANCE_BASE}/api/finance/v1/sales-reports/list"
    all_records: list[dict] = []
    offset = 0

    while True:
        body = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "period": period,
            "limit": min(limit, 1000),
            "offset": offset,
        }
        resp = await _finance_post(url, body, timeout=30)

        if not resp.is_success:
            _log.error("WB Finance API %s → %s %s", url, resp.status_code, resp.text[:300])
            resp.raise_for_status()

        data = resp.json()
        # API может вернуть список напрямую или обёрнутый объект
        if isinstance(data, list):
            batch = data
        else:
            batch = data.get("data") or data.get("reports") or []

        all_records.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    return all_records


async def get_detailed_report(
    date_from: str,
    date_to: str,
    fields: list[str] | None = None,
    limit: int = 100000,
) -> list[dict]:
    """POST /api/finance/v1/sales-reports/detailed — строчный детальный отчёт.

    Rate limit: 1 req/min. Пагинация по rrdId до ответа 204.
    Запрашиваем только нужные поля чтобы уменьшить объём.
    """
    if USE_MOCK:
        return _mock_detailed(date_from, date_to)

    url = f"{FINANCE_BASE}/api/finance/v1/sales-reports/detailed"
    if fields is None:
        fields = ["rrdId", "vendorCode", "nmId", "quantity", "docTypeName",
                  "retailAmount", "forPay", "deliveryService", "paidStorage",
                  "paidAcceptance", "penalty", "deduction", "cashbackAmount",
                  "cashbackCommissionChange", "acquiringFee", "rrDate", "saleDt"]

    all_rows: list[dict] = []
    rrd_id = 0

    while True:
        body = {
            "dateFrom": date_from,
            "dateTo":   date_to,
            "limit":    limit,
            "rrdId":    rrd_id,
            "period":   "weekly",
            "fields":   fields,
        }
        resp = await _finance_post(url, body, timeout=60)

        if resp.status_code == 204:
            break  # нет данных / конец пагинации
        if not resp.is_success:
            _log.error("WB Finance detailed %s → %s %s", url, resp.status_code, resp.text[:300])
            resp.raise_for_status()

        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break

        all_rows.extend(batch)
        last_rrd = batch[-1].get("rrdId", 0)
        if last_rrd <= rrd_id or len(batch) < limit:
            break
        rrd_id = last_rrd
        # пауза между страницами обеспечивается _rate_limit()

    return all_rows


def _mock_detailed(date_from: str, date_to: str) -> list[dict]:
    """Mock детального отчёта на основе mock_reports."""
    import random
    from datetime import date, timedelta
    random.seed(42)

    try:
        df = datetime.strptime(date_from[:10], "%Y-%m-%d").date()
        dt = datetime.strptime(date_to[:10],   "%Y-%m-%d").date()
    except ValueError:
        df = date(2025, 1, 1)
        dt = date.today()

    SKUS = ["BMN-0035","BMN-0013","BMN-0028","BMN-0002","BMN-0008",
            "BMN-0036","BMN-0004","BMN-0006","ST-01","ST-07"]

    rows = []
    rrd = 1
    cur = df
    while cur <= dt:
        for sku in SKUS:
            for _ in range(random.randint(0, 5)):
                retail = round(random.uniform(300, 900), 2)
                rows.append({
                    "rrdId": rrd, "vendorCode": sku, "nmId": 0,
                    "quantity": 1, "docTypeName": "Продажа",
                    "retailAmount": str(retail),
                    "forPay": str(round(retail * 0.78, 2)),
                    "deliveryService": str(round(retail * 0.11, 2)),
                    "paidStorage": str(round(retail * 0.03, 2)),
                    "paidAcceptance": "0", "penalty": "0", "deduction": "0",
                    "cashbackAmount": "0", "cashbackCommissionChange": "0",
                    "acquiringFee": str(round(retail * 0.04, 2)),
                    "rrDate": cur.isoformat(), "saleDt": cur.isoformat() + "T00:00:00Z",
                })
                rrd += 1
            if random.random() < 0.1:  # ~10% возвраты
                rows.append({
                    "rrdId": rrd, "vendorCode": sku, "nmId": 0,
                    "quantity": 1, "docTypeName": "Возврат",
                    "retailAmount": str(-round(random.uniform(300, 900), 2)),
                    "forPay": "0", "deliveryService": "0", "paidStorage": "0",
                    "paidAcceptance": "0", "penalty": "0", "deduction": "0",
                    "cashbackAmount": "0", "cashbackCommissionChange": "0",
                    "acquiringFee": "0",
                    "rrDate": cur.isoformat(), "saleDt": cur.isoformat() + "T00:00:00Z",
                })
                rrd += 1
        cur += timedelta(days=1)
    return rows


def _mock_reports(date_from: str, date_to: str) -> list[dict]:
    """Generate realistic mock weekly reports for development."""
    from datetime import date
    try:
        df = datetime.strptime(date_from[:10], "%Y-%m-%d").date()
        dt = datetime.strptime(date_to[:10], "%Y-%m-%d").date()
    except ValueError:
        df = date(2025, 1, 1)
        dt = date.today()

    reports = []
    rid = 1000
    # weekly reports: find first Monday >= df
    d = df
    while d.weekday() != 0:
        d += timedelta(days=1)

    import random
    random.seed(42)

    while d <= dt:
        week_end = d + timedelta(days=6)
        retail = round(random.uniform(300_000, 700_000), 2)
        delivery = round(retail * random.uniform(0.08, 0.14), 2)
        storage  = round(retail * random.uniform(0.02, 0.05), 2)
        penalty  = round(retail * random.uniform(0, 0.01), 2)
        deduct   = round(retail * random.uniform(0, 0.005), 2)
        accept   = round(retail * random.uniform(0.005, 0.015), 2)
        cashback = round(retail * random.uniform(0, 0.03), 2)
        for_pay  = round(retail * 0.78 - delivery - storage - penalty - deduct - accept - cashback, 2)
        reports.append({
            "reportId": rid,
            "sellerFinanceName": "Biomed Nutrition",
            "dateFrom": d.isoformat(),
            "dateTo": week_end.isoformat(),
            "createDate": (week_end + timedelta(days=2)).isoformat(),
            "currency": "RUB",
            "reportType": 1,
            "retailAmountSum": str(retail),
            "forPaySum": str(max(for_pay, 0)),
            "avgSalePercent": round(random.uniform(20, 35), 1),
            "deliveryServiceSum": str(delivery),
            "paidStorageSum": str(storage),
            "paidAcceptanceSum": str(accept),
            "deductionSum": str(deduct),
            "penaltySum": str(penalty),
            "additionalPaymentSum": "0.00",
            "cashbackAmountSum": str(cashback),
            "cashbackDiscountSum": "0.00",
            "cashbackCommissionChangeSum": "0.00",
            "paymentSchedule": "",
            "bankPaymentSum": str(max(for_pay, 0)),
        })
        rid += 1
        d += timedelta(weeks=1)

    return reports


async def get_balance() -> dict:
    """Баланс продавца WB: GET /api/v1/account/balance.

    Возвращает {"currency": "RUB", "current": ..., "for_withdraw": ...}
    (имена полей у WB менялись — отдаём как есть)."""
    url = f"{FINANCE_BASE}/api/v1/account/balance"
    for attempt in range(3):
        await _rate_limit()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code == 429:
            await asyncio.sleep(_MIN_INTERVAL)
            continue
        if not resp.is_success:
            _log.warning("WB balance → %s %s", resp.status_code, resp.text[:200])
            return {}
        data = resp.json()
        return data.get("data") or data or {}
    return {}
