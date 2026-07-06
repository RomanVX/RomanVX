"""Приём заявок с публичного лендинга (форма «Бесплатный аудит»).

Заявка всегда дописывается в data/leads.jsonl (на Render диск эфемерный,
поэтому файл — только страховка между деплоями). Основные каналы уведомлений
настраиваются переменными окружения, работают все настроенные сразу:

  LEAD_TG_BOT_TOKEN / LEAD_TG_CHAT_ID — сообщение в Telegram через бота;
  LEAD_SMTP_HOST / LEAD_SMTP_PORT / LEAD_SMTP_USER / LEAD_SMTP_PASS /
  LEAD_EMAIL_TO / LEAD_EMAIL_FROM — письмо по SMTP (465 = SSL, иначе STARTTLS).

Если каналы настроены, но ни один не доставил — клиенту отдаётся ошибка,
чтобы он попробовал ещё раз или написал напрямую.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter()
_log = logging.getLogger("lead")

LEADS_FILE = Path(__file__).parent.parent / "data" / "leads.jsonl"

TG_BOT_TOKEN = os.getenv("LEAD_TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("LEAD_TG_CHAT_ID", "").strip()

SMTP_HOST = os.getenv("LEAD_SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("LEAD_SMTP_PORT", "465") or 465)
SMTP_USER = os.getenv("LEAD_SMTP_USER", "").strip()
SMTP_PASS = os.getenv("LEAD_SMTP_PASS", "")
EMAIL_TO = os.getenv("LEAD_EMAIL_TO", "").strip()
EMAIL_FROM = os.getenv("LEAD_EMAIL_FROM", "").strip() or SMTP_USER

# ── Простейший rate limit: не больше 5 заявок в час с одного IP ─────────────
_RATE_WINDOW = 3600
_RATE_MAX = 5
_hits: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_MAX:
        _hits[ip] = hits
        return True
    hits.append(now)
    _hits[ip] = hits
    if len(_hits) > 10_000:  # не даём словарю расти бесконечно
        _hits.clear()
    return False


def _clean(raw: object, max_len: int) -> str:
    value = str(raw or "").strip()
    value = value.replace("\r", " ").replace("\n", " ").replace("\0", " ")
    return value[:max_len]


def _store(lead: dict) -> bool:
    try:
        LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LEADS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(lead, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        _log.error("lead store failed: %s", exc)
        return False


def _message_text(lead: dict) -> str:
    return (
        "Новая заявка на бесплатный аудит (marketpartners.ru)\n\n"
        f"Имя: {lead['name']}\n"
        f"Телефон / Telegram: {lead['contact']}\n"
        f"Магазин на WB: {lead['shop'] or '—'}\n\n"
        f"Отправлено: {lead['ts']}\nIP: {lead['ip']}"
    )


async def _notify_telegram(text: str) -> bool:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text},
            )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        _log.error("telegram notify failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        _log.error("telegram notify failed: %s", exc)
    return False


def _send_email_sync(text: str) -> bool:
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header("Заявка на аудит с marketpartners.ru", "utf-8")
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
            server.starttls()
        with server:
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        return True
    except Exception as exc:
        _log.error("smtp notify failed: %s", exc)
        return False


async def _notify_email(text: str) -> bool:
    if not (SMTP_HOST and EMAIL_TO):
        return False
    return await asyncio.to_thread(_send_email_sync, text)


def _wants_json(request: Request) -> bool:
    return (
        request.headers.get("x-requested-with", "").lower() == "fetch"
        or "application/json" in request.headers.get("accept", "")
    )


def _respond(request: Request, ok: bool, code: int):
    if _wants_json(request):
        return JSONResponse({"ok": ok}, status_code=code)
    return RedirectResponse(f"/?sent={'1' if ok else '0'}#audit", status_code=303)


@router.post("/api/lead")
async def create_lead(request: Request):
    form = await request.form()

    # honeypot: людям поле не видно; боту отвечаем «успехом»
    if _clean(form.get("website"), 300):
        return _respond(request, True, 200)

    # за прокси Render реальный адрес клиента — в X-Forwarded-For
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "—")
    if _rate_limited(ip):
        return _respond(request, False, 429)

    name = _clean(form.get("name"), 100)
    contact = _clean(form.get("contact"), 150)
    shop = _clean(form.get("shop"), 300)
    agree = _clean(form.get("agree"), 10)

    if not name or not contact or not agree:
        return _respond(request, False, 422)

    lead = {
        "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "name": name,
        "contact": contact,
        "shop": shop,
        "ip": ip,
    }
    stored = _store(lead)

    text = _message_text(lead)
    channels_configured = bool(TG_BOT_TOKEN and TG_CHAT_ID) or bool(SMTP_HOST and EMAIL_TO)
    tg_ok, mail_ok = await asyncio.gather(_notify_telegram(text), _notify_email(text))

    if channels_configured:
        ok = tg_ok or mail_ok
        if not ok:
            _log.error("lead received but no notification delivered: %s", lead)
    else:
        # каналы не настроены — заявка хотя бы записана в файл и лог
        ok = stored
        _log.warning("lead stored without notification (настройте LEAD_TG_* или LEAD_SMTP_*): %s", lead)

    return _respond(request, ok, 200 if ok else 500)


# ── Совместимость с вариантом для shared-хостинга (send.php) ────────────────

@router.post("/send.php", include_in_schema=False)
async def create_lead_php_compat(request: Request):
    """Форма со старым action=send.php продолжает работать и на FastAPI."""
    return await create_lead(request)


@router.get("/send.php", include_in_schema=False)
async def send_php_source_guard():
    """Не отдаём исходник send.php из папки лендинга как статику."""
    return RedirectResponse("/", status_code=302)
