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


async def _fetch_detail_bg(date_from: str, date_to: str) -> None:
    """Фоновая задача: загружает детальный отчёт (медленно, 1 req/min) и кеширует."""
    global _detail_cache, _detail_cache_ts, _detail_fetching
    if _detail_fetching:
        return
    _detail_fetching = True
    try:
        cache_key = f"{date_from}_{date_to}"
        async with _detail_lock:
            rows = await wb_finance_client.get_detailed_report(date_from, date_to)
            _detail_cache = {"key": cache_key, "rows": rows}
            _detail_cache_ts = _time.monotonic()
            _log.info("Detail report cached: %d rows", len(rows))
    except Exception as e:
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
    global _pnl_cache, _pnl_cache_ts

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

    # Детальный отчёт — берём из кеша если есть, иначе запускаем фоновую загрузку
    cache_key = f"{date_from}_{date_to}"
    detail_ready = (_detail_cache.get("key") == cache_key
                    and _time.monotonic() - _detail_cache_ts < _DETAIL_TTL)
    if refresh or not detail_ready:
        if refresh:
            _detail_cache.clear()
            _detail_cache_ts = 0.0
        # Запускаем фоновую загрузку (не блокируем ответ)
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

    # ── COGS из детального отчёта: vendorCode × unit_cost ────────────────────
    costs = cost_store.get_costs()
    cogs_by_month: dict[str, float] = {}
    qty_by_month_sku: dict[str, dict[str, int]] = {}  # mk → {sku: qty}

    def _f(v) -> float:
        try: return float(str(v).replace(",", ".") or 0)
        except: return 0.0

    for row in detail_rows:
        doc_type = row.get("docTypeName", "")
        if doc_type not in ("Продажа", "Корректировка"):
            continue
        sku = (row.get("vendorCode") or "").strip()
        qty = int(row.get("quantity") or 0)
        if not sku or qty <= 0:
            continue
        # дата операции
        rr = (row.get("rrDate") or row.get("saleDt") or "")[:10]
        if not rr:
            continue
        mk = rr[:7]  # "2025-05"
        unit_cost = costs.get(sku, 0.0)
        if unit_cost > 0:
            cogs_by_month[mk] = cogs_by_month.get(mk, 0.0) + unit_cost * qty
        # собираем qty для справки
        if mk not in qty_by_month_sku:
            qty_by_month_sku[mk] = {}
        qty_by_month_sku[mk][sku] = qty_by_month_sku[mk].get(sku, 0) + qty

    # ── формируем ответ: список месяцев, строки P&L ───────────────────────────
    sorted_months = sorted(month_totals.keys(), reverse=True)

    RU_MON = ["","Янв","Фев","Мар","Апр","Май","Июн",
               "Июл","Авг","Сен","Окт","Ноя","Дек"]

    months_out = []
    for mk in sorted_months:
        y, m = mk.split("-")
        months_out.append({"key": mk, "label": RU_MON[int(m)] + " " + y})

    def mval(mk, key):
        return round(month_totals.get(mk, {}).get(key, 0.0))

    pnl_rows = []

    def row(key, label, formula, style="normal", sign=1):
        vals = {}
        for mk in sorted_months:
            if formula == "direct":
                vals[mk] = round(mval(mk, key) * sign)
            elif formula == "cogs":
                vals[mk] = -round(cogs_by_month.get(mk, 0.0))
            elif formula == "gross":
                bank = mval(mk, "bankPayment")
                cogs = round(cogs_by_month.get(mk, 0.0))
                vals[mk] = round(bank - cogs)
            elif formula == "gross_pct":
                bank = mval(mk, "bankPayment")
                retail = mval(mk, "retailAmount")
                cogs = round(cogs_by_month.get(mk, 0.0))
                gross = bank - cogs
                vals[mk] = round(gross / retail * 100) if retail else 0
        pnl_rows.append({"key": key, "label": label, "style": style,
                         "formula": formula, "values": vals})

    row("retailAmount",   "📦 Выручка (розн.)",         "direct",   "header")
    row("deliveryService","  − Логистика",               "direct",   "cost",  -1)
    row("paidStorage",    "  − Хранение",                "direct",   "cost",  -1)
    row("paidAcceptance", "  − Приёмка",                 "direct",   "cost",  -1)
    row("penalty",        "  − Штрафы",                  "direct",   "cost",  -1)
    row("deduction",      "  − Удержания",               "direct",   "cost",  -1)
    row("cashbackAmount", "  − Кэшбэк WB",               "direct",   "cost",  -1)
    row("bankPayment",    "💳 Поступление от WB",         "direct",   "subtotal")
    row("cogs",           "  − Себестоимость",            "cogs",     "cost")
    row("gross",          "✅ Валовая прибыль",           "gross",    "total")
    row("gross_pct",      "   Маржа %",                  "gross_pct","pct")

    cogs_has_data = len(costs) > 0 and any(v > 0 for v in cogs_by_month.values())
    result = {
        "months": months_out,
        "rows":   pnl_rows,
        "cogs_loaded": len(costs) > 0,
        "cogs_has_data": cogs_has_data,
        "fetched_at": _wb_cache.get("fetched_at", ""),
    }
    _pnl_cache = result
    _pnl_cache_ts = _time.monotonic()
    return result


@router.get("/ozon/reports")
async def get_ozon_reports():
    return {"reports": [], "message": "OZON финансовые отчёты будут добавлены позже"}


@router.get("/ym/reports")
async def get_ym_reports():
    return {"reports": [], "message": "YM финансовые отчёты будут добавлены позже"}
