"""Симулятор «что если»: меняем цену, ДРР, себестоимость — видим исход.

Честная сценарная модель, а не машинное обучение: сорок SKU и десятки
ценовых событий — не тот объём, на котором можно учить модель, зато
достаточно, чтобы соединить три известные вещи:

  затраты  — юнитка, детерминированная арифметика (комиссия, логистика,
             хранение, себестоимость, реклама);
  спрос    — базовый объём из прогноза Холта по sales_daily;
  реакция  — эластичность из истории: как объём менялся после прошлых
             изменений цены у ЭТОГО артикула.

Где данных мало — говорим об этом прямо (confidence), а не рисуем
уверенную кривую.
"""
import asyncio
import logging

_log = logging.getLogger("simulator")

_DEFAULT_ELAST = -1.0     # если истории нет: 1% цены → 1% спроса
_GRID = (-30, -20, -15, -10, -5, 0, 5, 10, 15, 20, 30)


def _unit_profit(b: dict, price: float, drr_pct: float,
                 cogs: float | None = None) -> float:
    """Та же арифметика, что в юнитке и калькуляторе маржи."""
    comm = (b.get("comm_pct") or 0) + (b.get("acq_pct") or 0)
    fixed = ((b.get("logist") or 0) + (b.get("storage") or 0)
             + (b.get("other") or 0)
             + (b.get("cogs") or 0 if cogs is None else cogs))
    return price * (1 - (comm + drr_pct) / 100) - fixed


async def _elasticity_map() -> dict:
    """Медианная эластичность по SKU + общая медиана как запасной вариант."""
    try:
        import elasticity
        d = await elasticity.get(365)
    except Exception as e:
        _log.warning("elasticity: %s", str(e)[:150])
        return {"per_sku": {}, "global": None}
    per = {}
    for row in d.get("summary") or []:
        per[str(row["sku"]).upper()] = {"e": row["elasticity_median"],
                                        "n": row["events"]}
    vals = sorted(v["e"] for v in per.values())
    glob = vals[len(vals) // 2] if vals else None
    return {"per_sku": per, "global": glob}


async def base(sku: str = "") -> dict:
    """Текущее состояние артикулов: цена, объём, затраты, эластичность."""
    from routers import tools as _tools
    import agent_review as _ar
    import wb_client as _wb
    data = await _tools.get_margin(mp="WB")
    items = data.get("items") or []
    if not items:
        return {"error": "юнитка ещё собирается — попробуй через минуту"}
    live = {}
    try:
        live = await asyncio.wait_for(_wb.get_current_prices(), timeout=30)
    except Exception:
        pass
    el = await _elasticity_map()
    out = []
    for b in items:
        s = str(b.get("sku") or "")
        if sku and s.upper() != sku.upper():
            continue
        lp = (live.get(s) or {}).get("discounted") or b.get("price0") or 0
        m = _ar._margin_math({**b, "price0": lp})
        e = el["per_sku"].get(s.upper())
        ratio = ((b.get("buyer0") or 0) / (b.get("price0") or 1)) if b.get("price0") else None
        out.append({
            "sku": s, "name": (b.get("name") or "")[:60],
            "price_seller": round(lp), "price_buyer": round(lp * ratio) if ratio else None,
            "buyer_ratio": round(ratio, 3) if ratio else None,
            "cogs": round(b.get("cogs") or 0),
            "comm_pct": round((b.get("comm_pct") or 0) + (b.get("acq_pct") or 0), 1),
            "logist": round(b.get("logist") or 0),
            "storage": round(b.get("storage") or 0),
            "other": round(b.get("other") or 0),
            "drr_pct": m["drr_pct"], "be_drr_pct": m["be_drr_pct"],
            "qty_month": m["qty_month"],
            "profit_unit": m["profit_unit"], "margin_pct": m["margin_pct"],
            "profit_month": m["profit_month"],
            "elasticity": (e or {}).get("e", el["global"] if el["global"] is not None else _DEFAULT_ELAST),
            "elasticity_events": (e or {}).get("n", 0),
            "elasticity_source": ("свой SKU" if e else
                                  "медиана кабинета" if el["global"] is not None
                                  else "допущение 1:1"),
            "_raw": {k: b.get(k) for k in
                     ("comm_pct", "acq_pct", "logist", "storage", "other", "cogs")},
        })
    if not out:
        return {"error": f"артикул {sku} не найден"}
    return {"items": out}


def _confidence(events: int) -> str:
    if events >= 5:
        return "высокая"
    if events >= 2:
        return "средняя"
    return "низкая"


async def simulate(sku: str, price_seller: float | None = None,
                   drr_pct: float | None = None, cogs: float | None = None,
                   qty_override: float | None = None) -> dict:
    """Один сценарий: что будет с объёмом, выручкой и прибылью."""
    b = await base(sku)
    if b.get("error"):
        return b
    it = b["items"][0]
    raw = it["_raw"]
    p0, q0 = it["price_seller"], it["qty_month"]
    p1 = float(price_seller) if price_seller else p0
    d1 = float(drr_pct) if drr_pct is not None else it["drr_pct"]
    c1 = float(cogs) if cogs is not None else it["cogs"]
    e = it["elasticity"]

    dprice = (p1 - p0) / p0 * 100 if p0 else 0
    q1 = float(qty_override) if qty_override is not None else \
        max(0.0, q0 * (1 + e * dprice / 100))

    pu0 = _unit_profit(raw, p0, it["drr_pct"], it["cogs"])
    pu1 = _unit_profit(raw, p1, d1, c1)
    # запас неопределённости: разброс эластичности ±50% от оценки
    q_lo = max(0.0, q0 * (1 + e * 1.5 * dprice / 100))
    q_hi = max(0.0, q0 * (1 + e * 0.5 * dprice / 100))

    def _pack(price, qty, pu):
        return {"price_seller": round(price),
                "price_buyer": round(price * it["buyer_ratio"]) if it["buyer_ratio"] else None,
                "qty_month": round(qty),
                "revenue_month": round(price * qty),
                "profit_unit": round(pu),
                "margin_pct": round(pu / price * 100, 1) if price else None,
                "profit_month": round(pu * qty)}

    now, new = _pack(p0, q0, pu0), _pack(p1, q1, pu1)
    return {
        "sku": it["sku"], "name": it["name"],
        "now": now, "new": new,
        "delta": {"revenue": new["revenue_month"] - now["revenue_month"],
                  "profit": new["profit_month"] - now["profit_month"],
                  "qty": new["qty_month"] - now["qty_month"],
                  "margin_pp": round((new["margin_pct"] or 0) - (now["margin_pct"] or 0), 1)},
        "range": {"profit_low": round(pu1 * min(q_lo, q_hi)),
                  "profit_high": round(pu1 * max(q_lo, q_hi))},
        "assumptions": {
            "elasticity": e, "events": it["elasticity_events"],
            "source": it["elasticity_source"],
            "confidence": _confidence(it["elasticity_events"]),
            "note": "объём пересчитан по исторической реакции спроса на цену; "
                    "затраты — из юнитки, реклама — по заданному ДРР"},
    }


async def curve(sku: str, drr_pct: float | None = None) -> dict:
    """Кривая прибыли по сетке цен: где максимум и где точка безубытка."""
    b = await base(sku)
    if b.get("error"):
        return b
    it = b["items"][0]
    raw = it["_raw"]
    p0, q0, e = it["price_seller"], it["qty_month"], it["elasticity"]
    d = it["drr_pct"] if drr_pct is None else float(drr_pct)
    points = []
    for step in _GRID:
        p = p0 * (1 + step / 100)
        q = max(0.0, q0 * (1 + e * step / 100))
        pu = _unit_profit(raw, p, d, it["cogs"])
        points.append({"step_pct": step, "price_seller": round(p),
                       "price_buyer": round(p * it["buyer_ratio"]) if it["buyer_ratio"] else None,
                       "qty_month": round(q), "revenue_month": round(p * q),
                       "profit_month": round(pu * q),
                       "margin_pct": round(pu / p * 100, 1) if p else None})
    best_profit = max(points, key=lambda x: x["profit_month"])
    best_rev = max(points, key=lambda x: x["revenue_month"])
    return {"sku": it["sku"], "name": it["name"], "current_price": p0,
            "points": points,
            "best_profit": best_profit, "best_revenue": best_rev,
            "elasticity": e, "events": it["elasticity_events"],
            "confidence": _confidence(it["elasticity_events"]),
            "warning": ("Мало наблюдений по этому SKU — кривая построена на "
                        "общей реакции кабинета, считай её ориентиром, "
                        "а не законом.") if it["elasticity_events"] < 2 else ""}
