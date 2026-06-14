"""Async client for Wildberries Advertising (Advert) API."""
import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from config import WB_API_KEY

_log = logging.getLogger(__name__)

ADVERT_BASE = "https://advert-api.wildberries.ru"

TYPE_NAMES   = {4: "Каталог", 5: "Поиск+Каталог", 6: "Поиск", 7: "Поиск", 8: "Автокампания", 9: "Поиск"}
STATUS_NAMES = {-1: "Удалена", 4: "Готова", 7: "Активна", 8: "Пауза", 9: "Завершена", 11: "Пауза WB"}


def _headers() -> dict:
    return {"Authorization": WB_API_KEY, "Content-Type": "application/json"}


async def _get(path: str, params: dict | None = None) -> list | dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{ADVERT_BASE}{path}", headers=_headers(), params=params or {})
        if r.status_code == 204:
            return []
        _log.debug("GET %s → %s", path, r.status_code)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body) -> list | dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{ADVERT_BASE}{path}", headers=_headers(), json=body)
        if r.status_code == 204:
            return []
        _log.debug("POST %s → %s", path, r.status_code)
        r.raise_for_status()
        return r.json()


async def _ids_for(status: int, type_: int) -> list[int]:
    try:
        data = await _get("/adv/v1/promotion/adverts",
                          {"status": status, "type": type_, "limit": 100, "offset": 0})
        if isinstance(data, list):
            return [x for x in data if isinstance(x, int)]
        return []
    except Exception:
        return []


async def get_all_campaign_ids() -> list[int]:
    """Fetch IDs for active, paused and ready campaigns across all types."""
    tasks = [
        _ids_for(status, type_)
        for status in (7, 8, 4)       # active, paused, ready
        for type_ in (4, 5, 6, 8, 9)  # all ad types
    ]
    results = await asyncio.gather(*tasks)
    seen, ids = set(), []
    for chunk in results:
        for i in chunk:
            if i not in seen:
                seen.add(i)
                ids.append(i)
    return ids


async def get_campaign_details(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    result = await _post("/adv/v1/promotion/adverts", ids[:100])
    return result if isinstance(result, list) else []


async def get_fullstats(ids: list[int], date_from: datetime, date_to: datetime) -> list[dict]:
    """POST /adv/v2/fullstats — aggregated stats per campaign."""
    if not ids:
        return []

    # Build date list (max 31 days per request)
    dates = []
    cur = date_from
    while cur <= date_to and len(dates) < 31:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    # WB allows max 100 ids per request
    payload = [{"id": i, "dates": dates} for i in ids[:100]]
    try:
        result = await _post("/adv/v2/fullstats", payload)
        return result if isinstance(result, list) else []
    except Exception as e:
        _log.warning("fullstats error: %s", e)
        return []
