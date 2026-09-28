"""Аналитический дашборд МПФИТ (фулфилмент Market Partners).

Отдельный лёгкий сервис Render: свой процесс и память, общая база
mp-postgres со своей схемой (DB_SCHEMA=mpfit). Пул соединений берём из
backend/db.py — тот же проверенный код, что у кабинетов Biomed/ФК.

Env: DATABASE_URL, DB_SCHEMA=mpfit, MPFIT_TOKEN (когда выдадут),
DASH_USER / DASH_PASSWORD (вход в дашборд, Basic Auth).
"""
import asyncio
import base64
import logging
import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import db  # noqa: E402  (backend/db.py: пул Postgres + схема)

import mpfit_client  # noqa: E402

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("mpfit_dash")
HERE = Path(__file__).resolve().parent
app = FastAPI(title="MPFIT Analytics")

_PUBLIC = ("/health",)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Финансы внутри — без пароля не пускаем. Пока DASH_PASSWORD не задан,
    сервис закрыт целиком (кроме /health для проверок Render)."""
    if request.url.path in _PUBLIC:
        return await call_next(request)
    user = (os.environ.get("DASH_USER") or "admin").strip()
    pwd = (os.environ.get("DASH_PASSWORD") or "").strip()
    if not pwd:
        return Response("Задайте DASH_PASSWORD в настройках Render", status_code=503)
    auth = request.headers.get("authorization", "")
    ok = False
    if auth.lower().startswith("basic "):
        try:
            u, _, p = base64.b64decode(auth[6:]).decode().partition(":")
            ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, pwd)
        except Exception:
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="MPFIT"'})
    return await call_next(request)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/status")
async def status():
    """Состояние среды: база, схема, токен МПФИТ и живая проверка API."""
    out: dict = {"db": None, "schema": db.DB_SCHEMA or "public",
                 "mpfit_token": bool(mpfit_client.token()), "mpfit": None}
    try:
        await asyncio.to_thread(
            db.execute, "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        out["db"] = "postgres" if db.IS_PG else "sqlite"
    except Exception as e:
        out["db"] = f"ошибка: {str(e)[:200]}"
    if out["mpfit_token"]:
        try:
            out["mpfit"] = await mpfit_client.ping()
        except Exception as e:
            out["mpfit"] = {"ok": False, "error": str(e)[:300]}
    return JSONResponse(out)


# Разрешены только методы чтения из спеки (list/GET). Создание заказов,
# приёмок, пополнение баланса и правка оплаты сюда не пропускаются.
_READ_POST = {"/v1/services/list", "/v1/products/list", "/v1/products/stocks",
              "/v1/arrivals/list", "/v1/orders/list", "/v1/orders/types/list",
              "/v1/companies/list", "/v1/products/categories/list",
              "/v1/invoices/list", "/v1/analytics/storage/list", "/v1/cim-codes"}


@app.post("/api/probe")
async def probe(payload: dict):
    """Прокси на чтение к API МПФИТ для разработки: {path, body} или
    {path} для GET /v1/orders/{id} и /v1/arrivals/{id}."""
    import re
    path = str(payload.get("path") or "")
    if re.fullmatch(r"/v1/(orders|arrivals)/\d+", path):
        return await mpfit_client.call("GET", path)
    if path not in _READ_POST:
        return JSONResponse({"error": "метод не разрешён (только чтение)"}, status_code=400)
    return await mpfit_client.call("POST", path, payload.get("body") or {"limit": 10, "last_id": 0})


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html",
                        headers={"Cache-Control": "no-cache"})
