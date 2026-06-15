"""Async client for Yandex Market Partner API — stocks and orders."""
import logging
from datetime import datetime, timedelta

import httpx

from config import YM_API_KEY, YM_CAMPAIGN_ID, YM_BUSINESS_ID

_log = logging.getLogger(__name__)
_BASE = "https://api.partner.market.yandex.ru"


def _headers() -> dict:
    return {
        "Api-Key": YM_API_KEY,
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{_BASE}{path}", headers=_headers(), json=body)
        top_keys = list(r.json().keys()) if r.is_success else []
        _log.info("[YM] POST %s → %s | top_keys=%s | resp[:500]=%s",
                  path, r.status_code, top_keys, r.text[:500])
        if not r.is_success:
            r.raise_for_status()
        return r.json()


async def get_stocks() -> dict[str, int]:
    """Return {offer_id: count} for FIT (ready-to-ship) items."""
    if not YM_API_KEY or not YM_CAMPAIGN_ID:
        return {}
    try:
        data = await _post(
            f"/campaigns/{YM_CAMPAIGN_ID}/offers/stocks",
            {"limit": 100},
        )
        result: dict[str, int] = {}
        for wh in (data.get("result") or {}).get("warehouses") or []:
            for offer in wh.get("offers") or []:
                offer_id = offer.get("offerId", "")
                if not offer_id:
                    continue
                for stock in offer.get("stocks") or []:
                    if stock.get("type") == "FIT":
                        result[offer_id] = result.get(offer_id, 0) + (stock.get("count") or 0)
        _log.info("YM stocks: %d articles", len(result))
        return result
    except Exception as e:
        _log.warning("YM get_stocks error: %s", e)
        return {}


async def get_sales_28d() -> dict[str, float]:
    """Return {offer_id: avg_daily_qty} over last 28 days via business orders."""
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return {}
    try:
        dt_to   = datetime.utcnow()
        dt_from = dt_to - timedelta(days=28)
        from_date = dt_from.strftime("%d-%m-%Y")
        to_date   = dt_to.strftime("%d-%m-%Y")

        totals: dict[str, int] = {}
        next_page_token = None

        while True:
            body: dict = {
                "filter": {"fromDate": from_date, "toDate": to_date},
                "limit": 50,
            }
            if next_page_token:
                body["pageToken"] = next_page_token

            data = await _post(
                f"/v1/businesses/{YM_BUSINESS_ID}/orders",
                body,
            )
            # Response structure: {"orders": [...], "paging": {"nextPageToken": "..."}}
            orders = data.get("orders") or []
            _log.info("[YM] orders page: got %d orders", len(orders))
            if not orders:
                break
            for order in orders:
                for item in order.get("items") or []:
                    oid = item.get("offerId", "")
                    qty = item.get("count") or 0
                    if oid:
                        totals[oid] = totals.get(oid, 0) + qty
            next_page_token = (data.get("paging") or {}).get("nextPageToken")
            if not next_page_token or len(orders) < 50:
                break

        _log.info("YM sales 28d: %d articles", len(totals))
        return {k: round(v / 28, 2) for k, v in totals.items()}
    except Exception as e:
        _log.warning("YM get_sales_28d error: %s", e)
        return {}
