"""Async client for Wildberries Statistics API."""
import logging
from datetime import datetime, timedelta

import httpx

from config import WB_API_KEY, USE_MOCK
import mock_data

_log = logging.getLogger(__name__)

STATS_BASE = "https://statistics-api.wildberries.ru/api/v1"


def _headers() -> dict:
    return {"Authorization": WB_API_KEY}


async def _get(url: str, params: dict) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        if not resp.is_success:
            _log.error("WB API %s -> %s: %s", url, resp.status_code, resp.text[:200])
        resp.raise_for_status()
        return resp.json()


async def get_sales(date_from: datetime, date_to: datetime) -> list[dict]:
    if USE_MOCK:
        return mock_data.generate_sales(date_from, date_to)
    return await _get(
        f"{STATS_BASE}/supplier/sales",
        {"dateFrom": date_from.strftime("%Y-%m-%dT00:00:00"), "flag": 0},
    )


async def get_orders(date_from: datetime, date_to: datetime) -> list[dict]:
    if USE_MOCK:
        return mock_data.generate_orders(date_from, date_to)
    return await _get(
        f"{STATS_BASE}/supplier/orders",
        {"dateFrom": date_from.strftime("%Y-%m-%dT00:00:00"), "flag": 0},
    )


async def get_stocks() -> list[dict]:
    if USE_MOCK:
        return mock_data.generate_stocks()
    date_from = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    return await _get(
        f"{STATS_BASE}/supplier/stocks",
        {"dateFrom": date_from},
    )
