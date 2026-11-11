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
_TTL = 900                     # 15 мин: мониторинг «внутри дня»
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

def _msk(ts: str) -> tuple[str, int]:
    """ISO-время Ozon (UTC) → (день МСК, час МСК)."""
    try:
        t = datetime.fromisoformat((ts or "").replace("Z", "+00:00").split(".")[0].replace("+00:00", ""))
    except ValueError:
        return (ts or "")[:10], 0
    t += timedelta(hours=3)
    return t.strftime("%Y-%m-%d"), t.hour


async def _orders(since: date, to: date) -> tuple[dict, dict, dict]:
    """{(день МСК, артикул): [шт, выручка]}, {день: [локальных, всего]} (FBO)
    и почасовая раскладка {(день МСК, час, артикул): [шт, выручка]} — для «внутри дня»."""
    import ozon_client as oz
    by = defaultdict(lambda: [0, 0.0])
    loc = defaultdict(lambda: [0, 0])
    hr = defaultdict(lambda: [0, 0.0])
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
            day, hour = _msk(p.get("created_at") or p.get("in_process_at") or "")
            f = p.get("financial_data") or {}
            if f.get("cluster_from") and f.get("cluster_to"):
                loc[day][1] += 1
                loc[day][0] += 1 if f["cluster_from"] == f["cluster_to"] else 0
            for pr in p.get("products") or []:
                x = by[(day, pr.get("offer_id"))]; h = hr[(day, hour, pr.get("offer_id"))]
                q = int(pr.get("quantity") or 0); m = oz._price_amount(pr.get("price")) * q
                x[0] += q; x[1] += m; h[0] += q; h[1] += m
        cursor = d.get("cursor") or ""
        if not d.get("has_next") or not cursor or not ps:
            break
    try:
        for p in await oz.fbs_postings_v4(s, t):
            if p.get("status") == "cancelled":
                continue
            day, hour = _msk(p.get("in_process_at") or p.get("created_at") or "")
            for pr in p.get("products") or []:
                x = by[(day, pr.get("offer_id"))]; h = hr[(day, hour, pr.get("offer_id"))]
                q = int(pr.get("quantity") or 0); m = oz._price_amount(pr.get("price")) * q
                x[0] += q; x[1] += m; h[0] += q; h[1] += m
    except Exception as e:
        _log.warning("growth fbs: %s", e)
    return by, loc, hr


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
    # база сравнения — ПРЕДЫДУЩИЙ МЕСЯЦ целиком (месяц к месяцу), дни — только с даты старта
    pm_end = date(start.year, start.month, 1) - timedelta(days=1)
    pm_start = pm_end.replace(day=1)
    m0 = date(today.year, today.month, 1)
    since = min(pm_start, m0)
    _state["progress"] = "заказы и локализация"
    orders, loc, hourly = await _orders(since, today)
    _state["progress"] = "реклама"
    sku_ad, grp_ad, camps = await _ads(since, today)
    _state["progress"] = "цены, ставки, позиции"
    prices = await oz.get_prices()
    bids = await _bids()
    pos_now = await _positions(today - timedelta(days=17), today - timedelta(days=4))  # у Ozon лаг ~3 дня
    E = base["econ"]
    arts = [a for g in GROUPS.values() for a in g]
    horizon = date.fromisoformat(base["horizon_end"])
    yday = today - timedelta(days=1)
    # «после старта»: полные дни start..вчера; если полных ещё нет — сегодняшний неполный
    aft0, aft1 = start, (yday if yday >= start else today)
    after_partial = aft1 == today

    def period(d0, d1, groups_=None):
        gs = groups_ or list(GROUPS)
        arts_ = [a for g in gs for a in GROUPS[g]]
        q = r = ad = pay = gross = 0.0; days = 0; lsum = [0, 0]
        d = d0
        while d <= d1:
            ds = str(d); days += 1
            for a in arts_:
                qq, rr = orders.get((ds, a), [0, 0]); sp = sku_ad.get((ds, a), 0); ad += sp
                if not qq:
                    pay -= sp; gross -= sp
                    continue
                q += qq; r += rr
                e = E.get(a) or E.get(base.get("proxy", {}).get(a, ""), None)
                if e:
                    x = _econ(e, qq, rr, sp); pay += x.get("payout_rub", 0); gross += x.get("gross_rub", 0)
            cpc = sum(grp_ad.get((ds, g), 0) for g in gs); ad += cpc; pay -= cpc; gross -= cpc
            if groups_ is None:
                lc = loc.get(ds, [0, 0]); lsum[0] += lc[0]; lsum[1] += lc[1]
            d += timedelta(days=1)
        pc = lambda v: round(v / r * 100, 1) if r else None
        return {"from": str(d0), "to": str(d1), "days": days, "orders": int(q), "revenue": round(r), "ad": round(ad),
                "orders_day": round(q / days, 1) if days else 0, "revenue_day": round(r / days) if days else 0,
                "drr": pc(ad), "payout": pc(pay), "margin": pc(gross),
                "local": round(lsum[0] / lsum[1] * 100) if lsum[1] else None}

    oct_ = period(pm_start, pm_end)
    pre = period(m0, start - timedelta(days=1)) if start > m0 else None
    after = period(aft0, aft1); after["partial"] = after_partial
    mtd = period(m0, yday) if yday >= m0 else None
    import calendar as _cal
    mdays = _cal.monthrange(today.year, today.month)[1]
    left = (date(today.year, today.month, mdays) - today).days + 1          # сегодня + оставшиеся дни
    forecast = round((mtd["revenue"] if mtd else 0) + after["revenue_day"] * left)
    base_day_rev = oct_["revenue_day"] or base["kpi"]["revenue_month"] / 30
    total_days = max(1, (horizon - start).days)
    xr = base["targets"]["revenue_x"]
    plan_of = lambda d: round(base_day_rev * (1 + min(1.0, ((d - start).days + 1) / total_days) * (xr - 1)))
    mom = {"prev": oct_, "pre": pre, "after": after, "mtd": mtd, "forecast_month": forecast,
           "prev_month_label": pm_start.strftime("%m.%Y"), "month_days": mdays,
           "groups": [{"group": g, "prev": period(pm_start, pm_end, [g])["orders_day"],
                       "pre": period(m0, start - timedelta(days=1), [g])["orders_day"] if pre else None,
                       "after": period(aft0, aft1, [g])["orders_day"]} for g in GROUPS]}

    # по товарам: октябрь в день → после старта в день
    def agg(a, d0, d1):
        q = r = s_ = 0.0
        d = d0
        while d <= d1:
            o = orders.get((str(d), a), [0, 0]); q += o[0]; r += o[1]; s_ += sku_ad.get((str(d), a), 0)
            d += timedelta(days=1)
        return q, r, s_
    n_prev, n_aft = (pm_end - pm_start).days + 1, (aft1 - aft0).days + 1
    import catalog as _cat
    rows = []
    for g, ga in GROUPS.items():
        for a in ga:
            q0, r0, s0 = agg(a, pm_start, pm_end)
            q1, r1, s1 = agg(a, aft0, aft1)
            pr = prices.get(a, {})
            e = E.get(a)
            x = _econ(e, q1, r1, s1) if e else {}
            od0, od1 = q0 / n_prev, q1 / n_aft
            delta = round((od1 / od0 - 1) * 100) if od0 else None
            mp, mn = pr.get("marketing_price") or pr.get("price"), pr.get("min_price")
            bq = (base.get("positions") or {}).get(a)
            flags = []
            if delta is not None and delta <= -30 and q0 >= 10:
                flags.append(f"заказы {delta}% к октябрю")
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
    win = {str(aft0 + timedelta(days=i)) for i in range(n_aft)}
    groups = []
    for g, ga in GROUPS.items():
        rr = [r for r in rows if r["group"] == g]
        rev = sum(r["revenue_7d"] for r in rr)
        cpc = sum(v for (d, gg), v in grp_ad.items() if gg == g and d in win)
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
    days_in = max(0, (today - start).days + 1)
    kpi_now = {**after, "revenue_base": oct_["revenue_day"],
               "plan": round(sum(plan_of(aft0 + timedelta(days=i)) for i in range(n_aft)) / n_aft),
               "days_since_start": days_in, "window": "after_start"}
    for c in camps:                                           # кампании: после старта
        s_ = sum(c["days"].get(d, [0, 0, 0])[0] for d in win)
        o = sum(c["days"].get(d, [0, 0, 0])[1] for d in win)
        m = sum(c["days"].get(d, [0, 0, 0])[2] for d in win)
        c.update(spent_7d=round(s_), orders_7d=o, drr_7d=round(s_ / m * 100, 1) if m else None)
        c.pop("days", None)
    camps.sort(key=lambda c: -c.get("spent_7d", 0))

    # ── дни с даты старта ──
    daily = []
    d = start
    while d <= today:
        x = period(d, d); x.update(date=str(d), plan=plan_of(d), partial=d == today, after_start=True)
        daily.append(x)
        d += timedelta(days=1)

    # ── внутри дня: сегодня / вчера / обычный день прошлого месяца (медиана по часам) ──
    now_msk = datetime.utcnow() + timedelta(hours=3)
    now_h = now_msk.hour
    tday, yd = str(today), str(yday)
    base_days = [str(pm_start + timedelta(days=i)) for i in range(n_prev)]
    import statistics as _st
    def cum(day, key, arts_=None):
        acc, out = 0.0, []
        for h in range(24):
            acc += sum(hourly.get((day, h, a), [0, 0])[key] for a in (arts_ or arts))
            out.append(acc)
        return out
    def curve(days, key=0):
        cs = [cum(dd_, key) for dd_ in days]
        return [round(_st.median(c[h] for c in cs), 1 if key == 0 else 0) for h in range(24)]
    cq_t, cq_y, cq_b = curve([tday]), curve([yd]), curve(base_days)
    cr_t, cr_y, cr_b = curve([tday], 1), curve([yd], 1), curve(base_days, 1)
    def grp_to_hour(days, hour=None):
        hour = now_h if hour is None else hour
        return {g: _st.median(cum(dd_, 0, ga)[hour] for dd_ in days) for g, ga in GROUPS.items()}
    gt, gy, gb = grp_to_hour([tday]), grp_to_hour([yd]), grp_to_hour(base_days)
    gfull = grp_to_hour(base_days, 23)
    intraday = {"now": now_msk.strftime("%H:%M"), "hour": now_h, "today": tday, "yesterday": yd,
                "base_from": base_days[0], "base_to": base_days[-1], "base_label": "обычный день " + pm_start.strftime("%m.%Y"),
                "orders": {"today": cq_t, "yesterday": cq_y, "base": cq_b},
                "revenue": {"today": cr_t, "yesterday": cr_y, "base": cr_b},
                "to_hour": {"orders": [cq_t[now_h], cq_y[now_h], cq_b[now_h]],
                            "revenue": [cr_t[now_h], cr_y[now_h], cr_b[now_h]]},
                "groups": [{"group": g, "today": round(gt[g]), "yesterday": round(gy[g]), "base": round(gb[g], 1),
                            "base_full_day": round(gfull[g], 1)} for g in GROUPS],
                "plan_day": plan_of(today) if today >= start else None}
    return {"built": (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M"),
            "base": {k: base[k] for k in ("start", "horizon_end", "kpi", "targets", "notes") if k in base},
            "current_week": None, "kpi_now": kpi_now, "series": [], "daily": daily, "intraday": intraday, "mom": mom,
            "groups": groups, "rows": rows,
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
    cur = await asyncio.to_thread(_kv, _BASE_KEY, None) or {}
    payload = {**cur, **payload}                       # можно прислать только изменённые поля
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
