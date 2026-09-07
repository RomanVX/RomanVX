"""Заявки с публичного лендинга фулфилмента (marketpartners.ru) → Telegram.

Публичный endpoint (без cookie-сессии, префикс в auth._PUBLIC_PREFIXES).
Защита: honeypot-поле, лимит по IP, обрезка длин. Сообщение уходит в чат
LEADS_TG_CHAT_ID (если задан) либо в основной TG_CHAT_ID бота.
"""
import html
import logging
import os
import time

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/landing", tags=["landing"])
_log = logging.getLogger("landing")

_RATE: dict[str, list[float]] = {}      # ip → timestamps
_RATE_N, _RATE_WINDOW = 5, 600           # 5 заявок / 10 минут с одного IP

TOPICS = {"calc": "Расчёт тарифа", "tour": "Экскурсия по складу",
          "move": "Переезд", "fix": "Зафиксировать расчёт"}


def _clip(v, n: int) -> str:
    return str(v or "").strip()[:n]


@router.post("/lead")
async def landing_lead(payload: dict, request: Request):
    if _clip(payload.get("website"), 10):          # honeypot: боты заполняют всё
        return {"ok": True}
    ip = (request.headers.get("x-forwarded-for") or
          (request.client.host if request.client else "")).split(",")[0].strip()
    now = time.time()
    hits = [t for t in _RATE.get(ip, []) if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_N:
        return {"ok": False, "error": "Слишком много заявок, попробуйте позже"}
    name = _clip(payload.get("name"), 80)
    contact = _clip(payload.get("contact"), 120)
    if not contact:
        return {"ok": False, "error": "Укажите телефон или Telegram"}
    hits.append(now)
    _RATE[ip] = hits
    topic = TOPICS.get(_clip(payload.get("topic"), 20), _clip(payload.get("topic"), 40) or "—")
    form = _clip(payload.get("form"), 20)
    calc = payload.get("calc") if isinstance(payload.get("calc"), dict) else {}
    lines = [f"📩 <b>Заявка с сайта</b> · {html.escape(topic)}",
             f"👤 {html.escape(name) or '—'}",
             f"📞 {html.escape(contact)}"]
    if calc:
        lines.append("🧮 Калькулятор: " + html.escape(
            f"{_clip(calc.get('orders'), 12)} заказов/день · вес {_clip(calc.get('weight'), 20)}"
            f"{' · хрупкий' if calc.get('fragile') else ''} · ставка {_clip(calc.get('rate'), 12)} ₽"
            f" · ≈ {_clip(calc.get('month'), 20)} ₽/мес"))
    lines.append(f"🕒 {time.strftime('%d.%m.%Y %H:%M', time.gmtime(now + 3 * 3600))} МСК"
                 f" · {html.escape(form or 'форма')}")
    text = "\n".join(lines)
    try:
        import agent_review
        chat = os.getenv("LEADS_TG_CHAT_ID", "").strip() or ""
        ok = await agent_review.tg_send(text, chat_id=chat)
    except Exception as e:
        _log.warning("lead → tg: %s", e)
        ok = False
    _log.info("lead %s: %s / %s (tg=%s)", ip, name, contact, ok)
    if not ok:
        return {"ok": False, "error": "Не удалось отправить, напишите нам в Telegram"}
    return {"ok": True}
