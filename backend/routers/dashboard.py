import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Query, HTTPException
import analytics
import cache
import catalog as _catalog
import cost_store
import ozon_client
import sales_history
import wb_client
import ym_client

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/sales_history")
async def sales_history_endpoint(
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
):
    """Накопленная история продаж (день × площадка) из БД — за пределами окон API."""
    return sales_history.get_summary(date_from=date_from, date_to=date_to)

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
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        client = _anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        message = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        _log.error("Claude API error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Claude API error: {exc}")
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


@router.get("/sales_analytics")
async def get_sales_analytics(
    date_from:   Annotated[Optional[str], _DATE_FROM] = None,
    date_to:     Annotated[Optional[str], _DATE_TO]   = None,
    days:        Annotated[int, _DAYS]                = 30,
    marketplace: str = "all",   # wb / ozon / ym / all
    brand:       str = "all",   # all / Джага / Satisfucktion / Aloe
):
    from collections import defaultdict

    dt_from, dt_to = _range(date_from, date_to, days)
    df_str = dt_from.strftime("%Y-%m-%d")
    dt_str = dt_to.strftime("%Y-%m-%d")

    (wb_sales_all, _, _), oz_rows, ym_rows = await asyncio.gather(
        _fetch(dt_from, dt_to, None, None),
        ozon_client.get_sales_detail(df_str, dt_str),
        ym_client.get_sales_detail(df_str, dt_str),
    )

    # Brand/name map: WB sales → catalog → cost_store
    uploaded_names = cost_store.get_names()

    def _art_brand(art: str, wb_brand: str = "") -> str:
        if wb_brand:
            return wb_brand
        cat = _catalog.CATALOG.get(art)
        if cat:
            return cat["brand"]
        return uploaded_names.get(art, "")

    def _art_name(art: str) -> str:
        if art in uploaded_names:
            return uploaded_names[art]
        cat = _catalog.CATALOG.get(art)
        if cat:
            return cat["name"]
        return ""

    brand_map: dict[str, str] = {}
    for s in wb_sales_all:
        art = s.get("supplierArticle", "")
        if art and art not in brand_map:
            brand_map[art] = _art_brand(art, s.get("brand", ""))
    # Fill from catalog for arts not in WB sales
    for art, info in _catalog.CATALOG.items():
        if art not in brand_map:
            brand_map[art] = info["brand"]

    brand_filter = brand if brand != "all" else None
    if brand_filter:
        wb_sales = [s for s in wb_sales_all if _art_brand(s.get("supplierArticle", ""), s.get("brand", "")) == brand_filter]
        oz_rows  = [r for r in oz_rows if brand_map.get(r["offer_id"]) == brand_filter]
        ym_rows  = [r for r in ym_rows if brand_map.get(r["shop_sku"]) == brand_filter]
    else:
        wb_sales = wb_sales_all

    # Date list for dynamics
    all_dates: list[str] = []
    cur = dt_from.date()
    end = dt_to.date()
    while cur <= end:
        all_dates.append(str(cur))
        from datetime import date as _date
        cur = (datetime.combine(cur, datetime.min.time()) + timedelta(days=1)).date()

    art_data: dict = defaultdict(lambda: {
        "wb_qty": 0, "wb_rev": 0.0,
        "oz_qty": 0, "oz_rev": 0.0,
        "ym_qty": 0, "ym_rev": 0.0,
    })
    date_data: dict = {d: {"wb_qty": 0, "wb_rev": 0.0, "oz_qty": 0, "oz_rev": 0.0, "ym_qty": 0, "ym_rev": 0.0}
                       for d in all_dates}

    _wb_sales_filtered = [s for s in wb_sales if analytics._is_sale(s)]

    if marketplace in ("wb", "all"):
        for s in _wb_sales_filtered:
            art  = s.get("supplierArticle", "")
            date = (s.get("date") or "")[:10]
            rev  = analytics._gross(s)
            art_data[art]["wb_qty"] += 1
            art_data[art]["wb_rev"] += rev
            if date in date_data:
                date_data[date]["wb_qty"] += 1
                date_data[date]["wb_rev"] += rev

    if marketplace in ("ozon", "all"):
        for r in oz_rows:
            art = r["offer_id"]
            art_data[art]["oz_qty"] += r["qty"]
            art_data[art]["oz_rev"] += r["revenue"]
            if r["date"] in date_data:
                date_data[r["date"]]["oz_qty"] += r["qty"]
                date_data[r["date"]]["oz_rev"] += r["revenue"]

    if marketplace in ("ym", "all"):
        for r in ym_rows:
            art = r["shop_sku"]
            art_data[art]["ym_qty"] += r["qty"]
            art_data[art]["ym_rev"] += r["revenue"]
            if r["date"] in date_data:
                date_data[r["date"]]["ym_qty"] += r["qty"]
                date_data[r["date"]]["ym_rev"] += r["revenue"]

    table = []
    for art, d in art_data.items():
        tq = d["wb_qty"] + d["oz_qty"] + d["ym_qty"]
        tr = d["wb_rev"] + d["oz_rev"] + d["ym_rev"]
        if tq == 0:
            continue
        table.append({
            "article": art,
            "name": _art_name(art),
            "brand": brand_map.get(art, ""),
            "wb_qty": d["wb_qty"], "wb_rev": round(d["wb_rev"], 2),
            "oz_qty": d["oz_qty"], "oz_rev": round(d["oz_rev"], 2),
            "ym_qty": d["ym_qty"], "ym_rev": round(d["ym_rev"], 2),
            "total_qty": tq, "total_rev": round(tr, 2),
        })
    table.sort(key=lambda x: x["total_rev"], reverse=True)

    total_qty = sum(r["total_qty"] for r in table)
    total_rev = sum(r["total_rev"] for r in table)
    period_days = max(1, _days(dt_from, dt_to))
    best = table[0] if table else None

    return {
        "kpi": {
            "total_qty": total_qty,
            "total_rev": round(total_rev, 2),
            "avg_per_day": round(total_qty / period_days, 1),
            "best": {"article": best["article"], "name": best["name"], "rev": best["total_rev"]} if best else None,
        },
        "dynamics": [{"date": d, **v} for d, v in sorted(date_data.items())],
        "top10": table[:10],
        "table": table,
    }


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


def _week_ranges(n_weeks: int = 8) -> list[tuple]:
    """Last n_weeks Mon–Sun ranges (date objects), oldest first, including current week."""
    today = (datetime.utcnow() + timedelta(hours=3)).date()  # Moscow time
    monday_this_week = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(n_weeks):
        start = monday_this_week - timedelta(weeks=(n_weeks - 1 - i))
        end = start + timedelta(days=6)
        weeks.append((start, end))
    return weeks


def _week_label(start, end) -> str:
    return f"{start.strftime('%d.%m')} - {end.strftime('%d.%m')}"


import time as _wtime
_weekly_cache: dict = {}
_weekly_cache_ts: float = 0.0
_WEEKLY_TTL = 1800  # 30 минут
_weekly_fetch_lock = asyncio.Lock()  # single-flight: только один фетч одновременно


@router.post("/weekly_summary/invalidate", include_in_schema=False)
async def invalidate_weekly_cache():
    global _weekly_cache, _weekly_cache_ts
    _weekly_cache = {}
    _weekly_cache_ts = 0.0
    return {"status": "ok"}


@router.get("/weekly_summary")
async def get_weekly_summary():
    """Сводка Продажи/Выкупы по неделям (Пн-Вс) за последние 8 недель, все МП."""
    global _weekly_cache, _weekly_cache_ts
    if _weekly_cache and _wtime.monotonic() - _weekly_cache_ts < _WEEKLY_TTL:
        return _weekly_cache

    async with _weekly_fetch_lock:
        # double-check: пока ждали лок, кто-то уже наполнил кеш
        if _weekly_cache and _wtime.monotonic() - _weekly_cache_ts < _WEEKLY_TTL:
            return _weekly_cache
        weeks = _week_ranges(8)
        n = len(weeks)
        dt_from = datetime.combine(weeks[0][0], datetime.min.time())
        dt_to   = datetime.combine(weeks[-1][1], datetime.min.time())

        import logging as _wslog
        _wslog = _wslog.getLogger("weekly_summary")

        date_from_str = weeks[0][0].strftime("%Y-%m-%d")
        date_to_str   = weeks[-1][1].strftime("%Y-%m-%d")
        week_str_ranges = [(s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")) for s, e in weeks]

        # OZON/YM — быстрые, отдельный короткий таймаут
        try:
            oz_rows, ym_rows = await asyncio.wait_for(
                asyncio.gather(
                    ozon_client.get_sales_detail(date_from_str, date_to_str),
                    ym_client.get_sales_detail(date_from_str, date_to_str),
                ),
                timeout=90,
            )
        except Exception as _exc:
            _wslog.error("[WS] OZON/YM fetch failed: %s", _exc)
            oz_rows, ym_rows = [], []

        # WB воронка — медленная (8 запросов с лимитом), длинный таймаут
        try:
            wb_funnel_weeks = await asyncio.wait_for(
                wb_client.get_nm_report_weeks(week_str_ranges), timeout=300,
            )
        except Exception as _exc:
            _wslog.error("[WS] WB funnel fetch failed: %s", _exc)
            wb_funnel_weeks = []
        wb_ok = any(f.get("orders_rub") or f.get("orders_qty") for f in wb_funnel_weeks)

        def week_index(date_str: str):
            try:
                d = datetime.strptime((date_str or "")[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
            for i, (s, e) in enumerate(weeks):
                if s <= d <= e:
                    return i
            return None

        rub = {mp: {"sales": [0.0] * n, "buyout": [0.0] * n} for mp in ("WB", "OZON", "YM")}
        qty = {mp: {"sales": [0] * n, "buyout": [0] * n} for mp in ("WB", "OZON", "YM")}

        for i, fw in enumerate(wb_funnel_weeks):
            if i >= n:
                break
            rub["WB"]["sales"][i]  = fw.get("orders_rub", 0.0)
            qty["WB"]["sales"][i]  = fw.get("orders_qty", 0)
            rub["WB"]["buyout"][i] = fw.get("buyouts_rub", 0.0)
            qty["WB"]["buyout"][i] = fw.get("buyouts_qty", 0)

        for r in oz_rows:
            idx = week_index(r.get("date"))
            if idx is None:
                continue
            rub["OZON"]["sales"][idx] += r["revenue"]
            qty["OZON"]["sales"][idx] += r["qty"]
            if (r.get("status") or "").lower() == "delivered":
                # выкупы по дате доставки (как в Excel-запросе "Выкупы ШТ OZON")
                idx_d = week_index(r.get("delivered_date") or r.get("date"))
                if idx_d is not None:
                    rub["OZON"]["buyout"][idx_d] += r["revenue"]
                    qty["OZON"]["buyout"][idx_d] += r["qty"]

        for r in ym_rows:
            idx = week_index(r.get("date"))
            if idx is not None:
                rub["YM"]["sales"][idx] += r["revenue"]
                qty["YM"]["sales"][idx] += r["qty"]
            if (r.get("status") or "") == "DELIVERED":
                idx_d = week_index(r.get("update_date"))
                if idx_d is not None:
                    rub["YM"]["buyout"][idx_d] += r["revenue"]
                    qty["YM"]["buyout"][idx_d] += r["qty"]

        def block(d: dict, mp: str) -> dict:
            return {"sales": [round(v, 2) for v in d[mp]["sales"]],
                    "buyout": [round(v, 2) for v in d[mp]["buyout"]]}

        def total(d: dict) -> dict:
            return {
                "sales":  [round(sum(d[mp]["sales"][i]  for mp in ("WB", "OZON", "YM")), 2) for i in range(n)],
                "buyout": [round(sum(d[mp]["buyout"][i] for mp in ("WB", "OZON", "YM")), 2) for i in range(n)],
            }

        result = {
            "weeks": [_week_label(s, e) for s, e in weeks],
            "rub": {"OZON": block(rub, "OZON"), "WB": block(rub, "WB"), "YM": block(rub, "YM"), "total": total(rub)},
            "qty": {"OZON": block(qty, "OZON"), "WB": block(qty, "WB"), "YM": block(qty, "YM"), "total": total(qty)},
        }
        if wb_ok:                      # кешируем только полный результат с данными WB
            _weekly_cache = result
            _weekly_cache_ts = _wtime.monotonic()
        return result


# ── Заказы по неделям с разбивкой по SKU ──────────────────────────────────────

import catalog as _cat

_wo_cache: dict = {}
_wo_cache_ts: float = 0.0
_WO_TTL = 1800
_wo_lock = asyncio.Lock()


@router.get("/weekly_orders")
async def get_weekly_orders():
    """Заказы по неделям (Пн-Вс) с разбивкой по SKU, все МП."""
    global _wo_cache, _wo_cache_ts
    if _wo_cache and _wtime.monotonic() - _wo_cache_ts < _WO_TTL:
        return _wo_cache

    async with _wo_lock:
        if _wo_cache and _wtime.monotonic() - _wo_cache_ts < _WO_TTL:
            return _wo_cache

        weeks = _week_ranges(8)
        n = len(weeks)
        date_from_str = weeks[0][0].strftime("%Y-%m-%d")
        date_to_str   = weeks[-1][1].strftime("%Y-%m-%d")
        dt_from = datetime.combine(weeks[0][0], datetime.min.time())
        dt_to   = datetime.combine(weeks[-1][1], datetime.min.time())

        def week_idx(date_str: str):
            try:
                d = datetime.strptime((date_str or "")[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
            for i, (s, e) in enumerate(weeks):
                if s <= d <= e:
                    return i
            return None

        # ── WB: из кеша orders (индивидуальные записи с nmId/supplierArticle) ──
        wb_by_sku: dict = {}   # sku → {"rub": [n], "qty": [n], "name": str}
        wb_total_rub = [0.0] * n
        wb_total_qty = [0]   * n
        try:
            _, wb_orders, _ = await cache.get_raw_data(dt_from, dt_to)
            for o in wb_orders:
                if o.get("isCancel"):
                    continue
                idx = week_idx(o.get("date") or o.get("lastChangeDate"))
                if idx is None:
                    continue
                raw = o.get("nmId") or o.get("supplierArticle") or ""
                sku = _cat.resolve_wb(raw) if raw else str(raw)
                name = o.get("subject") or o.get("category") or sku
                price = float(o.get("priceWithDisc") or o.get("totalPrice") or 0)
                if sku not in wb_by_sku:
                    wb_by_sku[sku] = {"rub": [0.0]*n, "qty": [0]*n, "name": name}
                wb_by_sku[sku]["rub"][idx] += price
                wb_by_sku[sku]["qty"][idx] += 1
                wb_total_rub[idx] += price
                wb_total_qty[idx] += 1
        except Exception as e:
            import logging; logging.getLogger(__name__).warning("weekly_orders WB: %s", e)

        # ── OZON / YM ──
        oz_ok = ym_ok = True
        try:
            oz_rows, ym_rows = await asyncio.wait_for(
                asyncio.gather(
                    ozon_client.get_sales_detail(date_from_str, date_to_str),
                    ym_client.get_sales_detail(date_from_str, date_to_str),
                ),
                timeout=120,
            )
        except Exception as _exc:
            import logging; logging.getLogger(__name__).warning("weekly_orders OZON/YM: %s", _exc)
            oz_rows, ym_rows = [], []
            oz_ok = ym_ok = False

        def agg_rows(rows, sku_field, resolve):
            by_sku: dict = {}
            total_rub = [0.0] * n
            total_qty = [0]   * n
            for r in rows:
                idx = week_idx(r.get("date"))
                if idx is None:
                    continue
                raw = r.get(sku_field) or ""
                sku = resolve(raw) if raw else ""
                rev = float(r.get("revenue") or 0)
                qty = int(r.get("qty") or 0)
                if sku not in by_sku:
                    by_sku[sku] = {"rub": [0.0]*n, "qty": [0]*n, "name": sku}
                by_sku[sku]["rub"][idx] = round(by_sku[sku]["rub"][idx] + rev, 2)
                by_sku[sku]["qty"][idx] += qty
                total_rub[idx] = round(total_rub[idx] + rev, 2)
                total_qty[idx] += qty
            return by_sku, total_rub, total_qty

        oz_by_sku, oz_rub, oz_qty = agg_rows(oz_rows, "offer_id", _cat.resolve_ozon)
        ym_by_sku, ym_rub, ym_qty = agg_rows(ym_rows, "shop_sku", _cat.resolve_ym)

        def clean_sku(by_sku, total_rub, total_qty):
            """Sort SKUs by total revenue desc, round numbers."""
            result = []
            for sku, d in by_sku.items():
                cat = _cat.CATALOG.get(sku) or {}
                result.append({
                    "sku": sku,
                    "name": d["name"] or cat.get("name", sku),
                    "brand": cat.get("brand", ""),
                    "group": cat.get("group", ""),
                    "rub": [round(v, 2) for v in d["rub"]],
                    "qty": d["qty"],
                })
            result.sort(key=lambda x: sum(x["rub"]), reverse=True)
            return result

        result = {
            "weeks": [_week_label(s, e) for s, e in weeks],
            "WB":   {"total_rub": [round(v,2) for v in wb_total_rub], "total_qty": wb_total_qty,
                     "skus": clean_sku(wb_by_sku, wb_total_rub, wb_total_qty)},
            "OZON": {"total_rub": oz_rub, "total_qty": oz_qty,
                     "skus": clean_sku(oz_by_sku, oz_rub, oz_qty)},
            "YM":   {"total_rub": ym_rub, "total_qty": ym_qty,
                     "skus": clean_sku(ym_by_sku, ym_rub, ym_qty)},
        }
        # Кешируем всегда. Если Ozon/YM упали — TTL короткий (5 мин),
        # чтобы при следующем заходе попробовать снова; если всё ок — 30 мин.
        _wo_cache = result
        _wo_cache_ts = _wtime.monotonic() if (oz_ok and ym_ok) else (_wtime.monotonic() - _WO_TTL + 300)
        return result


@router.post("/weekly_orders/invalidate", include_in_schema=False)
async def invalidate_weekly_orders():
    global _wo_cache, _wo_cache_ts
    _wo_cache = {}; _wo_cache_ts = 0.0
    return {"status": "ok"}


# ── Помесячная сводка Продажи/Выкупы ──────────────────────────────────────────

_RU_MONTHS = ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
              "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


def _month_ranges(n_months: int = 6) -> list[tuple]:
    """Последние n_months календарных месяцев (date-диапазоны), старые первыми, вкл. текущий."""
    today = (datetime.utcnow() + timedelta(hours=3)).date()  # Moscow time
    ranges = []
    y, m = today.year, today.month
    months_back = []
    for _ in range(n_months):
        months_back.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    for (yy, mm) in reversed(months_back):
        start = datetime(yy, mm, 1).date()
        if mm == 12:
            end = datetime(yy, 12, 31).date()
        else:
            end = (datetime(yy, mm + 1, 1) - timedelta(days=1)).date()
        ranges.append((start, end))
    return ranges


def _month_label(start) -> str:
    return f"{_RU_MONTHS[start.month]} {start.year}"


_monthly_cache: dict = {}
_monthly_cache_ts: float = 0.0
_MONTHLY_TTL = 1800  # 30 минут
_monthly_fetch_lock = asyncio.Lock()


@router.post("/monthly_summary/invalidate", include_in_schema=False)
async def invalidate_monthly_cache():
    global _monthly_cache, _monthly_cache_ts
    _monthly_cache = {}
    _monthly_cache_ts = 0.0
    return {"status": "ok"}


@router.get("/monthly_summary")
async def get_monthly_summary():
    """Сводка Продажи/Выкупы по месяцам за последние 6 месяцев, все МП."""
    global _monthly_cache, _monthly_cache_ts
    if _monthly_cache and _wtime.monotonic() - _monthly_cache_ts < _MONTHLY_TTL:
        return _monthly_cache

    async with _monthly_fetch_lock:
        if _monthly_cache and _wtime.monotonic() - _monthly_cache_ts < _MONTHLY_TTL:
            return _monthly_cache

        months = _month_ranges(6)
        n = len(months)

        import logging as _mslog
        _mslog = _mslog.getLogger("monthly_summary")

        date_from_str = months[0][0].strftime("%Y-%m-%d")
        date_to_str   = months[-1][1].strftime("%Y-%m-%d")
        month_str_ranges = [(s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")) for s, e in months]

        try:
            oz_rows, ym_rows = await asyncio.wait_for(
                asyncio.gather(
                    ozon_client.get_sales_detail(date_from_str, date_to_str),
                    ym_client.get_sales_detail(date_from_str, date_to_str),
                ),
                timeout=90,
            )
        except Exception as _exc:
            _mslog.error("[MS] OZON/YM fetch failed: %s", _exc)
            oz_rows, ym_rows = [], []

        try:
            wb_funnel_months = await asyncio.wait_for(
                wb_client.get_nm_report_weeks(month_str_ranges), timeout=300,
            )
        except Exception as _exc:
            _mslog.error("[MS] WB funnel fetch failed: %s", _exc)
            wb_funnel_months = []
        wb_ok = any(f.get("orders_rub") or f.get("orders_qty") for f in wb_funnel_months)

        def month_index(date_str: str):
            try:
                d = datetime.strptime((date_str or "")[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
            for i, (s, e) in enumerate(months):
                if s <= d <= e:
                    return i
            return None

        rub = {mp: {"sales": [0.0] * n, "buyout": [0.0] * n} for mp in ("WB", "OZON", "YM")}
        qty = {mp: {"sales": [0] * n, "buyout": [0] * n} for mp in ("WB", "OZON", "YM")}

        for i, fw in enumerate(wb_funnel_months):
            if i >= n:
                break
            rub["WB"]["sales"][i]  = fw.get("orders_rub", 0.0)
            qty["WB"]["sales"][i]  = fw.get("orders_qty", 0)
            rub["WB"]["buyout"][i] = fw.get("buyouts_rub", 0.0)
            qty["WB"]["buyout"][i] = fw.get("buyouts_qty", 0)

        for r in oz_rows:
            idx = month_index(r.get("date"))
            if idx is None:
                continue
            rub["OZON"]["sales"][idx] += r["revenue"]
            qty["OZON"]["sales"][idx] += r["qty"]
            if (r.get("status") or "").lower() == "delivered":
                idx_d = month_index(r.get("delivered_date") or r.get("date"))
                if idx_d is not None:
                    rub["OZON"]["buyout"][idx_d] += r["revenue"]
                    qty["OZON"]["buyout"][idx_d] += r["qty"]

        for r in ym_rows:
            idx = month_index(r.get("date"))
            if idx is not None:
                rub["YM"]["sales"][idx] += r["revenue"]
                qty["YM"]["sales"][idx] += r["qty"]
            if (r.get("status") or "") == "DELIVERED":
                idx_d = month_index(r.get("update_date"))
                if idx_d is not None:
                    rub["YM"]["buyout"][idx_d] += r["revenue"]
                    qty["YM"]["buyout"][idx_d] += r["qty"]

        def block(d: dict, mp: str) -> dict:
            return {"sales": [round(v, 2) for v in d[mp]["sales"]],
                    "buyout": [round(v, 2) for v in d[mp]["buyout"]]}

        def total(d: dict) -> dict:
            return {
                "sales":  [round(sum(d[mp]["sales"][i]  for mp in ("WB", "OZON", "YM")), 2) for i in range(n)],
                "buyout": [round(sum(d[mp]["buyout"][i] for mp in ("WB", "OZON", "YM")), 2) for i in range(n)],
            }

        result = {
            "months": [_month_label(s) for s, e in months],
            "rub": {"OZON": block(rub, "OZON"), "WB": block(rub, "WB"), "YM": block(rub, "YM"), "total": total(rub)},
            "qty": {"OZON": block(qty, "OZON"), "WB": block(qty, "WB"), "YM": block(qty, "YM"), "total": total(qty)},
        }
        if wb_ok:
            _monthly_cache = result
            _monthly_cache_ts = _wtime.monotonic()
        return result


