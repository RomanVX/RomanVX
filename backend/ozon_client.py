"""Async client for Ozon Seller API — stocks and FBO sales."""
import logging
from datetime import datetime, timedelta

import httpx

from config import OZON_CLIENT_ID, OZON_API_KEY

_log = logging.getLogger(__name__)
_BASE = "https://api-seller.ozon.ru"


def _headers() -> dict:
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{_BASE}{path}", headers=_headers(), json=body)
        _log.debug("OZON POST %s → %s", path, r.status_code)
        if not r.is_success:
            _log.error("OZON %s → %s %s", path, r.status_code, r.text[:300])
            r.raise_for_status()
        return r.json()


async def _get_all_skus() -> list[dict]:
    """Paginate /v4/product/info/attributes → list of {sku, offer_id}."""
    items: list[dict] = []
    last_id = ""
    while True:
        data = await _post("/v4/product/info/attributes", {
            "filter": {"visibility": "ALL"},
            "last_id": last_id,
            "limit": 1000,
        })
        result = data.get("result") or []
        if not result:
            break
        for r in result:
            items.append({"sku": str(r.get("sku", "")), "offer_id": r.get("offer_id", "")})
        last_id = data.get("last_id", "")
        if not last_id or len(result) < 1000:
            break
    _log.info("OZON: got %d SKUs", len(items))
    return items


async def get_stocks() -> dict[str, int]:
    """Return {offer_id: available_stock_count} — all articles."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return {}
    try:
        skus_info = await _get_all_skus()
        sku_list = [s["sku"] for s in skus_info if s["sku"]]
        sku_to_offer = {s["sku"]: s["offer_id"] for s in skus_info}

        result: dict[str, int] = {}
        # Batch by 100
        for i in range(0, len(sku_list), 100):
            batch = sku_list[i:i + 100]
            data = await _post("/v1/analytics/stocks", {"skus": batch})
            for item in data.get("items") or []:
                offer_id = sku_to_offer.get(str(item.get("sku", "")))
                if offer_id:
                    result[offer_id] = result.get(offer_id, 0) + (item.get("available_stock_count") or 0)
        _log.info("OZON stocks: %d articles", len(result))
        return result
    except Exception as e:
        _log.warning("OZON get_stocks error: %s", e)
        return {}


async def get_sales_28d() -> dict[str, float]:
    """Return {offer_id: avg_daily_qty} over last 28 days via FBO postings."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return {}
    try:
        dt_to   = datetime.utcnow()
        dt_from = dt_to - timedelta(days=28)
        since = dt_from.strftime("%Y-%m-%dT00:00:00.000Z")
        to    = dt_to.strftime("%Y-%m-%dT23:59:59.000Z")

        totals: dict[str, int] = {}
        offset = 0
        while True:
            data = await _post("/v2/posting/fbo/list", {
                "dir": "ASC",
                "filter": {"since": since, "to": to, "status": ""},
                "limit": 1000,
                "offset": offset,
            })
            result = data.get("result") or []
            if not result:
                break
            for posting in result:
                for prod in posting.get("products") or []:
                    oid = prod.get("offer_id", "")
                    qty = prod.get("quantity") or 0
                    if oid:
                        totals[oid] = totals.get(oid, 0) + qty
            offset += len(result)
            if len(result) < 1000:
                break

        _log.info("OZON sales 28d: %d articles", len(totals))
        return {k: round(v / 28, 2) for k, v in totals.items()}
    except Exception as e:
        _log.warning("OZON get_sales_28d error: %s", e)
        return {}
