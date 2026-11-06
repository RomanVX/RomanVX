"""Календарь акций Ozon и WB с рекомендациями по каждому товару.

Ozon:  GET /v1/actions — акции, доступные кабинету (даты, автодобавление,
       дата заморозки); POST /v1/actions/candidates — товары-кандидаты
       (текущая цена, максимальная цена по акции); POST /v1/actions/products —
       уже участвующие. Экономика — /v5/product/info/prices: комиссия FBO,
       логистика, эквайринг, себестоимость (net_price) и флаги автоприменения/
       автодобавления в акции.
WB:    dp-calendar-api /api/v1/calendar/promotions (+details, +nomenclatures):
       плановая цена и рекомендуемая скидка по каждому артикулу.
Остатки и скорость продаж — из тех же источников, что «Склад».

Рекомендация считается правилами (а не ИИ), причина пишется словами:
  нет остатка → не участвовать;  убыток по акционной цене → не участвовать;
  залежи (запас > 90 дн. или нет продаж) → участвовать, распродать;
  мало запаса (< 14 дн.) → не нужно, продастся и так;
  скидка ≤ 10% → участвовать;  10–25% → можно, если цель — оборот;
  > 25% → только для залежей.
"""
import asyncio
import logging
import time as _time
from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import Response

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/promo", tags=["promo"])

_state: dict = {"building": False, "progress": "", "error": "", "data": None, "ts": 0.0}
_TTL = 3 * 3600
_bg: set = set()

REC = {"yes": "Участвовать", "maybe": "Можно", "no": "Не участвовать", "clear": "Распродать залежи"}


def _plain(html: str) -> str:
    """Описания акций Ozon приходят с HTML-тегами — оставляем текст."""
    import re as _re
    t = _re.sub(r"<br\s*/?>|</p>|</li>", " · ", html or "")
    t = _re.sub(r"<[^>]+>", "", t)
    return _re.sub(r"\s*·\s*(·\s*)+", " · ", _re.sub(r"[ \t]+", " ", t)).strip(" ·")


def _num(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0.0


def _recommend(price, action_price, stock, per_day, cost, payout) -> tuple[str, str, dict]:
    """Возвращает (код, причина, экономика)."""
    disc = round((1 - action_price / price) * 100, 1) if price and action_price else None
    days = round(stock / per_day) if per_day else (None if not stock else 999)
    profit = round(payout - cost) if cost and payout is not None else None
    margin = round(profit / action_price * 100, 1) if profit is not None and action_price else None
    eco = {"discount": disc, "stock_days": days, "profit": profit, "margin": margin}
    if not stock:
        return "no", "нет остатка", eco
    if profit is not None and profit < 0:
        return "no", f"убыток {abs(profit)} ₽ со штуки по акционной цене", eco
    slow = (days is not None and days > 90) or (not per_day and stock)
    if slow:
        return "clear", ("нет продаж за 28 дн." if not per_day else f"запас на {days} дн.") + \
            " — акция поможет распродать", eco
    if days is not None and days < 14:
        return "no", f"запаса на {days} дн. — продастся и без скидки", eco
    if disc is None:
        return "maybe", "нет данных о цене акции", eco
    if disc <= 10:
        return "yes", f"скидка {disc}% — недорого за продвижение", eco
    if disc <= 25:
        tail = f", маржа {margin}%" if margin is not None else ""
        return "maybe", f"скидка {disc}%{tail} — если цель оборот и место в выдаче", eco
    return "no", f"скидка {disc}% — слишком глубоко для ходового товара", eco


# ── Ozon ────────────────────────────────────────────────────────────────────

async def _oz_prices() -> dict:
    """offer_id → цена, комиссия, логистика, себестоимость, флаги автоакций."""
    import ozon_client as oz
    out, cursor = {}, ""
    for _ in range(20):
        body = {"filter": {"visibility": "ALL"}, "limit": 1000}
        if cursor:
            body["cursor"] = cursor
        data = await oz._post("/v5/product/info/prices", body)
        items = data.get("items") or []
        for it in items:
            p, c = it.get("price") or {}, it.get("commissions") or {}
            logi = _num(c.get("fbo_deliv_to_customer_amount")) + \
                (_num(c.get("fbo_direct_flow_trans_min_amount")) + _num(c.get("fbo_direct_flow_trans_max_amount"))) / 2
            out[str(it.get("offer_id") or "").strip()] = {
                "product_id": str(it.get("product_id") or ""),
                "price": _num(p.get("price")), "min_price": _num(p.get("min_price")),
                "net_price": _num(p.get("net_price")),
                "commission_pct": _num(c.get("sales_percent_fbo")), "logistics": round(logi),
                "acquiring": _num(it.get("acquiring")),
                "auto_action": bool(p.get("auto_action_enabled")),
                "auto_add": bool(p.get("auto_add_to_ozon_actions_list_enabled")),
            }
        cursor = data.get("cursor") or ""
        if not cursor or not items:
            break
    return out


async def _oz_stocks_all() -> dict:
    """offer_id → свободный остаток на всех складах (FBO + FBS) — /v4/product/info/stocks."""
    import ozon_client as oz
    out, cursor = {}, ""
    for _ in range(50):
        body = {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor}
        data = await oz._post("/v4/product/info/stocks", body)
        items = data.get("items") or []
        for it in items:
            free = sum(max(int(x.get("present") or 0) - int(x.get("reserved") or 0), 0)
                       for x in it.get("stocks") or [])
            out[str(it.get("offer_id") or "").strip()] = free
        cursor = data.get("cursor") or ""
        if not cursor or not items:
            break
    return out


async def _oz_paged(path: str, action_id) -> list[dict]:
    import ozon_client as oz
    out, last = [], None
    for _ in range(200):
        body = {"action_id": action_id, "limit": 100}
        if last:
            body["last_id"] = last
        data = await oz._post(path, body)
        res = data.get("result") or {}
        prods = res.get("products") or []
        out += prods
        last = res.get("last_id")
        if not prods or len(prods) < 100 or not last or str(last) == "0":
            break
        await asyncio.sleep(0.2)
    return out


_OZ_TYPE_RU = {"STOCK_DISCOUNT": "Скидка на сток / бустинг", "ELASTIC_BOOSTING": "Эластичный бустинг",
               "DISCOUNT": "Скидка", "DISCOUNT_WITH_CONDITION": "Скидка с условием",
               "MARKETPLACE_MULTI_LEVEL_DISCOUNT_ON_AMOUNT": "Многоуровневая скидка от суммы",
               "VOUCHER": "Промокод", "INSTALLMENT": "Рассрочка", "BUNDLE": "Комплект"}


async def _build_ozon() -> list[dict]:
    import ozon_client as oz
    import catalog as _cat
    import cost_store
    actions = await oz.get_actions()
    if not actions:
        return []
    _state["progress"] = "Ozon: цены, остатки, продажи…"
    prices = await _oz_prices()
    by_pid = {v["product_id"]: k for k, v in prices.items() if v["product_id"]}
    try:
        stocks = await _oz_stocks_all()          # FBO + FBS — у многих селлеров часть товара на своём складе
    except Exception as e:
        _log.warning("promo ozon stocks v4: %s", e)
        stocks = await oz.get_stocks()
    sales = await oz.get_sales_28d()
    costs = cost_store.get_costs()
    out = []
    for n, a in enumerate(actions, 1):
        _state["progress"] = f"Ozon: акция {n} из {len(actions)} — «{a.get('title', '')[:40]}»"
        aid = a.get("id")
        try:
            cand = await _oz_paged("/v1/actions/candidates", aid)
            part = await _oz_paged("/v1/actions/products", aid)
        except Exception as e:
            _log.warning("promo ozon %s: %s", aid, e)
            cand, part = [], []
        in_action = {str(p.get("id")): p for p in part}
        items = []
        for p in list(cand) + [p for p in part if str(p.get("id")) not in {str(c.get("id")) for c in cand}]:
            pid = str(p.get("id"))
            art = by_pid.get(pid) or _cat.resolve_ozon(pid)
            pr = prices.get(art, {})
            price = _num(p.get("price")) or pr.get("price", 0)
            ap = _num(p.get("action_price")) or _num(p.get("max_action_price"))
            stock = stocks.get(art, 0)
            per_day = sales.get(art, 0)
            payout = None
            if ap and pr:
                payout = ap * (1 - pr["commission_pct"] / 100) - pr["logistics"] - pr["acquiring"]
            cost = costs.get(art) or pr.get("net_price") or 0
            code, why, eco = _recommend(price, ap, stock, per_day, cost, payout)
            info = _cat.lookup(art)
            items.append({
                "art": art, "name": info.get("name", art)[:100], "brand": info.get("brand", ""),
                "price": round(price), "action_price": round(ap) if ap else None,
                "rec_price": round(_num(p.get("alert_max_action_price"))) or None,
                "in_action": pid in in_action, "add_mode": p.get("add_mode") or "",
                "stock": stock, "per_day": per_day, "cost": round(cost) if cost else None,
                "payout": round(payout) if payout is not None else None,
                "auto_add": pr.get("auto_add"), "auto_action": pr.get("auto_action"),
                "commission_pct": pr.get("commission_pct"),
                "rec": code, "rec_text": REC[code], "why": why, **eco,
            })
        counts = {k: sum(1 for i in items if i["rec"] == k) for k in REC}
        out.append({
            "mp": "OZON", "id": str(aid), "title": a.get("title") or "",
            "type": _OZ_TYPE_RU.get(a.get("action_type") or "", a.get("action_type") or ""),
            "description": _plain(a.get("description") or "")[:700],
            "start": (a.get("date_start") or "")[:10], "end": (a.get("date_end") or "")[:10],
            "freeze": (a.get("freeze_date") or "")[:10],
            "auto_add_dates": [d[:16].replace("T", " ") for d in (a.get("auto_add_dates") or [])],
            "candidates": int(a.get("potential_products_count") or len(cand)),
            "participating": int(a.get("participating_products_count") or len(part)),
            "counts": counts, "items": items,
        })
        await asyncio.sleep(0.3)
    return out


# ── WB ──────────────────────────────────────────────────────────────────────

_WB_CAL = "https://dp-calendar-api.wildberries.ru"


async def _wb_get(path: str, params) -> dict:
    import wb_client as wb
    for attempt in range(5):
        r = await wb._http().get(_WB_CAL + path, headers=wb._headers(), params=params)
        if r.status_code == 429:
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if not r.is_success:
            raise RuntimeError(f"WB календарь {r.status_code}: {r.text[:150]}")
        return r.json() or {}
    raise RuntimeError("WB календарь: 429")


async def _build_wb() -> list[dict]:
    from config import CABINET_MARKETPLACES, USE_MOCK
    if "WB" not in CABINET_MARKETPLACES or USE_MOCK:
        return []
    import wb_client as wb
    import catalog as _cat
    import cost_store
    now = datetime.utcnow()
    data = await _wb_get("/api/v1/calendar/promotions", {
        "startDateTime": now.strftime("%Y-%m-%dT00:00:00Z"),
        "endDateTime": (now + timedelta(days=60)).strftime("%Y-%m-%dT23:59:59Z"),
        "allPromo": "false", "limit": 1000, "offset": 0})
    promos = (data.get("data") or {}).get("promotions") or []
    if not promos:
        return []
    details = {}
    ids = [p["id"] for p in promos]
    for i in range(0, len(ids), 100):
        d = await _wb_get("/api/v1/calendar/promotions/details",
                          [("promotionIDs", x) for x in ids[i:i + 100]])
        for p in (d.get("data") or {}).get("promotions") or []:
            details[p.get("id")] = p
    _state["progress"] = "WB: остатки и продажи…"
    stock: dict = {}
    try:
        for r in await wb.get_stocks():
            nm = str(r.get("nmId") or "")
            stock[nm] = stock.get(nm, 0) + int(r.get("quantity") or 0)
    except Exception as e:
        _log.warning("promo wb stocks: %s", e)
    per_day: dict = {}
    try:
        for r in await wb.get_orders(now - timedelta(days=28), now):
            if r.get("isCancel"):
                continue
            nm = str(r.get("nmId") or "")
            per_day[nm] = per_day.get(nm, 0) + 1 / 28
    except Exception as e:
        _log.warning("promo wb orders: %s", e)
    costs = cost_store.get_costs()
    out = []
    for n, p in enumerate(promos, 1):
        _state["progress"] = f"WB: акция {n} из {len(promos)} — «{p.get('name', '')[:40]}»"
        det = details.get(p["id"], {})
        items = []
        if p.get("type") != "auto":
            for in_action in ("true", "false"):
                off = 0
                for _ in range(50):
                    d = await _wb_get("/api/v1/calendar/promotions/nomenclatures", {
                        "promotionID": p["id"], "inAction": in_action, "limit": 1000, "offset": off})
                    noms = (d.get("data") or {}).get("nomenclatures") or []
                    for x in noms:
                        nm = str(x.get("id"))
                        art = _cat.resolve_wb(nm)
                        price = _num(x.get("price"))
                        plan = _num(x.get("planPrice"))
                        cur = price * (1 - _num(x.get("discount")) / 100) if price else 0
                        code, why, eco = _recommend(cur or price, plan, stock.get(nm, 0),
                                                    round(per_day.get(nm, 0), 2), costs.get(art) or 0, None)
                        info = _cat.lookup(art)
                        items.append({"art": art, "name": info.get("name", art)[:100], "brand": info.get("brand", ""),
                                      "price": round(cur or price), "action_price": round(plan) if plan else None,
                                      "rec_price": None, "in_action": in_action == "true", "add_mode": "",
                                      "stock": stock.get(nm, 0), "per_day": round(per_day.get(nm, 0), 2),
                                      "cost": costs.get(art), "payout": None, "auto_add": None, "auto_action": None,
                                      "plan_discount": x.get("planDiscount"),
                                      "rec": code, "rec_text": REC[code], "why": why, **eco})
                    if len(noms) < 1000:
                        break
                    off += 1000
                    await asyncio.sleep(0.7)
                await asyncio.sleep(0.7)
        counts = {k: sum(1 for i in items if i["rec"] == k) for k in REC}
        out.append({
            "mp": "WB", "id": str(p["id"]), "title": p.get("name") or "",
            "type": "Автоакция" if p.get("type") == "auto" else "Акция",
            "description": _plain(det.get("description") or "")[:700],
            "advantages": det.get("advantages") or [],
            "start": (p.get("startDateTime") or "")[:10], "end": (p.get("endDateTime") or "")[:10],
            "freeze": "", "auto_add_dates": [],
            "candidates": det.get("inPromoActionTotal", 0) + det.get("notInPromoActionTotal", 0) if det else len(items),
            "participating": det.get("inPromoActionTotal", sum(1 for i in items if i["in_action"])) if det else 0,
            "counts": counts, "items": items,
            "note": "По автоакциям WB список товаров API не отдаёт — смотрите в кабинете WB, "
                    "в «Календаре акций»." if p.get("type") == "auto" else "",
        })
    return out


async def _build():
    _state.update(building=True, error="", progress="Собираю акции…")
    try:
        actions, errors = [], []
        for name, fn in (("Ozon", _build_ozon), ("WB", _build_wb)):
            try:
                actions += await fn()
            except Exception as e:
                errors.append(f"{name}: {str(e)[:200]}")
                _log.warning("promo %s: %s", name, e)
        actions.sort(key=lambda a: (a["start"] or "9999", a["end"] or ""))
        _state["data"] = {"actions": actions, "errors": errors,
                          "built": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")}
        _state["ts"] = _time.time()
        _state["error"] = "; ".join(errors)
        import snapshot
        await asyncio.to_thread(snapshot.save, "promo_calendar", _state["data"])
    except Exception as e:
        _state["error"] = str(e)[:300]
    finally:
        _state.update(building=False, progress="")


def _ensure(refresh: bool):
    if _state["data"] is None:
        import snapshot
        snap = snapshot.load("promo_calendar", None)
        if snap:
            _state["data"] = snap
            _state["ts"] = _time.time() - _TTL + 600
    stale = refresh or _state["data"] is None or _time.time() - _state["ts"] > _TTL
    if stale and not _state["building"]:
        t = asyncio.get_event_loop().create_task(_build())
        _bg.add(t)
        t.add_done_callback(_bg.discard)


@router.get("/calendar")
async def calendar(refresh: bool = Query(default=False)):
    """Акции обеих площадок без списков товаров (их отдаёт /action)."""
    _ensure(refresh)
    d = _state["data"]
    if not d:
        return {"building": True, "progress": _state["progress"], "error": _state["error"]}
    acts = []
    ads_by_mp = {mp: _ad_map(mp) for mp in ("OZON", "WB")}
    for a in d["actions"]:
        e = _with_drr(a, ads_by_mp[a["mp"]][0])
        acts.append({k: v for k, v in e.items() if k != "items"})
    auto_add = None
    for a in d["actions"]:
        if a["mp"] == "OZON" and a["items"]:
            seen = {}
            for i in a["items"]:
                seen[i["art"]] = i.get("auto_add")
            auto_add = (auto_add or {}) | seen
    return {"actions": acts, "built": d["built"], "errors": d.get("errors") or [],
            "building": _state["building"], "progress": _state["progress"],
            "ozon_auto_add_on": sum(1 for v in (auto_add or {}).values() if v),
            "ozon_ads": ads_by_mp["OZON"][1]}


# ── Акция как рекламная кампания: ДРР скидки против ДРР рекламы ─────────────
# Скидка для входа в акцию — это деньги, которые мы «тратим» с каждой продажи,
# как ставку в рекламе. ДРР акции = скидка в рублях ÷ исходная цена (по решению
# владельца — равен скидке в %; раньше было ÷ цена в акции, как выручка
# с продажи) — та же формула, что ДРР рекламы (расход ÷ выручка с рекламы).
# «С учётом комиссии»: со скидки Ozon берёт меньше комиссии, поэтому реальная
# потеря выплаты = скидка × (1 − комиссия%).

def _ad_map(mp: str) -> tuple[dict, dict]:
    """art → ДРР рекламы товара (Ozon Performance, последние 28 дн.) и итог кабинета."""
    if mp != "OZON":
        return {}, {}
    try:
        from routers import tools as _t
        res = _t._OZADS2.get(28) or next(iter(_t._OZADS2.values()), None)
        if not res:
            import snapshot
            res = snapshot.load("ozads2_28", None)
        if not res:
            # реклама ещё не собиралась — запускаем сборку, к следующему открытию будет
            import ozon_perf_client as _pc
            if _pc.configured() and 28 not in _t._OZADS2_STATE["building"]:
                _t._OZADS2_STATE["building"].add(28)
                _t._spawn(_t._ozads2_bg(28))
            return {}, {}
        m = {}
        for r in res.get("skus") or []:
            a = m.setdefault(r["art"], {"spent": 0.0, "money": 0.0, "gmv": 0.0})
            a["spent"] += r.get("spent") or 0
            a["money"] += r.get("orders_money") or 0
            a["gmv"] = max(a["gmv"], r.get("gmv") or 0)
        out = {art: {"ad_drr": round(v["spent"] / v["money"] * 100, 1) if v["money"] else None,
                     "ad_general_drr": round(v["spent"] / v["gmv"] * 100, 1) if v["gmv"] else None,
                     "ad_spent": round(v["spent"])} for art, v in m.items()}
        return out, {"drr": (res.get("total") or {}).get("cur", {}).get("drr"),
                     "period": res.get("period")}
    except Exception as e:
        _log.warning("promo ad map: %s", e)
        return {}, {}


def _with_drr(a: dict, ads: dict) -> dict:
    """Добавляет к товарам акции ДРР скидки и сравнение с рекламой, к акции — итог."""
    items = []
    sw_disc = sw_rev = sw_net = 0.0
    ad_spent = ad_money = 0.0
    for i in a.get("items") or []:
        i = dict(i)
        price, ap = i.get("price") or 0, i.get("action_price") or 0
        disc_rub = price - ap if price and ap and price > ap else 0
        i["discount_rub"] = round(disc_rub) if ap else None
        # ДРР акции считаем от исходной цены (решение владельца 06.11): совпадает со скидкой, %
        i["promo_drr"] = round(disc_rub / price * 100, 1) if ap and price else None
        comm = i.get("commission_pct")
        i["promo_drr_net"] = round(disc_rub * (1 - comm / 100) / price * 100, 1) if ap and price and comm is not None else None
        ad = ads.get(i["art"]) or {}
        i["ad_drr"], i["ad_general_drr"], i["ad_spent"] = ad.get("ad_drr"), ad.get("ad_general_drr"), ad.get("ad_spent")
        if i["promo_drr"] is None:
            i["vs_ad"] = ""
        elif i["ad_drr"] is None:
            i["vs_ad"] = "нет рекламы товара"
        else:
            diff = round(i["promo_drr"] - i["ad_drr"], 1)
            i["vs_ad"] = "дешевле рекламы" if diff <= 0 else f"дороже рекламы на {diff} п.п."
        items.append(i)
        # итог акции: взвешиваем по ожидаемым продажам (продажи в день), товары без остатка не считаем
        q = i.get("per_day") or 0
        if ap and (i.get("stock") or 0) > 0 and q:
            sw_disc += disc_rub * q
            sw_rev += price * q
            if comm is not None:
                sw_net += disc_rub * (1 - comm / 100) * q
            if i["ad_drr"] is not None and ad.get("ad_spent"):
                ad_spent += ad["ad_spent"]
                ad_money += ad["ad_spent"] / (i["ad_drr"] / 100) if i["ad_drr"] else 0
    out = dict(a)
    out["items"] = items
    out["promo_drr"] = round(sw_disc / sw_rev * 100, 1) if sw_rev else None
    out["promo_drr_net"] = round(sw_net / sw_rev * 100, 1) if sw_rev and sw_net else None
    out["promo_cost_day"] = round(sw_disc) if sw_rev else None      # ₽ в день при текущем темпе продаж
    out["ad_drr_same"] = round(ad_spent / ad_money * 100, 1) if ad_money else None
    return out


def _find(mp: str, aid: str) -> dict | None:
    d = _state["data"] or {}
    return next((a for a in d.get("actions", []) if a["mp"] == mp.upper() and a["id"] == aid), None)


@router.get("/action")
async def action(mp: str, id: str):
    a = _find(mp, id)
    if not a:
        return {"error": "Акция не найдена — обновите календарь"}
    ads, total = _ad_map(a["mp"])
    return {**_with_drr(a, ads), "ads_total": total}


@router.get("/export.xlsx")
async def export(mp: str, id: str):
    """Excel по акции — для согласования с клиентом."""
    import io
    from urllib.parse import quote
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    a = _find(mp, id)
    if not a:
        return Response("Акция не найдена", status_code=404)
    a = _with_drr(a, _ad_map(a["mp"])[0])
    wb = Workbook()
    ws = wb.active
    ws.title = "Акция"
    ws.append([f"{a['mp']} · {a['title']}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"{a['start']} — {a['end']}" + (f" · автодобавление: {', '.join(a['auto_add_dates'])}" if a.get("auto_add_dates") else "")])
    ws.append([])
    head = ["Артикул", "Товар", "Бренд", "Рекомендация", "Причина", "Цена сейчас", "Цена в акции", "Скидка, %",
            "Остаток", "Продаж в день", "Запас, дн.", "Себестоимость", "К выплате со штуки", "Прибыль со штуки",
            "Маржа, %", "Уже в акции", "Скидка, ₽", "ДРР акции, %", "ДРР акции с учётом комиссии, %",
            "ДРР рекламы товара, %", "Акция против рекламы"]
    ws.append(head)
    for c in ws[4]:
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical="center")
    fills = {"yes": "E2EFDA", "clear": "DDEBF7", "maybe": "FFF2CC", "no": "FCE4D6"}
    order = {"yes": 0, "clear": 1, "maybe": 2, "no": 3}
    for i in sorted(a["items"], key=lambda x: (order.get(x["rec"], 9), -(x["stock"] or 0))):
        ws.append([i["art"], i["name"], i["brand"], i["rec_text"], i["why"], i["price"], i["action_price"],
                   i["discount"], i["stock"], i["per_day"], i["stock_days"], i["cost"], i["payout"],
                   i["profit"], i["margin"], "да" if i["in_action"] else "", i["discount_rub"], i["promo_drr"],
                   i["promo_drr_net"], i["ad_drr"], i["vs_ad"]])
        ws.cell(ws.max_row, 4).fill = PatternFill("solid", fgColor=fills.get(i["rec"], "FFFFFF"))
    if a.get("promo_drr") is not None:
        ws["A2"].value = (ws["A2"].value or "") + (f" · ДРР акции {a['promo_drr']}%"
                          + (f" (с учётом комиссии {a['promo_drr_net']}%)" if a.get("promo_drr_net") is not None else "")
                          + (f" · ДРР рекламы этих товаров {a['ad_drr_same']}%" if a.get("ad_drr_same") is not None else ""))
    for col, w in zip("ABCDEFGHIJKLMNOPQRSTU", (16, 50, 16, 18, 48, 11, 11, 9, 9, 10, 9, 12, 12, 12, 9, 9, 10, 11, 14, 12, 24)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B5"
    buf = io.BytesIO()
    wb.save(buf)
    name = f"Акция_{a['mp']}_{a['start']}_{a['title'][:40]}.xlsx".replace("/", "-")
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
