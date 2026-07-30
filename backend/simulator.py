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

import db

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
    # нулевая или положительная медиана означает, что наблюдений мало и они
    # шумные: спрос не может НЕ реагировать на цену. В таком случае честнее
    # взять консервативное допущение, чем рисовать «цена не влияет».
    if glob is None or glob > -0.2:
        glob = None
    return {"per_sku": per, "global": glob}


def _recent_rate(days: int = 14) -> dict:
    """Фактический темп продаж за последние дни: {SKU_UPPER: шт/мес}.

    Объём в юнитке — прогноз по 12 неделям, он ОТСТАЁТ, когда темп резко
    меняется (сняли продвижение, ушли в аут, сезон). Для симулятора это
    критично: считать эффект цены на несуществующем объёме бессмысленно.
    """
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        rows = db.fetchall(
            "SELECT sku, SUM(qty) FROM sales_daily "
            "WHERE platform = 'WB' AND sale_date >= ? GROUP BY sku", (since,))
    except Exception as e:
        _log.warning("recent rate: %s", str(e)[:150])
        return {}
    return {str(r[0]).upper(): round(float(r[1] or 0) / days * 30)
            for r in rows}


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
    recent = await asyncio.to_thread(_recent_rate, 14)
    recent7 = await asyncio.to_thread(_recent_rate, 7)
    out = []
    for b in items:
        s = str(b.get("sku") or "")
        if sku and s.upper() != sku.upper():
            continue
        lp = (live.get(s) or {}).get("discounted") or b.get("price0") or 0
        m = _ar._margin_math({**b, "price0": lp})
        e = el["per_sku"].get(s.upper())
        # объём: берём свежий факт, если он сильно разошёлся с прогнозом
        q_plan = m["qty_month"]
        q_recent = recent.get(s.upper())
        q_recent7 = recent7.get(s.upper())
        q_used, q_note = q_plan, ""
        if q_recent is not None and q_plan:
            drift = (q_recent - q_plan) / q_plan * 100
            if abs(drift) >= 30:
                q_used = q_recent
                q_note = (f"объём взят по факту последних 14 дней "
                          f"({q_recent} шт/мес), прогноз юнитки {q_plan} "
                          f"устарел на {round(drift)}%")
        if q_recent7 is not None and q_recent and q_recent:
            d7 = (q_recent7 - q_recent) / q_recent * 100 if q_recent else 0
            if d7 <= -30:
                q_note = ((q_note + "; ") if q_note else "") + \
                    (f"темп продолжает падать: за 7 дней {q_recent7} шт/мес "
                     f"против {q_recent} за 14 — эффект цены считать рано, "
                     "сначала разберись с причиной падения")
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
            "qty_month": q_used,
            "qty_plan": q_plan, "qty_recent14": q_recent, "qty_recent7": q_recent7,
            "qty_note": q_note,
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
    # запас неопределённости. Если истории мало, честнее показать оба края:
    # «спрос не заметил» и «спрос упал сильнее цены», а не одну цифру.
    if it["elasticity_events"] < 2:
        e_lo, e_hi = -1.5, 0.0
    else:
        e_lo, e_hi = e * 1.5, e * 0.5
    q_lo = max(0.0, q0 * (1 + e_lo * dprice / 100))
    q_hi = max(0.0, q0 * (1 + e_hi * dprice / 100))

    def _pack(price, qty, pu):
        return {"price_seller": round(price),
                "price_buyer": round(price * it["buyer_ratio"]) if it["buyer_ratio"] else None,
                "qty_month": round(qty),
                "revenue_month": round(price * qty),
                "profit_unit": round(pu),
                "margin_pct": round(pu / price * 100, 1) if price else None,
                "profit_month": round(pu * qty)}

    def _costs(price, drr, cogs_v):
        comm_pct = (raw.get("comm_pct") or 0) + (raw.get("acq_pct") or 0)
        return {
            "цена продавца": round(price),
            "комиссия и эквайринг": -round(price * comm_pct / 100),
            "логистика": -round(raw.get("logist") or 0),
            "хранение": -round(raw.get("storage") or 0),
            "прочее (штрафы, удержания)": -round(raw.get("other") or 0),
            "себестоимость": -round(cogs_v),
            "реклама (ДРР)": -round(price * drr / 100),
            "= прибыль на штуку": round(_unit_profit(raw, price, drr, cogs_v)),
        }

    now, new = _pack(p0, q0, pu0), _pack(p1, q1, pu1)
    return {
        "breakdown": {"now": _costs(p0, it["drr_pct"], it["cogs"]),
                      "new": _costs(p1, d1, c1),
                      "comm_pct": round((raw.get("comm_pct") or 0)
                                        + (raw.get("acq_pct") or 0), 1)},
        "sku": it["sku"], "name": it["name"],
        "now": now, "new": new,
        "delta": {"revenue": new["revenue_month"] - now["revenue_month"],
                  "profit": new["profit_month"] - now["profit_month"],
                  "qty": new["qty_month"] - now["qty_month"],
                  "margin_pp": round((new["margin_pct"] or 0) - (now["margin_pct"] or 0), 1)},
        "range": {"profit_low": round(pu1 * min(q_lo, q_hi)),
                  "profit_high": round(pu1 * max(q_lo, q_hi))},
        "volume_note": it.get("qty_note") or "",
        "volume_source": {"plan": it.get("qty_plan"),
                          "fact_14d": it.get("qty_recent14"),
                          "fact_7d": it.get("qty_recent7"),
                          "used": it["qty_month"]},
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
            "volume_note": it.get("qty_note") or "",
            "points": points,
            "best_profit": best_profit, "best_revenue": best_rev,
            "elasticity": e, "events": it["elasticity_events"],
            "confidence": _confidence(it["elasticity_events"]),
            "warning": ("Мало наблюдений по этому SKU — кривая построена на "
                        "общей реакции кабинета, считай её ориентиром, "
                        "а не законом.") if it["elasticity_events"] < 2 else ""}


async def optimal(max_step: int = 15) -> dict:
    """Оптимальное ценообразование по всем SKU: максимум месячной прибыли
    на кривой цен в коридоре ±max_step% от текущей закреплённой цены.

    Рекомендация выдаётся только когда выигрыш ощутим (≥1000 ₽/мес или ≥5%)
    — двигать цену ради копеек вреднее, чем не двигать. Уверенность зависит
    от числа собственных ценовых экспериментов SKU (эластичность)."""
    b = await base()
    if b.get("error"):
        return b
    items, total_gain = [], 0
    for it in b["items"]:
        p0, q0, e = it["price_seller"], it["qty_month"], it["elasticity"]
        if not p0:
            continue
        d = it["drr_pct"]
        pts = []
        for step in _GRID:
            if abs(step) > max_step:
                continue
            p = p0 * (1 + step / 100)
            q = max(0.0, (q0 or 0) * (1 + e * step / 100))
            pu = _unit_profit(it["_raw"], p, d, it["cogs"])
            pts.append({"step": step, "price": round(p), "qty": round(q),
                        "profit": round(pu * q), "unit": round(pu)})
        cur = next(x for x in pts if x["step"] == 0)
        best = max(pts, key=lambda x: x["profit"])
        delta = best["profit"] - cur["profit"]
        recommend = (best["step"] != 0
                     and (delta >= max(1000, abs(cur["profit"]) * 0.05)
                          or (cur["unit"] < 0 <= best["unit"])))
        row = {
            "sku": it["sku"], "name": it["name"],
            "price_now": p0,
            "buyer_now": it["price_buyer"],
            "profit_now": cur["profit"], "margin_now": it["margin_pct"],
            "price_opt": best["price"] if recommend else p0,
            "buyer_opt": (round(best["price"] * it["buyer_ratio"])
                          if recommend and it["buyer_ratio"] else it["price_buyer"]),
            "step_pct": best["step"] if recommend else 0,
            "profit_opt": best["profit"] if recommend else cur["profit"],
            "delta_month": delta if recommend else 0,
            "qty_now": q0, "qty_opt": best["qty"] if recommend else q0,
            "elasticity": e, "elasticity_source": it["elasticity_source"],
            "confidence": _confidence(it["elasticity_events"]),
            "note": it.get("qty_note") or "",
            "verdict": ("поднять цену" if recommend and best["step"] > 0 else
                        "снизить цену" if recommend else
                        "цена оптимальна в коридоре ±%d%%" % max_step),
        }
        if recommend:
            total_gain += delta
        items.append(row)
    items.sort(key=lambda r: -r["delta_month"])
    return {
        "items": items, "max_step": max_step,
        "total_gain_month": round(total_gain),
        "method": ("максимум прибыли на кривой цена×объём; объём реагирует "
                   "по эластичности из НАШИХ прошлых изменений цен; ДРР и "
                   "затраты — текущие из юнитки; коридор ±%d%% от "
                   "закреплённой цены" % max_step),
        "caveats": [
            "Эластичность с уверенностью «низкая» — допущение, не факт: такие "
            "рекомендации проверять экспериментом (одно изменение за раз, "
            "замер через 7-14 дней).",
            "Если у SKU падает темп продаж (см. note) — сначала причина "
            "падения, потом цена.",
            "Смена цены меняет проходимость акций — см. правила кабинета.",
        ],
    }
