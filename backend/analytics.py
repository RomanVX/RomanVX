"""Core analytics for the WB seller dashboard. Pure Python, no pandas."""
from __future__ import annotations
from collections import defaultdict
from typing import Any

COMMISSION_RATE = 0.15
STORAGE_RATE    = 0.015
COST_RATE       = 0.30
AD_RATE         = 0.06
TAX_RATE        = 0.07
ACCEPTANCE_PER  = 1.5
OTHER_RATE      = 0.01


def _date(s: str) -> str:
    return s[:10]

def _gross(r: dict) -> float:
    if "priceWithDisc" in r:
        return r["priceWithDisc"]
    return r.get("totalPrice", 0) * (1 - r.get("discountPercent", 0) / 100)

def _commission(r: dict) -> float:
    return r.get("commissionRub", round(_gross(r) * COMMISSION_RATE))

def _delivery(r: dict) -> float:
    return r.get("deliveryRub", r.get("deliveryAmount", 0)) or 0

def _storage(r: dict) -> float:
    return r.get("storageRub", round(_gross(r) * STORAGE_RATE))

def _cost(r: dict) -> float:
    return r.get("costRub", round(r.get("totalPrice", _gross(r)) * COST_RATE))

def _forpay(r: dict) -> float:
    if "forPay" in r:
        return r["forPay"]
    return _gross(r) - _commission(r) - _delivery(r) - _storage(r)


def filter_records(records: list[dict], brand: str | None, category: str | None) -> list[dict]:
    if not brand and not category:
        return records
    out = []
    for r in records:
        if brand and r.get("brand") != brand:
            continue
        if category and r.get("category") != category:
            continue
        out.append(r)
    return out


def available_filters(stocks: list[dict], sales: list[dict]) -> dict:
    brands, cats = set(), set()
    for r in list(stocks) + list(sales):
        if r.get("brand"):
            brands.add(r["brand"])
        if r.get("category"):
            cats.add(r["category"])
    return {"brands": sorted(brands), "categories": sorted(cats)}


def finance_aggregate(sales: list[dict], orders: list[dict]) -> dict[str, float]:
    realization = sum(_forpay(r) for r in sales)
    sales_after_spp = sum(_gross(r) for r in sales)
    sales_qty = len(sales)
    commission = sum(_commission(r) for r in sales)
    logistics = sum(_delivery(r) for r in sales)
    storage = sum(_storage(r) for r in sales)
    cost = sum(_cost(r) for r in sales)
    orders_rub = sum(_gross(r) for r in orders)
    orders_qty = len(orders)
    buyout_rate = (sales_qty / orders_qty * 100) if orders_qty else 0.0
    advertising = round(realization * AD_RATE)
    drr = (advertising / realization * 100) if realization else 0.0
    tax = round(sales_after_spp * TAX_RATE)
    paid_acceptance = round(sales_qty * ACCEPTANCE_PER)
    other = round(realization * OTHER_RATE)
    gross_margin = realization - cost
    net_profit = realization - cost - advertising - tax - paid_acceptance - other
    margin_pct = (net_profit / sales_after_spp * 100) if sales_after_spp else 0.0
    roi = (net_profit / cost * 100) if cost else 0.0
    return {
        "net_profit": round(net_profit),
        "margin_pct": round(margin_pct, 1),
        "profit_before_opex": round(realization - cost),
        "gross_margin_pct": round(gross_margin / sales_after_spp * 100, 1) if sales_after_spp else 0.0,
        "sales_after_spp": round(sales_after_spp),
        "sales_qty": sales_qty,
        "realization": round(realization),
        "commission": round(commission),
        "orders_rub": round(orders_rub),
        "orders_qty": orders_qty,
        "buyout_rate": round(buyout_rate, 1),
        "logistics": round(logistics),
        "logistics_pct": round(logistics / sales_after_spp * 100, 1) if sales_after_spp else 0.0,
        "advertising": advertising,
        "drr": round(drr, 1),
        "storage": round(storage),
        "storage_pct": round(storage / sales_after_spp * 100, 1) if sales_after_spp else 0.0,
        "paid_acceptance": paid_acceptance,
        "other_deductions": other,
        "roi": round(roi, 1),
        "cost": round(cost),
        "tax": tax,
        "tax_pct": round(tax / sales_after_spp * 100, 1) if sales_after_spp else 0.0,
        "tax_base": round(sales_after_spp),
    }


def _delta(cur: float, prev: float) -> dict:
    d = cur - prev
    pct = (d / prev * 100) if prev else 0.0
    return {"delta": round(d), "delta_pct": round(pct, 1)}


def finance_cards(cur: dict, prev: dict) -> list[dict]:
    def card(title, icon, key, unit="₽", secondary="", invert=False):
        c = {"title": title, "icon": icon, "unit": unit,
             "value": cur[key], "prev": prev[key],
             "secondary": secondary, "invert": invert}
        c.update(_delta(cur[key], prev[key]))
        return c
    return [
        card("Чистая прибыль",            "💰", "net_profit",        "₽", f"{cur['margin_pct']}% маржа"),
        card("Прибыль без опер. расходов", "📈", "profit_before_opex","₽", f"{cur['gross_margin_pct']}% маржа"),
        card("Продажи",                    "🛒", "sales_after_spp",   "₽", f"{cur['sales_qty']} шт"),
        card("Реализация",                 "💳", "realization",       "₽", "к перечислению"),
        card("Вознаграждение WB",          "🏦", "commission",        "₽", "комиссия",        invert=True),
        card("Заказы",                     "📦", "orders_rub",        "₽", f"{cur['orders_qty']} шт"),
        card("Процент выкупа",             "✅", "buyout_rate",       "%", "выкуплено"),
        card("Логистика",                  "🚚", "logistics",         "₽", f"{cur['logistics_pct']}%", invert=True),
        card("Реклама",                    "📢", "advertising",       "₽", f"ДРР {cur['drr']}%",       invert=True),
        card("Хранение",                   "🏬", "storage",           "₽", f"{cur['storage_pct']}%",   invert=True),
        card("Платная приёмка",            "📥", "paid_acceptance",   "₽", "приёмка",         invert=True),
        card("Прочие удержания",           "➖", "other_deductions",  "₽", "удержания",       invert=True),
        card("ROI",                        "🎯", "roi",               "%", "рентабельность"),
        card("Себестоимость продаж",       "🧾", "cost",              "₽", "COGS",            invert=True),
        card("Налоги",                     "🏛️","tax",               "₽", f"{cur['tax_pct']}%", invert=True),
        card("Налоговая база",             "📐", "tax_base",          "₽", "после СПП"),
    ]


def revenue_structure(agg: dict) -> list[dict]:
    return [
        {"label": "Реализация",        "value": agg["realization"],               "color": "#3b82f6"},
        {"label": "Продажи после СПП", "value": agg["sales_after_spp"],           "color": "#60a5fa"},
        {"label": "Логистика",         "value": -agg["logistics"],                "color": "#ec4899"},
        {"label": "Комиссия WB",       "value": -agg["commission"],               "color": "#f472b6"},
        {"label": "Реклама",           "value": -agg["advertising"],              "color": "#fb7185"},
        {"label": "Хранение",          "value": -agg["storage"],                  "color": "#f9a8d4"},
        {"label": "Себестоимость",     "value": -agg["cost"],                     "color": "#fda4af"},
        {"label": "Налоги",            "value": -agg["tax"],                      "color": "#e879f9"},
        {"label": "Валовая маржа",     "value": agg["realization"] - agg["cost"], "color": "#22c55e"},
        {"label": "Чистая прибыль",    "value": agg["net_profit"],                "color": "#2ecc71"},
    ]


def kpi_summary(sales: list[dict], orders: list[dict], stocks: list[dict]) -> dict[str, Any]:
    agg = finance_aggregate(sales, orders)
    stock_value = sum(
        (r.get("quantity", r.get("quantityFull", 0)) * r.get("Price", 0)) for r in stocks
    )
    return {
        "total_revenue": agg["realization"],
        "total_sales": agg["sales_qty"],
        "total_orders": agg["orders_qty"],
        "buyout_rate": agg["buyout_rate"],
        "stock_value": round(stock_value, 2),
    }


def sales_dynamics(sales: list[dict], orders: list[dict]) -> list[dict]:
    rev: dict[str, float] = defaultdict(float)
    scnt: dict[str, int] = defaultdict(int)
    for r in sales:
        d = _date(r["date"])
        rev[d] += _forpay(r)
        scnt[d] += 1
    ocnt: dict[str, int] = defaultdict(int)
    for r in orders:
        ocnt[_date(r["date"])] += 1
    days = sorted(set(rev) | set(ocnt))
    return [{"date": d, "revenue": round(rev[d]), "sales_count": scnt[d], "orders_count": ocnt[d]} for d in days]


def _by_sku(sales: list[dict], days: int) -> dict[int, dict]:
    agg: dict[int, dict] = {}
    for r in sales:
        nm = r["nmId"]
        if nm not in agg:
            agg[nm] = {
                "nmId": nm, "subject": r.get("subject", ""),
                "supplierArticle": r.get("supplierArticle", str(nm)),
                "brand": r.get("brand", ""), "category": r.get("category", ""),
                "realization": 0.0, "sales_after_spp": 0.0, "sold": 0,
                "cost": 0.0, "commission": 0.0, "logistics": 0.0,
            }
        a = agg[nm]
        a["realization"] += _forpay(r)
        a["sales_after_spp"] += _gross(r)
        a["sold"] += 1
        a["cost"] += _cost(r)
        a["commission"] += _commission(r)
        a["logistics"] += _delivery(r)
    return agg


def calc_daily_sales(sales: list[dict], days: int) -> list[dict]:
    return [{
        "nmId": a["nmId"], "subject": a["subject"],
        "supplierArticle": a["supplierArticle"],
        "brand": a["brand"], "category": a["category"],
        "total_revenue": round(a["realization"]),
        "total_qty": a["sold"],
        "avg_daily_sales": round(a["sold"] / days, 2),
    } for a in _by_sku(sales, days).values()]


def _abc_map(items: list[tuple[int, float]]) -> dict[int, str]:
    ordered = sorted(items, key=lambda x: x[1], reverse=True)
    total = sum(v for _, v in ordered) or 1.0
    out, cum = {}, 0.0
    for nm, v in ordered:
        cum += v / total * 100
        out[nm] = "A" if cum <= 80 else "B" if cum <= 95 else "C"
    return out


def products_table(sales: list[dict], orders: list[dict], days: int) -> list[dict]:
    skus = _by_sku(sales, days)
    ord_qty: dict[int, int] = defaultdict(int)
    returns: dict[int, int] = defaultdict(int)
    for r in orders:
        ord_qty[r["nmId"]] += 1
        if r.get("isCancel"):
            returns[r["nmId"]] += 1
    abc_rev = _abc_map([(nm, a["realization"]) for nm, a in skus.items()])
    profits = {nm: a["realization"] - a["cost"] - a["realization"] * AD_RATE - a["sales_after_spp"] * TAX_RATE
               for nm, a in skus.items()}
    abc_profit = _abc_map(list(profits.items()))
    rows = []
    for nm, a in skus.items():
        profit = profits[nm]
        ad = a["realization"] * AD_RATE
        margin = (profit / a["sales_after_spp"] * 100) if a["sales_after_spp"] else 0.0
        roi = (profit / a["cost"] * 100) if a["cost"] else 0.0
        drr = (ad / a["realization"] * 100) if a["realization"] else 0.0
        oq = ord_qty.get(nm, 0)
        buyout = (a["sold"] / oq * 100) if oq else 0.0
        rows.append({
            "nmId": nm, "subject": a["subject"], "supplierArticle": a["supplierArticle"],
            "brand": a["brand"], "category": a["category"],
            "realization": round(a["realization"]),
            "sales_after_spp": round(a["sales_after_spp"]),
            "for_pay": round(a["realization"]),
            "profit": round(profit), "margin": round(margin, 1), "roi": round(roi, 1),
            "drr": round(drr, 1), "buyout": round(buyout, 1),
            "orders": oq, "sold": a["sold"], "returns": returns.get(nm, 0),
            "abc_rev": abc_rev.get(nm, "C"), "abc_profit": abc_profit.get(nm, "C"),
        })
    rows.sort(key=lambda r: r["realization"], reverse=True)
    return rows


def top_skus(sales: list[dict], days: int, n: int = 20) -> list[dict]:
    rows = calc_daily_sales(sales, days)
    rows.sort(key=lambda r: r["total_revenue"], reverse=True)
    return rows[:n]


def abc_by_revenue(sales: list[dict], days: int) -> list[dict]:
    rows = calc_daily_sales(sales, days)
    if not rows:
        return []
    rows.sort(key=lambda r: r["total_revenue"], reverse=True)
    total = sum(r["total_revenue"] for r in rows) or 1.0
    cum = 0.0
    for r in rows:
        share = r["total_revenue"] / total * 100
        cum += share
        r["revenue_share"] = round(share, 2)
        r["cumulative_share"] = round(cum, 2)
        r["abc_category"] = "A" if cum <= 80 else "B" if cum <= 95 else "C"
    return rows


def warehouses(sales: list[dict], orders: list[dict], stocks: list[dict], days: int) -> list[dict]:
    sold_wh: dict[str, int] = defaultdict(int)
    for r in sales:
        sold_wh[r.get("warehouseName", "—")] += 1
    ord_wh: dict[str, int] = defaultdict(int)
    ret_wh: dict[str, int] = defaultdict(int)
    for r in orders:
        wh = r.get("warehouseName", "—")
        ord_wh[wh] += 1
        if r.get("isCancel"):
            ret_wh[wh] += 1
    stock_wh: dict[str, int] = defaultdict(int)
    for r in stocks:
        stock_wh[r.get("warehouseName", "—")] += r.get("quantity", r.get("quantityFull", 0))
    out = []
    for wh in sorted(set(stock_wh) | set(sold_wh)):
        stock = stock_wh.get(wh, 0)
        sold = sold_wh.get(wh, 0)
        per_day = sold / days if days else 0
        coverage = round(stock / per_day, 1) if per_day > 0 else 999.0
        oq = ord_wh.get(wh, 0)
        buyout = (sold / oq * 100) if oq else 0.0
        returns_pct = (ret_wh.get(wh, 0) / oq * 100) if oq else 0.0
        trend = [max(0, round(stock + per_day * (7 - i))) for i in range(8)]
        out.append({
            "warehouse": wh, "per_day": round(per_day, 1), "stock_qty": stock,
            "coverage_days": min(coverage, 999.0),
            "buyout": round(buyout, 1), "returns_pct": round(returns_pct, 1),
            "status_ok": coverage >= 14, "trend": trend,
        })
    out.sort(key=lambda r: r["coverage_days"])
    return out


def supplies(sales: list[dict], stocks: list[dict], days: int) -> list[dict]:
    daily = {r["nmId"]: r for r in calc_daily_sales(sales, days)}
    stock_qty: dict[int, int] = defaultdict(int)
    for r in stocks:
        stock_qty[r["nmId"]] += r.get("quantity", r.get("quantityFull", 0))
    rows = []
    for nm, d in daily.items():
        avg = d["avg_daily_sales"]
        stock = stock_qty.get(nm, 0)
        coverage = round(stock / avg, 1) if avg > 0 else 999.0
        priority = "urgent" if coverage < 14 else "planned" if coverage < 30 else "ok"
        rows.append({
            "nmId": nm, "subject": d["subject"], "supplierArticle": d["supplierArticle"],
            "brand": d["brand"], "category": d["category"],
            "stock_qty": stock, "avg_daily_sales": avg,
            "coverage_days": min(coverage, 999.0),
            "need_30d": max(0, round(avg * 30 - stock)),
            "need_60d": max(0, round(avg * 60 - stock)),
            "need_90d": max(0, round(avg * 90 - stock)),
            "priority": priority,
        })
    rows.sort(key=lambda r: ({"urgent": 0, "planned": 1, "ok": 2}[r["priority"]], r["coverage_days"]))
    return rows


def abc_by_turnover(sales: list[dict], stocks: list[dict], days: int) -> list[dict]:
    rows = sorted(supplies(sales, stocks, days), key=lambda r: r["coverage_days"])
    n = len(rows) or 1
    for i, r in enumerate(rows):
        pct = (i + 1) / n * 100
        r["abc_category"] = "A" if pct <= 50 else "B" if pct <= 80 else "C"
    return rows


def reorder_forecast(sales: list[dict], stocks: list[dict], days: int) -> list[dict]:
    return supplies(sales, stocks, days)
