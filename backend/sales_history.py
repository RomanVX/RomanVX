"""Накопление продаж по дням в Postgres/SQLite — история за пределами окон API.

API маркетплейсов отдают ограниченную историю (WB ~90 дней). Здесь мы
складываем агрегаты «день × площадка × артикул» (qty + выручка) с дедупликацией
по первичному ключу, поэтому повторные загрузки одного периода не дублируют
данные, а обновляют их до самых полных значений.
"""
import logging
from datetime import date as _date

import catalog as cat
import db

_log = logging.getLogger(__name__)


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
    """Накопленная история: итоги по площадкам и по дням."""
    rows = get_history(date_from, date_to)
    by_platform: dict = {}
    by_day: dict = {}
    for r in rows:
        p = by_platform.setdefault(r["platform"], {"qty": 0, "revenue": 0.0})
        p["qty"] += r["qty"]
        p["revenue"] += r["revenue"]
        d = by_day.setdefault(r["date"], {"date": r["date"]})
        key = r["platform"].lower()
        d[key] = round(d.get(key, 0.0) + r["revenue"], 2)
    for v in by_platform.values():
        v["revenue"] = round(v["revenue"], 2)
    bounds = db.fetchone("SELECT MIN(sale_date), MAX(sale_date) FROM sales_daily") or (None, None)
    return {
        "by_platform": by_platform,
        "daily": sorted(by_day.values(), key=lambda x: x["date"]),
        "stored_from": bounds[0],
        "stored_to": bounds[1],
        "days_stored": len(by_day),
    }
