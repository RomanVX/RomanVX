"""Client for WB Finance API — sales reports list.

Rate limit: 1 req/min per seller account.
Data available from 2025-01-01.
"""
import logging
from datetime import datetime, timedelta

import httpx

from config import WB_API_KEY, USE_MOCK

_log = logging.getLogger(__name__)

FINANCE_BASE = "https://finance-api.wildberries.ru"


def _headers() -> dict:
    return {"Authorization": WB_API_KEY, "Content-Type": "application/json"}


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
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=_headers(), json=body)

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
