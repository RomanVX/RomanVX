"""Async client for Wildberries Advertising (Advert) API.

WB has TWO possible advert API bases; we try both.
Also, the GET /adv/v1/promotion/adverts endpoint returns EITHER:
  - a list of integers  [12345, 67890, ...]
  - a list of objects   [{advertId: 12345, ...}, ...]
depending on WB API version. We handle both.

Requires a separate WB_ADVERT_KEY (set in Render env vars as WB_ADVERT_KEY).
Falls back to WB_API_KEY if WB_ADVERT_KEY is not set.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from config import WB_ADVERT_KEY

_log = logging.getLogger(__name__)

# WB has been migrating between these two domains — try both
_BASES = [
    "https://advert-api.wb.ru",
    "https://advert-api.wildberries.ru",
]

TYPE_NAMES   = {4: "Каталог", 5: "Поиск+Каталог", 6: "Поиск", 7: "Поиск", 8: "Автокампания", 9: "Поиск"}
STATUS_NAMES = {-1: "Удалена", 4: "Готова", 7: "Активна", 8: "Пауза", 9: "Завершена", 11: "Пауза WB"}


def _headers() -> dict:
    return {"Authorization": WB_ADVERT_KEY, "Content-Type": "application/json"}


async def _get(path: str, params: dict | None = None) -> list | dict:
    last_exc = None
    for base in _BASES:
        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(url, headers=_headers(), params=params or {})
                _log.info("[ADVERT] GET %s params=%s → %s body[:200]=%s",
                          url, params, r.status_code, r.text[:200])
                if r.status_code == 204:
                    return []
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            _log.warning("[ADVERT] GET %s FAILED: %s", url, e)
    raise last_exc


async def _post(path: str, body) -> list | dict:
    last_exc = None
    for base in _BASES:
        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(url, headers=_headers(), json=body)
                _log.info("[ADVERT] POST %s payload_len=%d → %s body[:200]=%s",
                          url, len(body) if isinstance(body, list) else 1,
                          r.status_code, r.text[:200])
                if r.status_code == 204:
                    return []
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            _log.warning("[ADVERT] POST %s FAILED: %s", url, e)
    raise last_exc


def _extract_ids(data) -> list[int]:
    """Handle both [int, ...] and [{advertId: int, ...}, ...] response formats."""
    if not isinstance(data, list):
        return []
    ids = []
    for item in data:
        if isinstance(item, int):
            ids.append(item)
        elif isinstance(item, dict):
            aid = item.get("advertId") or item.get("id")
            if aid:
                ids.append(int(aid))
    return ids


async def _ids_for_status(status: int) -> list[int]:
    """GET /adv/v1/promotion/adverts?status=S — returns campaign IDs for that status."""
    try:
        data = await _get("/adv/v1/promotion/adverts",
                          {"status": status, "limit": 100, "offset": 0})
        _log.info("[ADVERT] status=%d raw type=%s raw[:3]=%s",
                  status, type(data).__name__, str(data)[:300])
        ids = _extract_ids(data)
        _log.info("[ADVERT] status=%d → %d IDs extracted", status, len(ids))
        return ids
    except Exception as e:
        _log.warning("[ADVERT] ids_for_status(%s): %s", status, e)
        return []


async def get_all_campaign_ids() -> list[int]:
    """Fetch IDs for active (7), paused (8) and ready (4) campaigns."""
    results = await asyncio.gather(
        _ids_for_status(7),
        _ids_for_status(8),
        _ids_for_status(4),
    )
    seen, ids = set(), []
    for chunk in results:
        for i in chunk:
            if i not in seen:
                seen.add(i)
                ids.append(i)
    _log.info("[ADVERT] get_all_campaign_ids → %d IDs: %s", len(ids), ids[:10])
    return ids


async def get_campaign_details(ids: list[int]) -> list[dict]:
    """POST /adv/v1/promotion/adverts body=[id, ...] → campaign objects."""
    if not ids:
        return []
    result = await _post("/adv/v1/promotion/adverts", ids[:100])
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    return []


def _is_bonus_payment(rec: dict) -> bool:
    """Списание оплачено промо-бонусами WB (поддержка от площадки)."""
    for key in ("paymentType", "payment_type", "type"):
        v = rec.get(key)
        if isinstance(v, str) and "бонус" in v.lower():
            return True
    return False


async def get_spend_by_month(date_from: datetime, date_to: datetime) -> dict[str, dict]:
    """GET /adv/v1/upd — история списаний за рекламу.

    Возвращает {YYYY-MM: {"total": всего, "bonus": из них промо-бонусами}}.
    API отдаёт максимум 31 день за запрос — ходим чанками.
    """
    spend: dict[str, dict] = {}
    cur = date_from
    while cur <= date_to:
        chunk_end = min(cur + timedelta(days=30), date_to)
        try:
            data = await _get("/adv/v1/upd", {
                "from": cur.strftime("%Y-%m-%d"),
                "to":   chunk_end.strftime("%Y-%m-%d"),
            })
            for rec in data if isinstance(data, list) else []:
                mk = (rec.get("updTime") or "")[:7]
                if not mk:
                    continue
                m = spend.setdefault(mk, {"total": 0.0, "bonus": 0.0})
                amt = float(rec.get("updSum") or 0)
                m["total"] += amt
                if _is_bonus_payment(rec):
                    m["bonus"] += amt
        except Exception as e:
            _log.warning("[ADVERT] upd %s–%s failed: %s", cur.date(), chunk_end.date(), e)
        cur = chunk_end + timedelta(days=1)
    _log.info("[ADVERT] spend by month: %s",
              {k: (round(v["total"]), round(v["bonus"])) for k, v in spend.items()})
    return spend


async def get_fullstats(ids: list[int], date_from: datetime, date_to: datetime) -> list[dict]:
    """POST /adv/v2/fullstats — per-campaign aggregated stats."""
    if not ids:
        return []

    dates = []
    cur = date_from
    while cur <= date_to and len(dates) < 31:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    payload = [{"id": i, "dates": dates} for i in ids[:100]]
    _log.info("[ADVERT] fullstats payload preview (first 3): %s", payload[:3])
    try:
        result = await _post("/adv/v2/fullstats", payload)
        _log.info("[ADVERT] fullstats result type=%s len=%s preview=%s",
                  type(result).__name__,
                  len(result) if isinstance(result, list) else "n/a",
                  str(result)[:300])
        return result if isinstance(result, list) else []
    except Exception as e:
        _log.warning("[ADVERT] fullstats error: %s", e)
        return []
