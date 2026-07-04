"""Async client for Ozon Seller API — stocks and FBO sales."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx

from config import OZON_CLIENT_ID, OZON_API_KEY

_log = logging.getLogger(__name__)
_BASE = "https://api-seller.ozon.ru"

_CACHE_TTL = 3600        # 1 hour (sales)
_STOCKS_CACHE_TTL = 7200  # 2 hours (stocks — resilient to intermittent 500s)

_stocks_cache: dict[str, int] = {}
_stocks_ts: float = 0.0
_sales_cache: dict[str, float] = {}
_sales_ts: float = 0.0
_lock = asyncio.Lock()


def _headers() -> dict:
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


# Общий клиент с keep-alive (без него каждый запрос = новый TLS handshake)
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60)
    return _client


async def _post(path: str, body: dict) -> dict:
    r = await _http().post(f"{_BASE}{path}", headers=_headers(), json=body)
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


async def _fetch_stocks_v1(skus_info: list[dict]) -> dict[str, int]:
    """POST /v1/analytics/stocks by SKU batches — original endpoint."""
    sku_list = [s["sku"] for s in skus_info if s["sku"]]
    sku_to_offer = {s["sku"]: s["offer_id"] for s in skus_info}
    result: dict[str, int] = {}
    for i in range(0, len(sku_list), 100):
        batch = sku_list[i:i + 100]
        data = await _post("/v1/analytics/stocks", {"skus": batch})
        for item in data.get("items") or []:
            offer_id = sku_to_offer.get(str(item.get("sku", "")))
            if offer_id:
                result[offer_id] = result.get(offer_id, 0) + (item.get("available_stock_count") or 0)
    _log.info("OZON stocks v1: %d articles", len(result))
    return result


async def _fetch_stocks_warehouses() -> dict[str, int]:
    """POST /v2/analytics/stock_on_warehouses — fallback endpoint."""
    result: dict[str, int] = {}
    offset = 0
    while True:
        data = await _post("/v2/analytics/stock_on_warehouses", {
            "limit": 1000,
            "offset": offset,
            "warehouse_type": "ALL",
        })
        rows = (data.get("result") or {}).get("rows") or []
        if not rows:
            break
        for row in rows:
            oid = row.get("offer_id", "")
            if not oid:
                continue
            result[oid] = result.get(oid, 0) + (row.get("free_to_sell_amount") or 0)
        offset += len(rows)
        if len(rows) < 1000:
            break
    _log.info("OZON stocks warehouses: %d articles", len(result))
    return result


async def get_stocks() -> dict[str, int]:
    """Return {offer_id: stock} — cached 2 hours, v1 with retry → v2 fallback."""
    global _stocks_cache, _stocks_ts
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return {}
    if time.monotonic() - _stocks_ts < _STOCKS_CACHE_TTL and _stocks_cache:
        _log.info("OZON stocks: cache hit (%d articles)", len(_stocks_cache))
        return dict(_stocks_cache)
    async with _lock:
        if time.monotonic() - _stocks_ts < _STOCKS_CACHE_TTL and _stocks_cache:
            return dict(_stocks_cache)
        result: dict[str, int] = {}
        # Try v1 with up to 3 retries
        skus_info = await _get_all_skus()
        for attempt in range(3):
            try:
                result = await _fetch_stocks_v1(skus_info)
                break
            except Exception as e:
                _log.warning("OZON v1/analytics/stocks attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(3)
        # Fallback to v2 if v1 gave nothing
        if not result:
            try:
                result = await _fetch_stocks_warehouses()
            except Exception as e:
                _log.warning("OZON stock_on_warehouses fallback failed: %s", e)
        if result:
            _stocks_cache = result
            _stocks_ts = time.monotonic()
            _log.info("OZON stocks: %d articles (cached)", len(result))
        else:
            _log.warning("OZON stocks: all endpoints failed — using stale cache (%d)", len(_stocks_cache))
        return dict(_stocks_cache)


def _price_amount(v) -> float:
    """v3 отдаёт price объектом {amount, currency}, v2 отдавал строку."""
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return 0.0


async def _fbo_postings_v3(since: str, to: str, limit: int = 1000) -> list[dict]:
    postings: list[dict] = []
    cursor = ""
    while True:
        data = await _post("/v3/posting/fbo/list", {
            "cursor": cursor,
            "filter": {"since": since, "to": to},
            "limit": limit,
            "sort_dir": "ASC",
            "with": {"analytics_data": False, "financial_data": False, "legal_info": False},
        })
        batch = data.get("postings") or (data.get("result") or {}).get("postings") or []
        postings.extend(batch)
        cursor = data.get("cursor") or ""
        if not data.get("has_next") or not cursor or not batch:
            break
    return postings


async def _fbo_postings_v2(since: str, to: str) -> list[dict]:
    """Старый метод — работает до 01.08.2026, держим как фолбэк."""
    postings: list[dict] = []
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
        postings.extend(result)
        offset += len(result)
        if len(result) < 1000:
            break
    return postings


async def _fbo_postings(since: str, to: str) -> list[dict]:
    """Все FBO-отправления за период: v3 (курсорная пагинация), при ошибке
    v3 — лимит 100, затем фолбэк на v2 (жив до 01.08.2026)."""
    try:
        return await _fbo_postings_v3(since, to)
    except Exception as e:
        _log.error("OZON v3/posting/fbo/list (limit 1000) failed: %s — retry limit=100", e)
    try:
        return await _fbo_postings_v3(since, to, limit=100)
    except Exception as e:
        _log.error("OZON v3/posting/fbo/list (limit 100) failed: %s — fallback v2", e)
    return await _fbo_postings_v2(since, to)


def _posting_rows(postings: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for posting in postings:
        order_date = (posting.get("created_at") or "")[:10]
        # в v3 нет delivered_at; fact_delivery_date есть в живых ответах
        delivered_date = ((posting.get("fact_delivery_date") or posting.get("delivered_at") or "")[:10]
                          or order_date)
        status = posting.get("status", "")
        for prod in posting.get("products") or []:
            oid = prod.get("offer_id", "")
            qty = prod.get("quantity") or 0
            price = _price_amount(prod.get("price"))
            if oid and qty:
                rows.append({
                    "date": order_date,
                    "delivered_date": delivered_date,
                    "offer_id": oid, "qty": qty,
                    "revenue": price * qty, "status": status,
                })
    return rows


async def get_sales_detail(date_from: str, date_to: str) -> list[dict]:
    """Return [{date, offer_id, qty, revenue, status}] for given range — no cache."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    since = f"{date_from}T00:00:00.000Z"
    to    = f"{date_to}T23:59:59.000Z"
    rows: list[dict] = []
    try:
        rows = _posting_rows(await _fbo_postings(since, to))
    except Exception as e:
        _log.warning("OZON get_sales_detail error: %s", e)
    _log.info("OZON sales_detail: %d rows for %s–%s", len(rows), date_from, date_to)
    try:
        import sales_history
        sales_history.persist_detail_bg(rows, "Ozon")
    except Exception as e:
        _log.warning("sales_history persist (Ozon) failed: %s", e)
    return rows


async def get_sales_28d() -> dict[str, float]:
    """Return {offer_id: avg_daily_qty} — cached 1 hour."""
    global _sales_cache, _sales_ts
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return {}
    if time.monotonic() - _sales_ts < _CACHE_TTL and _sales_cache:
        _log.info("OZON sales: cache hit (%d articles)", len(_sales_cache))
        return dict(_sales_cache)
    async with _lock:
        if time.monotonic() - _sales_ts < _CACHE_TTL and _sales_cache:
            return dict(_sales_cache)
        try:
            dt_to   = datetime.utcnow()
            dt_from = dt_to - timedelta(days=28)
            since = dt_from.strftime("%Y-%m-%dT00:00:00.000Z")
            to    = dt_to.strftime("%Y-%m-%dT23:59:59.000Z")

            totals: dict[str, int] = {}
            for posting in await _fbo_postings(since, to):
                for prod in posting.get("products") or []:
                    oid = prod.get("offer_id", "")
                    qty = prod.get("quantity") or 0
                    if oid:
                        totals[oid] = totals.get(oid, 0) + qty

            _sales_cache = {k: round(v / 28, 2) for k, v in totals.items()}
            _sales_ts = time.monotonic()
            _log.info("OZON sales 28d: %d articles (cached)", len(_sales_cache))
            return dict(_sales_cache)
        except Exception as e:
            _log.warning("OZON get_sales_28d error: %s — returning stale cache (%d)", e, len(_sales_cache))
            return dict(_sales_cache)
