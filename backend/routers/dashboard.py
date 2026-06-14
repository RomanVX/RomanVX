from datetime import datetime, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Query, HTTPException
import analytics
import cache

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_DATE_FROM = Query(description="YYYY-MM-DD. Overrides ?days.")
_DATE_TO = Query(description="YYYY-MM-DD. Defaults to today.")
_DAYS = Query(ge=1, le=365)
_BRAND = Query()
_CATEGORY = Query()


def _range(date_from, date_to, days) -> tuple[datetime, datetime]:
    dt_to = datetime.fromisoformat(date_to) if date_to else datetime.utcnow()
    dt_from = datetime.fromisoformat(date_from) if date_from else dt_to - timedelta(days=days)
    return dt_from, dt_to


async def _fetch(dt_from, dt_to, brand, category):
    try:
        sales, orders, stocks = await cache.get_raw_data(dt_from, dt_to)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    sales = analytics.filter_records(sales, brand, category)
    orders = analytics.filter_records(orders, brand, category)
    stocks = analytics.filter_records(stocks, brand, category)
    return sales, orders, stocks


def _days(dt_from, dt_to) -> int:
    return max(1, (dt_to - dt_from).days)


# ─── Filters ──────────────────────────────────────────────────────────────────

@router.get("/filters")
async def get_filters(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to: Annotated[Optional[str], _DATE_TO] = None,
    days: Annotated[int, _DAYS] = 30,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, stocks = await _fetch(dt_from, dt_to, None, None)
    return analytics.available_filters(stocks, sales)


# ─── Finance (main screen) ────────────────────────────────────────────────────

@router.get("/finance")
async def get_finance(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to: Annotated[Optional[str], _DATE_TO] = None,
    days: Annotated[int, _DAYS] = 30,
    brand: Annotated[Optional[str], _BRAND] = None,
    category: Annotated[Optional[str], _CATEGORY] = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    length = dt_to - dt_from
    sales, orders, _ = await _fetch(dt_from, dt_to, brand, category)

    prev_to, prev_from = dt_from, dt_from - length
    p_sales, p_orders, _ = await _fetch(prev_from, prev_to, brand, category)

    cur = analytics.finance_aggregate(sales, orders)
    prev = analytics.finance_aggregate(p_sales, p_orders)
    return {
        "cards": analytics.finance_cards(cur, prev),
        "structure": analytics.revenue_structure(cur),
        "top_skus": analytics.top_skus(sales, _days(dt_from, dt_to), 5),
        "aggregate": cur,
    }


# ─── Sales dynamics ───────────────────────────────────────────────────────────

@router.get("/sales-dynamics")
async def get_sales_dynamics(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to: Annotated[Optional[str], _DATE_TO] = None,
    days: Annotated[int, _DAYS] = 30,
    brand: Annotated[Optional[str], _BRAND] = None,
    category: Annotated[Optional[str], _CATEGORY] = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, orders, _ = await _fetch(dt_from, dt_to, brand, category)
    return analytics.sales_dynamics(sales, orders)


# ─── Products table ───────────────────────────────────────────────────────────

@router.get("/products")
async def get_products(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to: Annotated[Optional[str], _DATE_TO] = None,
    days: Annotated[int, _DAYS] = 30,
    brand: Annotated[Optional[str], _BRAND] = None,
    category: Annotated[Optional[str], _CATEGORY] = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, orders, _ = await _fetch(dt_from, dt_to, brand, category)
    return analytics.products_table(sales, orders, _days(dt_from, dt_to))


# ─── Warehouses / stocks ──────────────────────────────────────────────────────

@router.get("/warehouses")
async def get_warehouses(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to: Annotated[Optional[str], _DATE_TO] = None,
    days: Annotated[int, _DAYS] = 30,
    brand: Annotated[Optional[str], _BRAND] = None,
    category: Annotated[Optional[str], _CATEGORY] = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, orders, stocks = await _fetch(dt_from, dt_to, brand, category)
    return analytics.warehouses(sales, orders, stocks, _days(dt_from, dt_to))


# ─── Supplies ─────────────────────────────────────────────────────────────────

@router.get("/supplies")
async def get_supplies(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to: Annotated[Optional[str], _DATE_TO] = None,
    days: Annotated[int, _DAYS] = 30,
    brand: Annotated[Optional[str], _BRAND] = None,
    category: Annotated[Optional[str], _CATEGORY] = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, stocks = await _fetch(dt_from, dt_to, brand, category)
    return analytics.supplies(sales, stocks, _days(dt_from, dt_to))


# ─── Legacy endpoints (kept) ──────────────────────────────────────────────────

@router.get("/kpi")
async def get_kpi(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to: Annotated[Optional[str], _DATE_TO] = None,
    days: Annotated[int, _DAYS] = 30,
    brand: Annotated[Optional[str], _BRAND] = None,
    category: Annotated[Optional[str], _CATEGORY] = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, orders, stocks = await _fetch(dt_from, dt_to, brand, category)
    return analytics.kpi_summary(sales, orders, stocks)


@router.get("/top-skus")
async def get_top_skus(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to: Annotated[Optional[str], _DATE_TO] = None,
    days: Annotated[int, _DAYS] = 30,
    n: Annotated[int, Query(ge=1, le=50)] = 20,
    brand: Annotated[Optional[str], _BRAND] = None,
    category: Annotated[Optional[str], _CATEGORY] = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, _ = await _fetch(dt_from, dt_to, brand, category)
    return analytics.top_skus(sales, _days(dt_from, dt_to), n)


@router.post("/cache/invalidate", include_in_schema=False)
async def invalidate_cache():
    cache.invalidate()
    return {"status": "ok"}
