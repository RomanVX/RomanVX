"""Накопление продаж по дням в Postgres/SQLite — история за пределами окон API.

API маркетплейсов отдают ограниченную историю (WB ~90 дней). Здесь мы
складываем агрегаты «день × площадка × артикул» (qty + выручка) с дедупликацией
по первичному ключу, поэтому повторные загрузки одного периода не дублируют
данные, а обновляют их до самых полных значений.
"""
import asyncio
import logging
from datetime import date as _date

import catalog as cat
import db

_log = logging.getLogger(__name__)

# Фоновые задачи записи в БД — держим ссылки, чтобы их не собрал GC.
_bg_tasks: set = set()


def _schedule(coro):
    """Запустить корутину в фоне (fire-and-forget), не блокируя event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # нет активного loop — пропускаем (например, при импорте)
    t = loop.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def persist_wb_bg(sales: list[dict]):
    """Неблокирующая запись WB: синхронный I/O к Neon уходит в отдельный поток."""
    _schedule(asyncio.to_thread(persist_wb, sales))


def persist_detail_bg(rows: list[dict], platform: str):
    """Неблокирующая запись Ozon/YM: I/O уходит в отдельный поток."""
    _schedule(asyncio.to_thread(persist_detail, rows, platform))


def _init():
    db.execute("""
        CREATE TABLE IF NOT EXISTS sales_daily (
            sale_date   TEXT,
            platform    TEXT,
            sku         TEXT,
            qty         INTEGER,
            revenue     REAL,
            updated_at  TEXT,
            PRIMARY KEY (sale_date, platform, sku)
        )
    """)

try:
    _init()
except Exception as _e:
    _log.error("sales_history _init failed (БД недоступна?): %s", _e)


def _upsert(agg: dict, platform: str):
    """agg: {(sale_date, sku): [qty, revenue]} → upsert одним запросом."""
    if not agg:
        return
    today = _date.today().isoformat()
    rows = [(d, platform, sku, int(q), float(rev), today)
            for (d, sku), (q, rev) in agg.items()]
    if db.IS_PG:
        db.executemany(
            "INSERT INTO sales_daily (sale_date, platform, sku, qty, revenue, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(sale_date, platform, sku) DO UPDATE SET "
            "qty=excluded.qty, revenue=excluded.revenue, updated_at=excluded.updated_at",
            rows,
        )
    else:
        db.executemany("INSERT OR REPLACE INTO sales_daily VALUES (?,?,?,?,?,?)", rows)
    _log.info("sales_daily: upserted %d rows for %s", len(rows), platform)


# ─── WRITE-THROUGH (вызывается при каждой загрузке данных) ──────────────────────

def persist_wb(sales: list[dict]):
    """WB statistics/supplier/sales: каждая запись = 1 шт, выручка priceWithDisc."""
    try:
        agg: dict = {}
        for s in sales or []:
            if s.get("isCancel"):
                continue
            d = (s.get("date") or "")[:10]
            if not d:
                continue
            raw = s.get("nmId") or s.get("supplierArticle") or ""
            sku = cat.resolve_wb(raw) if raw else ""
            rev = float(s.get("priceWithDisc") or s.get("finishedPrice") or 0)
            a = agg.setdefault((d, sku), [0, 0.0])
            a[0] += 1
            a[1] += rev
        _upsert(agg, "WB")
    except Exception as e:
        _log.warning("persist_wb failed: %s", e)


def persist_wb_orders(orders: list[dict]):
    """WB заказы (без отмен) → platform WB_ORDERS: то, что видит селлер
    в виджете «Заказы» ЛК. Поток WB (продажи) не трогаем — на нём живут
    прогнозы и юнитка."""
    try:
        agg: dict = {}
        for o in orders or []:
            if o.get("isCancel"):
                continue
            d = (o.get("date") or "")[:10]
            if not d:
                continue
            raw = o.get("nmId") or o.get("supplierArticle") or ""
            sku = cat.resolve_wb(raw) if raw else ""
            rev = float(o.get("priceWithDisc") or o.get("totalPrice") or 0)
            a = agg.setdefault((d, sku), [0, 0.0])
            a[0] += 1
            a[1] += rev
        _upsert(agg, "WB_ORDERS")
    except Exception as e:
        _log.warning("persist_wb_orders failed: %s", e)


def persist_wb_orders_bg(orders: list[dict]):
    _schedule(asyncio.to_thread(persist_wb_orders, orders))


def persist_detail(rows: list[dict], platform: str):
    """Ozon/YM детальные строки: [{date, offer_id/shop_sku/sku, qty, revenue}]."""
    try:
        resolve = {"Ozon": cat.resolve_ozon, "YM": cat.resolve_ym}.get(platform, lambda x: x)
        agg: dict = {}
        for r in rows or []:
            d = (r.get("date") or "")[:10]
            if not d:
                continue
            raw = r.get("offer_id") or r.get("shop_sku") or r.get("sku") or ""
            sku = resolve(raw) if raw else ""
            a = agg.setdefault((d, sku), [0, 0.0])
            a[0] += int(r.get("qty") or 0)
            a[1] += float(r.get("revenue") or 0)
        _upsert(agg, platform)
    except Exception as e:
        _log.warning("persist_detail(%s) failed: %s", platform, e)


# ─── READ ──────────────────────────────────────────────────────────────────────

def get_history(date_from=None, date_to=None, platform=None) -> list[dict]:
    where, params = ["1=1"], []
    if date_from:
        where.append("sale_date >= ?"); params.append(date_from)
    if date_to:
        where.append("sale_date <= ?"); params.append(date_to)
    if platform and platform != "all":
        where.append("platform = ?"); params.append(platform)
    rows = db.fetchall(
        "SELECT sale_date, platform, sku, qty, revenue FROM sales_daily "
        f"WHERE {' AND '.join(where)} ORDER BY sale_date",
        tuple(params),
    )
    return [{"date": r[0], "platform": r[1], "sku": r[2],
             "qty": r[3], "revenue": float(r[4] or 0)} for r in rows]


def get_summary(date_from=None, date_to=None) -> dict:
    """Накопленная история: итоги по площадкам и по дням.

    Агрегируем в SQL — тянуть все строки sale_date × platform × sku из Neon
    и складывать в Python на порядок медленнее из-за объёма передачи.
    """
    where, params = ["1=1"], []
    if date_from:
        where.append("sale_date >= ?"); params.append(date_from)
    if date_to:
        where.append("sale_date <= ?"); params.append(date_to)
    cond = " AND ".join(where)

    plat_rows = db.fetchall(
        f"SELECT platform, SUM(qty), SUM(revenue) FROM sales_daily WHERE {cond} GROUP BY platform",
        tuple(params),
    )
    by_platform = {r[0]: {"qty": int(r[1] or 0), "revenue": round(float(r[2] or 0), 2)}
                   for r in plat_rows}

    day_rows = db.fetchall(
        f"SELECT sale_date, platform, SUM(revenue), SUM(qty) FROM sales_daily "
        f"WHERE {cond} GROUP BY sale_date, platform ORDER BY sale_date",
        tuple(params),
    )
    by_day: dict = {}
    for d, platform, rev, qty in day_rows:
        row = by_day.setdefault(d, {"date": d})
        row[platform.lower()] = round(float(rev or 0), 2)
        row[platform.lower() + "_qty"] = int(qty or 0)

    bounds = db.fetchone("SELECT MIN(sale_date), MAX(sale_date) FROM sales_daily") or (None, None)
    return {
        "by_platform": by_platform,
        "daily": sorted(by_day.values(), key=lambda x: x["date"]),
        "stored_from": bounds[0],
        "stored_to": bounds[1],
        "days_stored": len(by_day),
    }


# ── История остатков: без неё нельзя отличить «упало из-за цены» от «аута» ──
def _init_stocks() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS stocks_daily (
        day TEXT, platform TEXT, sku TEXT, qty INTEGER,
        PRIMARY KEY (day, platform, sku))""")


async def snapshot_stocks() -> dict:
    """Суточный срез остатков по площадкам — копится вечно, как продажи."""
    from datetime import date
    _init_stocks()
    day = date.today().isoformat()
    rows = []
    try:
        from routers import dashboard as _dash
        for r in await _dash.get_stocks_table() or []:
            sku = str(r.get("sku") or "").strip()
            if not sku:
                continue
            for pf, key in (("WB", "wb_qty"), ("OZON", "oz_qty"), ("YM", "ym_qty")):
                q = r.get(key)
                if q is None:
                    continue
                rows.append((day, pf, sku, int(q or 0)))
    except Exception as e:
        _log.warning("stocks snapshot: %s", e)
        return {"saved": 0, "error": str(e)[:200]}
    for chunk in rows:
        db.execute(
            "INSERT INTO stocks_daily (day, platform, sku, qty) VALUES (?,?,?,?) "
            "ON CONFLICT (day, platform, sku) DO UPDATE SET qty = excluded.qty",
            chunk)
    return {"saved": len(rows), "day": day}


def stock_days(sku: str = "", days: int = 90) -> list[dict]:
    """История остатков: когда товар стоял в нуле."""
    from datetime import date, timedelta
    _init_stocks()
    since = (date.today() - timedelta(days=days)).isoformat()
    where, params = ["day >= ?"], [since]
    if sku:
        where.append("sku = ?"); params.append(sku)
    rows = db.fetchall(
        "SELECT day, platform, sku, qty FROM stocks_daily WHERE "
        + " AND ".join(where) + " ORDER BY day", tuple(params))
    return [dict(zip(["day", "platform", "sku", "qty"], r)) for r in rows]
