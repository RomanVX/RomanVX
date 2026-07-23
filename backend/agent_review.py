"""Агент-аналитик: еженедельный разбор кабинета → Telegram.

Собирает готовые данные проекта (юнитка/маржа/P&L/тарифы), прогоняет через
Claude в роли финдиректора и отправляет разбор в Telegram (личку или группу —
куда указывает TG_CHAT_ID; для группы бота нужно в неё добавить).

ENV: TG_BOT_TOKEN (от @BotFather), TG_CHAT_ID (id чата/группы; у групп
он отрицательный — можно узнать, написав боту и открыв
https://api.telegram.org/bot<TOKEN>/getUpdates).
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import httpx

from config import ANTHROPIC_API_KEY

_log = logging.getLogger("agent_review")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
_MODEL = "claude-opus-4-8"


def configured() -> bool:
    return bool(TG_BOT_TOKEN and TG_CHAT_ID and ANTHROPIC_API_KEY)


# ── База знаний агента: экспертиза по маркетплейсам ───────────────────────────
KNOWLEDGE = """ЭКСПЕРТИЗА (применяй в ответах, не пересказывай без надобности):

ЦЕНЫ И СПП (WB): продавец назначает цену ДО СПП (price_seller) — от неё
считаются выручка и комиссия. СПП — скидка за счёт WB, покупатель платит
price_buyer. Соотношение buyer/seller у SKU стабильно: новая цена продавца
≈ новая клиентская ÷ (buyer/seller). Повышение цены при том же рекламном
бюджете в ₽ механически снижает ДРР и повышает безубыточный ДРР.
На вопрос «какая цена сейчас/сегодня» отвечай из блока «ЦЕНЫ ПРОДАВЦА
СЕЙЧАС» (живой API) и «ИЗМЕНЕНИЯ ЦЕН» — цены в юнитке средние за окно и
отстают, для текущей цены их НЕ использовать.

КОМИССИЯ WB: своя у каждой категории, канал FBW (склад WB) ≠ FBS. 07.07.2026
WB поднял комиссии на +6-9 пп почти по всем категориям — сравнения маржи
«до/после июля» должны это учитывать. Эквайринг ~1.5-2% сверху.

РЕКЛАМА WB: типы — АРК/автокампании и поиск (аукцион ставок). Ключевая
метрика — ДРР (расход/выручка с рекламы). Правило: ДРР выше безубыточного
(be_drr) = торговля в минус. Лечение по порядку: 1) минус-фразы на запросы
с показами но без конверсий; 2) снижение ставок ступенями 20-30% с контролем
позиций (резкое отключение роняет и органику — карточка теряет буст);
3) перераспределение бюджета на SKU с запасом ДРР (be_drr − факт > 15 пп —
можно масштабировать). Реклама влияет на органическую выдачу: выкупы и
конверсия с рекламы поднимают позиции карточки.

ЛОГИСТИКА/ХРАНЕНИЕ WB: логистика списывается за каждую доставку (включая
невыкупы — процент выкупа критичен для маржи), хранение — от объёма и
коэффициента склада. Штрафы и удержания — разовые, в юнитке сглаживаются.

ОТЧЁТНОСТЬ WB: отчёт реализации формируется РАЗ В НЕДЕЛЮ (пн за прошлую
неделю) — свежие дни в точных данных отсутствуют, хвост добирается
оперативными продажами. История продаж в API — только 90 дней.
Для вопросов «сегодня/вчера/темп по дням» используй блоки «ПРОДАЖИ ПО
ДНЯМ» и «WB ПО SKU ПО ДНЯМ» — это оперативные данные с обновлением раз
в 30 минут. Сегодняшний день всегда неполный: сравнивай завершённые дни,
а про сегодня говори «на текущий момент столько-то».

ОСТАТКИ: дни запаса = остаток / темп продаж. ≤20 дней — срочно в поставку
(поставка на склад WB едет 3-10 дней); нулевой остаток у маржинального SKU —
потерянная прибыль и падение позиций карточки (алгоритм понижает out-of-stock).

OZON: Performance-реклама отдельным кабинетом, числа приходят строками с
запятой; воронка «показы→корзина→заказ» не строгая (доли могут быть >100%).
Премиум-аналитика даёт позиции и конверсии по кластерам.

ОТЗЫВЫ: рейтинг ниже 4.7 заметно режет конверсию; на негатив отвечать быстро
и по существу; частые темы минусов = задачи на доработку продукта/упаковки.

ПРИНЦИПЫ РЕКОМЕНДАЦИЙ: приоритет по деньгам в месяц; сначала заткнуть
убытки, потом масштабировать лидеров; факт прошлого месяца и прогноз
называть отдельно; свежие изменения цен/ставок в средних данных ещё не
видны — оговаривать; не советовать резких движений без проверки на 3-5 днях."""


async def tg_send_id(text: str, thread_id: int | None = None) -> int | None:
    """Как tg_send, но возвращает message_id (для последующего edit)."""
    if not TG_BOT_TOKEN:
        return None
    body = {"chat_id": TG_CHAT_ID, "text": text[:3900], "parse_mode": "HTML"}
    if thread_id:
        body["message_thread_id"] = thread_id
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                         json=body)
    try:
        return (r.json().get("result") or {}).get("message_id")
    except Exception:
        return None


async def tg_edit(message_id: int, text: str) -> None:
    if not TG_BOT_TOKEN or not message_id:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageText",
                         json={"chat_id": TG_CHAT_ID, "message_id": message_id,
                               "text": text[:3900], "parse_mode": "HTML"})
    except Exception:
        pass


async def tg_send(text: str, chat_id: str = "", thread_id: int | None = None) -> bool:
    """Отправка в Telegram; длинные тексты режутся по 3900 символов.
    thread_id — тема в группе-форуме (отвечаем туда, откуда спросили)."""
    if not TG_BOT_TOKEN:
        return False
    cid = chat_id or TG_CHAT_ID
    ok = True
    async with httpx.AsyncClient(timeout=30) as c:
        for i in range(0, len(text), 3900):
            body = {"chat_id": cid, "text": text[i:i + 3900],
                    "parse_mode": "HTML", "disable_web_page_preview": True}
            if thread_id:
                body["message_thread_id"] = thread_id
            r = await c.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", json=body)
            if r.status_code != 200:
                _log.warning("tg_send: %s %s", r.status_code, r.text[:200])
                ok = False
    return ok


def _margin_math(b: dict) -> dict:
    """Прибыль/маржа на штуку — та же математика, что в калькуляторе на фронте."""
    price = b.get("price0") or 0
    drr = round(b["advert"] / price * 100, 1) if price else 0
    pct = (b.get("comm_pct", 0) + b.get("acq_pct", 0) + drr) / 100
    fixed = b.get("logist", 0) + b.get("storage", 0) + b.get("other", 0) + b.get("cogs", 0)
    profit = price * (1 - pct) - fixed
    qty = b.get("qty_f") if b.get("qty_f") is not None else b.get("qty_m", 0)
    be_drr = (price * (1 - (b.get("comm_pct", 0) + b.get("acq_pct", 0)) / 100) - fixed) / price * 100 if price else 0
    return {"sku": b.get("sku"), "name": (b.get("name") or "")[:40],
            "price_seller": price,               # цена продавца, до СПП
            "price_buyer": b.get("buyer0"),      # цена для клиента, после СПП
            "profit_unit": round(profit),
            "margin_pct": round(profit / price * 100) if price else 0,
            "drr_pct": drr, "be_drr_pct": round(max(be_drr, 0), 1),
            "qty_month": round(qty or 0),
            "profit_month": round(profit * (qty or 0))}


async def build_review(mp: str = "WB") -> str | None:
    """Собирает данные и просит Claude сделать разбор. Возвращает текст или None."""
    from routers import tools as _tools
    from routers import finance as _fin
    import snapshot as _snap
    import asyncio

    data = await _tools.get_margin(mp=mp)
    items = data.get("items") or []
    if not items:
        _log.info("agent_review: юнитка ещё не готова (%s)", data.get("message"))
        return None
    rows = sorted((_margin_math(b) for b in items),
                  key=lambda x: x["profit_month"])
    total_profit = sum(r["profit_month"] for r in rows)
    total_rev = sum(r["price_seller"] * r["qty_month"] for r in rows)

    pnl_note = ""
    try:
        pnl = await _fin.get_wb_pnl(months=3)
        vals = {r["key"]: r.get("values") or {} for r in pnl.get("rows") or []}
        mks = sorted(vals.get("retailAmount", {}).keys())[-3:]
        if mks:
            pnl_note = json.dumps(
                [{"month": mk,
                  "revenue": vals.get("retailAmount", {}).get(mk),
                  "advert": vals.get("advert", {}).get(mk),
                  "gross_profit": vals.get("gross", {}).get(mk),
                  "margin_pct": vals.get("gross_pct", {}).get(mk)}
                 for mk in mks], ensure_ascii=False)
    except Exception as e:
        _log.warning("agent_review pnl: %s", e)

    tariff = await asyncio.to_thread(_snap.load, "wb_tariff_alert", None) or {}

    prompt = f"""Ты — финансовый директор селлера маркетплейсов. Сделай еженедельный
разбор кабинета {mp} для владельца. Пиши по-русски, кратко и по делу, без воды.

ПРО ЦЕНЫ: price_seller — цена продавца до скидки WB (СПП), база выручки и
комиссии; price_buyer — что платит покупатель после СПП (владелец называет её
«ценой для клиента»). Указывая цены в советах, давай обе.

ДАННЫЕ (прогноз на месяц по текущим ценам; profit_month — прогноз прибыли
артикула в месяц, be_drr_pct — безубыточный ДРР):
{json.dumps(rows, ensure_ascii=False)}

Итого прогноз: выручка {round(total_rev)} ₽/мес, прибыль {round(total_profit)} ₽/мес.
P&L последних месяцев: {pnl_note or "нет"}
Изменение тарифов WB: {json.dumps(tariff, ensure_ascii=False) if tariff else "не менялись"}

Формат ответа (Telegram, HTML: только <b> и <i>, без markdown, без заголовков-решёток):
1) Сводка — 2-3 предложения о состоянии кабинета.
2) 🔥 Главные проблемы — до 3, каждая с цифрой потерь.
3) ✅ Действия на неделю — 3-5 пунктов, каждый: что сделать + ожидаемый эффект в ₽.
Приоритезируй по деньгам. Если у артикула ДРР выше безубыточного — это всегда
проблема №1. Не выдумывай данных, которых нет."""

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    msg = await client.messages.create(
        model=_MODEL, max_tokens=1800, system=KNOWLEDGE,
        messages=[{"role": "user", "content": prompt}])
    text = msg.content[0].text.strip()
    head = f"🤖 <b>Разбор кабинета {mp}</b> · {datetime.utcnow().strftime('%d.%m.%Y')}\n\n"
    return head + text


async def build_stocks_summary() -> str:
    """Расклад по остаткам — по каждой площадке отдельно:
    🔴 горящие (≤20 дней запаса), 🟡 на исходе (21–45)."""
    from routers import dashboard as _dash
    rows = await _dash.get_stocks_table()
    if not rows:
        return "Остатки ещё собираются — попробуй через минуту."

    out = ["📦 <b>Остатки по площадкам</b>"]
    for icon, title, pq, pd_, pdays in (
            ("🟣", "Wildberries", "wb_qty", "wb_per_day", "wb_days"),
            ("🔵", "Ozon", "oz_qty", "oz_per_day", "oz_days"),
            ("🟡", "Яндекс.Маркет", "ym_qty", "ym_per_day", "ym_days")):
        active = [r for r in rows if r.get(pq, 0) > 0 or r.get(pd_, 0) > 0]
        if not active:
            continue
        red = sorted((r for r in active if r.get(pdays, 999) <= 20),
                     key=lambda r: r.get(pdays, 999))
        yellow = sorted((r for r in active if 20 < r.get(pdays, 999) <= 45),
                        key=lambda r: r.get(pdays, 999))
        line = lambda r: (f"  • <b>{r.get('supplierArticle')}</b> "
                          f"{(r.get('name') or '')[:26]} — {r.get(pq, 0)} шт, "
                          f"{'∞' if r.get(pdays, 999) >= 999 else r.get(pdays)} дн")
        out.append(f"\n{icon} <b>{title}</b> — {len(active)} позиций, "
                   f"🔴 {len(red)} · 🟡 {len(yellow)}")
        if red:
            out.append("🔴 Горят (≤20 дн) — в ближайшую поставку:")
            out += [line(r) for r in red[:12]]
            if len(red) > 12:
                out.append(f"  …и ещё {len(red) - 12}")
        else:
            out.append("🔴 Горящих нет.")
        if yellow:
            out.append("🟡 На исходе (21–45 дн):")
            out += [line(r) for r in yellow[:8]]
            if len(yellow) > 8:
                out.append(f"  …и ещё {len(yellow) - 8}")
    return "\n".join(out)


async def accumulate_prices() -> None:
    """Ежедневный слепок цен WB в price_history (вызывается прогревом).
    Пишет строку раз в день на SKU — история для вопросов «а вчера?»."""
    import db
    import wb_client
    try:
        prices = await wb_client.get_current_prices()
        if not prices:
            return
        await asyncio.to_thread(db.execute, """CREATE TABLE IF NOT EXISTS price_history (
            day TEXT, sku TEXT, price REAL, discounted REAL,
            PRIMARY KEY (day, sku))""")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        rows = [(today, sku, p.get("price") or 0, p.get("discounted") or 0)
                for sku, p in prices.items()]
        if db.IS_PG:
            await asyncio.to_thread(db.executemany,
                "INSERT INTO price_history (day, sku, price, discounted) VALUES (?,?,?,?) "
                "ON CONFLICT(day, sku) DO UPDATE SET price=excluded.price, "
                "discounted=excluded.discounted", rows)
        else:
            await asyncio.to_thread(db.executemany,
                "INSERT OR REPLACE INTO price_history VALUES (?,?,?,?)", rows)
        _log.info("price_history: %d цен за %s", len(rows), today)
    except Exception as e:
        _log.warning("accumulate_prices: %s", e)


async def _prices_context() -> str:
    """Текущие цены (живой API) + история изменений за 14 дней."""
    import db
    import wb_client
    parts = []
    try:
        prices = await wb_client.get_current_prices()
        if prices:
            parts.append("ЦЕНЫ ПРОДАВЦА СЕЙЧАС (живой API, до СПП, после скидки продавца): "
                         + json.dumps({s: p["discounted"] for s, p in prices.items()},
                                      ensure_ascii=False))
    except Exception as e:
        _log.warning("prices ctx: %s", e)
    try:
        since = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
        rows = await asyncio.to_thread(
            db.fetchall,
            "SELECT day, sku, discounted FROM price_history WHERE day>=? ORDER BY day",
            (since,))
        hist: dict[str, list] = {}
        for day, sku, disc in rows:
            h = hist.setdefault(sku, [])
            if not h or h[-1][1] != disc:      # пишем только изменения
                h.append((str(day)[:10], disc))
        changes = {s: h for s, h in hist.items() if len(h) > 1}
        if changes:
            parts.append("ИЗМЕНЕНИЯ ЦЕН ЗА 14 ДНЕЙ (день, цена до СПП): "
                         + json.dumps(changes, ensure_ascii=False))
    except Exception as e:
        _log.warning("price history ctx: %s", e)
    return "\n".join(parts)


async def _daily_context() -> str:
    """Посуточная динамика продаж (sales_daily, обновляется каждые 30 мин):
    итоги по дням/платформам за 14 дней + WB по SKU за 7 дней."""
    import db
    parts = []
    try:
        since = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
        rows = await asyncio.to_thread(
            db.fetchall,
            "SELECT sale_date, platform, SUM(qty), SUM(revenue) FROM sales_daily "
            "WHERE sale_date>=? GROUP BY sale_date, platform ORDER BY sale_date", (since,))
        days: dict = {}
        for d, pf, q, rev in rows:
            days.setdefault(str(d)[:10], {})[pf] = {"qty": int(q or 0), "rev": round(rev or 0)}
        if days:
            parts.append("ПРОДАЖИ ПО ДНЯМ за 14 дней (сегодняшний день НЕПОЛНЫЙ, "
                         "обновление раз в 30 мин): " + json.dumps(days, ensure_ascii=False))
        since7 = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        rows2 = await asyncio.to_thread(
            db.fetchall,
            "SELECT sku, sale_date, SUM(qty) FROM sales_daily "
            "WHERE platform='WB' AND sale_date>=? GROUP BY sku, sale_date "
            "ORDER BY sale_date", (since7,))
        by_sku: dict = {}
        for sku, d, q in rows2:
            by_sku.setdefault(str(sku), {})[str(d)[:10]] = int(q or 0)
        if by_sku:
            parts.append("WB ПО SKU ПО ДНЯМ за 7 дней (шт): "
                         + json.dumps(by_sku, ensure_ascii=False))
    except Exception as e:
        _log.warning("daily ctx: %s", e)
    return "\n".join(parts)


async def _gather_context() -> str:
    """Всё, что агент знает о кабинете, одним текстом — контекст для вопросов."""
    from routers import tools as _tools
    from routers import finance as _fin
    import snapshot as _snap
    import asyncio

    parts = []
    try:
        data = await _tools.get_margin(mp="WB")
        items = data.get("items") or []
        if items:
            rows = [_margin_math(b) for b in items]
            parts.append("ЮНИТ-ЭКОНОМИКА ПО SKU (прогноз на месяц, ₽): "
                         + json.dumps(rows, ensure_ascii=False))
    except Exception as e:
        _log.warning("qa margin: %s", e)
    try:
        parts.append("ОСТАТКИ:\n" + (await build_stocks_summary())
                     .replace("<b>", "").replace("</b>", ""))
    except Exception as e:
        _log.warning("qa stocks: %s", e)
    try:
        pnl = await _fin.get_wb_pnl(months=3)
        vals = {r["key"]: r.get("values") or {} for r in pnl.get("rows") or []}
        parts.append("P&L ПО МЕСЯЦАМ: " + json.dumps(vals, ensure_ascii=False))
    except Exception as e:
        _log.warning("qa pnl: %s", e)
    try:
        adv = await _tools.get_adv()
        camps = [{"name": c.get("name"), "type": c.get("type"),
                  "active": c.get("active"), "spend": c.get("spend"),
                  "revenue": c.get("revenue"), "drr": c.get("drr"),
                  "orders": c.get("orders"), "verdict": c.get("verdict"),
                  "skus": c.get("skus") or c.get("arts")}
                 for c in (adv.get("campaigns") or [])]
        if camps:
            parts.append(
                f"РЕКЛАМА WB ПО КАМПАНИЯМ (за {adv.get('days')} дн; verdict "
                f"waste=сливает бюджет): итого расход {adv.get('total_spend')} ₽, "
                f"выручка с рекламы {adv.get('total_revenue')} ₽, общий ДРР "
                f"{adv.get('total_drr')}%, слив {adv.get('waste')} ₽. Кампании: "
                + json.dumps(camps, ensure_ascii=False))
    except Exception as e:
        _log.warning("qa adv: %s", e)
    tariff = await asyncio.to_thread(_snap.load, "wb_tariff_alert", None)
    if tariff:
        parts.append("ИЗМЕНЕНИЕ ТАРИФОВ WB: " + json.dumps(tariff, ensure_ascii=False))
    pc = await _prices_context()
    if pc:
        parts.append(pc)
    dc = await _daily_context()
    if dc:
        parts.append(dc)
    try:
        comp = await _tools.competitors_get()
        if comp.get("queries"):
            slim = []
            for qb in comp["queries"]:
                items = [{"pos": i["position"], "brand": i["brand"],
                          "price": i["price"], "price_prev": i.get("price_prev"),
                          "rating": i["rating"], "fb": i["feedbacks"],
                          "ours": i["is_ours"]} for i in qb["items"][:12]]
                slim.append({"query": qb["query"], "top": items})
            parts.append(f"КОНКУРЕНТЫ В ВЫДАЧЕ WB (срез {comp.get('day')}, "
                         f"prev={comp.get('prev_day')}; ours=наши карточки): "
                         + json.dumps(slim, ensure_ascii=False))
    except Exception as e:
        _log.warning("qa competitors: %s", e)
    try:
        import agent_strategist as _st
        mem = await _st._t_memory({})
        parts.append("СТРАТЕГИЯ (план и задачи агента-стратега; на вопросы "
                     "«какой план/что решили/какие задачи» отвечай отсюда): " + mem)
    except Exception as e:
        _log.warning("qa strategy: %s", e)
    return "\n\n".join(parts)


_qa_inflight: set = set()   # (thread, вопрос) — защита от дублей из очереди

# диалоговая память: тема группы → последние реплики (переживает рестарт)
_dialogs: dict[str, list] = {}
_DIALOG_KEEP = 16   # реплик (8 пар вопрос-ответ)


def _dialog_key(thread: int | None) -> str:
    return f"tg_{thread or 'main'}"


def _dialog_load() -> None:
    global _dialogs
    if not _dialogs:
        import snapshot as _snap
        _dialogs = _snap.load("agent_dialogs", None) or {}


def _dialog_add(thread: int | None, role: str, content: str) -> None:
    import snapshot as _snap
    key = _dialog_key(thread)
    d = _dialogs.setdefault(key, [])
    d.append({"role": role, "content": content[:2500]})
    del d[:-_DIALOG_KEEP]
    try:
        _snap.save("agent_dialogs", _dialogs)
    except Exception:
        pass


async def _tg_get_photo(file_id: str) -> str | None:
    """Скачивает фото из Telegram → base64 (jpeg)."""
    import base64
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getFile",
                            params={"file_id": file_id})
            path = ((r.json().get("result") or {}).get("file_path"))
            if not path:
                return None
            f = await c.get(f"https://api.telegram.org/file/bot{TG_BOT_TOKEN}/{path}")
            if f.status_code != 200 or len(f.content) > 4_500_000:
                return None
            return base64.b64encode(f.content).decode()
    except Exception as e:
        _log.warning("tg photo: %s", e)
        return None


def _is_banter(q: str, has_photo: bool) -> bool:
    """Разговор/шутка или деловой вопрос? Дело — только если есть предметные
    слова; всё остальное короткое — трёп, и отвечать надо как в чате."""
    ql = q.lower()
    business = ("цен", "марж", "остат", "дрр", "прибыл", "продаж", "выручк",
                "реклам", "кампан", "конкурент", "постав", "отзыв", "юнитк",
                "склад", "темп", "тренд", "экономик", "разбор", "отчет", "отчёт")
    if any(w in ql for w in business):
        return False
    return has_photo or len(ql) < 120


async def _answer_question(question: str, thread: int | None,
                           use_history: bool = True,
                           image_b64: str | None = None) -> None:
    """Свободный вопрос к агенту: данные кабинета + история диалога → Claude.
    use_history=False — для ответов на цитаты: история не должна перетягивать
    разговор на прошлую тему. image_b64 — картинка из сообщения (vision)."""
    try:
        await asyncio.to_thread(_dialog_load)
        banter = _is_banter(question, bool(image_b64))
        if banter:
            # шутка/трёп: без простыни данных и без отчётного тона
            system = """Ты — Biomed Агент, ИИ в командном чате селлеров. Сейчас
не деловой вопрос, а трёп/мем/подкол. Отвечай как остроумный коллега с чувством
собственного достоинства.

ЖЁСТКИЕ ПРАВИЛА СТИЛЯ:
- 1-2 предложения. Чем короче, тем смешнее.
- НЕ начинай с «Ха», «О», «Ну что ж», «Принял вызов». Сразу панч.
- НЕ объясняй шутку и НЕ пересказывай мем — люди его видели.
- НЕ переходи в отчёт: никаких списков, сводок, «но если серьёзно».
- Максимум один эмодзи, можно ноль.
- Оружие: ирония, самоирония, культурные отсылки, дерзость без грубости.
- Одна цифра допустима, только если она сама по себе панчлайн.
- В чате есть бот-конкурент Mira — если речь о ней, можно элегантно съязвить.
Формат Telegram HTML (только <b> и <i>)."""
            history = list(_dialogs.get(_dialog_key(thread), []))[-4:]
            content = ([{"type": "image", "source": {"type": "base64",
                                                     "media_type": "image/jpeg",
                                                     "data": image_b64}},
                        {"type": "text", "text": question}]
                       if image_b64 else question)
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            msg = await client.messages.create(
                model=_MODEL, max_tokens=350, system=system,
                messages=history + [{"role": "user", "content": content}])
            answer = msg.content[0].text.strip()
            await tg_send(answer, thread_id=thread)
            await asyncio.to_thread(_dialog_add, thread, "user", question)
            await asyncio.to_thread(_dialog_add, thread, "assistant", answer)
            return
        ctx = await _gather_context()
        system = f"""Ты — агент-аналитик селлера маркетплейсов (кабинет Biomed,
WB/Ozon/ЯМ), общаешься с владельцем и его командой в Telegram. Отвечай
по-русски, кратко и с цифрами из данных. Формат — Telegram HTML (только <b> и
<i>, без markdown). Если в данных нет ответа — скажи прямо и подскажи, где
смотреть в дашборде. Не выдумывай числа. Помни контекст предыдущих реплик.
Тон: живой, свой в команде. Если с тобой шутят, прислали мем или просят
ответить с сарказмом — поддержи шутку остроумно и по-доброму, можешь поддеть
в ответ; юмор юмором, а числа не выдумывай и грубым не будь.

ОФОРМЛЕНИЕ (навык профессионального копирайтера): без эмодзи вовсе.
Каждая законченная мысль — с новой строки; смысловые блоки разделяй
пустой строкой. Никаких простыней текста: абзац = 1-2 коротких
предложения. Цифры и выводы — в начале строки, пояснения после.

{KNOWLEDGE}"""
        history = list(_dialogs.get(_dialog_key(thread), [])) if use_history else []
        user_text = f"АКТУАЛЬНЫЕ ДАННЫЕ КАБИНЕТА:\n{ctx}\n\nВОПРОС: {question}"
        if image_b64:
            content = [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/jpeg",
                                             "data": image_b64}},
                {"type": "text", "text": user_text},
            ]
        else:
            content = user_text
        messages = history + [{"role": "user", "content": content}]
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model=_MODEL, max_tokens=1500, system=system, messages=messages)
        answer = msg.content[0].text.strip()
        u = getattr(msg, "usage", None)
        if u:
            cost = (u.input_tokens * 5 + u.output_tokens * 25) / 1_000_000
            _log.info("qa usage: in=%d out=%d ≈ $%.3f (%.1f ₽)",
                      u.input_tokens, u.output_tokens, cost, cost * 95)
        await tg_send(answer, thread_id=thread)
        # в память кладём вопрос и ответ БЕЗ простыни данных — данные каждый
        # раз свежие, а старые копии только путали бы модель
        await asyncio.to_thread(_dialog_add, thread, "user", question)
        await asyncio.to_thread(_dialog_add, thread, "assistant", answer)
    except Exception as e:
        _log.error("qa failed: %s", e)
        err = str(e)
        if "credit balance" in err:
            await tg_send("💳 Закончился баланс Anthropic API — владельцу нужно "
                          "пополнить его в console.anthropic.com → Billing. "
                          "После пополнения отвечу сразу.", thread_id=thread)
        else:
            await tg_send(f"Не смог ответить: {err[:200]}", thread_id=thread)


async def _review_with_retry(thread: int | None) -> None:
    """Разбор по команде: до 12 попыток с паузой 30с, пока юнитка собирается."""
    import asyncio
    notified = False
    for attempt in range(12):
        res = await send_review("WB", thread_id=thread)
        if res.get("ok"):
            return
        err = res.get("error") or ""
        if "собира" not in err:
            await tg_send(f"Не вышло: {err}", thread_id=thread)
            return
        if not notified:
            await tg_send("Сервер ещё собирает точные данные WB после перезапуска — "
                          "подожду и пришлю разбор, как будет готово (обычно 3-7 минут).",
                          thread_id=thread)
            notified = True
        await asyncio.sleep(30)
    await tg_send("Данные так и не собрались за 6 минут — попробуй /review позже.",
                  thread_id=thread)


async def build_bid_calc(args: str) -> str:
    """Калькулятор максимальной ставки CPM из юнитки.

    /bid <артикул> <CTR%> [CR%] [целевой ДРР%]
    Максимальная ставка за 1000 показов = 1000 × CTR × CR × цена × ДРР
    (ДРР по умолчанию — безубыточный из юнитки данного SKU)."""
    parts = args.split()
    if len(parts) < 2:
        return ("<b>Калькулятор ставки</b>\n"
                "Формат: /bid АРТИКУЛ CTR% [CR%] [ДРР%]\n"
                "Пример: /bid AL-01 4 — потолок ставки при CTR 4%\n"
                "CR — конверсия клика в заказ (не задашь — покажу сетку), "
                "ДРР — целевой (не задашь — безубыточный из юнитки).")
    sku_q = parts[0].upper()
    try:
        ctr = float(parts[1].replace(",", "."))
        cr_in = float(parts[2].replace(",", ".")) if len(parts) > 2 else None
        drr_in = float(parts[3].replace(",", ".")) if len(parts) > 3 else None
    except ValueError:
        return "Не понял числа. Формат: /bid AL-01 4 [5] [15]"

    from routers import tools as _tools
    data = await _tools.get_margin(mp="WB")
    items = data.get("items") or []
    if not items:
        return "Юнитка ещё собирается — повтори через пару минут."
    b = next((x for x in items if str(x.get("sku", "")).upper() == sku_q), None)
    if not b:
        near = [x["sku"] for x in items if sku_q in str(x.get("sku", "")).upper()][:5]
        return f"Артикул {sku_q} не нашёл." + (f" Похожие: {', '.join(near)}" if near else "")
    m = _margin_math(b)
    price = m["price_seller"]
    be = m["be_drr_pct"]
    drr = drr_in if drr_in is not None else be
    rub_per_order = price * drr / 100          # допустимые ₽ рекламы на 1 заказ

    def cpm(cr):
        return 1000 * (ctr / 100) * (cr / 100) * rub_per_order

    lines = [f"<b>{m['sku']}</b> · цена {price:.0f} ₽ · безубыточный ДРР {be}%"
             + (f" · считаю по ДРР {drr:.0f}%" if drr_in is not None else ""),
             f"Допустимо рекламы на 1 заказ: <b>{rub_per_order:.0f} ₽</b>",
             f"CTR {ctr}%:"]
    if cr_in is not None:
        lines.append(f"CR {cr_in}% → <b>макс. ставка {cpm(cr_in):.0f} ₽/1000 показов</b>")
    else:
        for cr in (3, 5, 8, 12):
            lines.append(f"  CR {cr}% → макс. <b>{cpm(cr):.0f} ₽</b>/1000 показов")
        lines.append("CR = конверсия клика в заказ; свой: /bid "
                     f"{m['sku']} {ctr} 5")
    if drr_in is None:
        lines.append("Это ставка НУЛЕВОЙ маржи. Рабочий потолок — с целевым "
                     f"ДРР, напр.: /bid {m['sku']} {ctr} 5 15")
    return "\n".join(lines)


async def bot_loop() -> None:
    """Long-polling: команды в группе — /stocks (остатки) и /review (разбор).
    Отвечает только в чате TG_CHAT_ID (чужим — молчит)."""
    import asyncio
    if not TG_BOT_TOKEN:
        return
    _log.info("agent bot: слушаю команды в чате %s", TG_CHAT_ID)
    try:      # меню команд Telegram: подсказки при вводе «/»
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setMyCommands",
                         json={"commands": [
                             {"command": "stocks", "description": "Остатки по площадкам"},
                             {"command": "review", "description": "Разбор кабинета"},
                             {"command": "strategy", "description": "Стратег: сессия или вопрос после команды"},
                             {"command": "bid", "description": "Ставка CPM: /bid АРТИКУЛ CTR% [CR%] [ДРР%]"},
                             {"command": "reset", "description": "Очистить память диалога"},
                             {"command": "help", "description": "Справка"},
                         ]})
    except Exception as e:
        _log.warning("setMyCommands: %s", e)
    me = ""
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getMe")
            me = ((r.json().get("result") or {}).get("username") or "").lower()
    except Exception as e:
        _log.warning("getMe failed: %s", e)
    import snapshot as _snap
    # офсет переживает рестарты: сообщения из деплой-окна не теряются
    # и старые команды не выполняются повторно
    offset = int(await asyncio.to_thread(_snap.load, "tg_offset", 0) or 0)
    _paused = [bool(await asyncio.to_thread(_snap.load, "agent_paused", False))]
    while True:
        try:
            async with httpx.AsyncClient(timeout=70) as c:
                r = await c.get(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
                    params={"timeout": 50, "offset": offset,
                            "allowed_updates": '["message"]'})
            if r.status_code == 409:
                # 409 = либо второй потребитель getUpdates этим токеном,
                # либо на боте включён webhook. Webhook снимаем сами; чужой
                # поллер лечится только ротацией токена у BotFather.
                _log.warning("bot: 409 conflict: %s", r.text[:200])
                try:
                    async with httpx.AsyncClient(timeout=15) as c2:
                        wh = await c2.get(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getWebhookInfo")
                        info = (wh.json().get("result") or {})
                        if info.get("url"):
                            _log.warning("bot: обнаружен webhook %s — снимаю", info["url"])
                            await c2.get(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteWebhook")
                except Exception as e2:
                    _log.warning("bot: webhook check failed: %s", e2)
                await asyncio.sleep(15)
                continue
            body = r.json()
            if not body.get("ok"):
                _log.warning("bot getUpdates не ok: %s", str(body)[:200])
            for upd in (body.get("result") or []):
                offset = upd["update_id"] + 1
                await asyncio.to_thread(_snap.save, "tg_offset", offset)
                m0 = upd.get("message") or {}
                _log.info("bot msg: chat=%s from=%s text=%r",
                          (m0.get("chat") or {}).get("id"),
                          (m0.get("from") or {}).get("username"),
                          (m0.get("text") or "")[:80])
                msg = upd.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id") or "")
                raw = (msg.get("text") or msg.get("caption") or "").strip()
                text = raw.lower()
                thread = msg.get("message_thread_id")
                if chat != TG_CHAT_ID or not raw:
                    continue
                # стоп-слово: прервать сессию стратега и замолчать до «старт»
                low = raw.strip().lower().rstrip('!.')
                if low in ("стоп", "stop") or low.startswith(("стоп ", "stop ")):
                    import agent_strategist as _st
                    _st._cancel = True
                    _paused[0] = True
                    await asyncio.to_thread(_snap.save, "agent_paused", True)
                    await tg_send("Остановился: сессию прерываю, на вопросы "
                                  "не отвечаю. Вернуть: «старт»", thread_id=thread)
                    continue
                if low in ("старт", "start", "работай"):
                    if _paused[0]:
                        _paused[0] = False
                        await asyncio.to_thread(_snap.save, "agent_paused", False)
                        await tg_send("Снова в строю.", thread_id=thread)
                    continue
                if _paused[0]:
                    continue
                # свободный вопрос: упоминание @бота или reply на его сообщение
                reply_from = ((msg.get("reply_to_message") or {}).get("from") or {})
                is_mention = me and f"@{me}" in text
                is_reply = reply_from.get("is_bot") and \
                    (reply_from.get("username") or "").lower() == me
                if not text.startswith("/") and (is_mention or is_reply):
                    import re as _re
                    q = _re.sub(f"@{me}", "", raw, flags=_re.IGNORECASE).strip()
                    # реплай на чужое сообщение = контекст вопроса (боты друг
                    # друга в TG не видят — но текст приходит внутри реплая)
                    rm = msg.get("reply_to_message") or {}
                    rtext = (rm.get("text") or rm.get("caption") or "").strip()
                    rfrom = ((rm.get("from") or {}).get("first_name")
                             or (rm.get("from") or {}).get("username") or "")
                    # картинка: в самом сообщении или в цитируемом (мем и т.п.)
                    photos = msg.get("photo") or rm.get("photo") or []
                    photo_id = photos[-1].get("file_id") if photos else None
                    has_quote = bool(rtext and not is_reply)
                    if has_quote:
                        q = (f"Владелец переслал тебе сообщение от «{rfrom}» с просьбой: {q}\n\n"
                             f"СООБЩЕНИЕ ОТ {rfrom}:\n«{rtext[:1500]}»\n\n"
                             f"ВАЖНО: отвечай ИМЕННО на содержание этого сообщения — на его "
                             f"вопросы, пункты и требования, используя данные кабинета где они "
                             f"нужны. НЕ подменяй ответ общим разбором экономики кабинета.")
                    if not q and photo_id:
                        q = "Прокомментируй картинку."
                    if q:
                        # дедуп: после простоя очередь может принести один и
                        # тот же вопрос несколько раз — отвечаем один раз
                        key = (thread, q[:200])
                        if key in _qa_inflight:
                            continue
                        _qa_inflight.add(key)
                        if _is_banter(q, bool(photo_id)):
                            # для трёпа не позорить панч «Смотрю данные…» —
                            # просто «печатает…» как живой собеседник
                            try:
                                async with httpx.AsyncClient(timeout=10) as c3:
                                    await c3.post(
                                        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendChatAction",
                                        json={"chat_id": TG_CHAT_ID, "action": "typing",
                                              **({"message_thread_id": thread} if thread else {})})
                            except Exception:
                                pass
                        else:
                            await tg_send("Смотрю данные…", thread_id=thread)
                        async def _run(qq=q, th=thread, k=key, hq=has_quote, ph=photo_id):
                            try:
                                img = await _tg_get_photo(ph) if ph else None
                                await _answer_question(qq, th, use_history=not hq,
                                                       image_b64=img)
                            finally:
                                _qa_inflight.discard(k)
                        asyncio.get_event_loop().create_task(_run())
                    continue
                if not text.startswith("/"):
                    continue
                cmd = text.split()[0].split("@")[0]
                if cmd == "/bid":
                    arg = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
                    await tg_send(await build_bid_calc(arg), thread_id=thread)
                elif cmd == "/stocks":
                    await tg_send(await build_stocks_summary(), thread_id=thread)
                elif cmd == "/review":
                    await tg_send("Готовлю разбор кабинета — минута…", thread_id=thread)
                    # фоном: если юнитка ещё собирается (после рестарта это
                    # несколько минут) — ждём и повторяем, а не отказываем
                    asyncio.get_event_loop().create_task(_review_with_retry(thread))
                elif cmd == "/strategy":
                    focus_q = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""
                    status_id = await tg_send_id("Стратег сел за данные — "
                                  + ("разберу вопрос и вернусь с ответом…" if focus_q
                                     else "отчёт будет через несколько минут…"),
                                  thread_id=thread)
                    import agent_strategist as _st

                    async def _strun(th=thread, fq=focus_q, sid=status_id):
                        res = await _st.run_session(
                            trigger="команда /strategy в Telegram",
                            focus=fq, light=bool(fq), status_msg_id=sid)
                        if res.get("error"):
                            await tg_send(f"Сессия не удалась: {res['error']}",
                                          thread_id=th)
                    asyncio.get_event_loop().create_task(_strun())
                elif cmd == "/reset":
                    _dialogs.pop(_dialog_key(thread), None)
                    await tg_send("Память диалога очищена.", thread_id=thread)
                elif cmd in ("/start", "/help"):
                    await tg_send(
                        "Команды:\n/stocks — короткий расклад по остаткам (горящие позиции)\n"
                        "/review — полный разбор кабинета от агента\n"
                        "/reset — очистить память диалога\n\n"
                        f"Или просто спроси меня через упоминание: <i>@{me} что с маржой геля?</i> "
                        "— отвечу по данным кабинета.\n"
                        "Плюс сам присылаю разбор каждый понедельник в 9:00 МСК.",
                        thread_id=thread)
        except Exception as e:
            _log.warning("agent bot loop: %s", e)
            await asyncio.sleep(10)


async def send_review(mp: str = "WB", thread_id: int | None = None) -> dict:
    """Собрать разбор и отправить в Telegram."""
    if not configured():
        return {"ok": False, "error": "TG_BOT_TOKEN / TG_CHAT_ID / ANTHROPIC_API_KEY не заданы"}
    try:
        text = await build_review(mp)
    except Exception as e:
        _log.error("agent_review build failed: %s", e)
        return {"ok": False, "error": str(e)[:300]}
    if not text:
        return {"ok": False, "error": "данные юнитки ещё собираются — попробуйте позже"}
    sent = await tg_send(text, thread_id=thread_id)
    return {"ok": sent, "chars": len(text)}
