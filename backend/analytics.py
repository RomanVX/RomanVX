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
    """Price the buyer actually paid — matches WB 'Продажи' column.
    WB Statistics API returns finishedPrice (after SPP discount) on sales records."""
    fp = r.get("finishedPrice")
    if fp:
        return fp
    if "priceWithDisc" in r:
        return r["priceWithDisc"]
    return r.get("totalPrice", 0) * (1 - r.get("discountPercent", 0) / 100)

def _forpay(r: dict) -> float:
    """Net payout to seller after WB commission + logistics."""
    fp = r.get("forPay")
    if fp is not None:
        return fp
    return _gross(r) - _commission(r) - _delivery(r)

def _commission(r: dict) -> float:
    cr = r.get("commissionRub")
    if cr is not None:
        return cr
    # Derive from finishedPrice - forPay - delivery when possible
    if "forPay" in r and "deliveryRub" in r:
        return max(0, _gross(r) - r["forPay"] - r.get("deliveryRub", 0))
    return round(_gross(r) * COMMISSION_RATE)

def _delivery(r: dict) -> float:
    return r.get("deliveryRub", r.get("deliveryAmount", 0)) or 0

def _storage(r: dict) -> float:
    # storageFee per record if present (rare in /supplier/sales), else estimate
    return r.get("storageFee", r.get("storageRub", round(_gross(r) * STORAGE_RATE)))

def _cost(r: dict) -> float:
    return r.get("costRub", round(r.get("totalPrice", _gross(r)) * COST_RATE))


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
                "cost": 0.0, "commission": 0.0, "logistics": 0.0, "storage": 0.0,
            }
        a = agg[nm]
        a["realization"] += _forpay(r)
        a["sales_after_spp"] += _gross(r)
        a["sold"] += 1
        a["cost"] += _cost(r)
        a["commission"] += _commission(r)
        a["logistics"] += _delivery(r)
        a["storage"] += _storage(r)
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


def unit_economics(sales: list[dict], orders: list[dict], days: int,
                   costs: dict[str, float] | None = None) -> list[dict]:
    """Per-SKU unit economics. costs = {supplierArticle: cost_per_unit}."""
    skus = _by_sku(sales, days)
    ord_qty: dict[int, int] = defaultdict(int)
    for r in orders:
        ord_qty[r["nmId"]] += 1

    rows = []
    for nm, a in skus.items():
        n = a["sold"]
        if n == 0:
            continue

        avg_price         = a["sales_after_spp"] / n
        revenue_per_unit  = a["realization"] / n

        art = a["supplierArticle"]
        if costs and art in costs:
            cost_per_unit = costs[art]
            cost_source   = "file"
        else:
            cost_per_unit = a["cost"] / n
            cost_source   = "estimated"

        commission_per = a["commission"] / n
        logistics_per  = a["logistics"] / n
        storage_per    = a["storage"] / n
        ad_per         = a["realization"] * AD_RATE / n
        tax_per        = a["sales_after_spp"] * TAX_RATE / n

        profit_per = (revenue_per_unit - cost_per_unit - commission_per
                      - logistics_per - storage_per - ad_per - tax_per)
        margin = (profit_per / avg_price * 100) if avg_price else 0.0
        roi    = (profit_per / cost_per_unit * 100) if cost_per_unit else 0.0

        rows.append({
            "nmId": nm,
            "supplierArticle": art,
            "subject": a["subject"],
            "sold": n,
            "avg_price":        round(avg_price),
            "revenue_per_unit": round(revenue_per_unit),
            "cost_per_unit":    round(cost_per_unit),
            "cost_source":      cost_source,
            "commission_per":   round(commission_per),
            "logistics_per":    round(logistics_per),
            "storage_per":      round(storage_per),
            "ad_per":           round(ad_per),
            "tax_per":          round(tax_per),
            "profit_per":       round(profit_per),
            "margin":           round(margin, 1),
            "roi":              round(roi, 1),
        })

    rows.sort(key=lambda r: r["profit_per"], reverse=True)
    return rows


# ── Real unit economics from reportDetailByPeriod ─────────────────────────────

ACQUIRING_RATE = 0.027   # ~2.7% WB payment processing fee
USN_RATE       = 0.06    # УСН 6% of revenue (configurable)


def unit_economics_real(
    report: list[dict],
    days: int,
    costs: dict[str, float] | None = None,
    names: dict[str, str]   | None = None,
    usn_rate: float = USN_RATE,
) -> list[dict]:
    """Per-article unit economics from reportDetailByPeriod (real WB data).

    Key report fields:
      sa_name          — supplier article
      nm_id            — WB article ID
      subject_name     — product category
      brand_name       — brand
      doc_type_name    — "Продажа" / "Возврат" / "Хранение" / "Штраф" etc.
      retail_amount    — gross revenue (Выкупы ₽)
      commission_percent — WB commission %
      delivery_rub     — logistics cost ₽
      ppvz_vw          — WB total remuneration ₽ (commission incl.)
      storage_fee      — storage ₽
      penalty          — fines ₽
      deduction        — deductions (losses, defects etc.) ₽
      acceptance       — acceptance fee ₽
      additional_payment — bonuses from WB ₽
      for_pay          — net payout to seller ₽
    """
    agg: dict[str, dict] = {}

    for r in report:
        art = str(r.get("sa_name") or r.get("nm_id") or "")
        if not art:
            continue
        if art not in agg:
            agg[art] = {
                "nm_id": r.get("nm_id"),
                "subject": "", "brand": "",
                "sale_qty": 0, "return_qty": 0,
                "sale_rub": 0.0, "return_rub": 0.0,
                "logistics": 0.0, "commission": 0.0,
                "storage": 0.0, "penalty": 0.0, "deduction": 0.0,
                "acceptance": 0.0, "additional": 0.0, "for_pay": 0.0,
            }
        a = agg[art]
        if r.get("subject_name"):
            a["subject"] = r["subject_name"]
        if r.get("brand_name"):
            a["brand"] = r["brand_name"]

        doc    = r.get("doc_type_name", "")
        qty    = r.get("quantity", 0) or 0
        retail = r.get("retail_amount", 0.0) or 0.0
        comm_pct = r.get("commission_percent", 0.0) or 0.0

        if doc == "Продажа":
            a["sale_qty"] += qty
            a["sale_rub"] += retail
        elif doc == "Возврат":
            a["return_qty"] += qty
            a["return_rub"] += retail

        a["logistics"]   += abs(r.get("delivery_rub",        0.0) or 0.0)
        a["commission"]  += abs(retail * comm_pct / 100)
        a["storage"]     += abs(r.get("storage_fee",         0.0) or 0.0)
        a["penalty"]     += abs(r.get("penalty",             0.0) or 0.0)
        a["deduction"]   += abs(r.get("deduction",           0.0) or 0.0)
        a["acceptance"]  += abs(r.get("acceptance",          0.0) or 0.0)
        a["additional"]  +=    (r.get("additional_payment",  0.0) or 0.0)
        a["for_pay"]     +=    (r.get("for_pay",             0.0) or 0.0)

    rows = []
    for art, a in agg.items():
        net_qty = a["sale_qty"] - a["return_qty"]
        if net_qty <= 0:
            continue

        buy_rub   = a["sale_rub"] - a["return_rub"]
        acquiring = round(buy_rub * ACQUIRING_RATE, 2)
        tax       = round(buy_rub * usn_rate, 2)
        for_pay   = a["for_pay"]

        if costs and art in costs:
            cost_per    = costs[art]
            cost_source = "file"
        else:
            cost_per    = 0.0
            cost_source = "missing"
        cost_total = cost_per * net_qty

        name = (names or {}).get(art) or a["subject"]
        profit = round(for_pay - cost_total - acquiring - tax - a["penalty"] - a["deduction"])

        avg_price = buy_rub / net_qty if net_qty else 0
        margin    = round(profit / buy_rub * 100, 1)  if buy_rub     else 0.0
        roi       = round(profit / cost_total * 100, 1) if cost_total else 0.0

        rows.append({
            "supplierArticle": art,
            "nmId":            a["nm_id"],
            "name":            name,
            "subject":         a["subject"],
            "brand":           a["brand"],
            "sale_qty":        a["sale_qty"],
            "return_qty":      a["return_qty"],
            "sold":            net_qty,
            "buy_rub":         round(buy_rub),
            "avg_price":       round(avg_price),
            "cost_per_unit":   round(cost_per, 2),
            "cost_total":      round(cost_total),
            "cost_source":     cost_source,
            "logistics":       round(a["logistics"]),
            "commission":      round(a["commission"]),
            "acquiring":       round(acquiring),
            "storage":         round(a["storage"]),
            "penalty":         round(a["penalty"]),
            "deduction":       round(a["deduction"]),
            "acceptance":      round(a["acceptance"]),
            "additional":      round(a["additional"]),
            "for_pay":         round(for_pay),
            "tax":             round(tax),
            "profit":          profit,
            "margin":          margin,
            "roi":             roi,
        })

    rows.sort(key=lambda r: r["profit"], reverse=True)
    return rows


def monthly_pivot(report: list[dict], usn_rate: float = USN_RATE) -> list[dict]:
    """Aggregate reportDetailByPeriod records by calendar month."""
    from datetime import datetime as _dt

    RU_MONTHS = {1:"янв",2:"фев",3:"мар",4:"апр",5:"май",6:"июн",
                 7:"июл",8:"авг",9:"сен",10:"окт",11:"ноя",12:"дек"}

    by_month: dict[str, dict] = {}

    for r in report:
        raw_dt = (r.get("sale_dt") or r.get("rr_dt") or r.get("order_dt") or "")
        month  = raw_dt[:7]
        if not month or len(month) < 7:
            continue
        if month not in by_month:
            by_month[month] = {
                "sale_qty": 0, "return_qty": 0,
                "sale_rub": 0.0, "return_rub": 0.0,
                "logistics": 0.0, "commission": 0.0,
                "storage": 0.0, "penalty": 0.0, "deduction": 0.0,
                "acceptance": 0.0, "additional": 0.0, "for_pay": 0.0,
            }
        m   = by_month[month]
        doc = r.get("doc_type_name", "")
        qty = r.get("quantity", 0) or 0
        retail   = r.get("retail_amount", 0.0) or 0.0
        comm_pct = r.get("commission_percent", 0.0) or 0.0

        if doc == "Продажа":
            m["sale_qty"] += qty
            m["sale_rub"] += retail
        elif doc == "Возврат":
            m["return_qty"] += qty
            m["return_rub"] += retail

        m["logistics"]  += abs(r.get("delivery_rub",       0.0) or 0.0)
        m["commission"] += abs(retail * comm_pct / 100)
        m["storage"]    += abs(r.get("storage_fee",        0.0) or 0.0)
        m["penalty"]    += abs(r.get("penalty",            0.0) or 0.0)
        m["deduction"]  += abs(r.get("deduction",          0.0) or 0.0)
        m["acceptance"] += abs(r.get("acceptance",         0.0) or 0.0)
        m["additional"] +=    (r.get("additional_payment", 0.0) or 0.0)
        m["for_pay"]    +=    (r.get("for_pay",            0.0) or 0.0)

    rows = []
    for month, m in sorted(by_month.items()):
        net_qty   = m["sale_qty"] - m["return_qty"]
        buy_rub   = m["sale_rub"] - m["return_rub"]
        acquiring = round(buy_rub * ACQUIRING_RATE)
        tax       = round(buy_rub * usn_rate)
        profit    = round(m["for_pay"] - acquiring - tax - m["penalty"] - m["deduction"])
        margin    = round(profit / buy_rub * 100, 1) if buy_rub else 0.0

        try:
            mo    = _dt.strptime(month, "%Y-%m")
            label = f"{RU_MONTHS[mo.month]} {mo.year}"
        except Exception:
            label = month

        rows.append({
            "month":      month,
            "label":      label,
            "sale_qty":   m["sale_qty"],
            "return_qty": m["return_qty"],
            "net_qty":    net_qty,
            "buy_rub":    round(buy_rub),
            "logistics":  round(m["logistics"]),
            "commission": round(m["commission"]),
            "acquiring":  acquiring,
            "storage":    round(m["storage"]),
            "penalty":    round(m["penalty"]),
            "deduction":  round(m["deduction"]),
            "acceptance": round(m["acceptance"]),
            "additional": round(m["additional"]),
            "for_pay":    round(m["for_pay"]),
            "tax":        tax,
            "profit":     profit,
            "margin":     margin,
        })

    return rows
