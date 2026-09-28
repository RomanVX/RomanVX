"""Клиент REST API МПФИТ (спека: docs/mpfit_api/openapi.yaml, v1.4.0).

База https://app.mpfit.ru/api, Bearer-токен (env MPFIT_TOKEN, выдаёт
поддержка МПФИТ; токен компании-фулфилмента видит всех её селлеров).
Лимит 120 запросов в минуту на все методы. Пагинация курсорная:
limit + last_id, первый запрос last_id=0, конец — пустой data / last_id=None.
"""
import asyncio
import logging
import os

import httpx

BASE = "https://app.mpfit.ru/api"
_log = logging.getLogger("mpfit")
_client: httpx.AsyncClient | None = None


def token() -> str:
    return (os.environ.get("MPFIT_TOKEN") or "").strip()


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=BASE, timeout=60)
    return _client


async def call(method: str, path: str, body: dict | None = None) -> dict:
    """Один запрос с учётом лимита: на 429 ждём и повторяем."""
    if not token():
        raise RuntimeError("MPFIT_TOKEN не задан")
    headers = {"Authorization": f"Bearer {token()}", "Accept": "application/json"}
    for attempt in range(4):
        r = await _http().request(method, path, headers=headers, json=body)
        if r.status_code == 429:
            await asyncio.sleep(5 * (attempt + 1))
            continue
        if not r.is_success:
            raise RuntimeError(f"МПФИТ {path} → {r.status_code}: {r.text[:300]}")
        left = r.headers.get("X-RateLimit-Remaining")
        if left is not None and left.isdigit() and int(left) < 5:
            await asyncio.sleep(3)      # не выбираем лимит до дна
        return r.json()
    raise RuntimeError(f"МПФИТ {path}: лимит запросов, 4 попытки")


async def paginate(path: str, flt: dict | None = None, limit: int = 100,
                   max_pages: int = 1000) -> list[dict]:
    """Все элементы списка через курсор last_id."""
    out: list[dict] = []
    last_id = 0
    for _ in range(max_pages):
        body = {"limit": limit, "last_id": last_id}
        if flt:
            body["filter"] = flt
        res = (await call("POST", path, body)).get("result") or {}
        data = res.get("data") or []
        out.extend(data)
        last_id = res.get("last_id")
        if not data or last_id is None:
            break
    return out


async def ping() -> dict:
    """Проверка связки: список компаний, доступных токену."""
    companies = await paginate("/v1/companies/list", limit=100, max_pages=5)
    return {"ok": True, "companies": len(companies),
            "sample": [c.get("name") or c.get("title") for c in companies[:5]]}
