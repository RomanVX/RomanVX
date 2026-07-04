"""Finance router — WB sales reports + P&L by month."""
import asyncio
import logging
import time as _time
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, HTTPException

import wb_finance_client
import cost_store

router = APIRouter(prefix="/api/finance", tags=["finance"])
_log = logging.getLogger(__name__)

# ── кэш: 60 мин (rate limit WB Finance API — 1 req/min) ──────────────────────
_wb_cache: dict = {}
_wb_cache_ts: float = 0.0
_WB_TTL = 3600
_wb_lock = asyncio.Lock()

# ── P&L кэш ──────────────────────────────────────────────────────────────────
_pnl_cache: dict = {}
_pnl_cache_ts: float = 0.0


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ".") or 0)
    except (ValueError, TypeError):
        return 0.0


async def _get_wb_reports_cached(months: int = 6, refresh: bool = False) -> list[dict]:
    """Внутренняя функция — возвращает нормализованные отчёты WB (с кешем)."""
    global _wb_cache, _wb_cache_ts

    if not refresh and _wb_cache and _time.monotonic() - _wb_cache_ts < _WB_TTL:
        return _wb_cache.get("reports", [])

    async with _wb_lock:
        if not refresh and _wb_cache and _time.monotonic() - _wb_cache_ts < _WB_TTL:
            return _wb_cache.get("reports", [])

        dt_to   = datetime.utcnow() + timedelta(hours=3)
        dt_from = dt_to - timedelta(days=30 * months)
        min_date = datetime(2025, 1, 1)
        if dt_from < min_date:
            dt_from = min_date

        date_from = dt_from.strftime("%Y-%m-%d")
        date_to   = dt_to.strftime("%Y-%m-%d")

        try:
            raw = await wb_finance_client.get_sales_reports(date_from, date_to)
        except Exception as exc:
            _log.error("WB Finance API error: %s", exc)
            # есть устаревший кеш — отдаём его вместо ошибки (429 пройдёт сам)
            if _wb_cache.get("reports"):
                _log.warning("WB Finance: отдаём устаревший кеш (%d отчётов)",
                             len(_wb_cache["reports"]))
                return _wb_cache["reports"]
            raise HTTPException(status_code=502, detail=f"WB Finance API: {exc}")

        rows = []
        for r in sorted(raw, key=lambda x: x.get("dateFrom", ""), reverse=True):
            rows.append({
                "reportId":          r.get("reportId"),
                "dateFrom":          r.get("dateFrom", "")[:10],
                "dateTo":            r.get("dateTo", "")[:10],
                "createDate":        r.get("createDate", "")[:10],
                "currency":          r.get("currency", "RUB"),
                "reportType":        r.get("reportType", 1),
                "retailAmount":      _f(r.get("retailAmountSum")),
                "forPay":            _f(r.get("forPaySum")),
                "avgSalePercent":    r.get("avgSalePercent", 0),
                "deliveryService":   _f(r.get("deliveryServiceSum")),
                "paidStorage":       _f(r.get("paidStorageSum")),
                "paidAcceptance":    _f(r.get("paidAcceptanceSum")),
                "deduction":         _f(r.get("deductionSum")),
                "penalty":           _f(r.get("penaltySum")),
                "additionalPayment": _f(r.get("additionalPaymentSum")),
                "cashbackAmount":    _f(r.get("cashbackAmountSum")),
                "cashbackDiscount":  _f(r.get("cashbackDiscountSum")),
                "cashbackCommission":_f(r.get("cashbackCommissionChangeSum")),
                "bankPayment":       _f(r.get("bankPaymentSum")),
            })

        result = {"reports": rows, "fetched_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")}
        _wb_cache = result
        _wb_cache_ts = _time.monotonic()
        return rows


@router.get("/wb/reports")
async def get_wb_reports(
    months: int = Query(default=6, ge=1, le=24),
    refresh: bool = Query(default=False),
):
    reports = await _get_wb_reports_cached(months, refresh)
    return {"reports": reports, "fetched_at": _wb_cache.get("fetched_at", "")}


@router.post("/wb/reports/invalidate", include_in_schema=False)
async def invalidate_wb_finance():
    global _wb_cache, _wb_cache_ts, _pnl_cache, _pnl_cache_ts
    _wb_cache = {}
    _wb_cache_ts = 0.0
    _pnl_cache = {}
    _pnl_cache_ts = 0.0
    return {"status": "ok"}


_detail_cache: dict = {}
_detail_cache_ts: float = 0.0
_DETAIL_TTL = 86400  # 24ч — детальный отчёт меняется редко
_detail_lock = asyncio.Lock()


_detail_fetching: bool = False  # идёт ли фоновая загрузка
_detail_last_error: str = ""


def _normalize_stat_rows(stat_rows: list[dict]) -> list[dict]:
    """statistics-api reportDetailByPeriod (v5) → формат finance-api detailed.

    Кабинетная «Финансовая аналитика» строится из этого же отчёта,
    поэтому цифры сходятся с ЛК.
    """
    out = []
    for r in stat_rows:
        qty = r.get("quantity") or 0
        # «до СПП»: retail_amount в v5 — фактическая сумма ПОСЛЕ СПП;
        # цена продавца до СПП — retail_price_withdisc_rub (за единицу)
        pre_spp = (r.get("retail_price_withdisc_rub") or 0) * (qty or 1)
        out.append({
            "rrDate":          (r.get("rr_dt") or r.get("sale_dt") or "")[:10],
            "docTypeName":     r.get("doc_type_name") or "",
            "retailAmount":    r.get("retail_amount") or 0,
            "retailPreSpp":    pre_spp,
            "forPay":          r.get("ppvz_for_pay") or 0,
            "deliveryService": r.get("delivery_rub") or 0,
            "paidStorage":     r.get("storage_fee") or 0,
            "paidAcceptance":  r.get("acceptance") or 0,
            "penalty":         r.get("penalty") or 0,
            "deduction":       r.get("deduction") or 0,
            "cashbackAmount":  0,
            "vendorCode":      (r.get("sa_name") or "").strip(),
            "nmId":            r.get("nm_id"),
            "quantity":        qty,
        })
    return out


async def _fetch_detail_bg(date_from: str, date_to: str) -> None:
    """Фоновая задача: детальные строки для P&L.

    Основной источник — statistics-api reportDetailByPeriod (отдельный
    rate-limit, весь период за 1-2 запроса). Фолбэк — finance-api
    detailed (общий лимит 1 req/мин, загрузка занимает минуты).
    """
    global _detail_cache, _detail_cache_ts, _detail_fetching, _detail_last_error
    global _pnl_cache, _pnl_cache_ts
    if _detail_fetching:
        return
    _detail_fetching = True
    try:
        cache_key = f"{date_from}_{date_to}"
        async with _detail_lock:
            rows: list[dict] = []
            try:
                import wb_client
                stat_rows = await wb_client.get_report_detail(
                    datetime.strptime(date_from, "%Y-%m-%d"),
                    datetime.strptime(date_to, "%Y-%m-%d"),
                )
                rows = _normalize_stat_rows(stat_rows)
                if rows:
                    _log.info("Detail via statistics-api: %d rows", len(rows))
            except Exception as e:
                _log.warning("statistics-api detail failed (%s) — пробуем finance-api", e)

            if not rows:
                rows = await wb_finance_client.get_detailed_report(date_from, date_to)
                _log.info("Detail via finance-api: %d rows", len(rows))

            _detail_cache = {"key": cache_key, "rows": rows}
            _detail_cache_ts = _time.monotonic()
            _detail_last_error = ""
        # детали готовы → сбрасываем P&L-кэш, чтобы следующий запрос пересобрал
        # отчёт по точным датам (иначе weekly-версия жила бы до конца TTL)
        _pnl_cache = {}
        _pnl_cache_ts = 0.0
    except Exception as e:
        _detail_last_error = str(e)[:300]
        _log.error("Detail report fetch failed: %s", e)
    finally:
        _detail_fetching = False


def _get_detail_if_ready(date_from: str, date_to: str) -> list[dict]:
    """Возвращает детальный отчёт из кеша (без ожидания)."""
    cache_key = f"{date_from}_{date_to}"
    if _detail_cache.get("key") == cache_key \
            and _time.monotonic() - _detail_cache_ts < _DETAIL_TTL:
        return _detail_cache.get("rows", [])
    return []


@router.get("/wb/pnl")
async def get_wb_pnl(
    months: int = Query(default=6, ge=1, le=24),
    refresh: bool = Query(default=False),
):
    """P&L по месяцам. Сводные данные — сразу, COGS — после фоновой загрузки детального отчёта."""
    global _pnl_cache, _pnl_cache_ts, _detail_cache_ts

    if not refresh and _pnl_cache and _time.monotonic() - _pnl_cache_ts < _WB_TTL:
        return _pnl_cache

    # Диапазон дат
    dt_to   = datetime.utcnow() + timedelta(hours=3)
    dt_from = dt_to - timedelta(days=30 * months)
    min_date = datetime(2025, 1, 1)
    if dt_from < min_date:
        dt_from = min_date
    date_from = dt_from.strftime("%Y-%m-%d")
    date_to   = dt_to.strftime("%Y-%m-%d")

    # Сводные отчёты — быстро (кеш 1ч)
    summary_reports = await _get_wb_reports_cached(months, refresh)

    # Детальный отчёт — берём из кеша если есть, иначе запускаем фоновую загрузку.
    # ВАЖНО: refresh НЕ сбрасывает детальный кеш — его загрузка идёт с лимитом
    # 1 запрос/мин и занимает минуты; сброс возвращал бы weekly-цифры и
    # перезапускал загрузку по кругу. Детали протухают сами по TTL (24ч).
    cache_key = f"{date_from}_{date_to}"
    detail_ready = (_detail_cache.get("key") == cache_key
                    and _time.monotonic() - _detail_cache_ts < _DETAIL_TTL)
    if not detail_ready:
        asyncio.create_task(_fetch_detail_bg(date_from, date_to))
    detail_rows = _get_detail_if_ready(date_from, date_to)

    # ── Агрегируем сводные отчёты по месяцам (пропорционально дням) ────────────
    SUM_KEYS = ["retailAmount","forPay","deliveryService","paidStorage",
                "paidAcceptance","penalty","deduction","cashbackAmount",
                "additionalPayment","cashbackCommission","bankPayment"]

    month_totals: dict[str, dict] = {}

    def ensure(mk):
        if mk not in month_totals:
            month_totals[mk] = {k: 0.0 for k in SUM_KEYS}
        return month_totals[mk]

    for r in summary_reports:
        try:
            df = datetime.strptime(r["dateFrom"], "%Y-%m-%d").date()
            dt = datetime.strptime(r["dateTo"],   "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        segs: list[tuple[str, int]] = []
        cur = df
        while cur <= dt:
            mk = f"{cur.year}-{cur.month:02d}"
            if segs and segs[-1][0] == mk:
                segs[-1] = (mk, segs[-1][1] + 1)
            else:
                segs.append((mk, 1))
            cur += timedelta(days=1)
        total_days = sum(d for _, d in segs)
        for mk, days in segs:
            ratio = days / total_days
            m = ensure(mk)
            for k in SUM_KEYS:
                m[k] += r.get(k, 0.0) * ratio

    # ── Детальный отчёт: точные месяцы по датам операций (как в кабинете WB) ──
    # Если детальный отчёт загружен — пересобираем ВСЕ строки из него: недельный
    # отчёт размазывается по месяцам пропорционально дням и даёт расхождения
    # с финансовой аналитикой кабинета.
    costs = cost_store.get_costs()
    cogs_by_month: dict[str, float] = {}
    source = "weekly"

    def _f(v) -> float:
        try: return float(str(v).replace(",", ".") or 0)
        except: return 0.0

    if detail_rows:
        detail_totals: dict[str, dict] = {}

        def d_ensure(mk):
            if mk not in detail_totals:
                detail_totals[mk] = {k: 0.0 for k in SUM_KEYS}
            return detail_totals[mk]

        import catalog as _cat

        def _unit_cost(row) -> float:
            """Закупочная цена: сперва по артикулу продавца, иначе по nmId."""
            sku = (row.get("vendorCode") or "").strip()
            uc = costs.get(sku, 0.0)
            if uc <= 0 and row.get("nmId"):
                uc = costs.get(_cat.resolve_wb(row.get("nmId")), 0.0)
            return uc

        for row in detail_rows:
            rr = (row.get("rrDate") or row.get("saleDt") or "")[:10]
            if not rr:
                continue
            mk = rr[:7]
            m = d_ensure(mk)
            doc_type = row.get("docTypeName", "")
            sign = 1
            if doc_type == "Возврат":
                sign = -1
            if doc_type in ("Продажа", "Возврат", "Корректировка"):
                # выручка «до СПП» (цена продавца); fallback — фактическая сумма
                base = abs(_f(row.get("retailPreSpp"))) or abs(_f(row.get("retailAmount")))
                m["retailAmount"] += sign * base
                m["forPay"]       += sign * abs(_f(row.get("forPay")))
            # операционные затраты лежат в своих полях на любых строках
            m["deliveryService"] += _f(row.get("deliveryService"))
            m["paidStorage"]     += _f(row.get("paidStorage"))
            m["paidAcceptance"]  += _f(row.get("paidAcceptance"))
            m["penalty"]         += _f(row.get("penalty"))
            m["deduction"]       += _f(row.get("deduction"))
            m["cashbackAmount"]  += _f(row.get("cashbackAmount"))
            # COGS: количество проданного × закупочная (возврат — минус)
            if doc_type in ("Продажа", "Возврат", "Корректировка"):
                qty = int(row.get("quantity") or 0)
                uc = _unit_cost(row)
                if uc > 0 and qty > 0:
                    cogs_by_month[mk] = cogs_by_month.get(mk, 0.0) + sign * uc * qty

        if detail_totals:
            month_totals = detail_totals
            source = "detail"

    # ── Реклама (продвижение): списания из advert API по месяцам ──────────────
    advert_by_month = await _get_wb_advert_cached(dt_from, dt_to)

    # ── Единая структура P&L ──────────────────────────────────────────────────
    # Комиссия WB (вкл. эквайринг) = выручка − «к перечислению за товар» (forPay).
    # Реклама с оплатой «с баланса продаж» уже сидит в deduction («удержания»),
    # поэтому выделяем её отдельной строкой и вычитаем из прочих удержаний —
    # без двойного счёта. Если реклама платилась со счёта (не из баланса),
    # прочие удержания просто останутся 0, а расход всё равно виден в P&L.
    advert_bonus_by_month: dict[str, float] = {}
    for mk, m in month_totals.items():
        m["commission"] = m.get("retailAmount", 0.0) - m.get("forPay", 0.0)
        adv_info = advert_by_month.get(mk) or {}
        adv_total = adv_info.get("total", 0.0)
        adv_bonus = adv_info.get("bonus", 0.0)
        # промо-бонусы WB компенсируют часть рекламы — в затраты идёт чистое
        # списание, бонусная часть показывается справочной строкой
        m["advert"] = adv_total - adv_bonus
        advert_bonus_by_month[mk] = adv_bonus
        deductions_full = (m.get("deduction", 0.0) + m.get("cashbackAmount", 0.0)
                           + m.get("cashbackCommission", 0.0))
        m["deductions"] = max(deductions_full - adv_total, 0.0)

    pnl_rows = _build_pnl_rows(
        month_totals, cogs_by_month,
        [
            ("retailAmount",   "📦 Выручка (до СПП)",          "header"),
            ("commission",     "  − Комиссия WB и эквайринг",   "cost"),
            ("deliveryService","  − Логистика",                 "cost"),
            ("paidStorage",    "  − Хранение",                  "cost"),
            ("paidAcceptance", "  − Приёмка",                   "cost"),
            ("penalty",        "  − Штрафы",                    "cost"),
            ("deductions",     "  − Прочие удержания и кэшбэк", "cost"),
            ("advert",         "  − Продвижение (за вычетом бонусов)", "cost"),
        ],
    )

    # Справочная строка: сколько рекламы компенсировано промо-бонусами WB
    # (в затратах НЕ учитывается — уже вычтено из строки «Продвижение»)
    if any(v > 0 for v in advert_bonus_by_month.values()):
        sorted_mks = sorted(month_totals.keys(), reverse=True)
        bonus_row = {"key": "advert_bonus",
                     "label": "      ↳ компенсировано промо-бонусами WB",
                     "style": "note", "formula": "info",
                     "values": {mk: round(advert_bonus_by_month.get(mk, 0.0)) for mk in sorted_mks}}
        adv_idx = next((i for i, r in enumerate(pnl_rows) if r["key"] == "advert"), None)
        if adv_idx is not None:
            pnl_rows.insert(adv_idx + 1, bonus_row)

    cogs_has_data = len(costs) > 0 and any(v > 0 for v in cogs_by_month.values())
    result = {
        "months": _months_out(month_totals.keys()),
        "rows":   pnl_rows,
        "cogs_loaded": len(costs) > 0,
        "cogs_has_data": cogs_has_data,
        "source": source,
        "detail_fetching": _detail_fetching,
        "detail_error": _detail_last_error,
        "fetched_at": _wb_cache.get("fetched_at", ""),
    }
    _pnl_cache = result
    _pnl_cache_ts = _time.monotonic()
    return result


# кеш рекламных списаний WB (advert API, 6ч)
_wb_advert_cache: dict = {}
_wb_advert_ts: float = 0.0


async def _get_wb_advert_cached(dt_from: datetime, dt_to: datetime) -> dict[str, float]:
    global _wb_advert_cache, _wb_advert_ts
    if _wb_advert_cache and _time.monotonic() - _wb_advert_ts < 6 * 3600:
        return _wb_advert_cache
    try:
        from config import USE_ADVERT_MOCK
        if USE_ADVERT_MOCK:
            return {}
        import advert_client
        spend = await advert_client.get_spend_by_month(dt_from, dt_to)
        _wb_advert_cache = spend
        _wb_advert_ts = _time.monotonic()
        return spend
    except Exception as e:
        _log.warning("WB advert spend failed: %s", e)
        return _wb_advert_cache or {}


RU_MON = ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
          "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


def _months_out(month_keys) -> list[dict]:
    out = []
    for mk in sorted(month_keys, reverse=True):
        y, m = mk.split("-")
        out.append({"key": mk, "label": RU_MON[int(m)] + " " + y})
    return out


def _month_range(months: int) -> tuple[str, str]:
    dt_to = datetime.utcnow() + timedelta(hours=3)
    dt_from = (dt_to - timedelta(days=30 * months)).replace(day=1)
    return dt_from.strftime("%Y-%m-%d"), dt_to.strftime("%Y-%m-%d")


# ══ YM P&L ═════════════════════════════════════════════════════════════════════

_ym_pnl_cache: dict = {}
_ym_pnl_ts: float = 0.0
_YM_PNL_TTL = 3600

# Типы комиссий YM → строки P&L
_YM_FEE_GROUPS = {
    "commission": {"FEE"},
    "acquiring":  {"AGENCY", "PAYMENT_TRANSFER"},
    "delivery":   {"DELIVERY_TO_CUSTOMER", "EXPRESS_DELIVERY_TO_CUSTOMER", "CROSSREGIONAL_DELIVERY"},
    "processing": {"SORTING", "INTAKE_SORTING", "RETURN_PROCESSING", "RETURNED_ORDERS_STORAGE"},
    "advert":     {"AUCTION_PROMOTION", "LOYALTY_PARTICIPATION_FEE"},
}


@router.get("/ym/pnl")
async def get_ym_pnl(
    months: int = Query(default=6, ge=1, le=12),
    refresh: bool = Query(default=False),
):
    """P&L Яндекс Маркета по месяцам из stats/orders (выкупленные заказы)."""
    global _ym_pnl_cache, _ym_pnl_ts
    if not refresh and _ym_pnl_cache and _time.monotonic() - _ym_pnl_ts < _YM_PNL_TTL:
        return _ym_pnl_cache

    import ym_client
    import catalog as _cat

    date_from, date_to = _month_range(months)
    # захватываем заказы, созданные до начала периода, но доставленные внутри него
    pad_from = (datetime.strptime(date_from, "%Y-%m-%d") - timedelta(days=45)).strftime("%Y-%m-%d")
    try:
        orders = await ym_client.get_orders_stats(
            pad_from, date_to,
            statuses=["DELIVERED", "PARTIALLY_DELIVERED", "PARTIALLY_RETURNED"],
        )
    except Exception as exc:
        _log.error("YM stats/orders error: %s", exc)
        raise HTTPException(status_code=502, detail=f"YM API: {exc}")

    costs = cost_store.get_costs()
    KEYS = ["retailAmount", "commission", "acquiring", "delivery", "processing", "advert", "otherServices"]
    mt: dict[str, dict] = {}
    cogs_by_month: dict[str, float] = {}

    def ensure(mk):
        if mk not in mt:
            mt[mk] = {k: 0.0 for k in KEYS}
        return mt[mk]

    min_mk = date_from[:7]
    for o in orders:
        # месяц реализации = дата последнего статуса (доставки), fallback — создание
        d = (o.get("statusUpdateDate") or o.get("creationDate") or "")[:7]
        if not d or d < min_mk:
            continue
        m = ensure(d)
        for it in o.get("items") or []:
            for p in it.get("prices") or []:
                if p.get("type") in ("BUYER", "CASHBACK", "MARKETPLACE"):
                    m["retailAmount"] += float(p.get("total") or 0)
            sku = _cat.resolve_ym(it.get("shopSku") or "")
            qty = int(it.get("count") or 0)
            uc = costs.get(sku, 0.0)
            if uc > 0 and qty > 0:
                cogs_by_month[d] = cogs_by_month.get(d, 0.0) + uc * qty
        for c in o.get("commissions") or []:
            amt = float(c.get("actual") or 0)
            ctype = c.get("type") or ""
            for line, types in _YM_FEE_GROUPS.items():
                if ctype in types:
                    m[line] += amt
                    break
            else:
                m["otherServices"] += amt

    pnl_rows = _build_pnl_rows(
        mt, cogs_by_month,
        [
            ("retailAmount",  "📦 Выручка (выкупы)",           "header"),
            ("commission",    "  − Комиссия размещения",        "cost"),
            ("acquiring",     "  − Приём и перевод платежа",    "cost"),
            ("delivery",      "  − Логистика",                  "cost"),
            ("processing",    "  − Обработка и возвраты",       "cost"),
            ("advert",        "  − Продвижение и лояльность",   "cost"),
            ("otherServices", "  − Прочие услуги",              "cost"),
        ],
    )

    cogs_has_data = len(costs) > 0 and any(v > 0 for v in cogs_by_month.values())
    result = {
        "months": _months_out(mt.keys()),
        "rows": pnl_rows,
        "cogs_loaded": len(costs) > 0,
        "cogs_has_data": cogs_has_data,
        "fetched_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC"),
    }
    _ym_pnl_cache = result
    _ym_pnl_ts = _time.monotonic()
    return result


def _build_pnl_rows(month_totals: dict, cogs_by_month: dict, cost_lines: list) -> list[dict]:
    """Единый скелет P&L: выручка → затраты → к перечислению → COGS → валовая → маржа.

    cost_lines: [(key, label, style)], первая строка — выручка (header),
    остальные — затраты (вычитаются из выручки для строки bankPayment).
    """
    sorted_months = sorted(month_totals.keys(), reverse=True)
    rows = []

    def add(key, label, style, vals):
        rows.append({"key": key, "label": label, "style": style,
                     "formula": "direct", "values": vals})

    revenue_key = cost_lines[0][0]
    add(revenue_key, cost_lines[0][1], cost_lines[0][2],
        {mk: round(month_totals[mk].get(revenue_key, 0.0)) for mk in sorted_months})

    for key, label, style in cost_lines[1:]:
        add(key, label, style,
            {mk: -round(month_totals[mk].get(key, 0.0)) for mk in sorted_months})

    payout = {}
    for mk in sorted_months:
        m = month_totals[mk]
        payout[mk] = round(m.get(revenue_key, 0.0)
                           - sum(m.get(k, 0.0) for k, _, _ in cost_lines[1:]))
    add("bankPayment", "💳 К перечислению", "subtotal", payout)

    add("cogs", "  − Себестоимость", "cost",
        {mk: -round(cogs_by_month.get(mk, 0.0)) for mk in sorted_months})

    gross = {mk: payout[mk] - round(cogs_by_month.get(mk, 0.0)) for mk in sorted_months}
    add("gross", "✅ Валовая прибыль", "total", gross)

    pct = {}
    for mk in sorted_months:
        rev = month_totals[mk].get(revenue_key, 0.0)
        pct[mk] = round(gross[mk] / rev * 100) if rev else 0
    rows.append({"key": "gross_pct", "label": "   Маржа %", "style": "pct",
                 "formula": "gross_pct", "values": pct})
    return rows


# ══ Ozon P&L ══════════════════════════════════════════════════════════════════

_oz_pnl_cache: dict = {}
_oz_pnl_ts: float = 0.0
_OZ_PNL_TTL = 6 * 3600
_oz_pnl_building: bool = False


def _last_months(n: int) -> list[tuple[int, int]]:
    """[(year, month)] — последние n месяцев включая текущий, новые первыми."""
    d = datetime.utcnow() + timedelta(hours=3)
    out = []
    y, m = d.year, d.month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


async def _build_ozon_pnl(months: int) -> None:
    """Фоновая сборка P&L Ozon: /v2/finance/realization по месяцам + cash-flow."""
    global _oz_pnl_cache, _oz_pnl_ts, _oz_pnl_building
    if _oz_pnl_building:
        return
    _oz_pnl_building = True
    try:
        import ozon_client
        import catalog as _cat

        costs = cost_store.get_costs()
        KEYS = ["retailAmount", "commission", "delivery", "services"]
        mt: dict[str, dict] = {}
        cogs_by_month: dict[str, float] = {}

        for (y, m) in _last_months(months):
            mk = f"{y}-{m:02d}"
            month_start = f"{mk}-01"
            month_end = (datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")

            # 1) Отчёт о реализации (продажи − возвраты, комиссия, qty→COGS)
            realization_ok = False
            try:
                res = await ozon_client.get_realization_report(y, m)
                rows = res.get("rows") or []
                if rows:
                    t = {k: 0.0 for k in KEYS}
                    cogs = 0.0
                    for r in rows:
                        dc = r.get("delivery_commission") or {}
                        rc = r.get("return_commission") or {}
                        t["retailAmount"] += float(dc.get("amount") or 0) - float(rc.get("amount") or 0)
                        t["commission"]   += float(dc.get("commission") or 0) - float(rc.get("commission") or 0)
                        qty = int(dc.get("quantity") or 0) - int(rc.get("quantity") or 0)
                        offer = ((r.get("item") or {}).get("offer_id") or "").strip()
                        sku = _cat.resolve_ozon(offer)
                        uc = costs.get(sku, 0.0)
                        if uc > 0 and qty > 0:
                            cogs += uc * qty
                    mt[mk] = t
                    cogs_by_month[mk] = cogs
                    realization_ok = True
            except Exception as e:
                _log.warning("Ozon realization %s: %s", mk, e)

            # 2) Cash-flow: логистика и услуги (в отчёте реализации их нет)
            try:
                flows = await ozon_client.get_cash_flow(month_start, month_end)
                if flows:
                    if mk not in mt:
                        mt[mk] = {k: 0.0 for k in KEYS}
                    for f in flows:
                        mt[mk]["delivery"] += float(f.get("item_delivery_and_return_amount") or 0)
                        mt[mk]["services"] += float(f.get("services_amount") or 0)
                        if not realization_ok:
                            # текущий месяц: реализации ещё нет — берём выручку из cash-flow
                            mt[mk]["retailAmount"] += (float(f.get("orders_amount") or 0)
                                                       + float(f.get("returns_amount") or 0))
                            mt[mk]["commission"]   += float(f.get("commission_amount") or 0)
            except Exception as e:
                _log.warning("Ozon cash-flow %s: %s", mk, e)

        pnl_rows = _build_pnl_rows(
            mt, cogs_by_month,
            [
                ("retailAmount", "📦 Выручка (до соинвеста, реализация)", "header"),
                ("commission",   "  − Комиссия Ozon",                     "cost"),
                ("delivery",     "  − Логистика и возвраты",              "cost"),
                ("services",     "  − Услуги Ozon (вкл. продвижение)",    "cost"),
            ],
        )
        cogs_has_data = len(costs) > 0 and any(v > 0 for v in cogs_by_month.values())
        _oz_pnl_cache = {
            "months": _months_out(mt.keys()),
            "rows": pnl_rows,
            "cogs_loaded": len(costs) > 0,
            "cogs_has_data": cogs_has_data,
            "fetched_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC"),
        }
        _oz_pnl_ts = _time.monotonic()
        _log.info("Ozon P&L built: %d months", len(mt))
    except Exception as e:
        _log.error("Ozon P&L build failed: %s", e)
    finally:
        _oz_pnl_building = False


@router.get("/ozon/pnl")
async def get_ozon_pnl(
    months: int = Query(default=6, ge=1, le=12),
    refresh: bool = Query(default=False),
):
    """P&L Ozon по месяцам. Сборка в фоне (несколько запросов к API)."""
    global _oz_pnl_ts
    fresh = _oz_pnl_cache and _time.monotonic() - _oz_pnl_ts < _OZ_PNL_TTL
    if not refresh and fresh:
        return _oz_pnl_cache
    if refresh:
        _oz_pnl_ts = 0.0
    asyncio.create_task(_build_ozon_pnl(months))
    if _oz_pnl_cache:
        return _oz_pnl_cache  # отдаём устаревший, свежий соберётся в фоне
    return {"months": [], "rows": [],
            "message": "⏳ Отчёт Ozon собирается в фоне — обновите вкладку через 1-2 минуты"}


@router.get("/ozon/reports")
async def get_ozon_reports():
    return {"reports": [], "message": "OZON финансовые отчёты будут добавлены позже"}


@router.get("/ym/reports")
async def get_ym_reports():
    return {"reports": [], "message": "YM финансовые отчёты будут добавлены позже"}
