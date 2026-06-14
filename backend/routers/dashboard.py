from datetime import datetime, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Query, HTTPException
import analytics
import cache

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _parse_range(date_from, date_to, days):
    dt_to = datetime.utcnow()
    if date_to:
        dt_to = datetime.fromisoformat(date_to)
    dt_from = dt_to - timedelta(days=days)
    if date_from:
        dt_from = datetime.fromisoformat(date_from)
    return dt_from, dt_to


def _days_between(dt_from, dt_to):
    return max(1, (dt_to - dt_from).days)


async def _data(date_from, date_to, days):
    dt_from, dt_to = _parse_range(date_from, date_to, days)
    effective_days = _days_between(dt_from, dt_to)
    try:
        sales, orders, stocks = await cache.get_raw_data(dt_from, dt_to)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    return sales, orders, stocks, effective_days


_DATE_FROM = Query(description="Date from (YYYY-MM-DD).")
_DATE_TO   = Query(description="Date to (YYYY-MM-DD).")
_DAYS      = Query(ge=1, le=365)


@router.get("/kpi")
async def get_kpi(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    sales, orders, stocks, d = await _data(date_from, date_to, days)
    return analytics.kpi_summary(sales, orders, stocks)


@router.get("/sales-dynamics")
async def get_sales_dynamics(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    sales, orders, _, d = await _data(date_from, date_to, days)
    return analytics.sales_dynamics(sales, orders)


@router.get("/abc-revenue")
async def get_abc_revenue(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    sales, _, _, d = await _data(date_from, date_to, days)
    return analytics.abc_by_revenue(sales, d)


@router.get("/abc-turnover")
async def get_abc_turnover(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    sales, _, stocks, d = await _data(date_from, date_to, days)
    return analytics.abc_by_turnover(sales, stocks, d)


@router.get("/reorder")
async def get_reorder(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    sales, _, stocks, d = await _data(date_from, date_to, days)
    return analytics.reorder_forecast(sales, stocks, d)


@router.get("/top-skus")
async def get_top_skus(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
    n:         Annotated[int, Query(ge=1, le=50)]   = 20,
):
    sales, _, _, d = await _data(date_from, date_to, days)
    return analytics.top_skus(sales, d, n)


@router.post("/cache/invalidate", include_in_schema=False)
async def invalidate_cache():
    cache.invalidate()
    return {"status": "ok"}
