"""Async client for Yandex Market Partner API — stocks and orders."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

from catalog import SKU_ALIASES

import httpx

from config import YM_API_KEY, YM_CAMPAIGN_ID, YM_BUSINESS_ID

_log = logging.getLogger(__name__)
_BASE = "https://api.partner.market.yandex.ru"

_CACHE_TTL = 3600  # 1 hour — YM rate limit is 10000 pts/hr

_stocks_cache: dict[str, int] = {}
_stocks_ts: float = 0.0
_sales_cache: dict[str, float] = {}
_sales_ts: float = 0.0
_lock = asyncio.Lock()


def _headers() -> dict:
    return {
        "Api-Key": YM_API_KEY,
        "Content-Type": "application/json",
    }


# Общий клиент с keep-alive (без него каждый запрос = новый TLS handshake)
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60)
    return _client


async def _post(path: str, body: dict, params: dict | None = None) -> dict:
    r = await _http().post(f"{_BASE}{path}", headers=_headers(), json=body, params=params or {})
    if not r.is_success:
        _log.warning("[YM] POST %s → %s | resp[:300]=%s", path, r.status_code, r.text[:300])
        r.raise_for_status()
    return r.json()


async def _get(path: str, params: dict) -> dict:
    r = await _http().get(f"{_BASE}{path}", headers=_headers(), params=params)
    _log.info("[YM] GET %s params=%s → %s | resp[:400]=%s",
              path, params, r.status_code, r.text[:400])
    if not r.is_success:
        r.raise_for_status()
    return r.json()


async def _try_business_orders_post(date_from: str, date_to: str) -> dict[str, int]:
    """POST /businesses/{id}/orders — paginated, cached so rate limit OK."""
    try:
        totals: dict[str, int] = {}
        page_token: str | None = None
        while True:
            body: dict = {
                "dateFrom": date_from,
                "dateTo": date_to,
                "status": ["DELIVERED"],
                "limit": 50,
            }
            if page_token:
                body["pageToken"] = page_token
            data = await _post(f"/v1/businesses/{YM_BUSINESS_ID}/orders", body)
            orders = (data.get("result") or {}).get("orders") or []
            for order in orders:
                for item in order.get("items") or []:
                    oid = item.get("offerId") or (item.get("offer") or {}).get("offerId", "")
                    qty = item.get("initialCount") or item.get("count") or 0
                    if oid and qty:
                        totals[oid] = totals.get(oid, 0) + qty
            paging = (data.get("result") or {}).get("paging") or {}
            page_token = paging.get("nextPageToken")
            if not orders or not page_token:
                break
        _log.info("[YM] POST business/orders → %d articles", len(totals))
        return totals
    except Exception as e:
        _log.warning("[YM] POST business/orders failed: %s", e)
        return {}


def _parse_ym_orders(orders: list[dict], date_field: str = "creationDate") -> list[dict]:
    """Extract rows from orders list. date_field selects which date to use for bucketing."""
    from datetime import timezone as _tz
    msk = _tz(timedelta(hours=3))
    rows: list[dict] = []
    logged = False
    for order in orders:
        status = order.get("status") or ""
        raw_dt = order.get(date_field) or order.get("creationDate") or ""
        try:
            date_str = datetime.fromisoformat(raw_dt).astimezone(msk).strftime("%Y-%m-%d")
        except Exception:
            try:
                date_str = datetime.strptime(raw_dt[:10], "%d-%m-%Y").strftime("%Y-%m-%d")
            except Exception:
                date_str = raw_dt[:10]
        raw_update = order.get("updateDate") or ""
        try:
            update_date_str = datetime.fromisoformat(raw_update).astimezone(msk).strftime("%Y-%m-%d")
        except Exception:
            try:
                update_date_str = datetime.strptime(raw_update[:10], "%d-%m-%Y").strftime("%Y-%m-%d")
            except Exception:
                update_date_str = raw_update[:10]
        if not logged:
            _log.info("[YM] SAMPLE ORDER: status=%s date_field_raw=%s date_str=%s keys=%s",
                      status, raw_dt, date_str, list(order.keys()))
            items_sample = order.get("items") or []
            if items_sample:
                _log.info("[YM] SAMPLE ITEM: keys=%s value=%s", list(items_sample[0].keys()), items_sample[0])
            logged = True
        for item in order.get("items") or []:
            # Try multiple field names — older orders may use shopSku instead of offerId
            oid = item.get("offerId") or item.get("shopSku") or item.get("offerName") or ""
            oid = SKU_ALIASES.get(oid, oid)
            qty = item.get("count") or 0
            prices  = item.get("prices") or {}
            payment = float((prices.get("payment") or {}).get("value") or 0)
            subsidy = float((prices.get("subsidy") or {}).get("value") or 0)
            revenue = payment + subsidy
            if oid and qty:
                rows.append({"date": date_str, "update_date": update_date_str,
                             "shop_sku": oid, "qty": qty,
                             "revenue": revenue, "status": status})
    return rows


async def _fetch_pages(body: dict) -> list[dict]:
    """Paginate through businesses orders endpoint, return all order dicts."""
    orders_all: list[dict] = []
    page_token: str | None = None
    while True:
        query: dict = {"limit": 50}
        if page_token:
            query["pageToken"] = page_token
        try:
            data = await _post(f"/v1/businesses/{YM_BUSINESS_ID}/orders", body, params=query)
        except Exception as e:
            _log.warning("[YM] fetch failed: %s", e)
            break
        orders = data.get("orders") or []
        _log.info("[YM] page token=%s: %d orders (body=%s)", page_token, len(orders), list(body.keys()))
        orders_all.extend(orders)
        page_token = (data.get("paging") or {}).get("nextPageToken")
        if not orders or not page_token:
            break
    return orders_all


async def _fetch_chunk_orders(date_from: str, date_to: str) -> list[dict]:
    """Orders created in [date_from, date_to] — includes all statuses incl. CANCELLED.

    Uses dates.creationDateFrom/To per official API docs.
    creationDateTo is EXCLUSIVE — caller must pass end+1 day.
    """
    orders = await _fetch_pages({
        "dates": {
            "creationDateFrom": date_from,
            "creationDateTo":   date_to,   # exclusive — already +1 day from caller
        }
    })
    rows = _parse_ym_orders(orders, "creationDate")
    _log.info("[YM] chunk %s–%s: %d rows (incl. CANCELLED)", date_from, date_to, len(rows))
    return rows


async def _fetch_chunk_delivered(date_from: str, date_to: str) -> list[dict]:
    """Orders delivered in [date_from, date_to] — Выкупы. DELIVERED only, bucketed by updateDate."""
    orders = await _fetch_pages({
        "filter": {"fromDate": f"{date_from}T00:00:00Z", "toDate": f"{date_to}T23:59:59Z"},
        "statuses": ["DELIVERED"],
    })
    # Use updateDate for week bucketing so Выкупы land in the delivery week
    return [r for r in _parse_ym_orders(orders, "updateDate") if r["status"] == "DELIVERED"]


async def get_sales_detail(date_from: str, date_to: str) -> list[dict]:
    """Return [{date, shop_sku, qty, revenue, status}] for given range.

    Uses POST /v1/businesses/{businessId}/orders with dates.creationDateFrom/To.
    Max 29-day chunks (API allows 30 days; creationDateTo is exclusive so
    passing end+1 means the actual queried range is 30 days).
    """
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return []

    dt_from = datetime.strptime(date_from, "%Y-%m-%d")
    dt_to   = datetime.strptime(date_to,   "%Y-%m-%d")

    # creationDateTo is exclusive → pass chunk_end + 1 day
    # Keep chunks ≤ 29 days so exclusive end stays within 30-day API limit
    chunks: list[tuple[str, str]] = []
    cur = dt_from
    while cur <= dt_to:
        chunk_end    = min(cur + timedelta(days=28), dt_to)
        exclusive_to = (chunk_end + timedelta(days=1)).strftime("%Y-%m-%d")
        chunks.append((cur.strftime("%Y-%m-%d"), exclusive_to))
        cur = chunk_end + timedelta(days=1)

    results = await asyncio.gather(*[_fetch_chunk_orders(c0, c1) for c0, c1 in chunks])
    rows = [r for chunk in results for r in chunk]
    _log.info("[YM] sales_detail: chunks=%d rows=%d for %s–%s",
              len(chunks), len(rows), date_from, date_to)
    try:
        import sales_history
        sales_history.persist_detail_bg(rows, "YM")
    except Exception as e:
        _log.warning("sales_history persist (YM) failed: %s", e)
    return rows


async def get_orders_stats(date_from: str, date_to: str,
                           statuses: list[str] | None = None) -> list[dict]:
    """POST /v2/campaigns/{id}/stats/orders — детальная статистика заказов.

    Содержит items (shopSku, count, prices BUYER/CASHBACK/MARKETPLACE),
    commissions (FEE, AGENCY, DELIVERY_TO_CUSTOMER, ...) — база для P&L.
    Пагинация query-параметрами limit/page_token (до 200 заказов за запрос),
    лимит 10 000 req/час.
    """
    if not YM_API_KEY or not YM_CAMPAIGN_ID:
        return []
    body: dict = {"dateFrom": date_from, "dateTo": date_to}
    if statuses:
        body["statuses"] = statuses
    orders: list[dict] = []
    page_token = ""
    while True:
        params = {"limit": 200}
        if page_token:
            params["pageToken"] = page_token
        data = await _post(f"/v2/campaigns/{YM_CAMPAIGN_ID}/stats/orders", body, params=params)
        result = data.get("result") or {}
        batch = result.get("orders") or []
        orders.extend(batch)
        page_token = (result.get("paging") or {}).get("nextPageToken") or ""
        if not batch or not page_token:
            break
    _log.info("[YM] stats/orders: %d orders for %s–%s", len(orders), date_from, date_to)
    return orders


async def get_stocks() -> dict[str, int]:
    """Return {offer_id: FIT count} — cached 1 hour."""
    global _stocks_cache, _stocks_ts
    if not YM_API_KEY or not YM_CAMPAIGN_ID:
        return {}
    if time.monotonic() - _stocks_ts < _CACHE_TTL and _stocks_cache:
        _log.info("YM stocks: cache hit (%d articles)", len(_stocks_cache))
        return dict(_stocks_cache)
    async with _lock:
        if time.monotonic() - _stocks_ts < _CACHE_TTL and _stocks_cache:
            return dict(_stocks_cache)
        try:
            data = await _post(
                f"/v2/campaigns/{YM_CAMPAIGN_ID}/offers/stocks",
                {"limit": 100},
            )
            result: dict[str, int] = {}
            for wh in (data.get("result") or {}).get("warehouses") or []:
                for offer in wh.get("offers") or []:
                    offer_id = offer.get("offerId", "")
                    if not offer_id:
                        continue
                    offer_id = SKU_ALIASES.get(offer_id, offer_id)
                    for stock in offer.get("stocks") or []:
                        if stock.get("type") == "FIT":
                            result[offer_id] = result.get(offer_id, 0) + (stock.get("count") or 0)
            _stocks_cache = result
            _stocks_ts = time.monotonic()
            _log.info("YM stocks: %d articles (cached): %s", len(result), sorted(result.keys()))
            return dict(result)
        except Exception as e:
            _log.warning("YM get_stocks error: %s — returning stale cache (%d)", e, len(_stocks_cache))
            return dict(_stocks_cache)


async def get_sales_28d() -> dict[str, float]:
    """Return {offer_id: avg_daily_qty} — cached 1 hour."""
    global _sales_cache, _sales_ts
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return {}
    if time.monotonic() - _sales_ts < _CACHE_TTL and _sales_cache:
        _log.info("YM sales: cache hit (%d articles)", len(_sales_cache))
        return dict(_sales_cache)
    async with _lock:
        if time.monotonic() - _sales_ts < _CACHE_TTL and _sales_cache:
            return dict(_sales_cache)
        try:
            dt_to   = datetime.utcnow()
            dt_from = dt_to - timedelta(days=28)
            date_from = dt_from.strftime("%Y-%m-%d")
            date_to   = dt_to.strftime("%Y-%m-%d")

            # stats/orders — ПОСТРАНИЧНЫЙ (по умолчанию ~20 заказов на страницу):
            # без пагинации всё сверх первой страницы терялось и скорость
            # продаж занижалась в разы. get_orders_stats листает до конца.
            totals: dict[str, int] = {}
            for order in await get_orders_stats(date_from, date_to):
                if (order.get("status") or "") == "CANCELLED":
                    continue
                for item in order.get("items") or []:
                    oid = item.get("shopSku") or item.get("offerId") or ""
                    qty = item.get("count") or item.get("initialCount") or 0
                    if oid and qty:
                        oid = SKU_ALIASES.get(oid, oid)
                        totals[oid] = totals.get(oid, 0) + qty
            if not totals:
                # Fallback: POST /businesses/{id}/orders paginated
                totals = await _try_business_orders_post(date_from, date_to)

            _sales_cache = {k: round(v / 28, 2) for k, v in totals.items()}
            _sales_ts = time.monotonic()
            _log.info("YM sales 28d: %d articles (cached)", len(_sales_cache))
            return dict(_sales_cache)
        except Exception as e:
            _log.warning("YM get_sales_28d error: %s — returning stale cache (%d)", e, len(_sales_cache))
            return dict(_sales_cache)


# ══ Отчёт по стоимости услуг (Reports API) ══════════════════════════════════
# Официальный источник ВСЕХ затрат YM: комиссия размещения, буст продаж,
# доставка, приём/перевод платежа, хранение, обработка — то же, что XLSX
# «Отчёт о стоимости услуг» в кабинете.

async def get_services_report_month(year: int, month: int) -> bytes | None:
    """Генерирует «Отчёт по стоимости услуг» за месяц и скачивает XLSX.

    POST /reports/united-marketplace-services/generate → reportId,
    затем поллинг GET /reports/info/{reportId} до DONE и скачивание файла.
    """
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return None
    from calendar import monthrange
    last = monthrange(year, month)[1]
    body_variants = [
        # вариант с помесячными полями (кабинетный financial_month)
        {"businessId": int(YM_BUSINESS_ID),
         "yearFrom": year, "monthFrom": month, "yearTo": year, "monthTo": month},
        # вариант с датами
        {"businessId": int(YM_BUSINESS_ID),
         "dateFrom": f"{year}-{month:02d}-01",
         "dateTo": f"{year}-{month:02d}-{last:02d}"},
    ]
    report_id = None
    last_err = None
    for body in body_variants:
        # 420/429 — лимит генерации отчётов: ждём и повторяем (до 5 раз)
        for attempt in range(5):
            try:
                r = await _post("/reports/united-marketplace-services/generate",
                                body, params={"format": "FILE"})
                report_id = (r.get("result") or {}).get("reportId")
                break
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code in (420, 429):
                    _log.warning("YM report generate %s-%02d: %s — ждём 60с (%d/4)",
                                 year, month, e.response.status_code, attempt + 1)
                    await asyncio.sleep(60)
                    continue
                break   # 400 и прочее — пробуем другой вариант тела
            except Exception as e:
                last_err = e
                break
        if report_id:
            break
    if not report_id:
        raise RuntimeError(f"YM services report: не удалось сгенерировать ({last_err})")

    # поллинг статуса (генерация обычно 10-60 сек)
    file_url = None
    for _ in range(60):
        await asyncio.sleep(5)
        try:
            info = await _get(f"/reports/info/{report_id}", {})
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (420, 429):
                await asyncio.sleep(30)
                continue
            raise
        res = info.get("result") or {}
        status = res.get("status") or ""
        if status == "DONE":
            file_url = res.get("file")
            break
        if status in ("FAILED", "NO_DATA"):
            if status == "NO_DATA":
                return None
            raise RuntimeError(f"YM services report {year}-{month:02d}: {res.get('subStatus') or status}")
    if not file_url:
        raise RuntimeError(f"YM services report {year}-{month:02d}: таймаут генерации")

    r = await _http().get(file_url, follow_redirects=True, timeout=120)
    r.raise_for_status()
    return r.content


async def get_dimensions() -> dict[str, dict]:
    """Габариты карточек ЯМ: POST /v2/businesses/{id}/offer-mappings,
    поле offer.weightDimensions — length/width/height в СМ, weight в КГ."""
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return {}
    out: dict[str, dict] = {}
    page_token = ""
    for _ in range(20):
        params = {"limit": 200}
        if page_token:
            params["page_token"] = page_token
        data = await _post(f"/v2/businesses/{YM_BUSINESS_ID}/offer-mappings",
                           {}, params=params)
        result = (data.get("result") or {})
        for m in result.get("offerMappings") or []:
            offer = m.get("offer") or {}
            oid = str(offer.get("offerId") or "").strip()
            wd = offer.get("weightDimensions") or {}
            if not oid or not wd:
                continue
            oid = SKU_ALIASES.get(oid, oid)
            l = float(wd.get("length") or 0)
            w = float(wd.get("width") or 0)
            h = float(wd.get("height") or 0)
            out[oid.upper()] = {
                "length": l or None, "width": w or None, "height": h or None,
                "litres": round(l * w * h / 1000, 2) if l and w and h else None,
                "weight_g": (round(float(wd.get("weight")) * 1000)
                             if wd.get("weight") else None),
                "name": (offer.get("name") or "")[:80]}
        page_token = ((result.get("paging") or {}).get("nextPageToken")) or ""
        if not page_token:
            break
    _log.info("[YM] dimensions: %d карточек", len(out))
    return out
