import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Query, HTTPException
import analytics
import cache
import catalog as _catalog
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


@router.get("/weekly_summary")
async def get_weekly_summary():
    """Сводка Продажи/Выкупы по неделям (Пн-Вс) за последние 8 недель, все МП."""
    weeks = _week_ranges(8)
    n = len(weeks)
    dt_from = datetime.combine(weeks[0][0], datetime.min.time())
    dt_to   = datetime.combine(weeks[-1][1], datetime.min.time())

    import logging as _wblog
    _wb = _wblog.getLogger("weekly_summary.wb")
    try:
        wb_sales, wb_orders, _ = await cache.get_raw_data(dt_from, dt_to)
        _wb.info("[WB] cache ok: sales=%d orders=%d range=%s–%s",
                 len(wb_sales), len(wb_orders), dt_from.date(), dt_to.date())
    except Exception as _exc:
        _wb.error("[WB] cache.get_raw_data FAILED: %s", _exc)
        wb_sales, wb_orders = [], []

    date_from_str = weeks[0][0].strftime("%Y-%m-%d")
    date_to_str   = weeks[-1][1].strftime("%Y-%m-%d")
    try:
        oz_rows, ym_rows = await asyncio.wait_for(
            asyncio.gather(
                ozon_client.get_sales_detail(date_from_str, date_to_str),
                ym_client.get_sales_detail(date_from_str, date_to_str),
            ),
            timeout=120,
        )
    except asyncio.TimeoutError:
        import logging as _tlog
        _tlog.getLogger("weekly_summary").warning("[WS] marketplace detail fetch timed out after 120s")
        oz_rows, ym_rows = [], []

    import logging as _wslog
    _ws = _wslog.getLogger("weekly_summary")
    _ws.info("[WS] ym_rows total=%d date_from=%s date_to=%s",
             len(ym_rows), date_from_str, date_to_str)
    if ym_rows:
        r0 = ym_rows[0]
        _ws.info("[WS] ym_rows[0] keys=%s value=%s", list(r0.keys()), r0)
    _ws.info("[WS] weeks=%s", [(str(s), str(e)) for s, e in weeks])

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

    def _wb_price(r: dict) -> float:
        """priceWithDisc — цена после скидки продавца, ДО скидки СПП (за счёт WB).
        finishedPrice исключает СПП-скидку WB, поэтому для нашей выручки не подходит."""
        pwd = r.get("priceWithDisc")
        if pwd is not None:
            return float(pwd)
        return float(r.get("totalPrice") or 0) * (1 - float(r.get("discountPercent") or 0) / 100)

    # WB Продажи = /supplier/orders, isCancel != True, priceWithDisc (как в Power Query)
    if wb_orders:
        _ws.info("[WB] orders[0] keys=%s sample=%s", list(wb_orders[0].keys()), {k: wb_orders[0].get(k) for k in ("date", "lastChangeDate", "isCancel", "priceWithDisc", "totalPrice", "supplierArticle")})
    n_skip_cancel = n_skip_date = n_added = 0
    for r in wb_orders:
        if r.get("isCancel") is True:
            n_skip_cancel += 1
            continue
        idx = week_index(r.get("date"))
        if idx is None:
            n_skip_date += 1
            continue
        n_added += 1
        rub["WB"]["sales"][idx] += _wb_price(r)
        qty["WB"]["sales"][idx] += 1
    _ws.info("[WB] orders: added=%d skip_cancel=%d skip_date=%d / total=%d", n_added, n_skip_cancel, n_skip_date, len(wb_orders))

    # WB Выкупы = /supplier/sales все записи, priceWithDisc (как в Power Query)
    # Возвраты (R*) имеют отрицательный priceWithDisc — они вычитаются автоматически
    if wb_sales:
        _ws.info("[WB] sales[0] keys=%s sample=%s", list(wb_sales[0].keys()), {k: wb_sales[0].get(k) for k in ("date", "lastChangeDate", "saleID", "priceWithDisc", "supplierArticle")})
    n_skip_date_s = n_added_s = 0
    for r in wb_sales:
        idx = week_index(r.get("date"))
        if idx is None:
            n_skip_date_s += 1
            continue
        n_added_s += 1
        rub["WB"]["buyout"][idx] += _wb_price(r)
        qty["WB"]["buyout"][idx] += 1
    _ws.info("[WB] sales: added=%d skip_date=%d / total=%d", n_added_s, n_skip_date_s, len(wb_sales))
    _ws.info("[WB] rub[WB][sales]=%s", rub["WB"]["sales"])
    _ws.info("[WB] rub[WB][buyout]=%s", rub["WB"]["buyout"])

    # Ozon Продажи = все строки FBO; Выкупы = status == delivered
    for r in oz_rows:
        idx = week_index(r.get("date"))
        if idx is None:
            continue
        rub["OZON"]["sales"][idx] += r["revenue"]
        qty["OZON"]["sales"][idx] += r["qty"]
        if (r.get("status") or "").lower() == "delivered":
            rub["OZON"]["buyout"][idx] += r["revenue"]
            qty["OZON"]["buyout"][idx] += r["qty"]

    # Продажи — все статусы, по дате создания (date)
    # Выкупы — только DELIVERED, по дате доставки (update_date)
    ym_sales = ym_buyout = 0
    for r in ym_rows:
        idx = week_index(r.get("date"))
        if idx is not None:
            rub["YM"]["sales"][idx] += r["revenue"]
            qty["YM"]["sales"][idx] += r["qty"]
            ym_sales += 1
        if (r.get("status") or "") == "DELIVERED":
            idx_d = week_index(r.get("update_date"))
            if idx_d is not None:
                rub["YM"]["buyout"][idx_d] += r["revenue"]
                qty["YM"]["buyout"][idx_d] += r["qty"]
                ym_buyout += 1
    _ws.info("[WS] ym sales_rows=%d buyout_rows=%d / total=%d",
             ym_sales, ym_buyout, len(ym_rows))

    def block(d: dict, mp: str) -> dict:
        return {"sales": [round(v, 2) for v in d[mp]["sales"]],
                "buyout": [round(v, 2) for v in d[mp]["buyout"]]}

    def total(d: dict) -> dict:
        return {
            "sales":  [round(sum(d[mp]["sales"][i]  for mp in ("WB", "OZON", "YM")), 2) for i in range(n)],
            "buyout": [round(sum(d[mp]["buyout"][i] for mp in ("WB", "OZON", "YM")), 2) for i in range(n)],
        }

    return {
        "weeks": [_week_label(s, e) for s, e in weeks],
        "rub": {"OZON": block(rub, "OZON"), "WB": block(rub, "WB"), "YM": block(rub, "YM"), "total": total(rub)},
        "qty": {"OZON": block(qty, "OZON"), "WB": block(qty, "WB"), "YM": block(qty, "YM"), "total": total(qty)},
    }


