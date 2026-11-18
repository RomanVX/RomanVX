"""Продвижение Ozon по товару — инструмент специалиста по рекламе.

По каждому товару за период (N дней) и предыдущий такой же:
  продажи (аналитика Ozon: заказано шт и выручка) и их динамика;
  реклама: «за клики» (статистика кампаний по SKU) + «оплата за заказ»
           (отчёт orders/generate) — расход, ДРР, ROAS и динамика;
  цены: цена продавца, цена с акциями (marketing_price), минимальная;
  акции: в каких участвует (action_price) и куда может войти (max_action_price
         из кандидатов) — цена для акции и скидка к текущей цене;
  маржа на штуку при текущей цене и при цене акции — по юнит-экономике
        кабинета (снапшот калькулятора маржи Ozon: комиссия, эквайринг,
        логистика, хранение, прочее, себестоимость) + реклама на штуку.
Сборка долгая (отчёты Performance асинхронные, акций и товаров много) —
идёт в фоне, результат в kv `promo_ozon_<days>` на час. Только чтение.
"""
import asyncio
import logging
import time as _time
from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/promo", tags=["promo"])
_TTL = 3600
_MAX_PAGES = 30          # страниц по 100 товаров на акцию (кандидаты) — защита от бесконечности
_state: dict = {}        # days → {building, progress, error, ts}
_bg: set = set()


def _kv(key, default=None):
    import snapshot
    return snapshot.load(key, default)


def _kv_save(key, val):
    import snapshot
    snapshot.save(key, val)


def _msk_today() -> date:
    return (datetime.utcnow() + timedelta(hours=3)).date()


def _f(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


async def _prices() -> tuple[dict, dict]:
    """offer_id → цены; product_id → offer_id."""
    import ozon_client as oz
    out, pid = {}, {}
    cursor = ""
    for _ in range(30):
        body = {"filter": {"visibility": "ALL"}, "limit": 1000}
        if cursor:
            body["cursor"] = cursor
        data = await oz._post("/v5/product/info/prices", body)
        items = data.get("items") or []
        for it in items:
            off = str(it.get("offer_id") or "").strip()
            p = it.get("price") or {}
            out[off] = {"price": round(_f(p.get("price"))), "marketing_price": round(_f(p.get("marketing_price"))),
                        "min_price": round(_f(p.get("min_price"))), "old_price": round(_f(p.get("old_price")))}
            if it.get("product_id"):
                pid[int(it["product_id"])] = off
        cursor = data.get("cursor") or ""
        if not cursor or not items:
            break
    return out, pid


async def _actions(pid2off: dict, prices: dict) -> tuple[dict, list, list]:
    """offer_id → [акции]; плюс список акций. Участвует — action_price;
    кандидат — max_action_price (цена, при которой товар войдёт в акцию)."""
    import ozon_client as oz
    acts = await oz.get_actions()
    today = _msk_today().isoformat()
    live = [a for a in acts if str(a.get("date_end") or "9999")[:10] >= today]
    by_off = defaultdict(list)
    truncated = []
    for a in live:
        aid = a.get("id")
        meta = {"action_id": aid, "title": a.get("title") or "", "type": a.get("action_type") or "",
                "start": str(a.get("date_start") or "")[:10], "end": str(a.get("date_end") or "")[:10]}
        for kind, path in (("участвует", "/v1/actions/products"), ("можно войти", "/v1/actions/candidates")):
            last, pages = "", 0
            while pages < _MAX_PAGES:
                try:
                    data = await oz._post(path, {"action_id": aid, "limit": 100, "last_id": last})
                except Exception as e:
                    _log.warning("promo actions %s %s: %s", aid, path, str(e)[:120])
                    break
                res = data.get("result") or {}
                prods = res.get("products") or []
                for p in prods:
                    off = pid2off.get(int(p.get("id") or 0))
                    if not off:
                        continue
                    price = (prices.get(off) or {}).get("price") or _f(p.get("price"))
                    ap = _f(p.get("action_price")) if kind == "участвует" else _f(p.get("max_action_price"))
                    by_off[off].append({**meta, "status": kind, "action_price": round(ap) if ap else None,
                                        "discount": round((1 - ap / price) * 100, 1) if ap and price else None})
                pages += 1
                last = res.get("last_id") or ""
                if not last or len(prods) < 100:
                    break
                await asyncio.sleep(0.3)
            if pages >= _MAX_PAGES:
                truncated.append(f"{meta['title']} ({kind})")
    for off in by_off:   # ближайшая: сначала текущие, потом по дате старта
        by_off[off].sort(key=lambda x: (x["start"] > today, x["start"], x["status"] != "участвует"))
    return by_off, [{"id": a.get("id"), "title": a.get("title"), "start": str(a.get("date_start"))[:10],
                     "end": str(a.get("date_end"))[:10]} for a in live], truncated


async def _sales(d0: date, d1: date, sku2off: dict, names: dict) -> dict:
    import ozon_client as oz
    rows = await oz.get_analytics_data(str(d0), str(d1), ["revenue", "ordered_units"], limit=1000)
    out = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        off = sku2off.get(str(r.get("sku")))
        if off:
            if r.get("name"):
                names.setdefault(off, r["name"])
            m = r.get("metrics") or {}
            out[off][0] += _f(m.get("ordered_units")); out[off][1] += _f(m.get("revenue"))
    return out


async def _ads(d0: date, d1: date, sku2off: dict) -> dict:
    """offer_id → {cpc, cpo, views, clicks, ad_orders, ad_revenue}."""
    import ozon_perf_client as pc
    out = defaultdict(lambda: {"cpc": 0.0, "cpo": 0.0, "views": 0, "clicks": 0, "ad_orders": 0, "ad_revenue": 0.0})
    if not pc.configured():
        return out
    daily = await pc.get_daily(str(d0), str(d1))
    meta = {str(c.get("id")): c for c in await pc.get_campaigns()}
    ids = sorted({r["id"] for r in daily if r["spent"] > 0
                  and (meta.get(r["id"]) or {}).get("advObjectType") == "SKU"})
    if ids:
        st = await pc.sku_stats(ids, str(d0), str(d1))
        for rows in st.values():
            for r in rows:
                off = sku2off.get(str(r["sku"]))
                if not off:
                    continue
                o = out[off]
                o["cpc"] += r["spent"]; o["views"] += r["views"]; o["clicks"] += r["clicks"]
                o["ad_orders"] += r["orders"]; o["ad_revenue"] += r["orders_money"]
    for r in await pc.orders_report(str(d0), str(d1)):
        off = sku2off.get(str(r.get("sku")).strip()) or (r.get("art") or "").strip()
        if off:
            o = out[off]
            o["cpo"] += r.get("spent") or 0; o["ad_orders"] += r.get("qty") or 0; o["ad_revenue"] += r.get("revenue") or 0
    return out


def _margin(price, econ, ad_unit):
    if not price or not econ or not econ.get("cogs"):
        return None
    m = (price * (1 - (econ.get("comm_pct") or 0) / 100 - (econ.get("acq_pct") or 0) / 100)
         - (econ.get("logist") or 0) - (econ.get("storage") or 0) - (econ.get("other") or 0)
         - ad_unit - econ["cogs"])
    return {"rub": round(m), "pct": round(m / price * 100, 1)}


def _dyn(a, b):
    return round((a / b - 1) * 100) if b else None


async def _build(days: int) -> dict:
    import ozon_client as oz
    st = _state[days]
    d1 = _msk_today() - timedelta(days=1)
    d0 = d1 - timedelta(days=days - 1)
    p1, p0 = d0 - timedelta(days=1), d0 - timedelta(days=days)
    st["progress"] = "товары и цены"
    skus = await oz._get_all_skus()
    sku2off = {str(s["sku"]): s["offer_id"] for s in skus if s.get("sku")}
    names: dict = {}
    prices, pid2off = await _prices()
    st["progress"] = "продажи"
    cur, prev = await _sales(d0, d1, sku2off, names), await _sales(p0, p1, sku2off, names)
    st["progress"] = "реклама (отчёты Ozon Performance)"
    warn = []
    try:
        ad_cur, ad_prev = await _ads(d0, d1, sku2off), await _ads(p0, p1, sku2off)
    except Exception as e:
        _log.warning("promo ads: %s", e)
        ad_cur, ad_prev, _ = {}, {}, warn.append("реклама: " + str(e)[:100])
    st["progress"] = "акции"
    try:
        acts, act_list, truncated = await _actions(pid2off, prices)
    except Exception as e:
        _log.warning("promo actions: %s", e)
        acts, act_list, truncated, _ = {}, [], [], warn.append("акции: " + str(e)[:100])
    st["progress"] = "юнит-экономика"
    snap = await asyncio.to_thread(_kv, "margin_snap_OZON", None)
    if not (snap or {}).get("items"):
        try:     # калькулятор маржи ещё не открывали — собираем юнитку Ozon
            from routers import tools as _tools
            snap = await asyncio.wait_for(_tools.get_margin(mp="OZON"), 600)
        except Exception as e:
            _log.warning("promo: юнитка Ozon недоступна: %s", str(e)[:120])
            snap = {}
    import catalog as _cat
    econ = {str(it.get("sku") or "").strip().upper(): it for it in ((snap or {}).get("items") or [])}
    rows = []
    for off in set(cur) | set(ad_cur) | set(acts) | set(prev):
        q, rev = cur.get(off, [0, 0]); q0, rev0 = prev.get(off, [0, 0])
        a, a0 = ad_cur.get(off) or {}, ad_prev.get(off) or {}
        spend, spend0 = (a.get("cpc", 0) + a.get("cpo", 0)), (a0.get("cpc", 0) + a0.get("cpo", 0))
        if not (q or rev or spend or q0 or acts.get(off)):
            continue
        pr = prices.get(off) or {}
        e = econ.get(off.upper()) or econ.get(_cat.canon(off).upper())
        ad_unit = spend / q if q else 0
        al = [{**x, "margin": _margin(x.get("action_price"), e, ad_unit)} for x in (acts.get(off) or [])[:20]]
        near = al[0] if al else None
        info = _cat.lookup(off)
        rows.append({
            "offer": off, "name": (names.get(off) if info.get("brand") == "Прочее" else info.get("name")) or names.get(off) or off,
            "group": info.get("group") or "",
            "qty": round(q), "qty_prev": round(q0), "qty_dyn": _dyn(q, q0),
            "rev": round(rev), "rev_prev": round(rev0), "rev_dyn": _dyn(rev, rev0),
            "ad": round(spend), "ad_prev": round(spend0), "ad_dyn": _dyn(spend, spend0),
            "ad_cpc": round(a.get("cpc", 0)), "ad_cpo": round(a.get("cpo", 0)),
            "views": a.get("views", 0), "clicks": a.get("clicks", 0),
            "ad_orders": a.get("ad_orders", 0), "ad_rev": round(a.get("ad_revenue", 0)),
            "drr": round(spend / rev * 100, 1) if rev else None,
            "drr_prev": round(spend0 / rev0 * 100, 1) if rev0 else None,
            "roas": round(a.get("ad_revenue", 0) / spend, 2) if spend else None,
            "price": pr.get("price"), "buyer_price": pr.get("marketing_price"), "min_price": pr.get("min_price"),
            "actions": al, "actions_total": len(acts.get(off) or []),
            "promo_price": (near or {}).get("action_price"), "promo_discount": (near or {}).get("discount"),
            "margin": _margin(pr.get("price"), e, ad_unit),
            "margin_promo": (near or {}).get("margin"),
            "has_cost": bool(e and e.get("cogs")),
        })
    rows.sort(key=lambda r: (-(r["rev"] or 0), -(r["ad"] or 0)))
    tot = lambda k: sum(r[k] or 0 for r in rows)
    T = {k: tot(k) for k in ("qty", "qty_prev", "rev", "rev_prev", "ad", "ad_prev", "ad_rev")}
    T["drr"] = round(T["ad"] / T["rev"] * 100, 1) if T["rev"] else None
    T["drr_prev"] = round(T["ad_prev"] / T["rev_prev"] * 100, 1) if T["rev_prev"] else None
    T["roas"] = round(T["ad_rev"] / T["ad"], 2) if T["ad"] else None
    return {"days": days, "period": [str(d0), str(d1)], "prev": [str(p0), str(p1)],
            "rows": rows, "totals": T, "actions": act_list, "truncated": truncated,
            "has_costs": any(r["has_cost"] for r in rows), "warnings": warn,
            "built": (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")}


async def _run(days: int):
    st = _state[days]
    try:
        data = await _build(days)
        await asyncio.to_thread(_kv_save, f"promo_ozon_{days}", data)
        st.update(error="", ts=_time.time())
    except Exception as e:
        _log.exception("promo build")
        st["error"] = str(e)[:300]
    finally:
        st.update(building=False, progress="")


@router.get("/ozon")
async def promo_ozon(days: int = Query(default=7, ge=3, le=30), refresh: bool = False):
    st = _state.setdefault(days, {"building": False, "progress": "", "error": "", "ts": 0.0})
    data = await asyncio.to_thread(_kv, f"promo_ozon_{days}", None)
    fresh = data and st["ts"] and _time.time() - st["ts"] < _TTL
    if (refresh or not fresh) and not st["building"]:
        st.update(building=True, progress="старт", error="")
        t = asyncio.create_task(_run(days)); _bg.add(t); t.add_done_callback(_bg.discard)
    return {**(data or {}), "building": st["building"], "progress": st["progress"], "error": st["error"]}
