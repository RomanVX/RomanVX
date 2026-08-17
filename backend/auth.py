"""Серверная авторизация: пользователи, роли, cookie-сессии.

Роли:
  owner    — всё + управление пользователями (админка «Доступы»)
  director — всё в этом кабинете (финансы видит)
  manager  — всё, кроме финансов (юнитка доступна)

Доступ к кабинету = наличие пользователя в БД этого кабинета:
каждый деплой (Biomed / Фабрика красоты) хранит своих пользователей.
"""
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_DAYS = 30
COOKIE = "mp_session"

# стартовый владелец: логин/пароль из окружения (по умолчанию — прежние)
BOOT_LOGIN = os.getenv("ADMIN_LOGIN", "admin").strip()
BOOT_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin").strip()

ROLES = ("owner", "director", "manager")


def _init_tables():
    import db
    db.execute("CREATE TABLE IF NOT EXISTS users "
               "(login TEXT PRIMARY KEY, salt TEXT, pass_hash TEXT, role TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS sessions "
               "(token TEXT PRIMARY KEY, login TEXT, role TEXT, expires TEXT)")


def _hash(salt: str, password: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def ensure_bootstrap():
    """Создаёт владельца при первом запуске (если пользователей нет)."""
    import db
    try:
        _init_tables()
        n = db.fetchone("SELECT COUNT(*) FROM users")[0]
        if n == 0:
            salt = secrets.token_hex(8)
            db.execute("INSERT INTO users (login, salt, pass_hash, role) VALUES (?,?,?,?)",
                       (BOOT_LOGIN, salt, _hash(salt, BOOT_PASSWORD), "owner"))
            _log.info("Auth: создан владелец «%s»", BOOT_LOGIN)
    except Exception as e:
        _log.warning("Auth bootstrap failed: %s", e)


def _session_of(request: Request) -> dict | None:
    # демо-кабинет (мок-режим) открыт без пароля: все — владелец «demo».
    # Реальных данных и трат там нет, а экран логина мешает показам.
    from config import USE_MOCK
    if USE_MOCK:
        return {"login": "demo", "role": "owner"}
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    import db
    try:
        row = db.fetchone("SELECT login, role, expires FROM sessions WHERE token = ?", (token,))
    except Exception:
        return None
    if not row:
        return None
    login, role, expires = row
    try:
        if datetime.fromisoformat(expires) < datetime.utcnow():
            return None
    except ValueError:
        return None
    return {"login": login, "role": role}


# ── Middleware-проверка (вызывается из main) ─────────────────────────────────

_PUBLIC_PREFIXES = ("/api/auth/", "/api/cabinet", "/api/health",
                    "/api/wms/",   # WMS: собственные cookie-сессии (routers/wms.py)
                    # эндпоинты локального агента ниши — защищены собственным
                    # токеном WB_AGENT_TOKEN, сессия-cookie им не нужна
                    "/api/tools/niche/pending", "/api/tools/niche/ingest")
# менеджеру закрыты деньги: P&L, выплаты, ручные статьи, загрузка себеса.
# Юнитка (…/unit) — разрешена.
_MANAGER_BLOCKED = ("/api/finance/", "/api/upload/", "/api/dashboard/finance")
_MANAGER_ALLOWED_SUFFIX = ("/unit",)


def check_request(request: Request) -> Response | None:
    """None — пропустить; Response — отказ (401/403)."""
    from fastapi.responses import JSONResponse
    path = request.url.path
    if not path.startswith("/api/") or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return None
    sess = _session_of(request)
    if not sess:
        return JSONResponse({"detail": "Не авторизован"}, status_code=401)
    request.state.user = sess
    if sess["role"] == "manager":
        if any(path.startswith(p) for p in _MANAGER_BLOCKED) \
                and not any(path.rstrip("/").endswith(s) for s in _MANAGER_ALLOWED_SUFFIX):
            return JSONResponse({"detail": "Недостаточно прав"}, status_code=403)
    if path.startswith("/api/users") and sess["role"] != "owner":
        return JSONResponse({"detail": "Только для владельца"}, status_code=403)
    return None


# ── Эндпоинты ────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(payload: dict, response: Response):
    import asyncio
    import db
    login_ = str(payload.get("login") or "").strip()
    password = str(payload.get("password") or "")
    if not login_ or not password:
        raise HTTPException(status_code=400, detail="Введите логин и пароль")

    def _check():
        _init_tables()
        return db.fetchone("SELECT salt, pass_hash, role FROM users WHERE login = ?", (login_,))
    row = await asyncio.to_thread(_check)
    if not row or _hash(row[0], password) != row[1]:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = secrets.token_hex(32)
    expires = (datetime.utcnow() + timedelta(days=SESSION_DAYS)).isoformat()

    def _save():
        db.execute("INSERT INTO sessions (token, login, role, expires) VALUES (?,?,?,?)",
                   (token, login_, row[2], expires))
        db.execute("DELETE FROM sessions WHERE expires < ?", (datetime.utcnow().isoformat(),))
    await asyncio.to_thread(_save)
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax")
    return {"login": login_, "role": row[2]}


@router.post("/logout")
async def logout(request: Request, response: Response):
    import asyncio
    import db
    token = request.cookies.get(COOKIE)
    if token:
        await asyncio.to_thread(db.execute, "DELETE FROM sessions WHERE token = ?", (token,))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    sess = _session_of(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return sess


# ── Управление пользователями (владелец) ────────────────────────────────────

users_router = APIRouter(prefix="/api/users", tags=["users"])


@users_router.get("")
async def list_users():
    import asyncio
    import db
    def _load():
        _init_tables()
        return db.fetchall("SELECT login, role FROM users ORDER BY login")
    rows = await asyncio.to_thread(_load)
    return {"users": [{"login": r[0], "role": r[1]} for r in rows]}


@users_router.post("")
async def upsert_user(payload: dict):
    import asyncio
    import db
    login_ = str(payload.get("login") or "").strip()
    password = str(payload.get("password") or "")
    role = str(payload.get("role") or "manager").strip()
    if not login_ or role not in ROLES:
        raise HTTPException(status_code=400, detail="Нужны логин и роль (owner/director/manager)")

    def _save():
        _init_tables()
        exists = db.fetchone("SELECT salt FROM users WHERE login = ?", (login_,))
        if exists and not password:
            db.execute("UPDATE users SET role = ? WHERE login = ?", (role, login_))
            return "updated"
        if not password:
            raise HTTPException(status_code=400, detail="Для нового пользователя нужен пароль")
        salt = secrets.token_hex(8)
        if exists:
            db.execute("UPDATE users SET salt = ?, pass_hash = ?, role = ? WHERE login = ?",
                       (salt, _hash(salt, password), role, login_))
            db.execute("DELETE FROM sessions WHERE login = ?", (login_,))
            return "updated"
        db.execute("INSERT INTO users (login, salt, pass_hash, role) VALUES (?,?,?,?)",
                   (login_, salt, _hash(salt, password), role))
        return "created"
    result = await asyncio.to_thread(_save)
    return {"ok": True, "result": result}


@users_router.delete("/{login_}")
async def delete_user(login_: str, request: Request):
    import asyncio
    import db
    me_ = getattr(request.state, "user", None) or {}
    if me_.get("login") == login_:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")

    def _del():
        owners = db.fetchone("SELECT COUNT(*) FROM users WHERE role = 'owner' AND login != ?", (login_,))[0]
        row = db.fetchone("SELECT role FROM users WHERE login = ?", (login_,))
        if row and row[0] == "owner" and owners == 0:
            raise HTTPException(status_code=400, detail="Нельзя удалить последнего владельца")
        db.execute("DELETE FROM users WHERE login = ?", (login_,))
        db.execute("DELETE FROM sessions WHERE login = ?", (login_,))
    await asyncio.to_thread(_del)
    return {"ok": True}
