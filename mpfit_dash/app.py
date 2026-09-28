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
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import db  # noqa: E402  (backend/db.py: пул Postgres + схема)

import mpfit_client  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
import sync  # noqa: E402

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("mpfit_dash")
HERE = Path(__file__).resolve().parent
app = FastAPI(title="MPFIT Analytics")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

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


_bg: set = set()


@app.on_event("startup")
async def _startup():
    await asyncio.to_thread(store.init)
    t = asyncio.create_task(sync.loop())
    _bg.add(t)


def _user(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    try:
        return base64.b64decode(auth[6:]).decode().partition(":")[0]
    except Exception:
        return ""


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
    out["sync"] = {k: v for k, v in sync.state.items()}
    return JSONResponse(out)


async def _t(fn, *a):
    return await asyncio.to_thread(fn, *a)


@app.get("/api/summary")
async def api_summary():
    return {**await _t(report.summary), "sync": sync.state}


@app.get("/api/daily")
async def api_daily(days: int = 60):
    return await _t(report.daily, max(7, min(days, 365)))


@app.get("/api/speed")
async def api_speed(days: int = 30):
    return await _t(report.speed, max(7, min(days, 365)))


@app.get("/api/services")
async def api_services():
    return await _t(report.services)


@app.get("/api/clients")
async def api_clients():
    return await _t(report.clients)


@app.get("/api/finance")
async def api_finance(months: int = 6):
    return await _t(report.finance, max(1, min(months, 24)))


@app.get("/api/planfact")
async def api_planfact():
    return await _t(report.plan_fact)


@app.get("/api/plan")
async def api_plan():
    return await _t(store.plan_get)


@app.post("/api/plan")
async def api_plan_set(payload: dict):
    month = str(payload.get("month") or "")[:7]
    for metric in ("orders", "revenue", "expenses", "profit"):
        if metric in payload:
            await _t(store.plan_set, month, metric, payload[metric])
    return {"ok": True}


@app.get("/api/ledger")
async def api_ledger():
    return {"rows": await _t(store.ledger_list), "kinds": store.KINDS, "categories": store.CATEGORIES}


def _check_entry(e: dict):
    from datetime import date
    date.fromisoformat(str(e.get("date"))[:10])
    if e.get("kind") not in store.KINDS:
        raise ValueError("kind")
    if float(e.get("amount")) <= 0:
        raise ValueError("amount")


@app.post("/api/ledger")
async def api_ledger_add(payload: dict, request: Request):
    try:
        _check_entry(payload)
    except Exception:
        return JSONResponse({"error": "проверьте дату, тип и сумму"}, status_code=400)
    return {"id": await _t(store.ledger_add, payload, _user(request))}


@app.put("/api/ledger/{entry_id}")
async def api_ledger_put(entry_id: str, payload: dict):
    try:
        _check_entry(payload)
    except Exception:
        return JSONResponse({"error": "проверьте дату, тип и сумму"}, status_code=400)
    await _t(store.ledger_update, entry_id, payload)
    return {"ok": True}


@app.delete("/api/ledger/{entry_id}")
async def api_ledger_del(entry_id: str):
    await _t(store.ledger_delete, entry_id)
    return {"ok": True}


@app.post("/api/sync")
async def api_sync():
    t = asyncio.create_task(sync.run_once())
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return {"scheduled": True}


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
