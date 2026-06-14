"""Core analytics: ABC analysis, daily sales, reorder forecast. Pure Python."""
from __future__ import annotations
from collections import defaultdict
from typing import Any


def _parse_date(s: str) -> str:
    return s[:10]


def _group_sales(sales: list[dict]) -> dict[tuple, dict]:
    agg: dict[tuple, dict] = {}
    for r in sales:
        key = (r["nmId"], r.get("subject", ""))
        if key not in agg:
            agg[key] = {
                "total_revenue": 0.0,
                "total_qty": 0,
                "supplierArticle": r.get("supplierArticle", str(r["nmId"])),
            }
        agg[key]["total_revenue"] += r.get("forPay", 0)
        agg[key]["total_qty"] += 1
    return agg


def _group_stocks(stocks: list[dict]) -> dict[int, dict]:
    agg: dict[int, dict] = {}
    for r in stocks:
        nm = r["nmId"]
        qty = r.get("quantity", r.get("quantityFull", 0))
        price = r.get("Price", r.get("totalPrice", 0))
        if nm not in agg:
            agg[nm] = {"stock_qty": 0, "price_sum": 0.0, "price_n": 0}
        agg[nm]["stock_qty"] += qty
        agg[nm]["price_sum"] += price
        agg[nm]["price_n"] += 1
    return {nm: {"stock_qty": v["stock_qty"],
                 "price": v["price_sum"] / v["price_n"] if v["price_n"] else 0}
            for nm, v in agg.items()}


# ─── KPI summary ───────────────────────────────────────────────────────────────

def kpi_summary(sales: list[dict], orders: list[dict], stocks: list[dict]) -> dict[str, Any]:
    total_revenue = sum(r.get("forPay", 0) for r in sales)
    total_sales = len(sales)
    total_orders = len(orders)
    active_orders = sum(1 for r in orders if not r.get("isCancel", False))
    buyout_rate = round(total_sales / total_orders * 100, 1) if total_orders > 0 else 0.0
    stock_groups = _group_stocks(stocks)
    stock_value = sum(v["stock_qty"] * v["price"] for v in stock_groups.values())
    return {
        "total_revenue": round(total_revenue, 2),
        "total_sales": total_sales,
        "total_orders": total_orders,
        "active_orders": active_orders,
        "buyout_rate": buyout_rate,
        "stock_value": round(stock_value, 2),
    }


# ─── Daily sales dynamics ───────────────────────────────────────────────────────

def sales_dynamics(sales: list[dict], orders: list[dict]) -> list[dict]:
    rev: dict[str, float] = defaultdict(float)
    sales_cnt: dict[str, int] = defaultdict(int)
    for r in sales:
        d = _parse_date(r["date"])
        rev[d] += r.get("forPay", 0)
        sales_cnt[d] += 1

    ord_cnt: dict[str, int] = defaultdict(int)
    for r in orders:
        ord_cnt[_parse_date(r["date"])] += 1

    all_dates = sorted(set(rev) | set(ord_cnt))
    return [
        {
            "date": d,
            "revenue": round(rev[d], 2),
            "sales_count": sales_cnt[d],
            "orders_count": ord_cnt[d],
        }
        for d in all_dates
    ]


# ─── Average daily sales per SKU ───────────────────────────────────────────────────

def calc_daily_sales(sales: list[dict], days: int) -> list[dict]:
    agg = _group_sales(sales)
    result = []
    for (nm_id, subject), v in agg.items():
        result.append({
            "nmId": nm_id,
            "subject": subject,
            "supplierArticle": v["supplierArticle"],
            "total_revenue": round(v["total_revenue"], 2),
            "total_qty": v["total_qty"],
            "avg_daily_sales": round(v["total_qty"] / days, 2),
        })
    return result


# ─── ABC by revenue ────────────────────────────────────────────────────────────────

def abc_by_revenue(sales: list[dict], days: int) -> list[dict]:
    rows = calc_daily_sales(sales, days)
    if not rows:
        return []
    rows.sort(key=lambda r: r["total_revenue"], reverse=True)
    total = sum(r["total_revenue"] for r in rows) or 1.0
    cumulative = 0.0
    result = []
    for r in rows:
        share = round(r["total_revenue"] / total * 100, 2)
        cumulative = round(cumulative + share, 2)
        cat = "A" if cumulative <= 80 else "B" if cumulative <= 95 else "C"
        result.append({
            "nmId": r["nmId"],
            "subject": r["subject"],
            "supplierArticle": r["supplierArticle"],
            "total_revenue": r["total_revenue"],
            "revenue_share": share,
            "cumulative_share": cumulative,
            "abc_category": cat,
        })
    return result


# ─── ABC by turnover ────────────────────────────────────────────────────────────────

def abc_by_turnover(sales: list[dict], stocks: list[dict], days: int) -> list[dict]:
    daily = {r["nmId"]: r for r in calc_daily_sales(sales, days)}
    stock_groups = _group_stocks(stocks)
    all_ids = set(daily) | set(stock_groups)
    rows = []
    for nm_id in all_ids:
        d = daily.get(nm_id, {})
        s = stock_groups.get(nm_id, {"stock_qty": 0})
        avg = d.get("avg_daily_sales", 0.0)
        stock_qty = s["stock_qty"]
        coverage = round(stock_qty / avg, 1) if avg > 0 else 999.0
        rows.append({
            "nmId": nm_id,
            "subject": d.get("subject", ""),
            "supplierArticle": d.get("supplierArticle", str(nm_id)),
            "stock_qty": stock_qty,
            "avg_daily_sales": avg,
            "coverage_days": min(coverage, 999.0),
        })
    rows.sort(key=lambda r: r["coverage_days"])
    n = len(rows) or 1
    for i, r in enumerate(rows):
        pct = (i + 1) / n * 100
        r["abc_category"] = "A" if pct <= 50 else "B" if pct <= 80 else "C"
    return rows


# ─── Reorder forecast ────────────────────────────────────────────────────────────────

def reorder_forecast(sales: list[dict], stocks: list[dict], days: int) -> list[dict]:
    daily = calc_daily_sales(sales, days)
    stock_groups = _group_stocks(stocks)
    result = []
    for r in daily:
        nm_id = r["nmId"]
        avg = r["avg_daily_sales"]
        stock_qty = stock_groups.get(nm_id, {}).get("stock_qty", 0)
        result.append({
            "nmId": nm_id,
            "subject": r["subject"],
            "supplierArticle": r["supplierArticle"],
            "stock_qty": stock_qty,
            "avg_daily_sales": avg,
            "need_30d": max(0, round(avg * 30 - stock_qty)),
            "need_60d": max(0, round(avg * 60 - stock_qty)),
            "need_90d": max(0, round(avg * 90 - stock_qty)),
        })
    return result


# ─── Top SKUs ─────────────────────────────────────────────────────────────────────────

def top_skus(sales: list[dict], days: int, n: int = 20) -> list[dict]:
    rows = calc_daily_sales(sales, days)
    rows.sort(key=lambda r: r["total_revenue"], reverse=True)
    return rows[:n]
