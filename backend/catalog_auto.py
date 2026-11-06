"""Автокаталог для кабинетов без ручного каталога (Технолюкс, Аврора и др.).

У Biomed и ФК каталог прописан руками (catalog.py / catalog_fk.py). Для новых
кабинетов собираем его из карточек площадок:
  WB   — /content/v2/get/cards/list: nmID, vendorCode, title, brand, subjectName;
  Ozon — /v4/product/info/attributes: sku, offer_id, name, бренд (атрибут 85).
Артикул = артикул продавца (vendorCode / offer_id). Группа = бренд.

Каталог сохраняется в kv (snapshot) — после рестарта поднимается сразу,
обновление с площадок раз в 6 часов в фоне.
"""
import asyncio
import logging

import catalog as _cat

_log = logging.getLogger(__name__)
_SNAP_KEY = "auto_catalog"
REFRESH_SEC = 6 * 3600

state: dict = {"loaded": False, "items": 0, "updated": None, "error": ""}


def enabled() -> bool:
    from config import CABINET
    return CABINET not in ("biomed", "fk")


def _apply(data: dict) -> None:
    """Заливает собранный каталог в словари модуля catalog (на месте — ссылки
    в других модулях продолжают работать)."""
    _cat.WB_ID_TO_ART.clear(); _cat.WB_ID_TO_ART.update(data.get("wb") or {})
    _cat.OZON_ID_TO_ART.clear(); _cat.OZON_ID_TO_ART.update(data.get("ozon") or {})
    _cat.CATALOG.clear(); _cat.CATALOG.update(data.get("catalog") or {})
    _cat._CATALOG_UP.clear()
    _cat._CATALOG_UP.update({k.upper(): v for k, v in _cat.CATALOG.items()})
    brands: dict[str, int] = {}
    for v in _cat.CATALOG.values():
        b = v.get("brand") or "Прочее"
        brands[b] = brands.get(b, 0) + 1
    order = [b for b, _ in sorted(brands.items(), key=lambda kv: -kv[1]) if b != "Прочее"]
    _cat.BRAND_ORDER[:] = order
    state.update(loaded=True, items=len(_cat.CATALOG), updated=data.get("updated"))


def group_order() -> list[str]:
    return list(_cat.BRAND_ORDER) + ["Прочее"]


async def _wb_cards() -> list[dict]:
    from config import USE_MOCK
    if USE_MOCK:
        return []
    import wb_client as wb
    url = f"{wb.CONTENT_BASE}/content/v2/get/cards/list"
    cursor: dict = {"limit": 100}
    out: list[dict] = []
    for _ in range(100):                       # до 10 000 карточек
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        r = await wb._http().post(url, headers=wb._headers(), json=body)
        if r.status_code == 429:
            await asyncio.sleep(21)
            continue
        if not r.is_success:
            raise RuntimeError(f"WB cards/list {r.status_code}: {r.text[:150]}")
        data = r.json() or {}
        cards = data.get("cards") or []
        out += cards
        cur = data.get("cursor") or {}
        if len(cards) < cursor["limit"] or not cur.get("nmID"):
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
    return out


async def _ozon_products() -> list[dict]:
    from config import OZON_CLIENT_ID, CABINET_MARKETPLACES
    if not OZON_CLIENT_ID or "OZON" not in CABINET_MARKETPLACES:
        return []
    import ozon_client as oz
    out: list[dict] = []
    last_id = ""
    for _ in range(50):
        data = await oz._post("/v4/product/info/attributes",
                              {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000})
        res = data.get("result") or []
        out += res
        last_id = data.get("last_id") or ""
        if not last_id or len(res) < 1000:
            break
    return out


def _ozon_brand(item: dict) -> str:
    for a in item.get("attributes") or []:
        if a.get("id") == 85:                  # «Бренд»
            vals = a.get("values") or []
            if vals and vals[0].get("value"):
                return str(vals[0]["value"])
    return ""


async def build() -> dict:
    wb_map, oz_map, cat = {}, {}, {}
    errors = []
    try:
        wb_cards = await _wb_cards()
    except Exception as e:                     # ошибка WB не мешает собрать Ozon
        wb_cards = []
        errors.append(str(e)[:200])
    try:
        oz_items = await _ozon_products()
    except Exception as e:
        oz_items = []
        errors.append("Ozon: " + str(e)[:200])
    state["error"] = "; ".join(errors)
    for c in wb_cards:
        art = str(c.get("vendorCode") or "").strip()
        nm = c.get("nmID")
        if not art or not nm:
            continue
        wb_map[str(nm)] = art
        cat[art] = {"name": (c.get("title") or art)[:120], "brand": c.get("brand") or "Прочее",
                    "group": c.get("subjectName") or ""}
    for p in oz_items:
        art = str(p.get("offer_id") or "").strip()
        if not art:
            continue
        for key in (p.get("sku"), p.get("id")):
            if key:
                oz_map[str(key)] = art
        if art not in cat:
            cat[art] = {"name": (p.get("name") or art)[:120], "brand": _ozon_brand(p) or "Прочее",
                        "group": ""}
    from datetime import datetime
    return {"wb": wb_map, "ozon": oz_map, "catalog": cat,
            "updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M")}


def load_snapshot() -> None:
    """Синхронно при старте: последний сохранённый каталог из БД."""
    if not enabled():
        return
    try:
        import snapshot
        data = snapshot.load(_SNAP_KEY, None)
        if data:
            _apply(data)
            _log.info("автокаталог из БД: %d артикулов", state["items"])
    except Exception as e:
        _log.warning("автокаталог из БД: %s", str(e)[:150])


async def refresh() -> dict:
    data = await build()
    if not data["catalog"]:
        state["error"] = state["error"] or "площадки не вернули ни одной карточки"
        return state
    _apply(data)
    import snapshot
    await asyncio.to_thread(snapshot.save, _SNAP_KEY, data)
    _log.info("автокаталог обновлён: %d артикулов (WB %d, Ozon %d)",
              len(data["catalog"]), len(data["wb"]), len(data["ozon"]))
    return state


async def loop() -> None:
    if not enabled():
        return
    await asyncio.sleep(20)
    while True:
        try:
            await refresh()
        except Exception as e:
            state["error"] = str(e)[:200]
            _log.warning("автокаталог: %s", state["error"])
        await asyncio.sleep(REFRESH_SEC)
