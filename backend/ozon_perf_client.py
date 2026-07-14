"""Клиент Ozon Performance API (реклама) — отдельный сервис со своей
OAuth-авторизацией (client_id/client_secret → Bearer token).

ENV: OZON_PERF_CLIENT_ID, OZON_PERF_CLIENT_SECRET.
Хост: https://api-performance.ozon.ru
"""
import logging
import os
import time

import httpx

_log = logging.getLogger(__name__)
BASE = "https://api-performance.ozon.ru"

CLIENT_ID = os.getenv("OZON_PERF_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("OZON_PERF_CLIENT_SECRET", "").strip()

_token: str = ""
_token_exp: float = 0.0


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


async def _get_token() -> str:
    """Bearer-токен Performance API (кэш до истечения)."""
    global _token, _token_exp
    if _token and time.monotonic() < _token_exp - 60:
        return _token
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{BASE}/api/client/token", json={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        })
        r.raise_for_status()
        data = r.json()
    _token = data.get("access_token") or ""
    _token_exp = time.monotonic() + int(data.get("expires_in") or 1800)
    _log.info("Ozon Performance: токен получен (живёт %ss)", data.get("expires_in"))
    return _token


async def _headers() -> dict:
    return {"Authorization": f"Bearer {await _get_token()}",
            "Content-Type": "application/json"}


async def api_get(path: str, params: dict | None = None) -> tuple[int, object]:
    """GET к Performance API. Возвращает (status, json|text)."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{BASE}{path}", headers=await _headers(), params=params or {})
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:2000]


async def api_post(path: str, body: dict) -> tuple[int, object]:
    """POST к Performance API. Возвращает (status, json|text)."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{BASE}{path}", headers=await _headers(), json=body)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:2000]


async def get_campaigns() -> list[dict]:
    """Список рекламных кампаний."""
    st, data = await api_get("/api/client/campaign")
    if st != 200 or not isinstance(data, dict):
        _log.warning("Ozon Perf campaigns: HTTP %s %s", st, str(data)[:200])
        return []
    return data.get("list") or data.get("campaigns") or []


def _num(v) -> float:
    """'1 197,85' / '63' → float (Ozon отдаёт числа строками с запятой)."""
    if v is None:
        return 0.0
    s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


async def get_daily(date_from: str, date_to: str) -> list[dict]:
    """Ежедневная статистика по кампаниям: показы, клики, расход, заказы,
    выручка. Один вызов даёт все кампании за период."""
    st, data = await api_get("/api/client/statistics/daily/json",
                             {"dateFrom": date_from, "dateTo": date_to})
    if st != 200 or not isinstance(data, dict):
        _log.warning("Ozon Perf daily: HTTP %s %s", st, str(data)[:200])
        return []
    out = []
    for r in data.get("rows") or []:
        out.append({
            "id": str(r.get("id") or ""),
            "title": r.get("title") or "",
            "date": (r.get("date") or "")[:10],
            "views": int(_num(r.get("views"))),
            "clicks": int(_num(r.get("clicks"))),
            "spent": _num(r.get("moneySpent")),
            "orders": int(_num(r.get("orders"))),
            "orders_money": _num(r.get("ordersMoney")),
        })
    return out
