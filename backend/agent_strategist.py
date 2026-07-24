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
Не пересказывай, КАК считал и какие инструменты смотрел.
Не дублируй одну мысль в разных формулировках.
Цепочки типа «96→91→70→79→54» не пиши — скажи словами «темп упал втрое
за неделю» и дай одну опорную цифру.

Тон: опытный коллега-директор, на «ты», живой деловой язык. ЭМОДЗИ НЕ
ИСПОЛЬЗУЙ ВООБЩЕ. Без канцелярита («данный», «осуществляется», «в рамках»),
без заголовков-штампов «Решение/Основано/Риск». Каждая законченная мысль —
с новой строки, блоки разделяй пустой строкой, абзац 1-2 предложения.
Деньгами не распоряжаешься: всё, что меняет цены/ставки/поставки — только
рекомендация владельцу."""


_TOOL_RU = {"unit_economics": "юнитка", "stocks": "остатки", "pnl": "P&L",
            "advertising": "реклама", "adv_bids": "ставки CPM",
            "prices": "цены", "sales_daily": "продажи",
            "competitors": "конкуренты", "trends": "тренды",
            "ozon_search": "поиск Ozon", "repricer": "репрайсер",
            "repricer_propose": "предложения цен", "wb_search": "выдача WB",
            "promos": "акции", "fbs_compare": "FBS-расчёт", "memory": "память",
            "save_memory": "запись памяти"}


async def run_session(trigger: str = "manual", focus: str = "",
                      light: bool = False, status_msg_id: int | None = None) -> dict:
    """Стратегическая сессия: цикл с инструментами → отчёт/ответ в TG.

    light=True — быстрый режим для вопросов: минимум инструментов, память
    по необходимости, без обязательного save_memory."""
    global _running
    if not ANTHROPIC_API_KEY:
        return {"error": "нет ANTHROPIC_API_KEY"}
    if _running:
        return {"error": "сессия уже идёт"}
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
        dialog = await asyncio.to_thread(_snap.load, "strategist_dialog", None) or []
        if dialog and focus:
            recent = "\n".join(f"- Владелец: {d['q']}\n  Ты ответил: {d['a']}"
                                for d in dialog[-4:])
            user_msg += ("\n\nНЕДАВНИЙ ДИАЛОГ (короткие реплики владельца — "
                         "обычно уточнение к нему; «это озон» после записи факта "
                         "= поправь тот факт, а не новое исследование):\n" + recent)
        messages = [{"role": "user", "content": user_msg}]
        tools_used, saved = [], False
        started = asyncio.get_event_loop().time()
        for _step in range(_MAX_STEPS):
            global _cancel
            if _cancel:
                _cancel = False
                _log.info("strategist: остановлен стоп-словом")
                if status_msg_id:
                    await _ar.tg_edit(status_msg_id, "Остановлен стоп-словом.")
                return {"ok": True, "stopped": True}
            if asyncio.get_event_loop().time() - started > 1500:
                raise TimeoutError("сессия дольше 25 минут — прервана")
            try:      # «печатает…» в группе — видно, что стратег жив
                import httpx as _hx
                async with _hx.AsyncClient(timeout=8) as _c:
                    await _c.post(
                        f"https://api.telegram.org/bot{_ar.TG_BOT_TOKEN}/sendChatAction",
                        json={"chat_id": _ar.TG_CHAT_ID, "action": "typing"})
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
                        status_msg_id,
                        f"Готово  {'▰' * 10} 100% · "
                        f"{_secs // 60}:{_secs % 60:02d}")
                if report:
                    for i in range(0, len(report), 3900):
                        await _ar.tg_send(report[i:i + 3900])
                    if focus:
                        dialog.append({"q": focus[:300], "a": report[:500]})
                        await asyncio.to_thread(_snap.save, "strategist_dialog",
                                                dialog[-8:])
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
                    status_msg_id,
                    f"Стратег работает  {bar} ~{pct}%\n"
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
            await _ar.tg_send(f"Стратег упал: {str(e)[:200]}")
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
