"""Async client for Wildberries Statistics API."""
import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx

from config import WB_API_KEY, USE_MOCK
import mock_data

_log = logging.getLogger(__name__)

STATS_BASE    = "https://statistics-api.wildberries.ru/api/v1"
REPORT_BASE   = "https://statistics-api.wildberries.ru"
ANALYTICS_BASE = "https://seller-analytics-api.wildberries.ru"
CONTENT_BASE  = "https://content-api.wildberries.ru"

# Актуальные пути nm-report (WB периодически меняет версии)
_NM_REPORT_PATHS = [
    "/api/v2/nm-report/detail",
    "/api/v1/nm-report/detail",
]


def _headers() -> dict:
    return {"Authorization": WB_API_KEY}


# Общий клиент с keep-alive: новый AsyncClient на каждый запрос платит
# ~100-300мс за TCP+TLS handshake, переиспользуемый — нет.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60)
    return _client


async def _get(url: str, params: dict) -> list[dict]:
    resp = await _http().get(url, headers=_headers(), params=params)
    if not resp.is_success:
        _log.error("WB API %s → %s %s", url, resp.status_code, resp.text[:300])
        resp.raise_for_status()
    return resp.json()


async def analytics_post(path: str, body: dict) -> tuple[int, dict | str]:
    """POST на seller-analytics-api (Джем-методы). Возвращает (status, json|text)
    без raise — для пробника и штатных вызовов с мягкой обработкой."""
    resp = await _http().post(ANALYTICS_BASE + path, headers=_headers(), json=body)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:600]


COMMON_BASE = "https://common-api.wildberries.ru"


async def get_commission_tariffs() -> dict[int, dict]:
    """Официальные тарифы комиссии WB по категориям (subjectID → проценты).

    GET /api/v1/tariffs/commission: kgvpSupplier — FBO (склад WB),
    kgvpMarketplace — FBS, kgvpSupplierExpress и др."""
    if USE_MOCK:
        return {}
    resp = await _http().get(f"{COMMON_BASE}/api/v1/tariffs/commission",
                             headers=_headers(), params={"locale": "ru"})
    if not resp.is_success:
        _log.warning("WB tariffs/commission → %s %s", resp.status_code, resp.text[:200])
        return {}
    out = {}
    for r in (resp.json() or {}).get("report") or []:
        sid = r.get("subjectID")
        if sid is not None:
            out[int(sid)] = {
                "subjectName": r.get("subjectName") or "",
                "parentName": r.get("parentName") or "",
                "fbo": r.get("kgvpSupplier"),        # продажа со склада WB
                "fbs": r.get("kgvpMarketplace"),     # продажа со склада продавца
            }
    _log.info("WB tariffs/commission: %d категорий", len(out))
    return out


async def get_card_subjects() -> dict[int, dict]:
    """Категория каждой карточки кабинета: nmID → {subjectID, subjectName}.

    POST /content/v2/get/cards/list с курсорной пагинацией."""
    if USE_MOCK:
        return {}
    out: dict[int, dict] = {}
    cursor = {"limit": 100}
    for _ in range(50):  # защита от бесконечного цикла
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        resp = await _http().post(f"{CONTENT_BASE}/content/v2/get/cards/list",
                                  headers=_headers(), json=body)
        if not resp.is_success:
            _log.warning("WB cards/list → %s %s", resp.status_code, resp.text[:200])
            break
        data = resp.json() or {}
        cards = data.get("cards") or []
        for c in cards:
            nm = c.get("nmID")
            if nm:
                out[int(nm)] = {"subjectID": c.get("subjectID"),
                                "subjectName": c.get("subjectName") or ""}
        cur = data.get("cursor") or {}
        total = cur.get("total") or 0
        if total < cursor.get("limit", 100) or not cards:
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
    _log.info("WB cards/list: %d карточек", len(out))
    return out


def _learn_sku_map(rows: list[dict]) -> list[dict]:
    """Выучиваем связки nmId→артикул (нужны для остатков, где артикула нет)."""
    try:
        import catalog as _cat
        _cat.learn_wb(rows)
    except Exception as e:
        _log.debug("learn_wb failed: %s", e)
    return rows


async def get_sales(date_from: datetime, date_to: datetime) -> list[dict]:
    if USE_MOCK:
        return mock_data.generate_sales(date_from, date_to)
    rows = await _get(
        f"{STATS_BASE}/supplier/sales",
        {"dateFrom": date_from.strftime("%Y-%m-%dT00:00:00"), "flag": 0},
    )
    return await asyncio.to_thread(_learn_sku_map, rows)


async def get_orders(date_from: datetime, date_to: datetime) -> list[dict]:
    if USE_MOCK:
        return mock_data.generate_orders(date_from, date_to)
    rows = await _get(
        f"{STATS_BASE}/supplier/orders",
        {"dateFrom": date_from.strftime("%Y-%m-%dT00:00:00"), "flag": 0},
    )
    return await asyncio.to_thread(_learn_sku_map, rows)


async def _learn_from_cards() -> None:
    """Список карточек товаров (Content API) → связки nmID→vendorCode.

    Работает для всех товаров, включая те, что без продаж — в отличие от
    доучивания из заказов."""
    import catalog as _cat
    url = f"{CONTENT_BASE}/content/v2/get/cards/list"
    cursor: dict = {"limit": 100}
    learned: list[dict] = []
    for _ in range(50):   # до 5000 карточек
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        resp = await _http().post(url, headers=_headers(), json=body)
        if resp.status_code == 429:
            await asyncio.sleep(21)
            continue
        if not resp.is_success:
            _log.warning("WB cards list → %s %s", resp.status_code, resp.text[:200])
            return
        data = resp.json()
        cards = data.get("cards") or []
        for c in cards:
            if c.get("nmID") and c.get("vendorCode"):
                learned.append({"nmId": c["nmID"], "supplierArticle": c["vendorCode"]})
        cur = data.get("cursor") or {}
        if len(cards) < cursor["limit"] or not cur.get("nmID"):
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
    if learned:
        await asyncio.to_thread(_cat.learn_wb, learned)
        _log.info("WB cards: выучено %d связок nmID→артикул", len(learned))


async def get_stocks() -> list[dict]:
    """Остатки на складах WB.

    Старый GET statistics-api /api/v1/supplier/stocks отключён 23.06.2026.
    Новый: POST seller-analytics-api /api/analytics/v1/stocks-report/wb-warehouses
    (лимит 3 req/мин, до 250 тыс. строк, пагинация limit/offset).
    Ответ не содержит артикула продавца — резолвим nmId через каталог,
    и приводим строки к формату старого API (supplierArticle/quantity/...).
    """
    if USE_MOCK:
        return mock_data.generate_stocks()

    import catalog as _cat

    url = f"{ANALYTICS_BASE}/api/analytics/v1/stocks-report/wb-warehouses"
    items: list[dict] = []
    offset = 0
    LIMIT = 250_000
    while True:
        resp = await _http().post(url, headers=_headers(),
                                  json={"limit": LIMIT, "offset": offset})
        if resp.status_code == 429:
            _log.warning("WB stocks-report 429 — ждём 21с")
            await asyncio.sleep(21)
            continue
        if not resp.is_success:
            _log.error("WB stocks-report → %s %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()
        batch = (resp.json().get("data") or {}).get("items") or []
        items.extend(batch)
        if len(batch) < LIMIT:
            break
        offset += LIMIT
        await asyncio.sleep(21)  # rate limit 3 req/мин

    # если часть nmId не резолвится в артикул (новые товары) — доучиваем
    # связки из карточек товаров (Content API: nmID + vendorCode для ВСЕХ
    # товаров, даже без продаж), затем из заказов за 30 дней
    def _unresolved() -> bool:
        return any(it.get("nmId") and str(_cat.resolve_wb(it["nmId"])).isdigit()
                   for it in items)
    if _unresolved():
        try:
            await _learn_from_cards()
        except Exception as e:
            _log.warning("WB stocks: доучивание nmId из карточек не удалось: %s", e)
    if _unresolved():
        try:
            await get_orders(datetime.now() - timedelta(days=30), datetime.now())
        except Exception as e:
            _log.warning("WB stocks: доучивание nmId из заказов не удалось: %s", e)

    rows = []
    for it in items:
        nm = it.get("nmId")
        art = _cat.resolve_wb(nm) if nm else ""
        qty = it.get("quantity") or 0
        rows.append({
            "supplierArticle":  art,
            "nmId":             nm,
            "warehouseName":    it.get("warehouseName", ""),
            "quantity":         qty,
            "quantityFull":     qty,
            "inWayToClient":    it.get("inWayToClient") or 0,
            "inWayFromClient":  it.get("inWayFromClient") or 0,
        })
    _log.info("WB stocks-report: %d строк (%d складских позиций)", len(rows), len(items))
    return rows


_funnel_lock = asyncio.Lock()  # одна загрузка воронки одновременно (prefetch vs прямой запрос)


async def _nm_report_week_single(date_from: str, date_to: str) -> dict:
    """Один запрос к /api/analytics/v3/sales-funnel/products за период.

    При 429 повторяет до 3 раз с паузой 21 сек (лимит WB пополняется ~1 токен/20 сек).
    """
    FUNNEL_URL = f"{ANALYTICS_BASE}/api/analytics/v3/sales-funnel/products"
    body_base = {
        "selectedPeriod": {"start": date_from, "end": date_to},
        "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [],
        "skipDeletedNm": False,
        "orderBy": {"field": "orderSum", "mode": "desc"},
        "limit": 1000,
    }
    orders_rub = orders_qty = buyouts_rub = buyouts_qty = 0
    offset = 0
    while True:
        # запрос одной страницы с retry на 429
        resp = None
        for attempt in range(4):
            resp = await _http().post(FUNNEL_URL, headers=_headers(), json={**body_base, "offset": offset})
            if resp.status_code == 429:
                _log.warning("WB funnel %s–%s → 429, retry %d/3 через 21с", date_from, date_to, attempt + 1)
                await asyncio.sleep(21)
                continue
            break
        if resp is None or not resp.is_success:
            code = resp.status_code if resp is not None else "—"
            _log.error("WB funnel %s–%s → %s %s", date_from, date_to, code,
                       resp.text[:300] if resp is not None else "")
            break
        data   = resp.json().get("data") or {}
        prods  = data.get("products") or []
        for p in prods:
            stat = p.get("statistic") or {}
            sp   = stat.get("selected") or {}
            orders_rub  += float(sp.get("orderSum",   0) or 0)
            orders_qty  += int(sp.get("orderCount",   0) or 0)
            buyouts_rub += float(sp.get("buyoutSum",  0) or 0)
            buyouts_qty += int(sp.get("buyoutCount",  0) or 0)
        if len(prods) < 1000:
            break
        offset += 1000
    return {"orders_rub": orders_rub, "buyouts_rub": buyouts_rub,
            "orders_qty": orders_qty,  "buyouts_qty": buyouts_qty}


async def get_nm_report_weeks(week_ranges: list[tuple[str, str]]) -> list[dict]:
    """Воронка продаж для списка недель с соблюдением лимита WB (3 запроса/мин).

    Лимит — token bucket: burst 3, пополнение ~1 токен / 20 сек.
    Поэтому первые 3 запроса идут сразу, далее по одному с паузой 21 сек.
    Возвращает точные цифры кабинета WB: «Заказали на сумму / Выкупили на сумму».
    """
    if USE_MOCK:
        return [{"orders_rub": 0, "buyouts_rub": 0, "orders_qty": 0, "buyouts_qty": 0}] * len(week_ranges)

    async with _funnel_lock:
        results: list[dict] = []
        for i, (s, e) in enumerate(week_ranges):
            if i >= 3:                       # после burst-окна — пауза перед каждым запросом
                await asyncio.sleep(21)
            results.append(await _nm_report_week_single(s, e))
        return results
    return results


# Поля детального отчёта, которые реально читаются дальше (finance/analytics).
# Сырые записи несут 60+ полей (kiz, стикеры, названия офисов, ИНН...) —
# на 100k строк это сотни МБ и OOM на Render free. Оставляем только нужное.
_REPORT_KEEP = frozenset({
    "rrd_id", "nm_id", "sa_name", "brand_name", "subject_name", "ts_name",
    "doc_type_name", "supplier_oper_name", "bonus_type_name", "site_country",
    "order_dt", "sale_dt", "rr_dt",
    "quantity", "retail_price", "retail_amount", "retail_price_withdisc_rub",
    "sale_percent", "commission_percent", "ppvz_spp_prc",
    "ppvz_sales_commission", "ppvz_for_pay", "for_pay", "ppvz_vw", "ppvz_vw_nds",
    "ppvz_reward", "acquiring_fee",
    "delivery_rub", "delivery_amount", "return_amount",
    "storage_fee", "acceptance", "penalty", "deduction", "additional_payment",
})

_REPORT_PAGE = 10_000  # меньше страница → меньше пиковая память при парсинге


async def get_report_detail(date_from: datetime, date_to: datetime) -> list[dict]:
    """GET /api/v5/supplier/reportDetailByPeriod with auto-pagination via rrdid.

    Returns the full financial report: per-item commission, logistics,
    storage, deductions, penalties, acquiring, for_pay etc.
    Записи прорежены до _REPORT_KEEP — иначе полугодовой отчёт не влезает
    в память инстанса.
    """
    if USE_MOCK:
        return []

    all_records: list[dict] = []
    rrdid = 0
    df_str = date_from.strftime("%Y-%m-%d")
    dt_str = date_to.strftime("%Y-%m-%d")

    while True:
        # 429-retry: лимит 1 req/мин на метод
        data = None
        for attempt in range(4):
            resp = await _http().get(
                f"{REPORT_BASE}/api/v5/supplier/reportDetailByPeriod",
                headers=_headers(),
                params={"dateFrom": df_str, "dateTo": dt_str, "limit": _REPORT_PAGE, "rrdid": rrdid},
            )
            if resp.status_code == 429:
                _log.warning("reportDetailByPeriod 429 — ждём 62с (%d/3)", attempt + 1)
                await asyncio.sleep(62)
                continue
            if not resp.is_success:
                _log.error("reportDetailByPeriod → %s %s", resp.status_code, resp.text[:300])
                resp.raise_for_status()
            # парсинг в потоке: на 0.1 CPU Render free разбор 20k строк держит
            # event loop десятки секунд и все запросы получают таймауты
            data = await asyncio.to_thread(json.loads, resp.text)
            break
        if not data:
            break
        got = len(data)
        rrdid = data[-1]["rrd_id"]

        def _slim(d):
            import sys as _sys
            out = []
            for r in d:
                row = {}
                for k in _REPORT_KEEP:
                    v = r.get(k)
                    if v is None:
                        continue
                    if isinstance(v, str):
                        # бренды/артикулы/даты/типы повторяются в тысячах строк —
                        # интернирование хранит каждую строку в памяти один раз
                        v = _sys.intern(v)
                    row[k] = v
                out.append(row)
            return out

        all_records.extend(await asyncio.to_thread(_slim, data))
        del data  # сырые записи с полным набором полей больше не нужны
        try:
            import heavy
            _log.info("reportDetailByPeriod: got %d records (total %d, rss %.0f MB)",
                      got, len(all_records), heavy.rss_mb())
        except Exception:
            _log.info("reportDetailByPeriod: got %d records (total %d)", got, len(all_records))
        if got < _REPORT_PAGE:
            break
        await asyncio.sleep(62)  # лимит между страницами

    return all_records
