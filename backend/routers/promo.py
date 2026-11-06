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
                "rec": code, "rec_text": REC[code], "why": why, **eco,
            })
        counts = {k: sum(1 for i in items if i["rec"] == k) for k in REC}
        out.append({
            "mp": "OZON", "id": str(aid), "title": a.get("title") or "",
            "type": _OZ_TYPE_RU.get(a.get("action_type") or "", a.get("action_type") or ""),
            "description": (a.get("description") or "")[:600],
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
            "description": (det.get("description") or "")[:600],
            "advantages": det.get("advantages") or [],
            "start": (p.get("startDateTime") or "")[:10], "end": (p.get("endDateTime") or "")[:10],
            "freeze": "", "auto_add_dates": [],
            "candidates": det.get("inPromoActionTotal", 0) + det.get("notInPromoActionTotal", 0) if det else len(items),
            "participating": det.get("inPromoActionTotal", sum(1 for i in items if i["in_action"])) if det else 0,
            "counts": counts, "items": items,
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
    acts = [{k: v for k, v in a.items() if k != "items"} for a in d["actions"]]
    auto_add = None
    for a in d["actions"]:
        if a["mp"] == "OZON" and a["items"]:
            seen = {}
            for i in a["items"]:
                seen[i["art"]] = i.get("auto_add")
            auto_add = (auto_add or {}) | seen
    return {"actions": acts, "built": d["built"], "errors": d.get("errors") or [],
            "building": _state["building"], "progress": _state["progress"],
            "ozon_auto_add_on": sum(1 for v in (auto_add or {}).values() if v)}


def _find(mp: str, aid: str) -> dict | None:
    d = _state["data"] or {}
    return next((a for a in d.get("actions", []) if a["mp"] == mp.upper() and a["id"] == aid), None)


@router.get("/action")
async def action(mp: str, id: str):
    a = _find(mp, id)
    if not a:
        return {"error": "Акция не найдена — обновите календарь"}
    return a


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
    wb = Workbook()
    ws = wb.active
    ws.title = "Акция"
    ws.append([f"{a['mp']} · {a['title']}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"{a['start']} — {a['end']}" + (f" · автодобавление: {', '.join(a['auto_add_dates'])}" if a.get("auto_add_dates") else "")])
    ws.append([])
    head = ["Артикул", "Товар", "Бренд", "Рекомендация", "Причина", "Цена сейчас", "Цена в акции", "Скидка, %",
            "Остаток", "Продаж в день", "Запас, дн.", "Себестоимость", "К выплате со штуки", "Прибыль со штуки",
            "Маржа, %", "Уже в акции"]
    ws.append(head)
    for c in ws[4]:
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical="center")
    fills = {"yes": "E2EFDA", "clear": "DDEBF7", "maybe": "FFF2CC", "no": "FCE4D6"}
    order = {"yes": 0, "clear": 1, "maybe": 2, "no": 3}
    for i in sorted(a["items"], key=lambda x: (order.get(x["rec"], 9), -(x["stock"] or 0))):
        ws.append([i["art"], i["name"], i["brand"], i["rec_text"], i["why"], i["price"], i["action_price"],
                   i["discount"], i["stock"], i["per_day"], i["stock_days"], i["cost"], i["payout"],
                   i["profit"], i["margin"], "да" if i["in_action"] else ""])
        ws.cell(ws.max_row, 4).fill = PatternFill("solid", fgColor=fills.get(i["rec"], "FFFFFF"))
    for col, w in zip("ABCDEFGHIJKLMNOP", (16, 50, 16, 18, 48, 11, 11, 9, 9, 10, 9, 12, 12, 12, 9, 9)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B5"
    buf = io.BytesIO()
    wb.save(buf)
    name = f"Акция_{a['mp']}_{a['start']}_{a['title'][:40]}.xlsx".replace("/", "-")
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
