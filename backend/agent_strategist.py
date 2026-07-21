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
    "prices": (_t_prices, "Текущие цены наших товаров и история изменений за 14 дней"),
    "sales_daily": (_t_sales, "Продажи по дням за 14 дней по площадкам + по SKU за 7 дней"),
    "competitors": (_t_competitors, "Срезы выдачи WB по нашим запросам: позиции, цены и прирост отзывов конкурентов"),
    "trends": (_t_trends, "Радар трендов Ozon: нарастающие поисковые запросы. Параметры: source='my'|'market', filter='слова'"),
    "ozon_search": (_t_ozon_queries, "Поиск Ozon по нашим товарам: частота, наша позиция, конверсия, GMV из поиска"),
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

ФИНАЛЬНЫЙ ОТВЕТ — отчёт владельцу в Telegram (HTML-теги <b> допустимы):
краткая сводка состояния → план/факт по задачам → 2-4 решения в формате выше →
что мониторишь до следующей сессии. Пиши по-русски, деловым тоном, без воды.
Деньгами не распоряжаешься: всё, что меняет цены/ставки/поставки — только
рекомендация владельцу."""


async def run_session(trigger: str = "manual", focus: str = "") -> dict:
    """Полная стратегическая сессия: цикл с инструментами → отчёт в TG."""
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
        if focus:
            user_msg += f"\nФокус этой сессии: {focus}"
        else:
            user_msg += ("\nПроведи регулярную стратегическую сессию: память → "
                         "данные → план/факт → решения → обнови память → отчёт.")
        messages = [{"role": "user", "content": user_msg}]
        tools_used, saved = [], False
        started = asyncio.get_event_loop().time()
        for _step in range(_MAX_STEPS):
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
                model=_MODEL, max_tokens=3000, system=_SYSTEM,
                tools=_tool_schemas(), messages=messages)
            if msg.stop_reason != "tool_use":
                report = "".join(b.text for b in msg.content
                                 if getattr(b, "type", "") == "text").strip()
                if not saved:
                    _log.warning("strategist: сессия без save_memory")
                if report:
                    for i in range(0, len(report), 3900):
                        await _ar.tg_send(report[i:i + 3900])
                _log.info("strategist: сессия ок, шагов %d, инструменты: %s",
                          _step + 1, ",".join(tools_used))
                return {"ok": True, "steps": _step + 1, "tools": tools_used,
                        "report": report[:500]}
            messages.append({"role": "assistant", "content": msg.content})
            results = []
            for block in msg.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                fn = _TOOLS.get(block.name, (None, ""))[0]
                tools_used.append(block.name)
                if block.name == "save_memory":
                    saved = True
                try:
                    out = await fn(block.input or {}) if fn else f"неизвестный инструмент {block.name}"
                except Exception as e:
                    out = f"ошибка инструмента: {str(e)[:300]}"
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(out)[:_TOOL_TRIM]})
            messages.append({"role": "user", "content": results})
        return {"error": f"превышен лимит шагов ({_MAX_STEPS})"}
    except Exception as e:
        _log.error("strategist: %s", e)
        try:
            await _ar.tg_send(f"⚠️ Стратег упал: {str(e)[:200]}")
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
