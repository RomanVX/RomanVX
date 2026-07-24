"""Эластичность цены: как продажи реально реагировали на изменения цены.

Данные уже лежат в проекте с самого начала — price_history (снимок цен
по дням) и sales_daily (продажи по дням и SKU). Сопоставляем: находим
события изменения цены, сравниваем средний темп продаж до и после, и
получаем ответ на вопрос «поднимать или нет» фактом, а не спором.

Осторожно с выводами: на темп влияет не только цена (реклама, сезон,
остаток), поэтому события с аутом отбрасываем, а результат подаём как
наблюдение с числом наблюдений, а не как закон.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import db

_log = logging.getLogger("elasticity")

_WINDOW = 7          # дней до и после изменения
_MIN_CHANGE = 3.0    # % — меньше считаем шумом


def _events_for(sku: str, prices: list, sales: dict) -> list[dict]:
    """События смены цены у SKU с темпом продаж до и после."""
    out = []
    for i in range(1, len(prices)):
        d_prev, p_prev = prices[i - 1]
        d_cur, p_cur = prices[i]
        if not p_prev or not p_cur:
            continue
        change = (p_cur - p_prev) / p_prev * 100
        if abs(change) < _MIN_CHANGE:
            continue
        day = datetime.strptime(d_cur, "%Y-%m-%d").date()
        before = [sales.get((day - timedelta(days=k)).isoformat(), 0)
                  for k in range(1, _WINDOW + 1)]
        after = [sales.get((day + timedelta(days=k)).isoformat(), 0)
                 for k in range(0, _WINDOW)]
        if len(after) < _WINDOW:
            continue
        avg_b = sum(before) / _WINDOW
        avg_a = sum(after) / _WINDOW
        # событие бесполезно, если до этого не продавали или всё встало в ноль
        # (второе обычно означает аут, а не реакцию на цену)
        if avg_b < 0.5 or (avg_a == 0 and avg_b > 0 and p_cur < p_prev):
            continue
        qty_change = (avg_a - avg_b) / avg_b * 100
        out.append({
            "date": d_cur,
            "price_from": round(p_prev), "price_to": round(p_cur),
            "price_change_pct": round(change, 1),
            "qty_before": round(avg_b, 2), "qty_after": round(avg_a, 2),
            "qty_change_pct": round(qty_change, 1),
            # эластичность: на сколько % меняется спрос на 1% цены
            "elasticity": round(qty_change / change, 2) if change else None,
            "revenue_before": round(avg_b * p_prev * 30),
            "revenue_after": round(avg_a * p_cur * 30),
        })
    return out


def build(days: int = 180) -> dict:
    """Разбор всех изменений цены за период по каждому SKU."""
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        price_rows = db.fetchall(
            "SELECT sku, day, discounted FROM price_history "
            "WHERE day >= ? ORDER BY sku, day", (since,))
    except Exception as e:
        return {"error": f"история цен недоступна: {str(e)[:150]}"}
    if not price_rows:
        return {"error": "история цен пока пустая — она копится с каждым днём"}
    try:
        sale_rows = db.fetchall(
            "SELECT sku, sale_date, SUM(qty) FROM sales_daily "
            "WHERE sale_date >= ? AND platform = 'WB' GROUP BY sku, sale_date",
            (since,))
    except Exception as e:
        return {"error": f"история продаж недоступна: {str(e)[:150]}"}

    prices: dict = {}
    for sku, day, disc in price_rows:
        if disc:
            prices.setdefault(str(sku), []).append((str(day)[:10], float(disc)))
    # оставляем только точки изменения цены — плато не интересно
    for sku, seq in prices.items():
        clean = []
        for d, p in seq:
            if not clean or abs(clean[-1][1] - p) > 0.5:
                clean.append((d, p))
        prices[sku] = clean

    sales: dict = {}
    for sku, day, qty in sale_rows:
        sales.setdefault(str(sku), {})[str(day)[:10]] = float(qty or 0)

    per_sku = {}
    for sku, seq in prices.items():
        ev = _events_for(sku, seq, sales.get(sku, {}))
        if ev:
            per_sku[sku] = ev

    # сводка по SKU: медианная эластичность и вердикт
    summary = []
    for sku, ev in per_sku.items():
        els = sorted(e["elasticity"] for e in ev if e["elasticity"] is not None)
        if not els:
            continue
        med = els[len(els) // 2]
        ups = [e for e in ev if e["price_change_pct"] > 0]
        downs = [e for e in ev if e["price_change_pct"] < 0]
        if med > -0.5:
            verdict = "спрос почти не реагирует на цену — есть запас поднять"
        elif med < -1.5:
            verdict = "спрос чувствителен: подъём цены заметно режет объём"
        else:
            verdict = "умеренная реакция — шаги по 5-10% безопасны"
        summary.append({
            "sku": sku, "events": len(ev), "elasticity_median": med,
            "ups": len(ups), "downs": len(downs),
            "verdict": verdict,
            "last": ev[-1]})
    summary.sort(key=lambda x: x["elasticity_median"])
    return {"days": days, "skus": len(summary), "summary": summary,
            "events": per_sku}


async def get(days: int = 180) -> dict:
    return await asyncio.to_thread(build, days)
