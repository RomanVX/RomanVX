"""Finance router — WB sales reports (+ OZON/YM stubs)."""
import asyncio
import logging
import time as _time
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, HTTPException

import wb_finance_client

router = APIRouter(prefix="/api/finance", tags=["finance"])
_log = logging.getLogger(__name__)

# ── кэш: 60 мин (rate limit WB Finance API — 1 req/min) ──────────────────────
_wb_cache: dict = {}
_wb_cache_ts: float = 0.0
_WB_TTL = 3600
_wb_lock = asyncio.Lock()


@router.get("/wb/reports")
async def get_wb_reports(
    months: int = Query(default=6, ge=1, le=24, description="Сколько месяцев назад"),
    refresh: bool = Query(default=False),
):
    """Список отчётов реализации WB за последние N месяцев."""
    global _wb_cache, _wb_cache_ts

    if not refresh and _wb_cache and _time.monotonic() - _wb_cache_ts < _WB_TTL:
        return _wb_cache

    async with _wb_lock:
        if not refresh and _wb_cache and _time.monotonic() - _wb_cache_ts < _WB_TTL:
            return _wb_cache

        dt_to   = datetime.utcnow() + timedelta(hours=3)  # Moscow UTC+3
        dt_from = dt_to - timedelta(days=30 * months)
        # API данные с 2025-01-01
        min_date = datetime(2025, 1, 1)
        if dt_from < min_date:
            dt_from = min_date

        date_from = dt_from.strftime("%Y-%m-%d")
        date_to   = dt_to.strftime("%Y-%m-%d")

        try:
            reports = await wb_finance_client.get_sales_reports(date_from, date_to)
        except Exception as exc:
            _log.error("WB Finance API error: %s", exc)
            raise HTTPException(status_code=502, detail=f"WB Finance API: {exc}")

        # Нормализуем числа из строк
        def _f(v) -> float:
            try:
                return float(str(v).replace(",", ".") or 0)
            except (ValueError, TypeError):
                return 0.0

        rows = []
        for r in sorted(reports, key=lambda x: x.get("dateFrom", ""), reverse=True):
            rows.append({
                "reportId":               r.get("reportId"),
                "dateFrom":               r.get("dateFrom", "")[:10],
                "dateTo":                 r.get("dateTo", "")[:10],
                "createDate":             r.get("createDate", "")[:10],
                "currency":               r.get("currency", "RUB"),
                "reportType":             r.get("reportType", 1),
                "retailAmount":           _f(r.get("retailAmountSum")),
                "forPay":                 _f(r.get("forPaySum")),
                "avgSalePercent":         r.get("avgSalePercent", 0),
                "deliveryService":        _f(r.get("deliveryServiceSum")),
                "paidStorage":            _f(r.get("paidStorageSum")),
                "paidAcceptance":         _f(r.get("paidAcceptanceSum")),
                "deduction":              _f(r.get("deductionSum")),
                "penalty":                _f(r.get("penaltySum")),
                "additionalPayment":      _f(r.get("additionalPaymentSum")),
                "cashbackAmount":         _f(r.get("cashbackAmountSum")),
                "cashbackDiscount":       _f(r.get("cashbackDiscountSum")),
                "cashbackCommission":     _f(r.get("cashbackCommissionChangeSum")),
                "bankPayment":            _f(r.get("bankPaymentSum")),
            })

        result = {"reports": rows, "fetched_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")}
        _wb_cache = result
        _wb_cache_ts = _time.monotonic()
        return result


@router.post("/wb/reports/invalidate", include_in_schema=False)
async def invalidate_wb_finance():
    global _wb_cache, _wb_cache_ts
    _wb_cache = {}
    _wb_cache_ts = 0.0
    return {"status": "ok"}


@router.get("/ozon/reports")
async def get_ozon_reports():
    """OZON финансовые отчёты — в разработке."""
    return {"reports": [], "message": "OZON финансовые отчёты будут добавлены позже"}


@router.get("/ym/reports")
async def get_ym_reports():
    """Яндекс Маркет финансовые отчёты — в разработке."""
    return {"reports": [], "message": "YM финансовые отчёты будут добавлены позже"}
