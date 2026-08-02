"""FBS WB (Маркетплейс): сборочные задания и остатки склада продавца.

Склад продавца Biomed — Чехов (ID 1553062, СЦ Чехов-2). Работаем строго по
спеке docs/wb_api/orders_fbs.yaml + items.yaml (v3, marketplace-api):
  - GET  /api/v3/orders/new                — новые сборочные задания
  - GET  /api/v3/orders                    — задания с пагинацией (история)
  - POST /api/v3/orders/status             — статусы заданий
  - POST /api/v3/orders/stickers           — стикеры (PDF/PNG, base64)
  - PUT/POST /api/v3/stocks/{warehouseId}  — остатки по chrtId (ID размера)

chrtId размеров берём из content-API карточек и кешируем в kv: у нас
безразмерка, у каждой карточки один размер и один штрихкод.
"""
import asyncio
import logging
import os

import wb_client

_log = logging.getLogger("wb_fbs")

MP_BASE = "https://marketplace-api.wildberries.ru"
WAREHOUSE_ID = int(os.getenv("WB_FBS_WAREHOUSE_ID", "0") or 0)


def warehouse_id() -> int:
    if WAREHOUSE_ID:
        return WAREHOUSE_ID
    import snapshot as _snap
    wid = int(_snap.load("fbs_warehouse_id", 0) or 0)
    if not wid:
        from config import CABINET
        if CABINET == "biomed":      # склад Чехов, создан 03.08.2026
            wid = 1553062
            _snap.save("fbs_warehouse_id", wid)
    return wid


async def _req(method: str, path: str, **kw):
    resp = await wb_client._http().request(
        method, MP_BASE + path, headers=wb_client._headers(), **kw)
    if not resp.is_success:
        _log.warning("FBS %s %s → %s %s", method, path,
                     resp.status_code, resp.text[:200])
    return resp


async def chrt_map(refresh: bool = False) -> dict:
    """{АРТИКУЛ_UPPER: {chrtId, nmID, barcode}} из карточек content-API."""
    import snapshot as _snap
    if not refresh:
        cached = await asyncio.to_thread(_snap.load, "fbs_chrt_map", None)
        if cached:
            return cached
    out: dict = {}
    cursor = {"limit": 100}
    for _ in range(50):
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        resp = await wb_client._http().post(
            f"{wb_client.CONTENT_BASE}/content/v2/get/cards/list",
            headers=wb_client._headers(), json=body)
        if not resp.is_success:
            _log.warning("cards/list → %s %s", resp.status_code, resp.text[:150])
            break
        data = resp.json() or {}
        cards = data.get("cards") or []
        for c in cards:
            art = str(c.get("vendorCode") or "").strip()
            sizes = c.get("sizes") or []
            if not art or not sizes:
                continue
            s0 = sizes[0]
            out[art.upper()] = {
                "art": art, "nmID": c.get("nmID"),
                "chrtId": s0.get("chrtID"),
                "barcode": (s0.get("skus") or [""])[0]}
        cur = data.get("cursor") or {}
        if (cur.get("total") or 0) < cursor.get("limit", 100) or not cards:
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"),
                  "nmID": cur.get("nmID")}
    if out:
        await asyncio.to_thread(_snap.save, "fbs_chrt_map", out)
    _log.info("fbs chrt_map: %d карточек", len(out))
    return out


async def get_stocks() -> dict:
    """Текущие FBS-остатки склада: {АРТИКУЛ: qty}."""
    wid = warehouse_id()
    if not wid:
        return {"error": "не задан ID склада продавца (fbs_warehouse_id)"}
    cmap = await chrt_map()
    if not cmap:
        return {"error": "не удалось получить карточки (content-API)"}
    chrts = [v["chrtId"] for v in cmap.values() if v.get("chrtId")]
    by_chrt = {v["chrtId"]: k for k, v in cmap.items() if v.get("chrtId")}
    resp = await _req("POST", f"/api/v3/stocks/{wid}", json={"chrtIds": chrts})
    if not resp.is_success:
        return {"error": f"остатки → {resp.status_code}: {resp.text[:200]}"}
    stocks = {}
    for s in (resp.json() or {}).get("stocks") or []:
        art = by_chrt.get(s.get("chrtId"))
        if art:
            stocks[art] = int(s.get("amount") or 0)
    return {"stocks": stocks, "warehouse_id": wid}


async def set_stocks(items: list[dict]) -> dict:
    """Выставить остатки: items = [{sku: 'AL-01', qty: 20}, …]."""
    wid = warehouse_id()
    if not wid:
        return {"error": "не задан ID склада продавца"}
    cmap = await chrt_map()
    stocks, unknown = [], []
    for it in items:
        art = str(it.get("sku") or "").strip().upper()
        m = cmap.get(art)
        if not m or not m.get("chrtId"):
            unknown.append(art)
            continue
        stocks.append({"chrtId": m["chrtId"],
                       "amount": max(0, int(it.get("qty") or 0))})
    if not stocks:
        return {"error": f"ни один артикул не найден в карточках: {unknown}"}
    resp = await _req("PUT", f"/api/v3/stocks/{wid}", json={"stocks": stocks})
    if resp.status_code != 204:
        return {"error": f"обновление → {resp.status_code}: {resp.text[:250]}"}
    return {"updated": len(stocks), "unknown": unknown, "warehouse_id": wid}


def _order_row(o: dict, cmap_by_nm: dict) -> dict:
    nm = o.get("nmId")
    return {
        "id": o.get("id"), "created": str(o.get("createdAt") or "")[:16].replace("T", " "),
        "nm": nm, "sku": cmap_by_nm.get(nm, str(o.get("article") or nm)),
        "price": round((o.get("convertedPrice") or o.get("price") or 0) / 100),
        "address": ", ".join(filter(None, [
            (o.get("address") or {}).get("province") if isinstance(o.get("address"), dict) else None,
            (o.get("address") or {}).get("city") if isinstance(o.get("address"), dict) else None])),
        "supply_id": o.get("supplyId") or "",
    }


async def orders_overview() -> dict:
    """Задания: новые + последние с их статусами (сборка/доставка/выкуп)."""
    cmap = await chrt_map()
    by_nm = {v["nmID"]: k for k, v in cmap.items() if v.get("nmID")}

    new_resp = await _req("GET", "/api/v3/orders/new")
    if not new_resp.is_success:
        return {"error": f"orders/new → {new_resp.status_code}: {new_resp.text[:200]}"}
    new_orders = [_order_row(o, by_nm)
                  for o in (new_resp.json() or {}).get("orders") or []]

    from datetime import datetime, timedelta
    date_from = int((datetime.utcnow() - timedelta(days=14)).timestamp())
    all_resp = await _req("GET", "/api/v3/orders",
                          params={"limit": 200, "next": 0, "dateFrom": date_from})
    recent = [(_order_row(o, by_nm), o.get("id"))
              for o in ((all_resp.json() or {}).get("orders") or [])] \
        if all_resp.is_success else []

    statuses = {}
    ids = [oid for _, oid in recent if oid]
    if ids:
        st_resp = await _req("POST", "/api/v3/orders/status", json={"orders": ids[:1000]})
        if st_resp.is_success:
            for s in (st_resp.json() or {}).get("orders") or []:
                statuses[s.get("id")] = {"supplier": s.get("supplierStatus"),
                                         "wb": s.get("wbStatus")}
    rows = []
    for row, oid in recent:
        st = statuses.get(oid) or {}
        row["status"] = st.get("supplier") or ""
        row["wb_status"] = st.get("wb") or ""
        rows.append(row)
    return {"new": new_orders, "recent": rows,
            "warehouse_id": warehouse_id()}


async def stickers(order_ids: list[int]) -> bytes | None:
    """PDF со стикерами сборочных заданий (спека: type svg|zplv|zplh|png;
    берём png 580x400 и клеим в PDF по одному на страницу)."""
    resp = await _req("POST", "/api/v3/orders/stickers",
                      params={"type": "png", "width": 58, "height": 40},
                      json={"orders": order_ids[:100]})
    if not resp.is_success:
        return None
    import base64
    import io
    stickers_b64 = [(s.get("orderId"), s.get("file"))
                    for s in (resp.json() or {}).get("stickers") or []]
    if not stickers_b64:
        return None
    from reportlab.lib.pagesizes import landscape
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    buf = io.BytesIO()
    page = (58 * mm, 40 * mm)
    c = canvas.Canvas(buf, pagesize=page)
    for _oid, b64 in stickers_b64:
        try:
            img = ImageReader(io.BytesIO(base64.b64decode(b64)))
            c.drawImage(img, 0, 0, width=page[0], height=page[1])
            c.showPage()
        except Exception as e:
            _log.warning("sticker %s: %s", _oid, str(e)[:100])
    c.save()
    buf.seek(0)
    return buf.read()
