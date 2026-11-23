"""Owner portal and single-use sign-in handoffs over the shared PostgreSQL database."""

import hashlib
import hmac
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

import db

ORIGIN = "https://main.marketpartners.ru"
DESTINATIONS = {
    "tl": "https://tl.marketpartners.ru",
    "avr": "https://aurora.marketpartners.ru",
    "biomed": "https://biomed.marketpartners.ru",
    "fk": "https://fk.marketpartners.ru",
    "mpfit": ORIGIN,
}
PORTAL_COOKIE = "__Host-mp_portal"
CABINET_COOKIE = "__Host-mp_portal_dashboard"
SESSION_SECONDS = 12 * 60 * 60
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "frame-ancestors 'none'", "X-Content-Type-Options": "nosniff"}
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
router = APIRouter()


class Credentials(BaseModel):
    login: str = Field(max_length=128)
    password: str = Field(max_length=1024)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _cookie_digest(request, name):
    value = request.cookies.get(name, "")
    return _digest(value) if TOKEN.fullmatch(value) else None


def _configured():
    if not db.IS_PG:
        return False
    return bool(os.getenv("PORTAL_ADMIN_LOGIN") and (
        os.getenv("PORTAL_ADMIN_PASSWORD") or os.getenv("PORTAL_ADMIN_PASSWORD_HASH")
    ))


def _main_origin(request):
    if request.headers.get("origin") != ORIGIN:
        raise HTTPException(403, "Недопустимый источник запроса")


def _cookie(response, name, value):
    response.set_cookie(name, value, max_age=SESSION_SECONDS, secure=True,
                        httponly=True, samesite="strict")


def _clear_cookie(response, name):
    response.delete_cookie(name, secure=True, httponly=True, samesite="strict")


@lru_cache(maxsize=1)
def _init():
    with db._conn() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(734050612)")
        cursor.execute("CREATE SCHEMA IF NOT EXISTS mp_portal")
        cursor.execute("""CREATE TABLE IF NOT EXISTS mp_portal.sessions
            (token TEXT PRIMARY KEY, login TEXT NOT NULL, expires TIMESTAMPTZ NOT NULL)""")
        for table in ("tickets", "grants"):
            cursor.execute(f"""CREATE TABLE IF NOT EXISTS mp_portal.{table}
                (token TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES mp_portal.sessions(token)
                 ON DELETE CASCADE, audience TEXT NOT NULL, expires TIMESTAMPTZ NOT NULL)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS mp_portal.login_budget
            (bucket BIGINT PRIMARY KEY, attempts INTEGER NOT NULL)""")
        connection.commit()


def _password_ok(login, password):
    expected_login = os.environ["PORTAL_ADMIN_LOGIN"]
    if not hmac.compare_digest(login.encode(), expected_login.encode()):
        return False

    configured_password = os.getenv("PORTAL_ADMIN_PASSWORD")
    if configured_password:
        return hmac.compare_digest(_digest(password), _digest(configured_password))

    try:
        salt_hex, digest_hex = os.environ["PORTAL_ADMIN_PASSWORD_HASH"].split(":", 1)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def _portal_session(request):
    token = _cookie_digest(request, PORTAL_COOKIE)
    if not token or not _configured():
        raise HTTPException(401, "Войдите в портал")
    row = db.fetchone("SELECT login FROM mp_portal.sessions WHERE token = ? AND expires > now()", (token,))
    if not row:
        raise HTTPException(401, "Войдите в портал")
    return token, row[0]


@router.get("/portal")
def page():
    if not _configured():
        raise HTTPException(503, "Вход в портал пока не настроен")
    path = Path(__file__).resolve().parent.parent / "frontend" / "portal.html"
    return FileResponse(path, media_type="text/html", headers={**HEADERS, "Referrer-Policy": "strict-origin", "Content-Security-Policy":
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "form-action 'self' " + " ".join(DESTINATIONS.values()) + "; frame-ancestors 'none'; base-uri 'none'"})


@router.post("/api/auth/portal/login")
def login(payload: Credentials, request: Request, response: Response):
    _main_origin(request)
    if not _configured():
        raise HTTPException(503, "Вход в портал пока не настроен")
    _init()
    with db._conn() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM mp_portal.login_budget WHERE bucket < floor(extract(epoch from now()) / 300) - 1")
        cursor.execute("""INSERT INTO mp_portal.login_budget VALUES (floor(extract(epoch from now()) / 300), 1)
            ON CONFLICT (bucket) DO UPDATE SET attempts = login_budget.attempts + 1
            WHERE login_budget.attempts < 20 RETURNING attempts""")
        allowed = cursor.fetchone()
        connection.commit()
    if not allowed:
        raise HTTPException(429, "Слишком много попыток. Попробуйте через несколько минут")
    if not _password_ok(payload.login.strip(), payload.password):
        raise HTTPException(401, "Неверный логин или пароль")
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM mp_portal.sessions WHERE expires <= now() OR token = ?",
               (_cookie_digest(request, PORTAL_COOKIE),))
    db.execute("INSERT INTO mp_portal.sessions VALUES (?, ?, now() + interval '12 hours')",
               (_digest(token), payload.login.strip()))
    response.headers.update(HEADERS)
    _cookie(response, PORTAL_COOKIE, token)
    return {"login": payload.login.strip()}


@router.get("/api/auth/portal/me")
def me(request: Request, response: Response):
    _, login = _portal_session(request)
    response.headers.update(HEADERS)
    return {"login": login}


@router.post("/api/auth/portal/logout")
def logout(request: Request, response: Response):
    _main_origin(request)
    token, _ = _portal_session(request)
    db.execute("DELETE FROM mp_portal.sessions WHERE token = ?", (token,))
    _clear_cookie(response, PORTAL_COOKIE)
    response.headers.update(HEADERS)
    return {"ok": True}


@router.post("/api/auth/portal/launch/{audience}")
def launch(audience: str, request: Request, response: Response):
    _main_origin(request)
    if audience not in DESTINATIONS:
        raise HTTPException(404)
    session, _ = _portal_session(request)
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM mp_portal.tickets WHERE expires <= now()")
    db.execute("INSERT INTO mp_portal.tickets VALUES (?, ?, ?, now() + interval '45 seconds')",
               (_digest(token), session, audience))
    response.headers.update(HEADERS)
    path = "mpfit-exchange" if audience == "mpfit" else "exchange"
    return {"ticket": token, "destination": DESTINATIONS[audience] + "/api/auth/portal/" + path}


async def exchange(request, audience):
    import asyncio

    _main_origin(request)
    if not db.IS_PG or audience not in DESTINATIONS:
        raise HTTPException(404)
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 128:
            raise HTTPException(413)
    values = parse_qs(body.decode("ascii", errors="replace")).get("ticket", [])
    ticket = values[0] if len(values) == 1 else ""
    if not TOKEN.fullmatch(ticket):
        raise HTTPException(401, "Откройте кабинет заново из портала")
    token = secrets.token_urlsafe(32)

    def claim():
        with db._conn() as connection, connection.cursor() as cursor:
            cursor.execute("""WITH consumed AS (
                DELETE FROM mp_portal.tickets AS t USING mp_portal.sessions AS s
                WHERE t.token = %s AND t.audience = %s AND t.expires > now()
                    AND t.session = s.token AND s.expires > now()
                RETURNING t.session, s.expires
                ) INSERT INTO mp_portal.grants
                SELECT %s, session, %s, expires FROM consumed RETURNING token""",
                (_digest(ticket), audience, _digest(token), audience))
            accepted = cursor.fetchone()
            connection.commit()
            return accepted

    if not await asyncio.to_thread(claim):
        raise HTTPException(401, "Откройте кабинет заново из портала")
    response = RedirectResponse("/?enter=1" if audience != "mpfit" else "/mpfit", 303, HEADERS)
    _cookie(response, CABINET_COOKIE, token)
    return response


def dashboard_user(request, audience):
    token = _cookie_digest(request, CABINET_COOKIE)
    if not token or not db.IS_PG or audience not in DESTINATIONS:
        return None
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin != DESTINATIONS[audience]:
        return None
    try:
        row = db.fetchone("""SELECT s.login FROM mp_portal.grants AS g
            JOIN mp_portal.sessions AS s ON s.token = g.session
            WHERE g.token = ? AND g.audience = ? AND g.expires > now() AND s.expires > now()""",
            (token, audience))
    except Exception:
        return None
    return {"login": row[0], "role": "owner"} if row else None


def dashboard_logout(request, response, audience):
    token = _cookie_digest(request, CABINET_COOKIE)
    if token and db.IS_PG and request.headers.get("origin") == DESTINATIONS.get(audience):
        db.execute("DELETE FROM mp_portal.grants WHERE token = ? AND audience = ?", (token, audience))
        _clear_cookie(response, CABINET_COOKIE)


@router.post("/api/auth/portal/exchange")
async def exchange_cabinet(request: Request):
    from config import CABINET
    return await exchange(request, CABINET)
