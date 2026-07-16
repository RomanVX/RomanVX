"""Агент-аналитик: еженедельный разбор кабинета → Telegram.

Собирает готовые данные проекта (юнитка/маржа/P&L/тарифы), прогоняет через
Claude в роли финдиректора и отправляет разбор в Telegram (личку или группу —
куда указывает TG_CHAT_ID; для группы бота нужно в неё добавить).

ENV: TG_BOT_TOKEN (от @BotFather), TG_CHAT_ID (id чата/группы; у групп
он отрицательный — можно узнать, написав боту и открыв
https://api.telegram.org/bot<TOKEN>/getUpdates).
"""
import json
import logging
import os
from datetime import datetime

import httpx

from config import ANTHROPIC_API_KEY

_log = logging.getLogger("agent_review")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
_MODEL = "claude-opus-4-8"


def configured() -> bool:
    return bool(TG_BOT_TOKEN and TG_CHAT_ID and ANTHROPIC_API_KEY)


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
            "price": price, "profit_unit": round(profit),
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
    total_rev = sum(r["price"] * r["qty_month"] for r in rows)

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

ДАННЫЕ (прогноз на месяц по текущим ценам; profit_month — прогноз прибыли
артикула в месяц, be_drr_pct — безубыточный ДРР):
{json.dumps(rows, ensure_ascii=False)}

Итого прогноз: выручка {round(total_rev)} ₽/мес, прибыль {round(total_profit)} ₽/мес.
P&L последних месяцев: {pnl_note or "нет"}
Изменение тарифов WB: {json.dumps(tariff, ensure_ascii=False) if tariff else "не менялись"}

Формат ответа (Telegram, HTML: только <b> и <i>, без markdown, без заголовков-решёток):
1) 📊 Сводка — 2-3 предложения о состоянии кабинета.
2) 🔥 Главные проблемы — до 3, каждая с цифрой потерь.
3) ✅ Действия на неделю — 3-5 пунктов, каждый: что сделать + ожидаемый эффект в ₽.
Приоритезируй по деньгам. Если у артикула ДРР выше безубыточного — это всегда
проблема №1. Не выдумывай данных, которых нет."""

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    msg = await client.messages.create(
        model=_MODEL, max_tokens=1800,
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


async def bot_loop() -> None:
    """Long-polling: команды в группе — /stocks (остатки) и /review (разбор).
    Отвечает только в чате TG_CHAT_ID (чужим — молчит)."""
    import asyncio
    if not TG_BOT_TOKEN:
        return
    _log.info("agent bot: слушаю команды в чате %s", TG_CHAT_ID)
    offset = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=70) as c:
                r = await c.get(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
                    params={"timeout": 50, "offset": offset,
                            "allowed_updates": '["message"]'})
            for upd in (r.json().get("result") or []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id") or "")
                text = (msg.get("text") or "").strip().lower()
                thread = msg.get("message_thread_id")
                if chat != TG_CHAT_ID or not text.startswith("/"):
                    continue
                cmd = text.split()[0].split("@")[0]
                if cmd == "/stocks":
                    await tg_send(await build_stocks_summary(), thread_id=thread)
                elif cmd == "/review":
                    await tg_send("⏳ Готовлю разбор кабинета — минута…", thread_id=thread)
                    # фоном: если юнитка ещё собирается (после рестарта это
                    # несколько минут) — ждём и повторяем, а не отказываем
                    asyncio.get_event_loop().create_task(_review_with_retry(thread))
                elif cmd in ("/start", "/help"):
                    await tg_send(
                        "Команды:\n/stocks — короткий расклад по остаткам (горящие позиции)\n"
                        "/review — полный разбор кабинета от агента\n"
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
