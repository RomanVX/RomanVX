import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, HTTPException
import analytics
import cache

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


async def _data(days: int) -> tuple[list, list, list]:
    try:
        return await cache.get_raw_data(days)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")


@router.get("/kpi")
async def get_kpi(days: Annotated[int, Query(ge=1, le=365)] = 30):
    sales, orders, stocks = await _data(days)
    return analytics.kpi_summary(sales, orders, stocks)


@router.get("/sales-dynamics")
async def get_sales_dynamics(days: Annotated[int, Query(ge=1, le=365)] = 30):
    sales, orders, _ = await _data(days)
    return analytics.sales_dynamics(sales, orders)


@router.get("/abc-revenue")
async def get_abc_revenue(days: Annotated[int, Query(ge=1, le=365)] = 30):
    sales, _, _ = await _data(days)
    return analytics.abc_by_revenue(sales, days)


@router.get("/abc-turnover")
async def get_abc_turnover(days: Annotated[int, Query(ge=1, le=365)] = 30):
    sales, _, stocks = await _data(days)
    return analytics.abc_by_turnover(sales, stocks, days)


@router.get("/reorder")
async def get_reorder(days: Annotated[int, Query(ge=1, le=365)] = 30):
    sales, _, stocks = await _data(days)
    return analytics.reorder_forecast(sales, stocks, days)


@router.get("/top-skus")
async def get_top_skus(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    n: Annotated[int, Query(ge=1, le=50)] = 20,
):
    sales, _, _ = await _data(days)
    return analytics.top_skus(sales, days, n)


@router.post("/cache/invalidate", include_in_schema=False)
async def invalidate_cache():
    cache.invalidate()
    return {"status": "ok"}
