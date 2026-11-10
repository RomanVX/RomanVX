"""Фиксация цен: сторож для товаров, снятых с репрайсера Ozon.

Репрайсер Ozon ставит одну цену на Ozon и WB сразу. Товары, которым нужны
РАЗНЫЕ цены на площадках, снимаются с репрайсера и фиксируются здесь:
раз в 30 минут сверяем текущие цены с зафиксированными и, если площадка
(автоакции WB, акции Ozon) их сдвинула, возвращаем обратно и пишем в журнал
«Плана роста».

kv `price_lock`: {"items": [{art, wb_nm, wb_price, wb_discount, wb_target,
                             oz_price, oz_old}], "updated": "..."}
  wb_target — цена продавца после его скидки (до СПП), допуск ±2 ₽;
  oz_price  — цена продавца Ozon, она же min_price (акции ниже не опустят).
"""
import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pricelock", tags=["pricelock"])

_KEY = "price_lock"
_EVERY = 30 * 60
_status: dict = {"checked": "", "restored": [], "error": ""}


def _kv(default):
    import snapshot
    return snapshot.load(_KEY, default) or default


def _now() -> str:
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")


async def _journal(text: str, group: str = "") -> None:
    try:
        from routers import growth
        j = await asyncio.to_thread(growth._kv, growth._JOURNAL_KEY, [])
        j.append({"date": str(growth._msk_today()), "text": text[:500], "group": group})
        await asyncio.to_thread(growth._kv_save, growth._JOURNAL_KEY, j[-300:])
    except Exception as e:                      # журнал — не критично
        _log.warning("pricelock journal: %s", e)


async def check(fix: bool = True) -> dict:
    lock = await asyncio.to_thread(_kv, {})
    items = lock.get("items") or []
    if not items:
        return {"items": 0}
    import ozon_client as oz
    import wb_client
    drift_wb, drift_oz, report = [], [], []

    wb = await wb_client.get_current_prices()
    for it in items:
        if not it.get("wb_nm"):
            continue
        cur = next((v for v in wb.values() if v.get("nmID") == it["wb_nm"]), None)
        if not cur:
            continue
        if abs((cur.get("discounted") or 0) - it["wb_target"]) > 2:
            drift_wb.append(it)
            report.append(f"WB {it['art']}: {cur.get('discounted')} → {it['wb_target']} ₽")

    arts = [it["art"] for it in items if it.get("oz_price")]
    if arts:
        r = await oz._post("/v5/product/info/prices",
                           {"filter": {"offer_id": arts, "visibility": "ALL"}, "limit": 100})
        cur_oz = {i["offer_id"]: i.get("price") or {} for i in (r.get("items") or [])}
        for it in items:
            p = cur_oz.get(it["art"])
            if not p or not it.get("oz_price"):
                continue
            if round(float(p.get("price") or 0)) != it["oz_price"] or \
               round(float(p.get("min_price") or 0)) != it["oz_price"]:
                drift_oz.append(it)
                report.append(f"Ozon {it['art']}: {p.get('price')} (мин. {p.get('min_price')}) → {it['oz_price']} ₽")

    if fix and drift_wb:
        body = {"data": [{"nmID": it["wb_nm"], "price": it["wb_price"], "discount": it["wb_discount"]}
                         for it in drift_wb]}
        resp = await wb_client._http().post(f"{wb_client.PRICES_BASE}/api/v2/upload/task",
                                            headers=wb_client._headers(), json=body)
        if not resp.is_success:
            report.append(f"WB: ошибка восстановления {resp.status_code} {resp.text[:150]}")
    if fix and drift_oz:
        await oz._post("/v1/product/import/prices", {"prices": [
            {"offer_id": it["art"], "price": str(it["oz_price"]), "min_price": str(it["oz_price"]),
             "old_price": str(it.get("oz_old") or 0), "currency_code": "RUB"} for it in drift_oz]})
    if report and fix:
        await _journal("Сторож цен вернул зафиксированные цены: " + "; ".join(report))
        _log.warning("pricelock: %s", report)
    _status.update(checked=_now(), restored=report, error="")
    return {"items": len(items), "drift": report}


async def loop():
    await asyncio.sleep(240)
    while True:
        try:
            await check()
        except Exception as e:
            _status.update(checked=_now(), error=str(e)[:200])
            _log.warning("pricelock loop: %s", e)
        await asyncio.sleep(_EVERY)


def _owner(request: Request):
    import auth as _auth
    if (_auth._session_of(request) or {}).get("role") != "owner":
        raise HTTPException(403, "Только для владельца")


@router.get("")
async def get_lock(request: Request):
    _owner(request)
    return {**(await asyncio.to_thread(_kv, {})), "status": _status}


@router.post("")
async def set_lock(request: Request, payload: dict):
    """{items: [...]} — заменить список; пустой список снимает фиксацию."""
    _owner(request)
    import snapshot
    data = {"items": payload.get("items") or [], "updated": _now()}
    await asyncio.to_thread(snapshot.save, _KEY, data)
    return {"ok": True, "items": len(data["items"])}


@router.post("/check")
async def run_check(request: Request, fix: bool = False):
    _owner(request)
    return await check(fix=fix)
