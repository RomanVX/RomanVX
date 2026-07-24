"""Агент видит весь дашборд: каталог эндпоинтов из нашего же OpenAPI
и возможность дёрнуть любой GET напрямую, без сети.

Вместо ручных обёрток на каждый инструмент агент получает список всех
доступных методов дашборда (тот же swagger, что у /docs) и универсальный
вызов. Запросы идут внутрь процесса через ASGI — без HTTP-порта, без
лишней памяти на второй клиент.

Только GET и только чтение: POST/PATCH/DELETE агенту недоступны, менять
кабинет он может исключительно через agent_actions с подтверждением.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

import db

_log = logging.getLogger("agent_api")

_catalog_cache: list | None = None
_token_cache: str = ""

# шумные и служебные пути в каталог не отдаём
_SKIP = ("/api/auth", "/openapi", "/docs", "/redoc", "/static",
         "/probe", "/sandbox", "/export", "/invalidate", "/health")


def _internal_token() -> str:
    """Долгоживущая owner-сессия для внутренних вызовов агента."""
    global _token_cache
    if _token_cache:
        return _token_cache
    try:
        row = db.fetchone(
            "SELECT token FROM sessions WHERE login = ? AND expires > ?",
            ("agent-internal", datetime.utcnow().isoformat()))
        if row:
            _token_cache = row[0]
            return _token_cache
        token = uuid.uuid4().hex
        db.execute(
            "INSERT INTO sessions (token, login, role, expires) VALUES (?,?,?,?)",
            (token, "agent-internal", "owner",
             (datetime.utcnow() + timedelta(days=3650)).isoformat()))
        _token_cache = token
    except Exception as e:
        _log.warning("internal token: %s", e)
    return _token_cache


def catalog() -> list[dict]:
    """Каталог GET-эндпоинтов дашборда: путь, что делает, параметры."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    try:
        import main
        spec = main.app.openapi()
    except Exception as e:
        _log.warning("openapi: %s", e)
        return []
    out = []
    for path, item in (spec.get("paths") or {}).items():
        if any(s in path for s in _SKIP):
            continue
        op = item.get("get")
        if not op:
            continue
        params = [p.get("name") for p in (op.get("parameters") or [])
                  if p.get("name") not in ("request",)]
        out.append({"path": path,
                    "что": (op.get("summary") or op.get("description") or
                            "").strip().split("\n")[0][:120],
                    "параметры": params or None})
    _catalog_cache = sorted(out, key=lambda x: x["path"])
    return _catalog_cache


async def call(path: str, params: dict | None = None) -> str:
    """Вызов GET-эндпоинта дашборда внутри процесса (ASGI, без сети)."""
    if not path.startswith("/"):
        path = "/" + path
    if any(s in path for s in ("/auth", "/openapi")):
        return "этот путь агенту недоступен"
    import httpx
    import main
    try:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://internal",
                                     timeout=120) as c:
            r = await c.get(path, params=params or {},
                            cookies={"mp_session": _internal_token()})
    except Exception as e:
        return f"ошибка вызова {path}: {str(e)[:250]}"
    if r.status_code == 404:
        near = [e["path"] for e in catalog()
                if path.strip("/").split("/")[-1] in e["path"]][:5]
        return (f"404 {path}." +
                (f" Похожие пути: {', '.join(near)}" if near else
                 " Посмотри каталог инструментом dashboard_catalog."))
    if not r.is_success:
        return f"{r.status_code} на {path}: {r.text[:300]}"
    try:
        data = r.json()
    except Exception:
        return r.text[:6000]
    return json.dumps(data, ensure_ascii=False, default=str)
