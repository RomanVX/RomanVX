"""«Где деньги» — находки с суммой в рублях и готовым действием.

Не отчёт, а список решений: что именно течёт, сколько это стоит в месяц
и что нажать, чтобы починить. Считается из уже собранных данных
(юнитка по живым ценам, реклама, ставки и фразы, остатки, цены Ozon),
поэтому дёшево и не грузит 512 МБ.

Все суммы — эффект в месяц. Отрицательный поток (теряем) идёт со знаком
плюс в поле amount: это «сколько вернём, если починим».
"""
import asyncio
import logging
from datetime import datetime, timedelta

_log = logging.getLogger("money")

_KV = "money_findings"
_TTL_MIN = 60


def _msk() -> datetime:
    return datetime.utcnow() + timedelta(hours=3)


def _f(kind: str, title: str, amount: float, evidence: str, action: str,
       sku: str = "", act_kind: str = "", act_payload: dict | None = None) -> dict:
    return {"kind": kind, "title": title, "amount": round(amount or 0),
            "evidence": evidence, "action": action, "sku": sku,
            "act_kind": act_kind, "act_payload": act_payload or {}}


def _arts(v) -> list[str]:
    """Артикулы кампании приходят и строками, и объектами — сводим к строкам."""
    out = []
    for x in v or []:
        if isinstance(x, dict):
            x = x.get("art") or x.get("sku") or x.get("nm") or x.get("nmId")
        if x:
            out.append(str(x))
    return out


# ── источники находок ────────────────────────────────────────────────────────
# Юнитка тяжёлая (heavy.guard), поэтому собирается ОДИН раз на весь прогон
# и раздаётся источникам — иначе параллельные вызовы душат друг друга.
async def _ctx() -> dict:
    from routers import tools as _tools
    import wb_client as _wb
    items, live = [], {}
    try:
        data = await asyncio.wait_for(_tools.get_margin(mp="WB"), timeout=180)
        items = data.get("items") or []
    except Exception as e:
        _log.warning("ctx margin: %s", str(e)[:150])
    try:
        live = await asyncio.wait_for(_wb.get_current_prices(), timeout=30)
    except Exception:
        pass
    return {"items": items, "live": live}


async def _find_losing_skus(ctx: dict) -> list[dict]:
    """Товары, которые продаются в минус — по ЖИВЫМ ценам, а не по юнитке."""
    import agent_review as _ar
    items, live = ctx["items"], ctx["live"]
    if not items:
        return []
    out = []
    for b in items:
        sku = str(b.get("sku"))
        lp = (live.get(sku) or {}).get("discounted")
        m = _ar._margin_math({**b, "price0": lp} if lp else b)
        if m["profit_month"] < -300:
            out.append(_f(
                "losing_sku",
                f"{sku}: продаётся в минус",
                -m["profit_month"],
                f"прибыль {m['profit_unit']} ₽/шт при цене {m['price_seller']} ₽, "
                f"маржа {m['margin_pct']}%, объём {m['qty_month']} шт/мес; "
                f"ДРР {m['drr_pct']}% при безубыточном {m['be_drr_pct']}%",
                "Поднять цену ступенью, срезать ДРР или снять с продвижения — "
                "каждый проданный экземпляр забирает деньги",
                sku=sku))
    return out


async def _find_ad_waste(ctx: dict) -> list[dict]:
    """Кампании, где реклама дороже, чем приносит."""
    from routers import tools as _tools
    adv = await asyncio.wait_for(_tools.get_adv(), timeout=60)
    days = adv.get("days") or 28
    out = []
    for c in adv.get("campaigns") or []:
        spend = c.get("spend") or 0
        rev = c.get("revenue") or 0
        verdict = c.get("verdict")
        if verdict not in ("waste", "bad") or spend < 500:
            continue
        per_month = spend / days * 30
        if verdict == "waste":
            amount, ev = per_month, f"{round(spend)} ₽ за {days} дн без заказов"
        else:
            drr = spend / rev * 100 if rev else 100
            # сколько платим сверх разумного порога ДРР 15%
            amount = per_month * max(0.0, (drr - 15) / drr)
            ev = f"ДРР {round(drr)}% при расходе {round(spend)} ₽ за {days} дн"
        if amount < 300:
            continue
        cid = c.get("id") or c.get("advertId")
        out.append(_f(
            "ad_waste", f"Реклама «{c.get('name') or cid}»: {c.get('verdict_why') or verdict}",
            amount, ev,
            "Снизить ставку ступенью 20-30% или поставить кампанию на паузу",
            sku=", ".join(_arts(c.get("skus") or c.get("arts"))[:3]),
            act_kind="campaign_state" if verdict == "waste" else "",
            act_payload={"advert_id": cid, "action": "pause"} if cid and verdict == "waste" else {}))
    return out


async def _find_minus_phrases(ctx: dict) -> list[dict]:
    """Фразы с показами и расходом, но без единого заказа."""
    from routers import tools as _tools
    data = await asyncio.wait_for(
        _tools.bid_queries(refresh=False, date_from="", date_to=""), timeout=60)
    rows = data.get("rows") or []
    if not rows:
        return []
    by_camp: dict = {}
    for r in rows:
        if (r.get("orders") or 0) > 0 or (r.get("spend") or 0) < 100:
            continue
        if (r.get("views") or 0) < 300:
            continue
        key = (r.get("camp_id"), (_arts(r.get("skus")) or ["?"])[0], r.get("campaign"))
        g = by_camp.setdefault(key, {"spend": 0.0, "phrases": []})
        g["spend"] += r.get("spend") or 0
        g["phrases"].append(r.get("phrase"))
    out = []
    for (cid, sku, name), g in by_camp.items():
        if g["spend"] < 500:
            continue
        out.append(_f(
            "minus_phrases",
            f"{sku}: {len(g['phrases'])} фраз жгут бюджет без заказов",
            g["spend"] / 14 * 30,
            f"кампания «{name}»: {round(g['spend'])} ₽ за 14 дн, "
            f"ноль заказов. Топ: {', '.join(str(p) for p in g['phrases'][:5])}",
            "Добавить фразы в минус — бюджет уйдёт на конверсионные запросы",
            sku=sku, act_kind="minus_phrases",
            act_payload={"advert_id": cid, "phrases": g["phrases"][:30]}))
    return out


async def _find_oos_losses(ctx: dict) -> list[dict]:
    """Товар в нуле или на исходе — считаем упущенную прибыль в месяц."""
    from routers import dashboard as _dash
    import agent_review as _ar
    rows = await asyncio.wait_for(_dash.get_stocks_table(), timeout=90)
    if not rows:
        return []
    margin: dict = {}
    for b in ctx["items"]:
        lp = (ctx["live"].get(str(b.get("sku"))) or {}).get("discounted")
        m = _ar._margin_math({**b, "price0": lp} if lp else b)
        margin[str(b.get("sku"))] = m["profit_unit"]
    out = []
    for r in rows:
        sku = str(r.get("sku") or r.get("art") or "")
        prof = margin.get(sku)
        if prof is None or prof <= 0:
            continue
        for pf, dk, vk in (("WB", "wb_days", "wb_per_day"),
                           ("Ozon", "oz_days", "oz_per_day"),
                           ("ЯМ", "ym_days", "ym_per_day")):
            days = r.get(dk)
            speed = r.get(vk) or 0
            if speed < 0.3:
                continue
            if days is not None and days <= 0:
                out.append(_f(
                    "oos", f"{sku} ({pf}): в нуле, продажи стоят",
                    speed * 30 * prof,
                    f"продавался по {round(speed, 1)} шт/день, прибыль "
                    f"{round(prof)} ₽/шт — пока в ауте, теряем весь этот поток",
                    "Срочно в поставку; карточка без остатка ещё и падает в выдаче",
                    sku=sku))
            elif days is not None and days <= 10:
                out.append(_f(
                    "oos_soon", f"{sku} ({pf}): кончится через {int(days)} дн",
                    speed * 20 * prof,
                    f"{round(speed, 1)} шт/день, прибыль {round(prof)} ₽/шт; "
                    f"поставка едет 3-10 дней — не успеваем",
                    "Ставить в ближайшую поставку, иначе уйдём в аут",
                    sku=sku))
    return out


async def _find_no_cogs(ctx: dict) -> list[dict]:
    """Нет себестоимости — значит маржа этого SKU вообще неизвестна."""
    blind = [str(b.get("sku")) for b in ctx["items"] if not (b.get("cogs") or 0)]
    if not blind:
        return []
    return [_f("no_cogs", f"Нет себестоимости у {len(blind)} SKU", 0,
               "без себестоимости: " + ", ".join(blind[:12]),
               "Заполнить себестоимость — иначе маржа и целевые цены "
               "по этим товарам считаются вслепую")]


async def _find_ozon_auto_action(ctx: dict) -> list[dict]:
    """Автоакции Ozon режут цену без спроса."""
    import ozon_client
    prices = await asyncio.wait_for(ozon_client.get_prices(), timeout=40)
    on = [a for a, p in (prices or {}).items()
          if p.get("auto_action_enabled") or p.get("auto_action")]
    if not on:
        return []
    return [_f("oz_auto", f"Ozon сам добавляет в акции {len(on)} SKU", 0,
               ", ".join(sorted(on)[:12]),
               "Проверить, выдержит ли маржа акционную цену; "
               "где нет — выключить автодобавление")]


_SOURCES = (_find_losing_skus, _find_ad_waste, _find_minus_phrases,
            _find_oos_losses, _find_no_cogs, _find_ozon_auto_action)


async def build() -> dict:
    """Собрать все находки. Каждый источник изолирован: упал — не роняет отчёт."""
    ctx = await _ctx()
    results = await asyncio.gather(*[s(ctx) for s in _SOURCES],
                                   return_exceptions=True)
    findings, errors = [], []
    for src, res in zip(_SOURCES, results):
        if isinstance(res, list):
            findings += res
        else:
            errors.append(f"{src.__name__}: {str(res)[:120] or type(res).__name__}")
            _log.warning("%s: %s", src.__name__, str(res)[:200])
    findings.sort(key=lambda f: -f["amount"])
    total = sum(f["amount"] for f in findings)
    by_kind: dict = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + f["amount"]
    return {"total": round(total), "findings": findings[:60],
            "by_kind": by_kind, "errors": errors,
            "built": _msk().strftime("%Y-%m-%d %H:%M")}


async def get(refresh: bool = False) -> dict:
    """Находки из kv; протухшие пересобираем."""
    import snapshot as _snap
    if not refresh:
        v = await asyncio.to_thread(_snap.load, _KV, None)
        if v and v.get("built"):
            try:
                age = _msk() - datetime.strptime(v["built"], "%Y-%m-%d %H:%M")
                if age < timedelta(minutes=_TTL_MIN):
                    return v
            except ValueError:
                pass
    data = await build()
    await asyncio.to_thread(_snap.save, _KV, data)
    return data
