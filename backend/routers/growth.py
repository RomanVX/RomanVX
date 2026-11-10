"""План роста Ozon (Biomed): точка старта → цели → факт по неделям.

Цели (с 10.11.2026, горизонт 13 недель): оборот ×2, к выводу ≥30%, маржа ≥20%,
реклама ≤8% выручки, локализация ≥70%. Точка старта (октябрь) и экономика
каждого товара (комиссия, логистика/шт, прочие услуги/шт, себестоимость)
загружаются один раз — POST /api/growth/baseline — и хранятся в kv.

Факт собирается из API (раз в час, фоном):
  заказы и выручка по дням — отправления FBO (v3) + FBS (v4), без отмен;
  реклама — дневная статистика кампаний Performance + отчёт «оплата за заказ»
            по SKU (расход по каждому товару);
  цены сейчас — /v5/product/info/prices (цена продавца, мин. цена, акции);
  ставки «оплаты за заказ» и индекс видимости — Performance search_promo;
  локализация — доля FBO-отправлений, где кластер склада = кластер покупателя.
Экономика недели по товару = выручка − комиссия% − (логистика+прочее)×шт
− реклама − себестоимость×шт (параметры из точки старта, цена и реклама — факт).
"""
import asyncio
import logging
import time as _time
from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/growth", tags=["growth"])

_BASE_KEY = "growth_baseline"
_JOURNAL_KEY = "growth_journal"
_STATE_KEY = "growth_state"
_TTL = 3600
_state: dict = {"building": False, "progress": "", "error": "", "data": None, "ts": 0.0}
_bg: set = set()

GROUPS = {
    "Фистинг Джага": ["BMN-0013", "BMN-0028", "BMN-0035", "BMN-0036"],
    "ST анал и фистинг": ["ST-03", "ST-07", "ST-14"],
    "ST классика": ["ST-01", "ST-02", "ST-04", "ST-05", "ST-06"],
    "Спреи Deep Throat": ["BMN-0110", "BMN-0115", "BMN-0116"],
    "Алоэ": ["AL-01", "AL-02", "AL-03", "AL-04", "AL-05", "AL-06", "AL-07"],
    "Наборы Алоэ": ["NA-01", "NA-02"],
}
SKU_GROUP = {a: g for g, arts in GROUPS.items() for a in arts}
# CPC-кампании → группа (для разнесения расхода «за клики»)
CAMPAIGN_GROUP = {"18066023": "Фистинг Джага", "18065988": "Спреи Deep Throat",
                  "42772261": "ST анал и фистинг", "19002685": "ST классика",
                  "19002712": "ST классика", "19002729": "ST классика",
                  "22168563": "Алоэ", "18066063": "Фистинг Джага",
                  "50360715": "Алоэ", "50360745": "ST классика"}


def _kv(key, default):
    import snapshot
    return snapshot.load(key, default) or default


def _kv_save(key, val):
    import snapshot
    snapshot.save(key, val)


def _owner(request: Request):
    import auth as _auth
    if (_auth._session_of(request) or {}).get("role") != "owner":
        raise HTTPException(403, "Только для владельца")


def _msk_today() -> date:
    return (datetime.utcnow() + timedelta(hours=3)).date()


# ── сбор факта ───────────────────────────────────────────────────────────────

async def _orders(since: date, to: date) -> tuple[dict, dict]:
    """{(день, артикул): [шт, выручка]} и {день: [локальных, всего]} (FBO)."""
    import ozon_client as oz
    by = defaultdict(lambda: [0, 0.0])
    loc = defaultdict(lambda: [0, 0])
    s, t = f"{since}T00:00:00Z", f"{to}T23:59:59Z"
    cursor = ""
    for _ in range(200):                                   # FBO v3 с финданными (кластеры)
        d = await oz._post("/v3/posting/fbo/list", {
            "cursor": cursor, "filter": {"since": s, "to": t}, "limit": 100,
            "sort_dir": "ASC", "with": {"financial_data": True}})
        ps = d.get("postings") or []
        for p in ps:
            if p.get("status") == "cancelled":
                continue
            day = (p.get("created_at") or p.get("in_process_at") or "")[:10]
            f = p.get("financial_data") or {}
            if f.get("cluster_from") and f.get("cluster_to"):
                loc[day][1] += 1
                loc[day][0] += 1 if f["cluster_from"] == f["cluster_to"] else 0
            for pr in p.get("products") or []:
                x = by[(day, pr.get("offer_id"))]
                q = int(pr.get("quantity") or 0)
                x[0] += q
                x[1] += oz._price_amount(pr.get("price")) * q
        cursor = d.get("cursor") or ""
        if not d.get("has_next") or not cursor or not ps:
            break
    try:
        for p in await oz.fbs_postings_v4(s, t):
            if p.get("status") == "cancelled":
                continue
            day = (p.get("in_process_at") or p.get("created_at") or "")[:10]
            for pr in p.get("products") or []:
                x = by[(day, pr.get("offer_id"))]
                q = int(pr.get("quantity") or 0)
                x[0] += q
                x[1] += oz._price_amount(pr.get("price")) * q
    except Exception as e:
        _log.warning("growth fbs: %s", e)
    return by, loc


async def _ads(since: date, to: date) -> tuple[dict, dict, list]:
    """Расход по SKU в день (оплата за заказ) + расход CPC по группе в день + кампании."""
    import ozon_perf_client as pc
    if not pc.configured():
        return {}, {}, []
    sku_day = defaultdict(float)
    try:
        for r in await pc.orders_report(str(since), str(to)):
            sku_day[(r["date"][:10], r.get("art") or r.get("sku"))] += r.get("spent") or 0
    except Exception as e:
        _log.warning("growth orders_report: %s", e)
    grp_day = defaultdict(float)
    camps = defaultdict(lambda: {"spent": 0.0, "orders": 0, "orders_money": 0.0, "days": {}})
    for r in await pc.get_daily(str(since), str(to)):
        c = camps[r["id"]]
        c["title"] = r["title"]
        c["spent"] += r["spent"]; c["orders"] += r["orders"]; c["orders_money"] += r["orders_money"]
        c["days"][r["date"]] = [r["spent"], r["orders"], r["orders_money"]]
        g = CAMPAIGN_GROUP.get(r["id"])
        if g:
            grp_day[(r["date"], g)] += r["spent"]
    meta = {}
    try:
        for c in await pc.get_campaigns():
            meta[str(c.get("id"))] = c
    except Exception:
        pass
    out = []
    for cid, c in camps.items():
        m = meta.get(cid, {})
        out.append({"id": cid, "title": c.get("title") or m.get("title", ""),
                    "state": m.get("state", ""), "type": m.get("advObjectType", ""),
                    "weekly_budget": round(int(m.get("weeklyBudget") or 0) / 1e6) or None,
                    "group": CAMPAIGN_GROUP.get(cid, ""), "days": c["days"]})
    return sku_day, grp_day, out


async def _bids() -> dict:
    import ozon_perf_client as pc
    if not pc.configured():
        return {}
    st, d = await pc.api_post("/api/client/campaign/search_promo/v2/products", {"page": 1, "pageSize": 100})
    out = {}
    if st == 200 and isinstance(d, dict):
        for p in d.get("products") or []:
            out[p.get("sourceSku")] = {"bid": round(float(p.get("bid") or 0), 1),
                                       "on": bool(p.get("searchPromoStatus")),
                                       "visibility": p.get("visibilityIndex")}
    return out


async def _positions(since: date, to: date) -> dict:
    """Средняя позиция товара в поиске Ozon за период (Запросы моего товара)."""
    import ozon_client as oz
    try:
        rows = await oz.get_product_queries(str(since), str(to))
    except Exception as e:
        _log.warning("growth positions: %s", e)
        return {}
    return {r.get("offer_id"): {"position": r.get("position"), "searchers": r.get("unique_search_users")}
            for r in rows if r.get("offer_id")}


def _econ(e: dict, qty: float, rev: float, ad: float) -> dict:
    if not rev:
        return {"payout": None, "margin": None}
    pay = rev * (1 - e["c"]) - (e["L"] + e["O"]) * qty - ad
    g = pay - e["cogs"] * qty
    return {"payout": round(pay / rev * 100, 1), "margin": round(g / rev * 100, 1),
            "payout_rub": round(pay), "gross_rub": round(g)}


async def _build() -> dict:
    import ozon_client as oz
    base = await asyncio.to_thread(_kv, _BASE_KEY, None)
    if not base:
        raise RuntimeError("нет точки старта — POST /api/growth/baseline")
    start = date.fromisoformat(base["start"])
    today = _msk_today()
    since = start - timedelta(days=28)                 # 4 недели до старта — для сравнения
    _state["progress"] = "заказы и локализация"
    orders, loc = await _orders(since, today)
    _state["progress"] = "реклама"
    sku_ad, grp_ad, camps = await _ads(since, today)
    _state["progress"] = "цены, ставки, позиции"
    prices = await oz.get_prices()
    bids = await _bids()
    pos_now = await _positions(today - timedelta(days=17), today - timedelta(days=4))  # у Ozon лаг ~3 дня
    E = base["econ"]
    arts = [a for g in GROUPS.values() for a in g]

    # недели: понедельник-воскресенье; неделя старта — та, что содержит start
    def wk(d: date) -> date:
        return d - timedelta(days=d.weekday())
    weeks = []
    w = wk(since)
    while w <= today:
        weeks.append(w)
        w += timedelta(days=7)
    horizon = date.fromisoformat(base["horizon_end"])
    # база оборота — заказы тех же товаров за 14 дней до старта (тот же источник, что и факт),
    # а не реализация месяца: иначе «старт» и «сейчас» несравнимы
    _b0 = start - timedelta(days=14)
    base_week_rev = sum(v[1] for (d, a), v in orders.items()
                        if a in arts and str(_b0) <= d < str(start)) / 2
    if not base_week_rev:
        base_week_rev = base["kpi"]["revenue_month"] / 30 * 7
    n_weeks = max(1, (horizon - start).days / 7)

    def plan_rev(w: date) -> float | None:
        if w < wk(start):
            return None
        k = min(1.0, ((w - wk(start)).days / 7 + 1) / n_weeks)
        return round(base_week_rev * (1 + k * (base["targets"]["revenue_x"] - 1)))

    series = []
    for w in weeks:
        days = [w + timedelta(days=i) for i in range(7)]
        dd = [str(d) for d in days if d <= today]
        rev = qty = ad = pay = gross = 0.0
        for a in arts:
            q = sum(orders.get((d, a), [0, 0])[0] for d in dd)
            r = sum(orders.get((d, a), [0, 0])[1] for d in dd)
            if not q:
                continue
            a_sp = sum(sku_ad.get((d, a), 0) for d in dd)
            e = E.get(a) or E.get(base.get("proxy", {}).get(a, ""), None)
            qty += q; rev += r; ad += a_sp
            if e:
                x = _econ(e, q, r, a_sp)
                pay += x.get("payout_rub", 0); gross += x.get("gross_rub", 0)
        cpc = sum(v for (d, g), v in grp_ad.items() if d in dd)
        pay -= cpc; gross -= cpc; ad += cpc
        lt = [loc.get(d, [0, 0]) for d in dd]
        lsum = sum(x[1] for x in lt)
        series.append({"week": str(w), "days": len(dd), "revenue": round(rev), "orders": int(qty),
                       "plan": plan_rev(w), "ad": round(ad),
                       "drr": round(ad / rev * 100, 1) if rev else None,
                       "payout": round(pay / rev * 100, 1) if rev else None,
                       "margin": round(gross / rev * 100, 1) if rev else None,
                       "local": round(sum(x[0] for x in lt) / lsum * 100) if lsum else None,
                       "is_start": w == wk(start)})

    # по товарам: база = 14 дней до старта, сейчас = последние 7 полных дней
    b0, b1 = start - timedelta(days=14), start - timedelta(days=1)
    n0, n1 = today - timedelta(days=7), today - timedelta(days=1)

    def agg(a, d0, d1):
        q = r = s = 0.0
        d = d0
        while d <= d1:
            o = orders.get((str(d), a), [0, 0]); q += o[0]; r += o[1]; s += sku_ad.get((str(d), a), 0)
            d += timedelta(days=1)
        return q, r, s
    import catalog as _cat
    rows = []
    for g, ga in GROUPS.items():
        for a in ga:
            q0, r0, s0 = agg(a, b0, b1)
            q1, r1, s1 = agg(a, n0, n1)
            pr = prices.get(a, {})
            e = E.get(a)
            x = _econ(e, q1, r1, s1) if e else {}
            od0, od1 = q0 / 14, q1 / 7
            delta = round((od1 / od0 - 1) * 100) if od0 else None
            mp, mn = pr.get("marketing_price") or pr.get("price"), pr.get("min_price")
            bq = (base.get("positions") or {}).get(a)
            flags = []
            if delta is not None and delta <= -30 and q0 >= 5:
                flags.append(f"заказы {delta}% к старту")
            if mp and mn and mp < mn * 0.98:
                flags.append("цена в акции ниже минимальной")
            if x.get("payout") is not None and x["payout"] < 25 and r1:
                flags.append(f"к выводу {x['payout']}%")
            rows.append({
                "art": a, "name": _cat.lookup(a).get("name") or "", "group": g,
                "orders_day_base": round(od0, 2), "orders_day_now": round(od1, 2),
                "payout_rub": x.get("payout_rub", 0), "gross_rub": x.get("gross_rub", 0),
                "delta": delta, "revenue_7d": round(r1), "ad_7d": round(s1),
                "price_base": round(e["P"]) if e else None, "price_now": pr.get("price"),
                "marketing_price": mp, "min_price": mn,
                "actions": [a2["title"] for a2 in pr.get("actions") or []][:4],
                "auto_action": pr.get("auto_action"),
                "bid": (bids.get(a) or {}).get("bid"), "bid_on": (bids.get(a) or {}).get("on"),
                "visibility": (bids.get(a) or {}).get("visibility"),
                "pos_base": bq, "pos_now": (pos_now.get(a) or {}).get("position"),
                **{k: x.get(k) for k in ("payout", "margin")},
                "flags": flags,
            })
    d7s = {str(n0 + timedelta(days=i)) for i in range(7)}
    groups = []
    for g, ga in GROUPS.items():
        rr = [r for r in rows if r["group"] == g]
        rev = sum(r["revenue_7d"] for r in rr)
        cpc = sum(v for (d, gg), v in grp_ad.items() if gg == g and d in d7s)
        ad = sum(r["ad_7d"] for r in rr) + cpc
        pay = sum(r["payout_rub"] or 0 for r in rr) - cpc
        gross = sum(r["gross_rub"] or 0 for r in rr) - cpc
        groups.append({"group": g, "orders_day_base": round(sum(r["orders_day_base"] for r in rr), 1),
                       "orders_day_now": round(sum(r["orders_day_now"] for r in rr), 1),
                       "revenue_7d": rev, "ad_7d": round(ad),
                       "drr": round(ad / rev * 100, 1) if rev else None,
                       "payout": round(pay / rev * 100, 1) if rev else None,
                       "margin": round(gross / rev * 100, 1) if rev else None,
                       "flags": sum(len(r["flags"]) for r in rr)})
    # итог «последние 7 дней» — скользящее окно, а не календарная неделя
    tot = {k: sum(gx[k] or 0 for gx in groups) for k in ("revenue_7d", "ad_7d")}
    pay7 = sum(r["payout_rub"] or 0 for r in rows) - sum(v for (d, _), v in grp_ad.items() if d in d7s)
    gross7 = sum(r["gross_rub"] or 0 for r in rows) - sum(v for (d, _), v in grp_ad.items() if d in d7s)
    lt = [loc.get(d, [0, 0]) for d in d7s]
    ltot = sum(x[1] for x in lt)
    days_in = max(0, (n1 - start).days + 1)
    k_plan = min(1.0, days_in / max(1, (horizon - start).days))
    rev7 = tot["revenue_7d"]
    kpi_now = {"from": str(n0), "to": str(n1),
               "revenue": round(rev7), "revenue_base": round(base_week_rev), "orders": int(sum(r["orders_day_now"] for r in rows) * 7),
               "plan": round(base_week_rev * (1 + k_plan * (base["targets"]["revenue_x"] - 1))),
               "ad": round(tot["ad_7d"]),
               "drr": round(tot["ad_7d"] / rev7 * 100, 1) if rev7 else None,
               "payout": round(pay7 / rev7 * 100, 1) if rev7 else None,
               "margin": round(gross7 / rev7 * 100, 1) if rev7 else None,
               "local": round(sum(x[0] for x in lt) / ltot * 100) if ltot else None,
               "days_since_start": days_in}
    for c in camps:                                           # кампании: последние 7 дней
        d7 = [str(n0 + timedelta(days=i)) for i in range(7)]
        s = sum(c["days"].get(d, [0, 0, 0])[0] for d in d7)
        o = sum(c["days"].get(d, [0, 0, 0])[1] for d in d7)
        m = sum(c["days"].get(d, [0, 0, 0])[2] for d in d7)
        c.update(spent_7d=round(s), orders_7d=o, drr_7d=round(s / m * 100, 1) if m else None)
        c.pop("days", None)
    camps.sort(key=lambda c: -c.get("spent_7d", 0))
    cur = series[-2] if len(series) > 1 and series[-1]["days"] < 4 else series[-1]
    return {"built": (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M"),
            "base": {k: base[k] for k in ("start", "horizon_end", "kpi", "targets", "notes") if k in base},
            "current_week": cur, "kpi_now": kpi_now, "series": series, "groups": groups, "rows": rows,
            "campaigns": camps, "journal": await asyncio.to_thread(_kv, _JOURNAL_KEY, [])}


async def _build_bg():
    _state.update(building=True, error="")
    try:
        data = await _build()
        _state["data"], _state["ts"] = data, _time.monotonic()
        await asyncio.to_thread(_kv_save, _STATE_KEY, data)
    except Exception as e:
        _state["error"] = str(e)[:300]
        _log.warning("growth build: %s", e)
    finally:
        _state.update(building=False, progress="")


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


@router.get("/state")
async def state(refresh: bool = Query(default=False)):
    if not _state["data"]:
        snap = await asyncio.to_thread(_kv, _STATE_KEY, None)
        if snap:
            _state["data"] = snap
    stale = not _state["ts"] or _time.monotonic() - _state["ts"] > _TTL
    if (refresh or stale) and not _state["building"]:
        _spawn(_build_bg())
    if _state["data"]:
        return {**_state["data"], "building": _state["building"], "progress": _state["progress"],
                "error": _state["error"]}
    return {"building": True, "progress": _state["progress"] or "старт", "error": _state["error"]}


@router.post("/baseline")
async def set_baseline(request: Request, payload: dict):
    """Точка старта: {start, horizon_end, kpi, targets, econ: {арт: {P,c,L,O,cogs,q}}, positions, notes}."""
    _owner(request)
    for k in ("start", "horizon_end", "kpi", "targets", "econ"):
        if k not in payload:
            raise HTTPException(400, f"нет поля {k}")
    await asyncio.to_thread(_kv_save, _BASE_KEY, payload)
    _state["ts"] = 0.0
    return {"ok": True}


@router.post("/journal")
async def add_journal(request: Request, payload: dict):
    """Запись в журнал действий: {date?, text, group?}."""
    _owner(request)
    j = await asyncio.to_thread(_kv, _JOURNAL_KEY, [])
    items = payload.get("items") or [payload]
    for it in items:
        if not it.get("text"):
            continue
        j.append({"date": it.get("date") or str(_msk_today()), "text": str(it["text"])[:500],
                  "group": it.get("group") or ""})
    j.sort(key=lambda x: x["date"])
    await asyncio.to_thread(_kv_save, _JOURNAL_KEY, j[-300:])
    if _state.get("data"):
        _state["data"]["journal"] = j[-300:]
    return {"ok": True, "count": len(j)}
