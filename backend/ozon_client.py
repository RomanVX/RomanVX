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


async def get_stocks_by_cluster() -> list[dict]:
    """Аналитика остатков Ozon по кластерам: /v1/analytics/stocks.

    Ozon сам считает ads (продажи/день), idc (дни покрытия), оборачиваемость
    и излишки — общие и по кластеру. Возвращаем сырые строки (SKU × кластер)."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    skus_info = await _get_all_skus()
    sku_list = [s["sku"] for s in skus_info if s["sku"]]
    rows: list[dict] = []
    for i in range(0, len(sku_list), 100):
        batch = sku_list[i:i + 100]
        try:
            data = await _post("/v1/analytics/stocks", {"skus": batch})
        except Exception as e:
            _log.warning("OZON stocks-by-cluster batch failed: %s", e)
            continue
        rows.extend(data.get("items") or [])
    _log.info("OZON stocks-by-cluster: %d строк (SKU×кластер)", len(rows))
    return rows


def _price_amount(v) -> float:
    """v3 отдаёт price объектом {amount, currency}, v2 отдавал строку."""
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return 0.0


async def _fbo_postings_v3(since: str, to: str, limit: int = 100) -> list[dict]:
    # max limit у v3 — 100 (у v2 был 1000): value must be inside range (0, 100]
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
    """Все FBO-отправления за период: v3 (курсорная пагинация, limit ≤ 100),
    при ошибке — фолбэк на v2 (жив до 01.08.2026)."""
    try:
        return await _fbo_postings_v3(since, to)
    except Exception as e:
        _log.error("OZON v3/posting/fbo/list failed: %s — fallback v2", e)
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


async def get_accrual_types() -> list[dict]:
    """POST /v1/finance/accrual/types — справочник типов начислений."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    data = await _post("/v1/finance/accrual/types", {})
    return data.get("accrual_types") or data.get("result") or []


async def get_accruals_day(date: str) -> list[dict]:
    """POST /v1/finance/accrual/by-day — начисления за день (пагинация last_id).

    Это источник кабинетной «Детализации начислений»: продажи, вознаграждение
    Ozon, доставка, FBO, продвижение, компенсации — построчно с типами.
    """
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    out: list[dict] = []
    last_id = ""
    while True:
        data = await _post("/v1/finance/accrual/by-day", {"date": date, "last_id": last_id})
        batch = data.get("accruals") or data.get("result") or []
        out.extend(batch)
        new_last = data.get("last_id") or ""
        if not batch or not new_last or new_last == last_id:
            break
        last_id = new_last
    return out


async def get_realization_report(year: int, month: int) -> dict:
    """POST /v2/finance/realization — отчёт о реализации за месяц.

    Возвращает result: {header, rows[]} — продажи/возвраты по SKU с комиссиями.
    Отчёт за месяц доступен ~с 5-го числа следующего месяца.
    """
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return {}
    data = await _post("/v2/finance/realization", {"year": year, "month": month})
    return data.get("result") or {}


async def get_cash_flow(date_from: str, date_to: str) -> list[dict]:
    """POST /v1/finance/cash-flow-statement/list — движение ДС по полупериодам.

    cash_flows[]: orders_amount, returns_amount, commission_amount,
    item_delivery_and_return_amount (логистика), services_amount (услуги).
    """
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    flows: list[dict] = []
    page = 1
    while True:
        data = await _post("/v1/finance/cash-flow-statement/list", {
            "date": {"from": f"{date_from}T00:00:00.000Z", "to": f"{date_to}T23:59:59.000Z"},
            "with_details": False,
            "page": page,
            "page_size": 10,
        })
        result = data.get("result") or {}
        batch = result.get("cash_flows") or []
        flows.extend(batch)
        if page >= int(data.get("page_count") or 1) or not batch:
            break
        page += 1
    return flows


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


# ══ Сертификаты и документы товаров ══════════════════════════════════════════

async def get_certificates(page_size: int = 200) -> list[dict]:
    """POST /v1/product/certificate/list — загруженные сертификаты продавца.

    Поля ответа у Ozon менялись — отдаём записи как есть (парсит вызывающий)."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    out: list[dict] = []
    page = 1
    while True:
        try:
            data = await _post("/v1/product/certificate/list",
                               {"page": page, "page_size": page_size})
        except Exception as e:
            _log.warning("OZON certificate/list: %s", e)
            break
        res = data.get("result") or {}
        certs = res.get("certificates") or res.get("items") or []
        out.extend(certs)
        if len(certs) < page_size:
            break
        page += 1
    return out


async def get_certificate_products(cert_number: str, page_size: int = 200) -> list[dict]:
    """POST /v1/product/certificate/products/list — товары, привязанные к сертификату."""
    out: list[dict] = []
    page = 1
    while True:
        try:
            data = await _post("/v1/product/certificate/products/list",
                               {"certificate_number": cert_number,
                                "page": page, "page_size": page_size})
        except Exception as e:
            _log.warning("OZON certificate/products %s: %s", cert_number, e)
            break
        res = data.get("result") or {}
        items = res.get("items") or []
        out.extend(items)
        if len(items) < page_size:
            break
        page += 1
    return out


async def get_analytics_data(date_from: str, date_to: str,
                             metrics: list[str], limit: int = 500) -> list[dict]:
    """POST /v1/analytics/data — аналитика по SKU (часть метрик — Premium).

    Возвращает [{sku, name, metrics: {метрика: значение}}]."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    out: list[dict] = []
    offset = 0
    while True:
        data = await _post("/v1/analytics/data", {
            "date_from": date_from, "date_to": date_to,
            "dimension": ["sku"], "metrics": metrics,
            "limit": limit, "offset": offset,
        })
        rows = (data.get("result") or {}).get("data") or []
        for r in rows:
            dim = (r.get("dimensions") or [{}])[0]
            vals = r.get("metrics") or []
            out.append({"sku": dim.get("id"), "name": dim.get("name") or "",
                        "metrics": dict(zip(metrics, vals))})
        if len(rows) < limit:
            break
        offset += limit
        await asyncio.sleep(1)
    return out


# ── Поисковые запросы (Seller API, полные данные — Premium/Premium Plus) ──────
def _ts(d: str, end: bool = False) -> str:
    """Ozon ждёт google.protobuf.Timestamp: «2026-07-06» → «2026-07-06T00:00:00Z»."""
    if "T" in d:
        return d
    return f"{d}T23:59:59Z" if end else f"{d}T00:00:00Z"
async def get_product_queries(date_from: str, date_to: str) -> list[dict]:
    """POST /v1/analytics/product-queries — сводка по СВОИМ товарам:
    сколько уникальных пользователей искали, GMV из поиска, конверсия.

    Данные считаются с лагом ~3 дня; глубже месяца — только по неделям."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    skus = [str(p.get("sku")) for p in await _get_all_skus() if p.get("sku")]
    if not skus:
        return []
    out: list[dict] = []
    page = 0
    while True:
        data = await _post("/v1/analytics/product-queries", {
            "date_from": _ts(date_from), "date_to": _ts(date_to, end=True),
            "skus": skus[:1000], "page": page, "page_size": 100,
        })
        items = data.get("items") or (data.get("result") or {}).get("items") or []
        out.extend(items)
        page += 1
        pages = int(data.get("page_count") or 0)
        if not items or page >= max(pages, 1):
            break
        await asyncio.sleep(1)
    return out


async def get_query_details(date_from: str, date_to: str,
                            skus: list | None = None) -> list[dict]:
    """POST /v1/analytics/product-queries/details — ТЕКСТЫ запросов по своим
    товарам с частотой/кликами. Источник «радара трендов»."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return []
    body: dict = {"date_from": _ts(date_from), "date_to": _ts(date_to, end=True),
                  "page": 0, "page_size": 100}
    if skus is None:
        skus = [str(p.get("sku")) for p in await _get_all_skus() if p.get("sku")]
    if skus:
        body["skus"] = [str(s) for s in skus][:1000]
    out: list[dict] = []
    while True:
        data = await _post("/v1/analytics/product-queries/details", body)
        items = data.get("items") or (data.get("result") or {}).get("items") or []
        out.extend(items)
        body["page"] += 1
        pages = int(data.get("page_count") or 0)
        if not items or body["page"] >= max(pages, 1):
            break
        await asyncio.sleep(1)
    return out
