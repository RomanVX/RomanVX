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


async def get_phrases(campaign_ids: list[str], date_from: str, date_to: str) -> list[dict]:
    """Отчёт по поисковым фразам (async): заказать → дождаться → скачать ZIP
    из CSV по каждой кампании → распарсить. Только SKU/search-promo кампании.

    Возвращает [{campaign_id, phrase, views, clicks, spent, orders, orders_money}]."""
    import asyncio
    import io
    import zipfile
    import csv as _csv

    st, data = await api_post("/api/client/statistics/phrases", {
        "campaigns": [str(c) for c in campaign_ids],
        "dateFrom": date_from, "dateTo": date_to})
    if st != 200 or not isinstance(data, dict) or not data.get("UUID"):
        _log.warning("Ozon phrases request: HTTP %s %s", st, str(data)[:200])
        return []
    uuid = data["UUID"]

    # ждём готовности отчёта
    for _ in range(40):
        await asyncio.sleep(3)
        s2, meta = await api_get(f"/api/client/statistics/{uuid}")
        state = meta.get("state") if isinstance(meta, dict) else None
        if state == "OK":
            break
        if state == "ERROR":
            _log.warning("Ozon phrases report ERROR: %s", str(meta)[:200])
            return []

    # скачиваем архив
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.get(f"{BASE}/api/client/statistics/report",
                        headers=await _headers(), params={"UUID": uuid})
    if r.status_code != 200 or not r.content:
        _log.warning("Ozon phrases report download: HTTP %s", r.status_code)
        return []

    out = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        _log.warning("Ozon phrases: не ZIP (%s)", r.content[:80])
        return []
    for name in zf.namelist():
        cid = name.split("_")[0]
        raw = zf.read(name).decode("utf-8-sig", errors="replace")
        # CSV Ozon с разделителем ; и заголовком-строкой
        reader = _csv.reader(io.StringIO(raw), delimiter=";")
        header = None
        for row in reader:
            if not row or len(row) < 2:
                continue
            low = [c.strip().lower() for c in row]
            if header is None:
                # ищем строку заголовка (содержит «запрос»/«фраз»)
                if any("запрос" in c or "фраз" in c or "поиск" in c for c in low):
                    header = low
                continue
            rec = dict(zip(header, row))
            def g(*keys):
                for k in keys:
                    for h, v in rec.items():
                        if k in h:
                            return v
                return ""
            phrase = g("запрос", "фраз", "поисков")
            if not phrase or phrase.lower().startswith("итого"):
                continue
            out.append({
                "campaign_id": cid, "phrase": phrase,
                "views": int(_num(g("показ"))),
                "clicks": int(_num(g("клик"))),
                "spent": _num(g("расход", "затрат", "потрач")),
                "orders": int(_num(g("заказ"))),
                "orders_money": _num(g("выручк", "сумма заказов", "продаж")),
            })
    _log.info("Ozon phrases: %d строк из %d файлов", len(out), len(zf.namelist()))
    return out


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
