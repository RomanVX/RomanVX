import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Query, HTTPException
import analytics
import cache
import cost_store
import ozon_client
import ym_client

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_DATE_FROM = Query(description="YYYY-MM-DD. Overrides ?days.")
_DATE_TO   = Query(description="YYYY-MM-DD. Defaults to today.")
_DAYS      = Query(ge=1, le=365)
_BRAND     = Query()
_CATEGORY  = Query()


def _range(date_from, date_to, days) -> tuple[datetime, datetime]:
    dt_to = datetime.fromisoformat(date_to) if date_to else datetime.utcnow()
    dt_from = datetime.fromisoformat(date_from) if date_from else dt_to - timedelta(days=days)
    return dt_from, dt_to


async def _fetch(dt_from, dt_to, brand, category):
    try:
        sales, orders, stocks = await cache.get_raw_data(dt_from, dt_to)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WB API error: {exc}")
    sales  = analytics.filter_records(sales,  brand, category)
    orders = analytics.filter_records(orders, brand, category)
    stocks = analytics.filter_records(stocks, brand, category)
    return sales, orders, stocks


def _days(dt_from, dt_to) -> int:
    return max(1, (dt_to - dt_from).days)


@router.get("/filters")
async def get_filters(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, stocks = await _fetch(dt_from, dt_to, None, None)
    return analytics.available_filters(stocks, sales)


@router.get("/finance")
async def get_finance(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
    brand:     Annotated[Optional[str], _BRAND]     = None,
    category:  Annotated[Optional[str], _CATEGORY]  = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    length = dt_to - dt_from
    sales, orders, _ = await _fetch(dt_from, dt_to, brand, category)
    p_sales, p_orders, _ = await _fetch(dt_from - length, dt_from, brand, category)
    cur  = analytics.finance_aggregate(sales, orders)
    prev = analytics.finance_aggregate(p_sales, p_orders)
    return {
        "cards":     analytics.finance_cards(cur, prev),
        "structure": analytics.revenue_structure(cur),
        "top_skus":  analytics.top_skus(sales, _days(dt_from, dt_to), 5),
        "aggregate": cur,
    }


@router.get("/sales-dynamics")
async def get_sales_dynamics(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
    brand:     Annotated[Optional[str], _BRAND]     = None,
    category:  Annotated[Optional[str], _CATEGORY]  = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, orders, _ = await _fetch(dt_from, dt_to, brand, category)
    return analytics.sales_dynamics(sales, orders)


@router.get("/products")
async def get_products(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
    brand:     Annotated[Optional[str], _BRAND]     = None,
    category:  Annotated[Optional[str], _CATEGORY]  = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, orders, _ = await _fetch(dt_from, dt_to, brand, category)
    return analytics.products_table(sales, orders, _days(dt_from, dt_to))


@router.get("/warehouses")
async def get_warehouses(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
    brand:     Annotated[Optional[str], _BRAND]     = None,
    category:  Annotated[Optional[str], _CATEGORY]  = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, orders, stocks = await _fetch(dt_from, dt_to, brand, category)
    return analytics.warehouses(sales, orders, stocks, _days(dt_from, dt_to))


import logging as _logging
_slog = _logging.getLogger("stocks_table")


@router.get("/stocks_table")
async def get_stocks_table():
    """Multi-marketplace stock status: WB + Ozon + YM per SKU."""
    dt_to   = datetime.utcnow()
    dt_from = dt_to - timedelta(days=28)

    (wb_sales, _, wb_stocks), oz_stocks, oz_sales, ym_stocks, ym_sales = await asyncio.gather(
        _fetch(dt_from, dt_to, None, None),
        ozon_client.get_stocks(),
        ozon_client.get_sales_28d(),
        ym_client.get_stocks(),
        ym_client.get_sales_28d(),
    )

    _slog.info("stocks_table: wb_sales=%d wb_stocks=%d oz_stocks=%d oz_sales=%d ym_stocks=%d ym_sales=%d",
               len(wb_sales), len(wb_stocks), len(oz_stocks), len(oz_sales), len(ym_stocks), len(ym_sales))

    names = cost_store.get_names()
    return analytics.stocks_table_multi(
        wb_sales=wb_sales, wb_stocks=wb_stocks,
        oz_stocks=oz_stocks, oz_sales=oz_sales,
        ym_stocks=ym_stocks, ym_sales=ym_sales,
        names=names, days=28,
    )


import time as _time
_reco_cache: dict = {}
_reco_cache_ts: float = 0.0
_RECO_TTL = 3600  # 1 час


@router.get("/supply-recommendations")
async def get_supply_recommendations():
    """Анализ остатков через Claude — рекомендации к поставке."""
    from config import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY не настроен")

    global _reco_cache, _reco_cache_ts
    if _reco_cache and _time.monotonic() - _reco_cache_ts < _RECO_TTL:
        return _reco_cache

    # Собираем данные остатков
    dt_to   = datetime.utcnow()
    dt_from = dt_to - timedelta(days=28)
    (wb_sales, _, wb_stocks), oz_stocks, oz_sales, ym_stocks, ym_sales = await asyncio.gather(
        _fetch(dt_from, dt_to, None, None),
        ozon_client.get_stocks(),
        ozon_client.get_sales_28d(),
        ym_client.get_stocks(),
        ym_client.get_sales_28d(),
    )
    names = cost_store.get_names()
    rows = analytics.stocks_table_multi(
        wb_sales=wb_sales, wb_stocks=wb_stocks,
        oz_stocks=oz_stocks, oz_sales=oz_sales,
        ym_stocks=ym_stocks, ym_sales=ym_sales,
        names=names, days=28,
    )

    # Формируем таблицу для промпта
    lines = ["Артикул | Название | WB_ост | WB_дней | OZ_ост | OZ_дней | YM_ост | YM_дней | Статус"]
    for r in rows:
        lines.append(
            f"{r.get('supplierArticle','')} | {r.get('name','')} | "
            f"{r.get('wb_qty',0)} | {r.get('wb_days_to_oos','—')} | "
            f"{r.get('oz_qty',0)} | {r.get('oz_days_to_oos','—')} | "
            f"{r.get('ym_qty',0)} | {r.get('ym_days_to_oos','—')} | "
            f"{r.get('status','')}"
        )
    table_text = "\n".join(lines)

    prompt = f"""Ты аналитик маркетплейсов. Проанализируй остатки товаров по трём площадкам (WB, OZON, YM) и дай рекомендации к поставке.

Данные на сегодня (период продаж 28 дней):
{table_text}

Статусы: red = менее 20 дней до OOS, yellow = 21-45 дней, green = более 45 дней, — = нет продаж/остатков.

Сформируй структурированный отчёт:

1. **СРОЧНО (red)** — артикулы с критически низким запасом хотя бы на одной площадке. Укажи конкретно на каком MP и сколько дней осталось.

2. **ПЛАНОВАЯ ПОСТАВКА (yellow)** — артикулы которые нужно заказать в ближайший месяц.

3. **ДИСБАЛАНС МЕЖДУ ПЛОЩАДКАМИ** — товары где есть остаток на одной площадке, но нет на другой. Рекомендуй перераспределение.

4. **ПРИОРИТЕТЫ** — топ-5 артикулов которые нужно поставить в первую очередь (по критичности и объёму продаж).

Отвечай на русском языке, кратко и по делу. Используй эмодзи для наглядности."""

    import anthropic as _anthropic
    client = _anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text

    result = {"text": text, "generated_at": dt_to.strftime("%d.%m.%Y %H:%M UTC")}
    _reco_cache = result
    _reco_cache_ts = _time.monotonic()
    return result


@router.post("/supply-recommendations/invalidate", include_in_schema=False)
async def invalidate_reco_cache():
    global _reco_cache, _reco_cache_ts
    _reco_cache = {}
    _reco_cache_ts = 0.0
    return {"status": "ok"}


@router.get("/supplies")
async def get_supplies(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
    brand:     Annotated[Optional[str], _BRAND]     = None,
    category:  Annotated[Optional[str], _CATEGORY]  = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, stocks = await _fetch(dt_from, dt_to, brand, category)
    return analytics.supplies(sales, stocks, _days(dt_from, dt_to))


@router.get("/kpi")
async def get_kpi(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
    brand:     Annotated[Optional[str], _BRAND]     = None,
    category:  Annotated[Optional[str], _CATEGORY]  = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, orders, stocks = await _fetch(dt_from, dt_to, brand, category)
    return analytics.kpi_summary(sales, orders, stocks)


@router.get("/top-skus")
async def get_top_skus(
    date_from: Annotated[Optional[str], _DATE_FROM]       = None,
    date_to:   Annotated[Optional[str], _DATE_TO]         = None,
    days:      Annotated[int, _DAYS]                      = 30,
    n:         Annotated[int, Query(ge=1, le=50)]         = 20,
    brand:     Annotated[Optional[str], _BRAND]           = None,
    category:  Annotated[Optional[str], _CATEGORY]        = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, _ = await _fetch(dt_from, dt_to, brand, category)
    return analytics.top_skus(sales, _days(dt_from, dt_to), n)


@router.get("/abc-revenue")
async def get_abc_revenue(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, _ = await _fetch(dt_from, dt_to, None, None)
    return analytics.abc_by_revenue(sales, _days(dt_from, dt_to))


@router.get("/abc-turnover")
async def get_abc_turnover(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, stocks = await _fetch(dt_from, dt_to, None, None)
    return analytics.abc_by_turnover(sales, stocks, _days(dt_from, dt_to))


@router.get("/reorder")
async def get_reorder(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    sales, _, stocks = await _fetch(dt_from, dt_to, None, None)
    return analytics.reorder_forecast(sales, stocks, _days(dt_from, dt_to))


@router.get("/unit-economics")
async def get_unit_economics(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
    brand:     Annotated[Optional[str], _BRAND]     = None,
    category:  Annotated[Optional[str], _CATEGORY]  = None,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    costs = cost_store.get_costs()
    names = cost_store.get_names()

    # Try real reportDetailByPeriod first
    try:
        report = await cache.get_report_data(dt_from, dt_to)
        if report:
            if brand or category:
                report = [r for r in report
                          if (not brand    or r.get("brand_name") == brand)
                          and (not category or r.get("subject_name") == category)]
            rows = analytics.unit_economics_real(
                report, _days(dt_from, dt_to), costs or None, names or None)
            return {"rows": rows, "costs_loaded": cost_store.count(), "source": "report"}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("report API failed, falling back: %s", exc)

    # Fallback: estimated from sales/orders
    sales, orders, _ = await _fetch(dt_from, dt_to, brand, category)
    return {
        "rows": analytics.unit_economics(sales, orders, _days(dt_from, dt_to), costs or None),
        "costs_loaded": cost_store.count(),
        "source": "estimated",
    }


@router.get("/monthly-pivot")
async def get_monthly_pivot(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    dt_from, dt_to = _range(date_from, date_to, days)
    try:
        report = await cache.get_report_data(dt_from, dt_to)
        rows = analytics.monthly_pivot(report)
        return {"rows": rows}
    except Exception as exc:
        raise HTTPException(502, f"Report API: {exc}")


@router.post("/cache/invalidate", include_in_schema=False)
async def invalidate_cache():
    cache.invalidate()
    cache.invalidate_report()
    return {"status": "ok"}
