"""Period totals and read-only data sources for the Biomed Orders preview."""
import asyncio
import logging
from datetime import datetime, timedelta
from functools import lru_cache
from time import monotonic

import catalog
import sales_history

_log = logging.getLogger(__name__)
_tasks = {}
_TTL = 900


def ranges(period, today):
    if period == "day":
        previous = today - timedelta(days=1)
        return (today, today), (previous, previous)
    if period == "week":
        start = today - timedelta(days=today.weekday())
        previous = start - timedelta(days=7)
        return (start, today), (previous, today - timedelta(days=7))
    start = today.replace(day=1)
    previous_end = start - timedelta(days=1)
    previous = previous_end.replace(day=1)
    return (start, today), (previous, min(previous_end, previous + (today - start)))


def matches(sku, brand):
    return not brand or catalog.lookup(sku).get("brand", "").strip().casefold() == brand.casefold()


@lru_cache(maxsize=128)
def order_totals(platform, brand, start, end, cache_minute):
    source = "WB_ORDERS" if platform == "WB" else platform
    rows = sales_history.get_history(start.isoformat(), end.isoformat(), source)
    rows = [row for row in rows if matches(row["sku"], brand)]
    return {"revenue": sum(row["revenue"] for row in rows),
            "units": sum(row["qty"] for row in rows)}


async def _wb_ads(start, end, brand):
    import advert_client as client
    from config import USE_ADVERT_MOCK, WB_ADVERT_KEY

    if USE_ADVERT_MOCK or not WB_ADVERT_KEY:
        return {}
    begin, finish = start.isoformat(), end.isoformat()
    ids = client.active_ids(await client.get_all_campaign_ids_ext(), begin)
    totals = dict(ad_revenue=0.0, ad_spend=0.0, ad_impressions=0)
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        data = await client._fullstats_get({
            "ids": ",".join(map(str, batch)), "beginDate": begin, "endDate": finish,
        })
        if not isinstance(data, list):
            return {}
        for campaign in data:
            for day in campaign.get("days") or []:
                for app in day.get("apps") or []:
                    rows = (app.get("nms") or []) if brand else [app]
                    for row in rows:
                        if brand and not matches(catalog.resolve_wb(row.get("nmId")), brand):
                            continue
                        totals["ad_revenue"] += float(row.get("sum_price") or 0)
                        totals["ad_spend"] += float(row.get("sum") or 0)
                        totals["ad_impressions"] += int(row.get("views") or 0)
    return totals


async def _ozon_ads(start, end, brand):
    import ozon_perf_client as client

    # The daily report is account-wide. Do not attribute mixed campaigns to a brand.
    if brand or not client.configured():
        return {}
    rows = await client.get_daily(start.isoformat(), end.isoformat(), strict=True)
    if not rows:
        return {}
    return {key: sum(row[field] for row in rows) for key, field in (
        ("ad_revenue", "orders_money"), ("ad_spend", "spent"), ("ad_impressions", "views"),
    )}


async def _ozon_impressions(start, end, brand):
    import ozon_client

    rows = await ozon_client.get_analytics_data(start.isoformat(), end.isoformat(), ["hits_view"])
    if not any(row["metrics"].get("hits_view") is not None for row in rows):
        return {}
    return {"impressions": sum(
        row["metrics"].get("hits_view") or 0
        for row in rows if matches(catalog.resolve_ozon(row["sku"]), brand)
    )}


async def _wb_expenses(start, end, brand):
    from routers import finance

    # The detail rows can lag the operating date. Only use covered intervals.
    dates = [row.get("rrDate", "")[:10] for row in finance._detail_cache.get("rows", [])
             if row.get("rrDate")]
    if not dates or max(dates) < start.isoformat() or min(dates) > end.isoformat():
        return {}
    partial = min(dates) > start.isoformat() or max(dates) < end.isoformat()
    result = await finance.get_wb_unit_days(start.isoformat(), end.isoformat())
    keys = ("commission", "acquiring", "delivery", "storage", "acceptance",
            "penalty", "deductions", "cogs")
    return {"expenses_without_ads": sum(
        sum(item["months"]["RANGE"].get(key, 0) for key in keys)
        for item in result.get("skus", []) if matches(item["sku"], brand)
    ), "expenses_partial": partial}


async def _ozon_expenses(start, end, brand):
    import cost_store
    import ozon_client
    from routers import finance

    types = {item.get("id"): item for item in await ozon_client.get_accrual_types()}
    if not types:
        return {}
    sums, skus = {}, {}
    found = False
    for offset in range((end - start).days + 1):
        rows = await ozon_client.get_accruals_day((start + timedelta(days=offset)).isoformat())
        found |= bool(rows)
        for row in rows:
            finance._oz_decompose_accrual(row, types, sums, skus)
    if not found:
        return {}
    costs = await asyncio.to_thread(cost_store.get_costs)
    selected = {sku: row for sku, row in skus.items() if matches(sku, brand)}
    cogs = sum(costs.get(sku, 0) * row.get("qty", 0) for sku, row in selected.items())
    if brand:
        expenses = -sum(value for row in selected.values() for key, value in row.items()
                        if key not in {"revenue", "qty"})
        ad_spend = -sum(row.get("advert", 0) for row in selected.values())
    else:
        expenses = -sum(value for key, value in sums.items()
                        if key not in {"Продажи и возвраты", "Программы партнёров"}
                        and not key.startswith("__") and (value < 0 or key in finance._OZ_GROUP_TO_COL))
        ad_spend = -sums.get("Продвижение и реклама", 0)
    return {"expenses": expenses + cogs, "financial_ad_spend": ad_spend}


async def _ym_expenses(start, end, brand):
    from routers import finance

    today = (datetime.utcnow() + timedelta(hours=3)).date()
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    if start.day != 1 or end != min(next_month - timedelta(days=1), today):
        return {}
    month = start.strftime("%Y-%m")
    groups, skus = await asyncio.to_thread(finance._ym_srv_load_db)
    if month not in groups or (brand and month not in skus):
        return {}
    articles = skus.get(month, {})
    columns = ({key: sum(row.get(key, 0) for sku, row in articles.items() if matches(sku, brand))
                for key in groups[month]} if brand else groups[month])
    revenue = finance._ym_unit_rev.get(month, {})
    cogs = sum(row.get("cogs", 0) for sku, row in revenue.items() if matches(sku, brand))
    return {"expenses": sum(columns.values()) + cogs, "ad_spend": columns.get("advert", 0)}


_SOURCES = {
    "WB": (_wb_ads, _wb_expenses),
    "OZON": (_ozon_ads, _ozon_impressions, _ozon_expenses),
    "YM": (_ym_expenses,),
}
_source_slots = asyncio.Semaphore(6)


async def _read_source(source, start, end, brand):
    try:
        async with _source_slots:
            return await asyncio.wait_for(source(start, end, brand), timeout=180)
    except Exception as exc:
        _log.warning("Development orders: %s unavailable (%s)", source.__name__, type(exc).__name__)
        return {}


def _source_result(source, start, end, brand):
    now = monotonic()
    for key, (task, created) in tuple(_tasks.items()):
        if task.done() and now - created > _TTL:
            del _tasks[key]
    key = source, start, end, brand
    if key not in _tasks:
        if len(_tasks) >= 96:
            finished = next((k for k, (task, _) in _tasks.items() if task.done()), None)
            if finished is None:
                return {}, True
            del _tasks[finished]
        _tasks[key] = asyncio.create_task(_read_source(source, start, end, brand)), now
    task, _ = _tasks[key]
    return (task.result(), False) if task.done() else ({}, True)


async def get_metrics(period, platforms, brand, today):
    periods = ranges(period, today)
    result = []
    pending = False
    for start, end in periods:
        blocks = {}
        for platform in platforms:
            metrics = dict(await asyncio.to_thread(order_totals, platform, brand, start, end, int(monotonic() // 60)))
            for source in _SOURCES[platform]:
                values, loading = _source_result(source, start, end, brand)
                pending |= loading
                metrics.update(values)
            if "ad_spend" not in metrics and "financial_ad_spend" in metrics:
                metrics["ad_spend"] = metrics.pop("financial_ad_spend")
            if "expenses_without_ads" in metrics:
                metrics["expenses"] = metrics["expenses_without_ads"] + metrics.get("ad_spend", 0)
                metrics["expenses_partial"] = metrics["expenses_partial"] or "ad_spend" not in metrics
            elif "expenses" not in metrics and "ad_spend" in metrics:
                metrics["expenses"] = metrics["ad_spend"]
                metrics["expenses_partial"] = True
            blocks[platform] = metrics
        result.append(blocks)
    return {"current": result[0], "previous": result[1], "pending": pending}
