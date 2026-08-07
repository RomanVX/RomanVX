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
            dims = c.get("dimensions") or {}
            litres = None
            try:      # габариты в см → литры (для тарифа логистики FBS)
                l, w, h = (float(dims.get(k) or 0) for k in ("length", "width", "height"))
                if l and w and h:
                    litres = round(l * w * h / 1000, 2)
            except (TypeError, ValueError):
                pass
            out[art.upper()] = {
                "art": art, "nmID": c.get("nmID"),
                "chrtId": s0.get("chrtID"),
                "barcode": (s0.get("skus") or [""])[0],
                "litres": litres,
                "title": (c.get("title") or "")[:80],
                "length": dims.get("length"), "width": dims.get("width"),
                "height": dims.get("height"),
                "weight_g": (round(float(dims.get("weightBrutto")) * 1000)
                             if dims.get("weightBrutto") else None)}
        cur = data.get("cursor") or {}
        if (cur.get("total") or 0) < cursor.get("limit", 100) or not cards:
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"),
                  "nmID": cur.get("nmID")}
    if out:
        await asyncio.to_thread(_snap.save, "fbs_chrt_map", out)
    _log.info("fbs chrt_map: %d карточек", len(out))
    return out


async def get_stocks(wid: int | None = None) -> dict:
    """Текущие FBS-остатки склада: {АРТИКУЛ: qty}."""
    wid = wid or warehouse_id()
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


async def set_stocks(items: list[dict], wid: int | None = None) -> dict:
    """Выставить остатки: items = [{sku: 'AL-01', qty: 20}, …]."""
    wid = wid or warehouse_id()
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
        # вытащить код/сообщение WB, а не хвост data-массива
        try:
            errs = resp.json()
            msg = "; ".join(f"{e.get('code')}: {e.get('message')}"
                            for e in (errs if isinstance(errs, list) else [errs])
                            if isinstance(e, dict) and (e.get("code") or e.get("message")))
        except Exception:
            msg = resp.text[:200]
        return {"error": f"обновление → {resp.status_code}: {msg or resp.text[:200]}"}
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

# ── Мультисклад: виртуальный общий остаток на несколько складов WB ──────────
# Конфиг в kv fbs_multi: {enabled, stock:{SKU:qty}, linked:[warehouseId],
#   safety, zero_if_fbo, seen:[orderId], last_sync}

async def list_warehouses() -> list[dict]:
    """GET /api/v3/warehouses — все склады продавца."""
    resp = await _req("GET", "/api/v3/warehouses")
    if not resp.is_success:
        return []
    out = []
    for w in resp.json() or []:
        out.append({"id": w.get("id"), "name": w.get("name"),
                    "officeId": w.get("officeId"),
                    "cargoType": w.get("cargoType"),
                    "deliveryType": w.get("deliveryType")})
    return out


def _multi_load() -> dict:
    import snapshot as _snap
    return _snap.load("fbs_multi", None) or {
        "enabled": False, "stock": {}, "linked": [], "safety": 2,
        "zero_if_fbo": 0, "seen": [], "last_sync": ""}


def _multi_save(cfg: dict) -> None:
    import snapshot as _snap
    _snap.save("fbs_multi", cfg)


async def multi_sync(force: bool = False) -> dict:
    """Синк виртуального остатка на все привязанные склады:
    1) новые заказы списывают штуки из виртуального стока;
    2) страховочный запас вычитается;
    3) опция: обнулять SKU, если на складах WB (FBO) лежит ≥ N шт;
    4) результат пушится на каждый привязанный склад."""
    cfg = await asyncio.to_thread(_multi_load)
    if not cfg.get("enabled") or not cfg.get("linked") or not cfg.get("stock"):
        return {"skipped": "мультисклад выключен или не настроен"}
    # 1. списание по новым заказам (любой склад кабинета)
    seen = set(cfg.get("seen") or [])
    consumed = 0
    resp = await _req("GET", "/api/v3/orders/new")
    if resp.is_success:
        cmap = await chrt_map()
        by_nm = {v["nmID"]: k for k, v in cmap.items() if v.get("nmID")}
        for o in (resp.json() or {}).get("orders") or []:
            oid = o.get("id")
            if oid in seen:
                continue
            seen.add(oid)
            art = by_nm.get(o.get("nmId"))
            if art and cfg["stock"].get(art):
                cfg["stock"][art] = max(0, int(cfg["stock"][art]) - 1)
                consumed += 1
    cfg["seen"] = list(seen)[-2000:]
    # 2-3. эффективный остаток
    safety = int(cfg.get("safety") or 0)
    zero_n = int(cfg.get("zero_if_fbo") or 0)
    fbo: dict = {}
    if zero_n:
        try:
            for r in await wb_client.get_stocks():
                a = str(r.get("supplierArticle") or "").strip().upper()
                if a:
                    fbo[a] = fbo.get(a, 0) + int(r.get("quantity") or 0)
        except Exception as e:
            _log.warning("multi fbo stocks: %s", str(e)[:120])
    items = []
    zeroed = []
    for sku, qty in cfg["stock"].items():
        eff = max(0, int(qty) - safety)
        if zero_n and fbo.get(sku, 0) >= zero_n:
            eff = 0
            zeroed.append(sku)
        items.append({"sku": sku, "qty": eff})
    # 4. пуш на все склады
    results = {}
    for wid in cfg["linked"]:
        r = await set_stocks(items, wid=int(wid))
        results[str(wid)] = r.get("error") or f"ok ({r.get('updated')})"
        await asyncio.sleep(0.3)
    from datetime import datetime as _dt, timedelta as _td
    cfg["last_sync"] = (_dt.utcnow() + _td(hours=3)).strftime("%Y-%m-%d %H:%M")
    await asyncio.to_thread(_multi_save, cfg)
    return {"consumed_orders": consumed, "pushed": results,
            "zeroed_by_fbo": zeroed, "last_sync": cfg["last_sync"]}
