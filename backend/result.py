"""Результат: что изменилось с момента подключения и что мы для этого сделали.

Две половины одного экрана:
  1. Динамика денег по месяцам — выручка, прибыль, маржа, ДРР — и дельта
     к базовому месяцу (месяц подключения кабинета).
  2. Журнал наших действий за тот же период: что применили, когда, с чем.

Считается из уже готового P&L, ничего нового не собираем.
"""
import asyncio
import logging
from datetime import datetime, timedelta

_log = logging.getLogger("result")
_KV_BASE = "result_baseline"


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


async def _pnl_months(months: int = 12) -> dict:
    """Помесячные показатели по всем площадкам: {месяц: {...}}."""
    from routers import finance as _fin
    out: dict = {}
    sources = (("wb", _fin.get_wb_pnl), ("ozon", _fin.get_ozon_pnl),
               ("ym", _fin.get_ym_pnl))
    for name, fn in sources:
        try:
            pnl = await asyncio.wait_for(fn(months=months, refresh=False),
                                         timeout=90)
        except Exception as e:
            _log.warning("pnl %s: %s", name, str(e)[:150])
            continue
        rows = {r["key"]: (r.get("values") or {}) for r in pnl.get("rows") or []}
        # ключи P&L проекта: выручка retailAmount, итог gross, реклама advert
        rev = rows.get("retailAmount") or rows.get("revenue") or {}
        prof = rows.get("gross") or rows.get("net_profit") or {}
        adv = rows.get("advert") or {}
        for month in rev:
            m = out.setdefault(str(month), {"revenue": 0.0, "profit": 0.0,
                                            "ads": 0.0, "platforms": {}})
            # затраты в P&L лежат со знаком минус — реклама берётся по модулю
            r_ = _num(rev.get(month))
            p_ = _num(prof.get(month))
            a_ = abs(_num(adv.get(month)))
            m["revenue"] += r_
            m["profit"] += p_
            m["ads"] += a_
            m["platforms"][name] = {"revenue": round(r_), "profit": round(p_)}
    for m in out.values():
        m["margin_pct"] = round(m["profit"] / m["revenue"] * 100, 1) if m["revenue"] else None
        m["drr_pct"] = round(m["ads"] / m["revenue"] * 100, 1) if m["revenue"] else None
        m["revenue"] = round(m["revenue"])
        m["profit"] = round(m["profit"])
        m["ads"] = round(m["ads"])
    return dict(sorted(out.items()))


async def get(months: int = 12) -> dict:
    import snapshot as _snap
    data = await _pnl_months(months)
    if not data:
        return {"error": "P&L ещё не собран — зайди через несколько минут"}
    keys = list(data)
    base_key = await asyncio.to_thread(_snap.load, _KV_BASE, None)
    if base_key not in data:
        base_key = keys[0]          # по умолчанию — первый месяц с данными
    base, last = data[base_key], data[keys[-1]]

    def _delta(a, b):
        if a is None or b is None:
            return None
        return round(b - a, 1)

    # что мы сделали за период — журнал применённых действий агента
    actions = []
    try:
        import agent_actions as aa
        for a in await asyncio.to_thread(aa.journal, 100):
            if a.get("status") == "done":
                actions.append({"when": a.get("applied") or a.get("created"),
                                "what": a.get("title"),
                                "why": (a.get("reason") or "")[:200]})
    except Exception:
        pass
    # и решения стратега, закрытые как выполненные
    try:
        import agent_strategist as st
        for t in await asyncio.to_thread(st._tasks_load, "done"):
            actions.append({"when": t.get("created"), "what": t.get("title"),
                            "why": (t.get("result") or "")[:200]})
    except Exception:
        pass
    actions.sort(key=lambda x: str(x.get("when") or ""), reverse=True)

    return {
        "months": [{"month": k, **v} for k, v in data.items()],
        "baseline_month": base_key,
        "baseline": base,
        "current": last,
        "delta": {
            "revenue": round(last["revenue"] - base["revenue"]),
            "revenue_pct": round((last["revenue"] - base["revenue"])
                                 / base["revenue"] * 100) if base["revenue"] else None,
            "profit": round(last["profit"] - base["profit"]),
            "margin_pp": _delta(base.get("margin_pct"), last.get("margin_pct")),
            "drr_pp": _delta(base.get("drr_pct"), last.get("drr_pct")),
        },
        "actions": actions[:40],
    }


async def set_baseline(month: str) -> dict:
    """Зафиксировать месяц подключения кабинета как точку отсчёта."""
    import snapshot as _snap
    await asyncio.to_thread(_snap.save, _KV_BASE, month[:7])
    return {"baseline_month": month[:7]}
