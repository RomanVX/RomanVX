"""Агент-стратег: tool-use цикл над данными дашборда.

Сам решает, какие данные посмотреть (юнитка, P&L, реклама, конкуренты,
тренды, цены, продажи), ведёт память целей/задач с датами проверки,
каждую сессию сверяет план с фактом и пишет отчёт в Telegram.
Действий с деньгами не совершает — только анализ и рекомендации."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

import db
from config import ANTHROPIC_API_KEY

import agent_review as _ar

_log = logging.getLogger("strategist")
_MODEL = "claude-opus-4-8"
_MAX_STEPS = 18          # предохранитель цикла
_TOOL_TRIM = 7000        # символов на результат инструмента

_running = False         # одна сессия за раз (512 МБ и здравый смысл)
_cancel = False          # стоп-слово из TG: прервать текущую сессию


def _init():
    db.execute("""CREATE TABLE IF NOT EXISTS strategist_tasks (
        id TEXT PRIMARY KEY, created TEXT, kind TEXT, title TEXT,
        metric TEXT, check_date TEXT, status TEXT,
        result TEXT, reasoning TEXT)""")


def _tasks_load(status: str = "") -> list[dict]:
    _init()
    rows = db.fetchall(
        "SELECT id, created, kind, title, metric, check_date, status, result, reasoning "
        "FROM strategist_tasks" + (" WHERE status = ?" if status else "")
        + " ORDER BY created DESC",
        (status,) if status else ())
    keys = ["id", "created", "kind", "title", "metric", "check_date",
            "status", "result", "reasoning"]
    return [dict(zip(keys, r)) for r in rows[:60]]


# ── инструменты ───────────────────────────────────────────────────────────────
async def _t_unit(_a: dict) -> str:
    from routers import tools as _tools
    data = await _tools.get_margin(mp="WB")
    items = data.get("items") or []
    if not items:
        return "юнитка ещё собирается — попробуй другие данные"
    return json.dumps([_ar._margin_math(b) for b in items], ensure_ascii=False)


async def _t_stocks(_a: dict) -> str:
    txt = await _ar.build_stocks_summary()
    return txt.replace("<b>", "").replace("</b>", "")


async def _t_pnl(_a: dict) -> str:
    from routers import finance as _fin
    pnl = await _fin.get_wb_pnl(months=3)
    vals = {r["key"]: r.get("values") or {} for r in pnl.get("rows") or []}
    return json.dumps(vals, ensure_ascii=False)


async def _t_adv(_a: dict) -> str:
    from routers import tools as _tools
    adv = await _tools.get_adv()
    camps = [{"name": c.get("name"), "active": c.get("active"),
              "spend": c.get("spend"), "revenue": c.get("revenue"),
              "drr": c.get("drr"), "orders": c.get("orders"),
              "verdict": c.get("verdict"), "skus": c.get("skus") or c.get("arts")}
             for c in (adv.get("campaigns") or [])]
    return (f"итого: расход {adv.get('total_spend')} ₽, выручка {adv.get('total_revenue')} ₽, "
            f"ДРР {adv.get('total_drr')}%, слив {adv.get('waste')} ₽ за {adv.get('days')} дн. "
            + json.dumps(camps, ensure_ascii=False))


async def _t_adv_bids(_a: dict) -> str:
    """Ставки CPM и фразы кампаний WB — кеш вкладки «Ставка CPM»."""
    from routers import tools as _tools
    data = await _tools.bid_queries(refresh=False, date_from="", date_to="")
    rows = data.get("rows") or []
    if not rows:
        return "нет данных по ставкам: " + str(data.get("error") or "кеш пуст")
    agg: dict = {}
    for r in rows:
        key = (r.get("campaign"), (r.get("skus") or ["?"])[0])
        a = agg.setdefault(key, {"bid": r.get("bid"), "views": 0, "clicks": 0,
                                 "spend": 0.0, "orders": 0, "phrases": []})
        a["views"] += r.get("views") or 0
        a["clicks"] += r.get("clicks") or 0
        a["spend"] += r.get("spend") or 0
        a["orders"] += r.get("orders") or 0
        if r.get("bid") and not a["bid"]:
            a["bid"] = r["bid"]
        if r.get("phrase"):
            a["phrases"].append(r)
    out = []
    for (camp, sku), a in sorted(agg.items(), key=lambda kv: -kv[1]["spend"]):
        top = sorted(a["phrases"], key=lambda x: -(x.get("spend") or 0))[:5]
        minus = sum(1 for p in a["phrases"]
                    if (p.get("views") or 0) >= 500 and not (p.get("orders") or 0))
        out.append({
            "campaign": camp, "sku": sku, "bid_cpm": a["bid"],
            "views": a["views"], "clicks": a["clicks"],
            "ctr": round(a["clicks"] / a["views"] * 100, 2) if a["views"] else None,
            "orders": a["orders"], "spend": round(a["spend"]),
            "cpm_fact": round(a["spend"] / a["views"] * 1000) if a["views"] else None,
            "minus_candidates": minus,
            "top_phrases": [{"q": p.get("phrase"), "views": p.get("views"),
                             "ctr": p.get("ctr"), "orders": p.get("orders"),
                             "spend": p.get("spend"), "bid": p.get("bid")}
                            for p in top]})
    return ("по кампаниям WB (период 14 дн, ставки текущие): "
            + json.dumps(out[:25], ensure_ascii=False))


async def _t_reviews(_a: dict) -> str:
    """Отзывы и рейтинг: где просел рейтинг и что пишут в негативе."""
    import reviews_client as rc
    table = await asyncio.to_thread(rc.get_rating_table)
    arts = table.get("articles") or []

    def _worst(a):
        vals = [a.get(p) for p in ("wb", "ozon", "ym")
                if a.get(p) and (a.get(p + "_cnt") or 0) >= 3]
        return min(vals) if vals else 5.0
    weak = [{"sku": a["sku"], "name": a.get("name"),
             "wb": a.get("wb"), "wb_n": a.get("wb_cnt"),
             "ozon": a.get("ozon"), "ozon_n": a.get("ozon_cnt"),
             "ym": a.get("ym"), "ym_n": a.get("ym_cnt")}
            for a in sorted(arts, key=_worst)[:12] if _worst(a) < 4.9]
    neg = await asyncio.to_thread(rc.get_all_reviews, None, 300)
    bad = [{"sku": r.get("sku"), "pf": r.get("platform"), "rating": r.get("rating"),
            "text": (r.get("text") or "")[:180],
            "date": str(r.get("created_at") or "")[:10],
            "answered": bool(r.get("answer"))}
           for r in neg if (r.get("rating") or 5) <= 3 and (r.get("text") or "")][:25]
    unans = sum(1 for r in neg if not r.get("answer") and (r.get("text") or ""))
    return json.dumps({"слабые_по_рейтингу": weak,
                       "негатив_последний": bad,
                       "без_ответа_всего": unans}, ensure_ascii=False, default=str)


async def _t_ozon_ads(_a: dict) -> str:
    """Реклама Ozon (Performance): кампании, расход, ДРР, воронка."""
    from routers import tools as _tools
    d = await _tools.get_ozads(refresh=False, days=28)
    camps = [{"name": c.get("name"), "state": c.get("state"),
              "spend": c.get("spend"), "revenue": c.get("revenue"),
              "drr": c.get("drr"), "orders": c.get("orders"),
              "views": c.get("views"), "clicks": c.get("clicks"),
              "ctr": c.get("ctr")} for c in (d.get("campaigns") or [])][:30]
    return json.dumps({"итого": {k: d.get(k) for k in
                                 ("total_spend", "total_revenue", "total_drr",
                                  "total_orders", "days")},
                       "кампании": camps}, ensure_ascii=False, default=str)


async def _t_ozon_phrases(_a: dict) -> str:
    """Поисковые фразы рекламы Ozon: где показы без заказов."""
    from routers import tools as _tools
    d = await _tools.get_ozphrases(refresh=False, days=14)
    rows = (d.get("rows") or d.get("phrases") or [])[:60]
    return json.dumps(rows, ensure_ascii=False, default=str)


async def _t_funnel(_a: dict) -> str:
    """Воронка Ozon: показы → карточка → корзина → заказ по SKU."""
    from routers import tools as _tools
    d = await _tools.get_funnel(refresh=False)
    rows = (d.get("rows") or d.get("items") or [])[:60]
    return json.dumps(rows, ensure_ascii=False, default=str)


async def _t_clusters(a: dict) -> str:
    """Остатки по кластерам: WB и/или Ozon — где кончается товар."""
    from routers import tools as _tools
    which = str(a.get("platform") or "both").lower()
    out = {}
    if which in ("both", "ozon"):
        try:
            d = await _tools.get_ozon_clusters(refresh=False)
            out["ozon"] = (d.get("rows") or d.get("clusters") or [])[:40]
        except Exception as e:
            out["ozon_error"] = str(e)[:200]
    if which in ("both", "wb"):
        try:
            d = await _tools.get_clusters(refresh=False)
            out["wb"] = (d.get("rows") or d.get("clusters") or [])[:40]
        except Exception as e:
            out["wb_error"] = str(e)[:200]
    return json.dumps(out, ensure_ascii=False, default=str)


async def _t_pnl_all(_a: dict) -> str:
    """P&L всех площадок: WB + Ozon + ЯМ, а не только WB."""
    from routers import finance as _fin
    out = {}
    for name, fn in (("wb", lambda: _fin.get_wb_pnl(months=3, refresh=False)),
                     ("ozon", lambda: _fin.get_ozon_pnl(months=3, refresh=False)),
                     ("ym", lambda: _fin.get_ym_pnl(months=3, refresh=False))):
        try:
            pnl = await fn()
            out[name] = {r["key"]: r.get("values") or {}
                         for r in pnl.get("rows") or []}
        except Exception as e:
            out[name] = f"ошибка: {str(e)[:150]}"
    return json.dumps(out, ensure_ascii=False, default=str)


async def _t_history(a: dict) -> str:
    """Вечная история продаж из БД: любой период, а не только 14 дней."""
    import sales_history as sh
    days = int(a.get("days") or 90)
    d_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    d = await asyncio.to_thread(sh.get_summary, d_from, None)
    daily = d.get("daily") or []
    # для длинных периодов сворачиваем в недели, чтобы не раздувать контекст
    if len(daily) > 45:
        weeks: dict = {}
        for row in daily:
            wk = str(row.get("date"))[:10]
            key = (datetime.strptime(wk, "%Y-%m-%d")
                   - timedelta(days=datetime.strptime(wk, "%Y-%m-%d").weekday())
                   ).strftime("%Y-%m-%d")
            w = weeks.setdefault(key, {})
            for k, v in row.items():
                if k != "date" and isinstance(v, (int, float)):
                    w[k] = round(w.get(k, 0) + v)
        series = [{"week": k, **v} for k, v in sorted(weeks.items())]
    else:
        series = daily
    return json.dumps({"итоги_по_площадкам": d.get("by_platform"),
                       "хранится_с": d.get("stored_from"),
                       "ряд": series}, ensure_ascii=False, default=str)


async def _t_prices(_a: dict) -> str:
    return (await _ar._prices_context()) or "нет данных"


async def _t_sales(_a: dict) -> str:
    return (await _ar._daily_context()) or "нет данных"


async def _t_competitors(_a: dict) -> str:
    from routers import tools as _tools
    comp = await _tools.competitors_get()
    if not comp.get("queries"):
        return "срезов конкурентов пока нет"
    slim = []
    for qb in comp["queries"]:
        slim.append({"query": qb["query"], "top": [
            {"pos": i["position"], "brand": i["brand"], "price": i["price"],
             "price_prev": i.get("price_prev"), "fb": i["feedbacks"],
             "fb_delta": i.get("fb_delta"), "ours": i["is_ours"]}
            for i in qb["items"][:12]]})
    return f"срез {comp.get('day')} (prev {comp.get('prev_day')}): " \
        + json.dumps(slim, ensure_ascii=False)


async def _t_trends(a: dict) -> str:
    from routers import tools as _tools
    res = await _tools.trends_get(weeks=12, min_cnt=0, q=str(a.get("filter") or ""))
    items = res.get("items") or []
    src = str(a.get("source") or "")
    if src in ("my", "market"):
        items = [i for i in items if (src == "my") == (i["source"] == "ozon_my")]
    slim = [{"q": i["query"], "src": "my" if i["source"] == "ozon_my" else "mkt",
             "last": i.get("last"), "d28": i.get("d28"), "d7": i.get("d7"),
             "slope": i.get("slope_pct"), "score": i.get("score"),
             "stage": i.get("stage"), "items": i.get("items_cnt")}
            for i in items[:40]]
    return json.dumps(slim, ensure_ascii=False) if slim else "трендов по фильтру нет"


async def _t_ozon_queries(_a: dict) -> str:
    import ozon_client
    from routers import tools as _tools
    d_from, d_to = _tools._trend_last_week()
    base = await ozon_client.get_product_queries(d_from, d_to)
    slim = [{"art": b.get("offer_id"), "searches": b.get("unique_search_users"),
             "position": b.get("position"), "view_conv": b.get("view_conversion"),
             "gmv": b.get("gmv")} for b in base[:60]]
    return f"поиск Ozon {d_from}—{d_to} по нашим товарам: " \
        + json.dumps(slim, ensure_ascii=False)


async def _t_repricer(_a: dict) -> str:
    import repricer as rp
    ov = await rp.overview()
    slim = [{"art": c["art"], "target": c["target"], "min_wb": c["min_wb"],
             "min_ozon": c["min_ozon"], "active": bool(c["active"]),
             "wb_seller_now": c.get("wb_seller_now"),
             "wb_buyer_now": c.get("wb_buyer_now"),
             "margin_at_target": c.get("margin_at_target"),
             "profit_at_target": c.get("profit_at_target")}
            for c in ov["items"]]
    return json.dumps({"config": slim, "pending_proposals": ov["proposals"],
                       "presets": ov["presets"]}, ensure_ascii=False)


async def _t_repricer_propose(a: dict) -> str:
    import repricer as rp
    items = a.get("items") or []
    reason = str(a.get("reason") or "")
    n = await asyncio.to_thread(rp.propose, items, reason)
    return f"предложений сохранено: {n}; владелец увидит их на вкладке Стратегия и решит"


async def _t_wb_search(a: dict) -> str:
    """Живая выдача WB по запросу — через домашний браузерный агент."""
    from routers import tools as _tools
    q = str(a.get("query") or "").strip()
    if not q:
        return "нужен параметр query"
    try:
        products, total = await asyncio.wait_for(_tools._wb_agent_search(q), timeout=150)
    except asyncio.TimeoutError:
        return ("домашний агент не ответил за 2.5 мин — он работает днём 11-21 МСК; "
                "попробуй кешированные срезы (competitors) или продолжи без выдачи")
    except Exception as e:
        return f"выдача недоступна: {str(e)[:200]}"
    if not products:
        return "выдача пустая — вероятно, агент офлайн; используй competitors"
    slim = [{"pos": i + 1, "brand": (p.get("brand") or "")[:30],
             "name": (p.get("name") or "")[:60], "price": p.get("price"),
             "rating": p.get("rating"), "feedbacks": p.get("feedbacks")}
            for i, p in enumerate(products[:25])]
    return f"выдача WB «{q}» (всего товаров {total}): " + json.dumps(slim, ensure_ascii=False)


async def _t_promos(_a: dict) -> str:
    """Акции обеих площадок: где состоим (Ozon) и куда зовут (WB)."""
    out = {}
    try:
        import ozon_client
        prices = await ozon_client.get_prices()
        # группируем по акции (иначе повторы названий съедают весь лимит вывода)
        by_action: dict = {}
        auto_arts = []
        for art, p in prices.items():
            if p.get("auto_action"):
                auto_arts.append(art)
            for a in p.get("actions") or []:
                t = (a.get("title") or "")[:60]
                by_action.setdefault(t, {"to": a.get("to"), "arts": []})["arts"].append(art)
        out["ozon"] = [{"action": t, "to": v["to"], "arts": sorted(set(v["arts"]))}
                       for t, v in by_action.items()] or "ни один товар не состоит в акциях"
        out["ozon_auto_action"] = sorted(auto_arts) or "нет"
        out["ozon_note"] = ("auto_action=true — Ozon сам добавляет товар в акции "
                            "(риск незаметного среза маржи)")
    except Exception as e:
        out["ozon_error"] = str(e)[:200]
    try:
        import wb_client
        import catalog as _cat
        nm_to_art = {str(k): v for k, v in getattr(_cat, "WB_ID_TO_ART", {}).items()}
        promos = await wb_client.get_promotions()
        wb_list = []
        for p in promos[:12]:
            row = {"id": p.get("id"), "name": p.get("name"),
                   "start": (p.get("startDateTime") or "")[:10],
                   "end": (p.get("endDateTime") or "")[:10],
                   "type": p.get("type"),
                   "advantages": p.get("advantages")}
            try:  # кто из наших уже затянут в акцию и на каких условиях
                noms = await wb_client.get_promo_nomenclatures(int(p.get("id")))
                ours = []
                for n in noms[:60]:
                    art = nm_to_art.get(str(n.get("id") or n.get("nmID") or ""))
                    if art:
                        ours.append({"art": art,
                                     "plan_price": n.get("planPrice") or n.get("price"),
                                     "discount": n.get("planDiscount") or n.get("discount")})
                row["our_in_action"] = ours or "никого"
            except Exception as e2:
                row["nomenclatures_error"] = str(e2)[:120]
            wb_list.append(row)
        out["wb"] = wb_list or "акций в календаре WB нет"
        out["wb_note"] = ("our_in_action — наши товары, уже затянутые в акцию, "
                         "с плановой ценой/скидкой; пусто у всех = автоакции "
                         "ещё не применились или товары не участвуют")
    except Exception as e:
        out["wb_error"] = str(e)[:200]
    return json.dumps(out, ensure_ascii=False)


async def _t_fbs(_a: dict) -> str:
    """Сравнение затрат FBW против FBS по официальным тарифам."""
    from routers import tools as _tools
    res = await _tools.fbs_compare()
    if res.get("error") or res.get("message"):
        return str(res.get("error") or res.get("message"))
    slim = [{"sku": i["sku"], "d_unit": i["delta_unit"], "d_month": i["delta_month"],
             "comm": f"{i['fbo_comm_pct']}→{i['fbs_comm_pct']}%",
             "logist": f"{i['fbo_logist']}→{i['fbs_logist']}",
             "storage_saved": i["fbo_storage"]}
            for i in res["items"]]
    return json.dumps({"delta_month_total": res["delta_month"],
                       "note": res["note"], "by_sku": slim}, ensure_ascii=False)


async def _t_simulate(a: dict) -> str:
    """Просчитать сценарий: новая цена/ДРР/себес → объём, выручка, прибыль."""
    import simulator
    sku = str(a.get("sku") or "")
    if not sku:
        return "нужен sku"
    if a.get("curve"):
        d = await simulator.curve(sku, drr_pct=a.get("drr"))
    else:
        d = await simulator.simulate(
            sku, price_seller=a.get("price"), drr_pct=a.get("drr"),
            cogs=a.get("cogs"))
    return json.dumps(d, ensure_ascii=False, default=str)


async def _t_elasticity(a: dict) -> str:
    """Реакция спроса на прошлые изменения цены — факт вместо спора."""
    import elasticity
    d = await elasticity.get(int(a.get("days") or 180))
    if d.get("error"):
        return d["error"]
    return json.dumps({"по_sku": d.get("summary")}, ensure_ascii=False,
                      default=str)


async def _t_money(_a: dict) -> str:
    """Где деньги: посчитанные утечки и упущенное с суммами и действиями."""
    import money
    d = await money.get()
    slim = [{"сумма_в_месяц": f["amount"], "что": f["title"],
             "факты": f["evidence"][:250], "действие": f["action"][:200],
             "sku": f.get("sku"), "есть_кнопка": bool(f.get("act_kind"))}
            for f in (d.get("findings") or [])[:25]]
    return json.dumps({"итого_в_месяц": d.get("total"), "находки": slim},
                      ensure_ascii=False)


async def _t_news(a: dict) -> str:
    """Новости площадок с разбором влияния на нас."""
    import news as _news
    days = int(a.get("days") or 30)
    items = await asyncio.to_thread(_news.listing, days, "", "")
    slim = [{"дата": i["published"][:10], "источник": i["source"],
             "важность": i.get("importance"), "заголовок": i["title"][:150],
             "нас_касается": (i.get("impact") or "")[:300],
             "вступает": i.get("effective_date")} for i in items[:40]]
    up = await asyncio.to_thread(_news.upcoming)
    return json.dumps({"новости": slim, "скоро_вступает_в_силу": up},
                      ensure_ascii=False)


async def _t_dashboard_catalog(_a: dict) -> str:
    """Каталог всех эндпоинтов дашборда (наш же OpenAPI)."""
    import agent_api
    cat = await asyncio.to_thread(agent_api.catalog)
    return json.dumps(cat, ensure_ascii=False)


async def _t_dashboard_api(a: dict) -> str:
    """Прямой вызов любого GET дашборда: данных больше, чем обёрток."""
    import agent_api
    path = str(a.get("path") or "")
    if not path:
        return "нужен path, например /api/tools/clusters"
    params = a.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    return (await agent_api.call(path, params))[:_TOOL_TRIM]


async def _t_propose_action(a: dict) -> str:
    """Предложить действие в кабинете — уходит владельцу на подтверждение."""
    import agent_actions as aa
    kind = str(a.get("kind") or "")
    if kind not in aa._EXEC:
        return f"неизвестный тип действия; доступны: {', '.join(aa._EXEC)}"
    payload = a.get("payload") or {}
    if not isinstance(payload, dict) or not payload:
        return "нужен payload с параметрами действия"
    aid = await asyncio.to_thread(
        aa.propose, kind, str(a.get("title") or kind), payload,
        str(a.get("reason") or ""))
    await aa.notify(aid)
    return (f"заявка {aid} создана и отправлена владельцу на подтверждение. "
            "Сам ты её не применяешь — дождись решения.")


async def _t_memory(_a: dict) -> str:
    import snapshot as _snap
    plan = await asyncio.to_thread(_snap.load, "strategist_plan", None) or {}
    tasks = await asyncio.to_thread(_tasks_load)
    return json.dumps({"current_plan": plan.get("text", "плана ещё нет"),
                       "plan_updated": plan.get("updated"),
                       "tasks": tasks}, ensure_ascii=False)


async def _t_save(a: dict) -> str:
    """Сохранить план и задачи; закрыть проверенные."""
    import snapshot as _snap
    _init()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    plan = str(a.get("plan") or "").strip()
    if plan:
        await asyncio.to_thread(_snap.save, "strategist_plan",
                                {"text": plan[:8000], "updated": now})
    n_new, n_closed = 0, 0
    for t in (a.get("new_tasks") or [])[:15]:
        title = str(t.get("title") or "").strip()
        if not title:
            continue
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO strategist_tasks VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], now, str(t.get("kind") or "task")[:20],
             title[:300], str(t.get("metric") or "")[:300],
             str(t.get("check_date") or "")[:10], "open", "",
             str(t.get("reasoning") or "")[:1000]))
        n_new += 1
    for t in (a.get("close_tasks") or [])[:30]:
        tid = str(t.get("id") or "")
        st = t.get("status") if t.get("status") in ("done", "failed", "dropped") else "done"
        if tid:
            await asyncio.to_thread(
                db.execute,
                "UPDATE strategist_tasks SET status = ?, result = ? WHERE id = ?",
                (st, str(t.get("result") or "")[:1000], tid))
            n_closed += 1
    return f"сохранено: план {'да' if plan else 'нет'}, новых задач {n_new}, закрыто {n_closed}"


_TOOLS = {
    "unit_economics": (_t_unit, "Юнит-экономика WB по каждому SKU: цена, себес, прибыль/шт, маржа, ДРР, прогноз продаж на месяц"),
    "stocks": (_t_stocks, "Остатки по всем маркетплейсам с днями запаса"),
    "pnl": (_t_pnl, "P&L WB по месяцам за 3 месяца: выручка, комиссия, логистика, хранение, реклама, прибыль"),
    "advertising": (_t_adv, "Рекламные кампании WB: расход, выручка, ДРР, вердикты где сливается бюджет"),
    "adv_bids": (_t_adv_bids, "Ставки CPM по кампаниям и артикулам WB: показы, клики, CTR, CR, факт-CPM, топ-фразы по расходу и кандидаты в минус-фразы"),
    "reviews": (_t_reviews, "Отзывы и рейтинг: слабые SKU по рейтингу, свежий негатив с текстом, сколько отзывов без ответа"),
    "ozon_ads": (_t_ozon_ads, "Реклама Ozon (Performance): кампании, расход, ДРР, показы/клики — вторая половина рекламного бюджета"),
    "ozon_phrases": (_t_ozon_phrases, "Поисковые фразы рекламы Ozon: где показы есть, а заказов нет"),
    "funnel": (_t_funnel, "Воронка Ozon по SKU: показы → карточка → корзина → заказ; где теряем продажи"),
    "clusters": (_t_clusters, "Остатки по кластерам WB и Ozon: где физически кончается товар (аргумент platform: wb|ozon|both)"),
    "pnl_all": (_t_pnl_all, "P&L всех трёх площадок за 3 месяца (WB, Ozon, ЯМ) — инструмент pnl показывает только WB"),
    "history": (_t_history, "Вечная история продаж из БД за любой период (аргумент days, по умолчанию 90) — не ограничена 14 днями и 90 днями API"),
    "prices": (_t_prices, "Текущие цены наших товаров и история изменений за 14 дней"),
    "sales_daily": (_t_sales, "Продажи по дням за 14 дней по площадкам + по SKU за 7 дней"),
    "competitors": (_t_competitors, "Срезы выдачи WB по нашим запросам: позиции, цены и прирост отзывов конкурентов"),
    "trends": (_t_trends, "Радар трендов Ozon: нарастающие поисковые запросы. Параметры: source='my'|'market', filter='слова'"),
    "ozon_search": (_t_ozon_queries, "Поиск Ozon по нашим товарам: частота, наша позиция, конверсия, GMV из поиска"),
    "repricer": (_t_repricer, "Репрайсер: целевые цены для покупателя, минималки Ozon/WB, "
                              "текущие цены WB и маржа при целевой цене"),
    "repricer_propose": (_t_repricer_propose, "Предложить владельцу изменения репрайсера. "
                         "Параметры: items [{art, target, min_wb, min_ozon, why}], reason. "
                         "Это ТОЛЬКО предложение — применяет владелец"),
    "wb_search": (_t_wb_search, "ЖИВАЯ выдача WB по любому поисковому запросу (топ-25: бренды, "
                  "цены, рейтинги, отзывы) — анализ ниши/конкурентов по запросу. Параметр: query. "
                  "Медленный (до 2 мин), работает днём 11-21 МСК; для наших постоянных запросов "
                  "быстрее competitors"),
    "promos": (_t_promos, "Акции маркетплейсов: в каких акциях Ozon состоят наши товары "
               "(+флаг автодобавления), календарь акций WB на 30 дней (куда зовут, даты, условия)"),
    "fbs_compare": (_t_fbs, "Переход WB на FBS: дельта прямых затрат на штуку и месяц по каждому "
                    "SKU (комиссия/логистика/хранение по официальным тарифам); конверсию и сроки не моделирует"),
    "simulate": (_t_simulate, "Симулятор «что если» по SKU: аргументы sku, price (новая цена продавца), drr, cogs — вернёт объём, выручку, прибыль и диапазон неопределённости. С curve=true строит кривую прибыли по цене и находит оптимум. Используй ДО того, как предлагать изменение цены или ставки"),
    "elasticity": (_t_elasticity, "Эластичность цены по нашей истории: как менялся темп продаж после каждого изменения цены, медианная реакция и вердикт «есть запас поднять / спрос чувствителен». Используй, когда речь о повышении или снижении цены"),
    "money": (_t_money, "Где деньги: готовый список утечек и упущенной прибыли с суммами в рублях за месяц и действиями — начинай отсюда, когда спрашивают «что делать» или «где теряем»"),
    "news": (_t_news, "Новости площадок за период (аргумент days) с разбором важности и влияния на нас + календарь вступающих в силу изменений"),
    "dashboard_catalog": (_t_dashboard_catalog, "Каталог ВСЕХ эндпоинтов дашборда с описаниями и параметрами — смотри сюда, если нужных данных нет в готовых инструментах"),
    "dashboard_api": (_t_dashboard_api, "Вызвать любой GET-эндпоинт дашборда напрямую: path (например /api/tools/clusters) и params. Так доступны любые данные проекта, а не только готовые инструменты"),
    "propose_action": (_t_propose_action, "Предложить действие в кабинете владельцу на подтверждение. "
        "kind: minus_phrases (payload: advert_id, nm_id, phrases[]) | set_bid (advert_id, nm_id, bid в рублях за 1000 показов, placement search|recommendations|combined, bid_was) | "
        "campaign_state (advert_id, action pause|start) | ozon_price (offer_id, price, old_price). "
        "Обязательно title и reason — коротко, зачем это и на чём основано. Сам НЕ применяешь: решает владелец"),
    "memory": (_t_memory, "Твоя память: текущий стратегический план и задачи (открытые и закрытые) с датами проверки"),
    "save_memory": (_t_save, "Сохранить обновлённый план и задачи. Параметры: plan (текст стратегии), "
                             "new_tasks [{title, kind: goal|task|hypothesis, metric, check_date YYYY-MM-DD, reasoning}], "
                             "close_tasks [{id, status: done|failed|dropped, result}]"),
}


def _tool_schemas() -> list[dict]:
    out = []
    for name, (_fn, desc) in _TOOLS.items():
        schema = {"type": "object", "properties": {}}
        if name == "trends":
            schema["properties"] = {"source": {"type": "string"},
                                    "filter": {"type": "string"}}
        elif name == "wb_search":
            schema["properties"] = {"query": {"type": "string"}}
        elif name == "repricer_propose":
            schema["properties"] = {
                "items": {"type": "array", "items": {"type": "object"}},
                "reason": {"type": "string"}}
        elif name == "save_memory":
            schema["properties"] = {
                "plan": {"type": "string"},
                "new_tasks": {"type": "array", "items": {"type": "object"}},
                "close_tasks": {"type": "array", "items": {"type": "object"}}}
        out.append({"name": name, "description": desc, "input_schema": schema})
    return out


_SYSTEM = """Ты — стратег-директор по маркетплейсам кабинета Biomed Nutrition
(WB + Ozon + ЯМ: косметика AL, интим-товары BMN/ST/SATISFUCKTION, спортпит).
У владельца своё производство, цикл запуска нового SKU 1-3 месяца.

""" + _ar.KNOWLEDGE + """

КАК РАБОТАЕШЬ:
1. Сначала ВСЕГДА читай память (memory) — там твой план и задачи с прошлых сессий.
2. Смотри данные инструментами по необходимости — не тяни всё подряд, иди от
   вопросов: что изменилось? что с задачами, у которых подошла дата проверки?
3. По каждой задаче с подошедшей датой — сверь план/факт по её метрике и закрой
   (done/failed) с честным выводом, ПОЧЕМУ сработало или нет.
4. Обнови план и поставь новые задачи через save_memory (обязательно перед
   финальным ответом). Задач в работе держи 3-7, не распыляйся.
РЕПРАЙСЕР — ТВОЯ ЗОНА ОТВЕТСТВЕННОСТИ ПО ЦЕНАМ: в конфиге целевые цены =
ФАКТИЧЕСКИЕ цены для покупателя из кабинетов (как есть). Твоя работа —
регулярно проверять их против юнитки (margin_at_target), цен конкурентов
(competitors, wb_search), спроса (trends, ozon_search, sales_daily) и класть
КОРРЕКТИРОВКИ через repricer_propose с числовым обоснованием why по каждому
артикулу. Владелец одобряет/отклоняет на вкладке Репрайсер; каждое одобренное
решение автоматически становится твоей задачей «проверить эффект через
7 дней» — сверяй её честно: не сработало = failed с выводом. Не предлагай
изменения ради изменений: если цена оптимальна — так и говори.
ЦЕНА И СПРОС: прежде чем предлагать изменение цены, просчитай сценарий
инструментом simulate (он покажет объём, выручку и прибыль при новой цене
и диапазон неопределённости) и проверь инструмент elasticity — там видно, как спрос реально реагировал на прошлые изменения
у этого SKU. Если реакции почти нет, запас поднять есть; если спрос
чувствителен, шаги мельче и с проверкой. Без этих данных цену трогать
не предлагай — это спор вслепую.
ИСТОЧНИК ЦЕН: любые числа «цена сейчас/сегодня» бери ТОЛЬКО из блока
АКТУАЛЬНЫЕ ЦЕНЫ в сообщении сессии (живой API) или инструмента prices.
Цены внутри unit_economics — средние за окно усреднения, после правок
владельца/репрайсера они отстают ДНЯМИ; называть их текущими запрещено.
Если живая цена и юнитка расходятся — явно скажи об этом и считай
экономику от живой цены.
ЖЕЛЕЗНЫЕ ПРАВИЛА ЦЕН: (1) низкая цена относительно конкурентов может быть
ОСОЗНАННОЙ стратегией владельца (ценовое лидерство = объём = отзывы = топ
выдачи; так построен топ-1 по фистам) — «конкуренты дороже» само по себе НЕ
аргумент поднимать; (2) корректировки — ступенями не более 10-15% за шаг с
проверкой темпа 5-7 дней, резкие подъёмы в 1.5-2 раза не предлагай никогда,
смену ценовой стратегии может инициировать только владелец; (3) чинить
битые конфигурации (target ниже минималки) — можно и нужно, но выравнивай
к текущей фактической цене, а не к ценам конкурентов. Помни:
целевая цена — ДЛЯ ПОКУПАТЕЛЯ (после всех скидок), не цена продавца;
воскресные просадки цен — акционный день репрайсера, это норма.
АКЦИИ ПЛОЩАДОК: инструмент promos показывает, в каких акциях Ozon состоят
товары (auto_action = Ozon добавляет сам — проверяй, не режет ли маржу
лидеров) и календарь акций WB. Входить в акцию имеет смысл, когда скидка
окупается буст-трафиком (обычно: высокомаржинальные SKU с запасом стока);
выводить — когда акция режет маржу без прироста темпа (это уже случалось
с BMN-0115). Вход/выход из акций — только рекомендация владельцу.

5. ФАКТЫ ОТ ВЛАДЕЛЬЦА — если он сообщает контекст, которого нет в данных
   (работает репрайсер, воскресенье — акционный день, сезонность, договорённости
   с поставщиками, планы производства) — ОБЯЗАТЕЛЬНО сохрани через save_memory
   как new_tasks с kind='note' без check_date. Эти заметки — твоя база знаний
   о бизнесе: учитывай их в каждом анализе (например, не паникуй из-за
   «странного» падения цены в воскресенье, если это акционный день) и не
   закрывай их, пока владелец не скажет, что факт устарел.

ФОРМАТ РЕШЕНИЙ — каждая рекомендация обязана содержать:
• Решение (конкретное действие с числами)
• На чём основано (конкретные цифры из данных, которые ты СЕЙЧАС посмотрел)
• Риск/цена бездействия
• Альтернативы, которые отверг и почему
• Как проверим (метрика + дата)
Если данным не доверяешь (устаревший себес, неполная неделя) — скажи прямо.

ФИНАЛЬНЫЙ ОТВЕТ — сообщение владельцу в Telegram (HTML-теги <b> допустимы).
Для полной сессии: краткая сводка → план/факт → решения → что мониторишь.
Для вопроса: просто ответ на вопрос.
СТИЛЬ — САМОЕ ВАЖНОЕ ПРАВИЛО. Ты пишешь в Telegram занятому владельцу,
а не отчёт инвесторам. Хлёстко, умно, по делу. Полотно из цифр — провал,
даже если цифры верные.

Формула ответа на вопрос:
1) Первая строка — ВЫВОД одним предложением (что делать / что происходит).
2) Обоснование — 2-4 коротких абзаца, В КАЖДОМ не больше одной-двух цифр,
   и только тех, что двигают решение. Цифра без вывода из неё — мусор.
3) Последняя строка — следующий шаг или вопрос владельцу, если выбор за ним.

Ограничения: ответ на вопрос — до 120 слов ЖЁСТКО; регулярный отчёт — до
250. Всё, что накопал сверх — не вываливай: держи в памяти (save_memory)
или предложи «могу разобрать глубже — скажи».
Не пересказывай, КАК считал и какие инструменты смотрел. Запрещено писать
«отвечаю из кеша», «данные в снимке есть», «вопрос — продолжение разговора»
и любые описания собственной кухни: владельцу нужен ответ, а не отчёт о
процессе. Названия инструментов (promos, money, adv_bids и прочие) — твоя
внутренняя кухня, в тексте их не упоминай: пиши «в акциях Ozon», «в разделе
Где деньги», «в рекламе».
НЕ ПРЕДЛАГАЙ ТО, ЧТО МОЖЕШЬ СДЕЛАТЬ САМ. Если для ответа нужно ещё раз
посмотреть данные — посмотри и ответь. «Хочешь, прогоню…» уместно только
там, где нужно РЕШЕНИЕ владельца или дорогая операция (живая выдача WB,
глубокая сессия), а не для обычного чтения.
Не дублируй одну мысль в разных формулировках.
Цепочки типа «96→91→70→79→54» не пиши — скажи словами «темп упал втрое
за неделю» и дай одну опорную цифру.

Тон: опытный коллега-директор, на «ты», живой деловой язык. ЭМОДЗИ НЕ
ИСПОЛЬЗУЙ ВООБЩЕ. Без канцелярита («данный», «осуществляется», «в рамках»),
без заголовков-штампов «Решение/Основано/Риск». Каждая законченная мысль —
с новой строки, блоки разделяй пустой строкой, абзац 1-2 предложения.
ДРР ВСЕГДА В ПАРЕ С БЕЗУБЫТОЧНЫМ. Число ДРР само по себе ничего не значит:
26% может быть нормой при безубыточном 40% и катастрофой при 12%. Никогда
не пиши «реклама в норме» или «ДРР высокий», не назвав рядом безубыточный
ДРР этого товара или кабинета. Если по одним SKU реклама льёт в минус, а
в целом по кабинету ДРР приемлемый — так и скажи, без общего вердикта
«всё хорошо».
ОБЪЁМ ПРОДАЖ: прежде чем считать эффект от цены, сверь объём со свежим
темпом (инструмент simulate сам подставляет факт последних 14 дней и
пишет volume_note). Если темп падает, любые расчёты «прибыль вырастет на
столько-то» бессмысленны — сначала причина падения, потом цена.
ДЕЙСТВИЯ: сам ничего в кабинете не меняешь. Если нужно поменять ставку,
поставить минус-фразы, остановить сливающую кампанию или изменить цену —
вызывай propose_action: заявка уйдёт владельцу с кнопками «Применить» и
«Отклонить». Предлагай только когда цифры на руках и выгода считается;
одна заявка — одно понятное действие, в reason назови цифру, из которой
оно следует. Если владелец уже отклонял похожее (см. память, kind=note) —
не предлагай снова, а спроси, что изменить."""


_TOOL_RU = {"unit_economics": "юнитка", "stocks": "остатки", "pnl": "P&L",
            "advertising": "реклама", "adv_bids": "ставки CPM",
            "prices": "цены", "sales_daily": "продажи",
            "reviews": "отзывы", "ozon_ads": "реклама Ozon",
            "ozon_phrases": "фразы Ozon", "funnel": "воронка Ozon",
            "clusters": "кластеры", "pnl_all": "P&L площадок",
            "history": "история продаж",
            "propose_action": "заявка на действие",
            "money": "где деньги", "elasticity": "эластичность цены",
            "simulate": "симулятор сценария", "news": "новости площадок", "dashboard_catalog": "каталог API", "dashboard_api": "данные дашборда",
            "competitors": "конкуренты", "trends": "тренды",
            "ozon_search": "поиск Ozon", "repricer": "репрайсер",
            "repricer_propose": "предложения цен", "wb_search": "выдача WB",
            "promos": "акции", "fbs_compare": "FBS-расчёт", "memory": "память",
            "save_memory": "запись памяти"}


_queue_lock = asyncio.Lock()   # сессии по очереди, а не «сессия уже идёт»
_queued = [0]                  # сколько ждёт в очереди (для честного статуса)


async def run_session(trigger: str = "manual", focus: str = "",
                      light: bool = False, status_msg_id: int | None = None,
                      chat: str = "", thread: int | None = None,
                      image_b64: str | None = None) -> dict:
    """Единая сессия агента: цикл с инструментами → ответ в чат, откуда спросили.

    light=True — быстрый режим для вопросов: минимум инструментов, память
    по необходимости, без обязательного save_memory.
    chat/thread — куда отвечать (по умолчанию основной чат).
    Параллельные запросы не отбиваются, а ждут очереди."""
    global _running
    if not ANTHROPIC_API_KEY:
        return {"error": "нет ANTHROPIC_API_KEY"}
    if _running and status_msg_id:
        await _ar.tg_edit(status_msg_id, chat_id=chat, text=
                          f"Агент занят другой задачей — ты {_queued[0] + 1}-й "
                          "в очереди, начну сразу как освобожусь…")
    _queued[0] += 1
    async with _queue_lock:
        _queued[0] -= 1
        return await _run_session_locked(trigger, focus, light, status_msg_id,
                                         chat, thread, image_b64)


async def _run_session_locked(trigger, focus, light, status_msg_id,
                              chat, thread, image_b64) -> dict:
    global _running
    _running = True
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        today = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d (%A)")
        user_msg = f"Сегодня {today}. Триггер сессии: {trigger}."
        if light and focus:
            user_msg += (f"\nБЫСТРЫЙ РЕЖИМ — вопрос владельца: {focus}\n"
                         "Ответь именно на вопрос МАКСИМУМ за 2 обращения к "
                         "инструментам (лучше 1); wb_search НЕ используй, если "
                         "вопрос решается кешированными данными; память читай "
                         "только если вопрос про план/задачи/факты; save_memory "
                         "— только если появилось что сохранить. Ответ короткий.")
        elif focus:
            user_msg += f"\nФокус этой сессии: {focus}"
        else:
            user_msg += ("\nПроведи регулярную стратегическую сессию: память → "
                         "данные → план/факт → решения → обнови память → отчёт.")
        # снимок кабинета: общая картина до первого вопроса, чтобы агент не
        # гадал вслепую, какой инструмент дёрнуть
        try:
            import agent_digest as _dg
            _snapshot = await _dg.get()
            try:
                import money as _mn
                _mm = await _mn.get()
                if _mm.get("total"):
                    _top = "; ".join(f"{f['title']} ({f['amount']} ₽)"
                                     for f in (_mm.get("findings") or [])[:3])
                    _snapshot += (f"\nГДЕ ДЕНЬГИ: найдено утечек и упущенного на "
                                  f"{_mm['total']} ₽/мес. Крупнейшее: {_top}. "
                                  "Полный список — инструмент money.")
            except Exception:
                pass
            if _snapshot:
                user_msg += ("\n\n" + _snapshot +
                             "\n(Это сводка. За деталями иди в инструменты; "
                             "если нужных данных нет — dashboard_catalog и "
                             "dashboard_api дают доступ ко всему дашборду.)")
        except Exception as _e:
            _log.warning("digest: %s", str(_e)[:150])
        # живые цены — ВСЕГДА в контексте: юнитка усредняет за окно и врёт
        # про «сейчас», из-за этого стратег уже давал советы по старым ценам
        try:
            _pr = await _ar._prices_context()
        except Exception:
            _pr = ""
        try:
            import ozon_client as _oz
            _ozp = await _oz.get_prices()
            if _ozp:
                _pr += "\nЦЕНЫ OZON СЕЙЧАС (живой API, цена продавца): " + \
                    json.dumps({a: p.get("price") for a, p in _ozp.items()},
                               ensure_ascii=False)
        except Exception:
            pass
        if _pr:
            user_msg += (
                "\n\nАКТУАЛЬНЫЕ ЦЕНЫ (живой API, единственный источник «цены "
                "сейчас»; цены из юнитки/unit_economics — средние за окно "
                "усреднения, они ОТСТАЮТ после правок и для текущих цен "
                "непригодны; клиентская цена WB = цена продавца × коэффициент "
                "buyer/seller из репрайсера):\n" + _pr[:5000])
        # экономика, пересчитанная сервером по живой цене — чтобы стратег не
        # цитировал устаревшие прибыль/маржу из юнитки
        try:
            import wb_client as _wb
            from routers import tools as _tools
            _live = await _wb.get_current_prices()
            _mdata = await _tools.get_margin(mp="WB")
            _rows = []
            for b in _mdata.get("items") or []:
                sku = str(b.get("sku"))
                lp = (_live.get(sku) or {}).get("discounted")
                old = b.get("price0") or 0
                if not lp or not old:
                    continue
                m = _ar._margin_math({**b, "price0": lp})
                ratio = (b.get("buyer0") or 0) / old
                _rows.append({
                    "sku": sku, "seller_now": lp,
                    "buyer_now": round(lp * ratio) if ratio else None,
                    "profit_unit": m["profit_unit"],
                    "margin_pct": m["margin_pct"],
                    "be_drr": m["be_drr_pct"],
                    "seller_avg_unit_econ": old})
            if _rows:
                user_msg += (
                    "\n\nЭКОНОМИКА WB ПО ЖИВЫМ ЦЕНАМ (пересчитана сервером: "
                    "прибыль на штуку, маржа и безубыточный ДРР при ТЕКУЩЕЙ "
                    "цене продавца; в разговоре о прибыли/марже/убытке "
                    "используй ТОЛЬКО эти числа — из unit_economics бери лишь "
                    "затраты и объёмы; seller_avg_unit_econ показан только "
                    "чтобы видеть, насколько юнитка отстала): "
                    + json.dumps(_rows, ensure_ascii=False))
        except Exception as e:
            _log.warning("strategist live margin: %s", str(e)[:150])
        # диалоговая память: последние обмены /strategy (переживает рестарт)
        import snapshot as _snap
        _dlg_key = f"strategist_dialog{('_c' + chat) if chat else ''}"
        dialog = await asyncio.to_thread(_snap.load, _dlg_key, None) or []
        if dialog and focus:
            recent = "\n".join(f"- Владелец: {d['q']}\n  Ты ответил: {d['a']}"
                                for d in dialog[-4:])
            user_msg += ("\n\nНЕДАВНИЙ ДИАЛОГ. Если сейчас пришла короткая "
                         "реплика вроде «прикинь», «давай», «да», «го», "
                         "«дальше» — это СОГЛАСИЕ на то, что ты сам предложил "
                         "в конце прошлого ответа: выполни это предложение, "
                         "не переспрашивай и не начинай новую тему. "
                         "(Короткие реплики владельца — "
                         "обычно уточнение к нему; «это озон» после записи факта "
                         "= поправь тот факт, а не новое исследование):\n" + recent)
        if image_b64:
            messages = [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/jpeg",
                                             "data": image_b64}},
                {"type": "text", "text": user_msg}]}]
        else:
            messages = [{"role": "user", "content": user_msg}]
        tools_used, saved = [], False
        started = asyncio.get_event_loop().time()
        for _step in range(_MAX_STEPS):
            global _cancel
            if _cancel:
                _cancel = False
                _log.info("strategist: остановлен стоп-словом")
                if status_msg_id:
                    await _ar.tg_edit(status_msg_id, "Остановлен стоп-словом.",
                                      chat_id=chat)
                return {"ok": True, "stopped": True}
            if asyncio.get_event_loop().time() - started > 1500:
                raise TimeoutError("сессия дольше 25 минут — прервана")
            try:      # «печатает…» в группе — видно, что стратег жив
                import httpx as _hx
                async with _hx.AsyncClient(timeout=8) as _c:
                    await _c.post(
                        f"https://api.telegram.org/bot{_ar.TG_BOT_TOKEN}/sendChatAction",
                        json={"chat_id": chat or _ar.TG_CHAT_ID,
                              "action": "typing",
                              **({"message_thread_id": thread} if thread else {})})
            except Exception:
                pass
            _log.info("strategist: шаг %d, инструменты: %s", _step + 1,
                      ",".join(tools_used[-4:]) or "—")
            msg = await client.messages.create(
                model=_MODEL, max_tokens=700 if light else 2500, system=_SYSTEM,
                tools=_tool_schemas(), messages=messages)
            if msg.stop_reason != "tool_use":
                report = "".join(b.text for b in msg.content
                                 if getattr(b, "type", "") == "text").strip()
                if not saved and not light:
                    _log.warning("strategist: сессия без save_memory")
                if status_msg_id:
                    _secs = int(asyncio.get_event_loop().time() - started)
                    await _ar.tg_edit(
                        status_msg_id, chat_id=chat, text=
                        f"Готово  {'▰' * 10} 100% · "
                        f"{_secs // 60}:{_secs % 60:02d}")
                if report:
                    for i in range(0, len(report), 3900):
                        await _ar.tg_send(report[i:i + 3900],
                                          chat_id=chat, thread_id=thread)
                    if focus:
                        dialog.append({"q": focus[:300], "a": report[:500]})
                        await asyncio.to_thread(_snap.save, _dlg_key, dialog[-8:])
                _log.info("strategist: сессия ок, шагов %d, инструменты: %s",
                          _step + 1, ",".join(tools_used))
                return {"ok": True, "steps": _step + 1, "tools": tools_used,
                        "report": report[:500]}
            messages.append({"role": "assistant", "content": msg.content})
            blocks = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
            for b in blocks:
                tools_used.append(b.name)
                if b.name == "save_memory":
                    saved = True
            if status_msg_id and blocks:
                names = " → ".join(_TOOL_RU.get(n, n) for n in tools_used[-8:])
                # шкала: ожидаемо ~3 шага в быстром режиме, ~10 в полном;
                # 95% — потолок, пока ответ не готов
                exp = 3 if light else 10
                pct = min(95, int((_step + 1) / exp * 100))
                bar = "▰" * round(pct / 10) + "▱" * (10 - round(pct / 10))
                secs = int(asyncio.get_event_loop().time() - started)
                await _ar.tg_edit(
                    status_msg_id, chat_id=chat, text=
                    f"Агент работает  {bar} ~{pct}%\n"
                    f"Шаг {_step + 1} · {secs // 60}:{secs % 60:02d} · "
                    f"смотрю: {names}…")

            async def _run_tool(b):
                fn = _TOOLS.get(b.name, (None, ""))[0]
                try:
                    return await fn(b.input or {}) if fn else f"неизвестный инструмент {b.name}"
                except Exception as e:
                    return f"ошибка инструмента: {str(e)[:300]}"
            outs = await asyncio.gather(*[_run_tool(b) for b in blocks])
            results = [{"type": "tool_result", "tool_use_id": b.id,
                        "content": str(o)[:_TOOL_TRIM]}
                       for b, o in zip(blocks, outs)]
            messages.append({"role": "user", "content": results})
        return {"error": f"превышен лимит шагов ({_MAX_STEPS})"}
    except Exception as e:
        _log.error("strategist: %s", e)
        try:
            await _ar.tg_send(f"Агент упал: {str(e)[:200]}",
                              chat_id=chat, thread_id=thread)
        except Exception:
            pass
        return {"error": str(e)[:300]}
    finally:
        _running = False


def due_tasks() -> list[dict]:
    """Открытые задачи, у которых подошла дата проверки."""
    today = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")
    return [t for t in _tasks_load("open")
            if t.get("check_date") and t["check_date"] <= today]
