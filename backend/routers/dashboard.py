import asyncio
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, HTTPException
import wb_client
import analytics

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _date_range(days: int) -> tuple[datetime, datetime]:
    date_to = datetime.utcnow()
    date_from = date_to - timedelta(days=days)
    return date_from, date_to


@router.get("/kpi")
async def get_kpi(days: Annotated[int, Query(ge=1, le=365)] = 30):
    date_from, date_to = _date_range(days)
    try:
        sales, orders, stocks = await asyncio.gather(
            wb_client.get_sales(date_from, date_to),
            wb_client.get_orders(date_from, date_to),
            wb_client.get_stocks(),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    return analytics.kpi_summary(sales, orders, stocks)


@router.get("/sales-dynamics")
async def get_sales_dynamics(days: Annotated[int, Query(ge=1, le=365)] = 30):
    date_from, date_to = _date_range(days)
    try:
        sales, orders = await asyncio.gather(
            wb_client.get_sales(date_from, date_to),
            wb_client.get_orders(date_from, date_to),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    return analytics.sales_dynamics(sales, orders)


@router.get("/abc-revenue")
async def get_abc_revenue(days: Annotated[int, Query(ge=1, le=365)] = 30):
    date_from, date_to = _date_range(days)
    try:
        sales = await wb_client.get_sales(date_from, date_to)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    return analytics.abc_by_revenue(sales, days)


@router.get("/abc-turnover")
async def get_abc_turnover(days: Annotated[int, Query(ge=1, le=365)] = 30):
    date_from, date_to = _date_range(days)
    try:
        sales, stocks = await asyncio.gather(
            wb_client.get_sales(date_from, date_to),
            wb_client.get_stocks(),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    return analytics.abc_by_turnover(sales, stocks, days)


@router.get("/reorder")
async def get_reorder(days: Annotated[int, Query(ge=1, le=365)] = 30):
    date_from, date_to = _date_range(days)
    try:
        sales, stocks = await asyncio.gather(
            wb_client.get_sales(date_from, date_to),
            wb_client.get_stocks(),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    return analytics.reorder_forecast(sales, stocks, days)


@router.get("/top-skus")
async def get_top_skus(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    n: Annotated[int, Query(ge=1, le=50)] = 20,
):
    date_from, date_to = _date_range(days)
    try:
        sales = await wb_client.get_sales(date_from, date_to)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    return analytics.top_skus(sales, days, n)
