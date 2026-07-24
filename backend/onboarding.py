"""Автоонбординг кабинета: каталог собирается из API, а не пишется руками.

Раньше подключение нового кабинета означало вручную написать catalog_XX.py
с маппингом nmId → артикул на несколько сотен строк. Всё это площадки
отдают сами:
  WB    — content/v2/get/cards/list: nmID, артикул продавца, название,
          бренд, категория (subjectName).
  Ozon  — v4/product/info/attributes: sku, offer_id, название.
  ЯМ    — offer_id из каталога кампании.
Единственное, что должен принести клиент, — себестоимость (файлом).

Результат кладётся в таблицу catalog_auto и подмешивается к статическому
каталогу: жёстко прописанное всегда в приоритете, чтобы не сломать
работающие кабинеты.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import db

_log = logging.getLogger("onboarding")


def _init() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS catalog_auto (
        art TEXT PRIMARY KEY, name TEXT, brand TEXT, grp TEXT,
        wb_id TEXT, ozon_id TEXT, ym_id TEXT, updated TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS catalog_auto_wb ON catalog_auto (wb_id)")


def _msk() -> str:
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")


async def _from_wb() -> dict:
    """WB: карточки → артикул продавца, nmID, имя, бренд, категория."""
    import wb_client
    out: dict = {}
    cursor = {"limit": 100}
    for _ in range(60):
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        try:
            r = await wb_client._http().post(
                f"{wb_client.CONTENT_BASE}/content/v2/get/cards/list",
                headers=wb_client._headers(), json=body)
            if not r.is_success:
                _log.warning("WB cards %s: %s", r.status_code, r.text[:200])
                break
            data = r.json() or {}
        except Exception as e:
            _log.warning("WB cards: %s", e)
            break
        cards = data.get("cards") or []
        for c in cards:
            art = str(c.get("vendorCode") or "").strip()
            if not art:
                continue
            out.setdefault(art, {})
            out[art].update({
                "wb_id": str(c.get("nmID") or ""),
                "name": (c.get("title") or c.get("subjectName") or art)[:200],
                "brand": (c.get("brand") or "")[:100],
                "grp": (c.get("subjectName") or "")[:100]})
        cur = data.get("cursor") or {}
        if len(cards) < 100 or not cur.get("updatedAt"):
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"),
                  "nmID": cur.get("nmID")}
    return out


async def _from_ozon() -> dict:
    """Ozon: товары → offer_id (наш артикул) + sku."""
    import ozon_client
    out: dict = {}
    try:
        items = await ozon_client._get_all_skus()
    except Exception as e:
        _log.warning("Ozon skus: %s", e)
        return out
    for it in items:
        art = str(it.get("offer_id") or "").strip()
        if art:
            out.setdefault(art, {})["ozon_id"] = str(it.get("sku") or "")
    return out


async def _from_ym() -> dict:
    """ЯМ: offer_id из каталога кампании."""
    out: dict = {}
    try:
        import ym_client
        fn = getattr(ym_client, "get_offers", None) or \
            getattr(ym_client, "get_catalog", None)
        if not fn:
            return out
        for it in (await fn()) or []:
            art = str(it.get("offerId") or it.get("offer_id") or "").strip()
            if art:
                out.setdefault(art, {})["ym_id"] = art
    except Exception as e:
        _log.warning("YM offers: %s", str(e)[:150])
    return out


async def scan() -> dict:
    """Собрать каталог со всех подключённых площадок и сохранить."""
    _init()
    wb, oz, ym = await asyncio.gather(_from_wb(), _from_ozon(), _from_ym(),
                                      return_exceptions=True)
    merged: dict = {}
    for src in (wb, oz, ym):
        if not isinstance(src, dict):
            continue
        for art, row in src.items():
            merged.setdefault(art, {}).update(row)
    if not merged:
        return {"error": "ни одна площадка не отдала каталог — проверь ключи"}
    now = _msk()
    for art, r in merged.items():
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO catalog_auto (art, name, brand, grp, wb_id, ozon_id, ym_id, updated)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (art) DO UPDATE SET "
            "name=excluded.name, brand=excluded.brand, grp=excluded.grp, "
            "wb_id=COALESCE(NULLIF(excluded.wb_id,''), catalog_auto.wb_id), "
            "ozon_id=COALESCE(NULLIF(excluded.ozon_id,''), catalog_auto.ozon_id), "
            "ym_id=COALESCE(NULLIF(excluded.ym_id,''), catalog_auto.ym_id), "
            "updated=excluded.updated",
            (art, r.get("name") or art, r.get("brand") or "", r.get("grp") or "",
             r.get("wb_id") or "", r.get("ozon_id") or "", r.get("ym_id") or "", now))
    groups = sorted({r.get("grp") or "" for r in merged.values()} - {""})
    return {"skus": len(merged),
            "wb": sum(1 for r in merged.values() if r.get("wb_id")),
            "ozon": sum(1 for r in merged.values() if r.get("ozon_id")),
            "ym": sum(1 for r in merged.values() if r.get("ym_id")),
            "groups": groups, "updated": now}


def load() -> dict:
    """Автокаталог из БД: {art: {name, brand, grp, wb_id, ozon_id, ym_id}}."""
    _init()
    rows = db.fetchall(
        "SELECT art, name, brand, grp, wb_id, ozon_id, ym_id FROM catalog_auto")
    keys = ["name", "brand", "grp", "wb_id", "ozon_id", "ym_id"]
    return {r[0]: dict(zip(keys, r[1:])) for r in rows}


def status() -> dict:
    """Что уже готово к работе, а чего для кабинета не хватает."""
    _init()
    auto = load()
    try:
        import cost_store
        costs = cost_store.get_costs() or {}
    except Exception:
        costs = {}
    try:
        import catalog as _cat
        static_n = len(getattr(_cat, "CATALOG", {}))
    except Exception:
        static_n = 0
    no_cost = [a for a in auto if not costs.get(a)]
    row = db.fetchone("SELECT MAX(updated) FROM catalog_auto")
    return {
        "auto_skus": len(auto),
        "static_skus": static_n,
        "with_wb": sum(1 for r in auto.values() if r.get("wb_id")),
        "with_ozon": sum(1 for r in auto.values() if r.get("ozon_id")),
        "cost_known": len(auto) - len(no_cost),
        "cost_missing": len(no_cost),
        "cost_missing_list": sorted(no_cost)[:30],
        "groups": sorted({r.get("grp") or "" for r in auto.values()} - {""}),
        "last_scan": row[0] if row else None,
        "ready": bool(auto) and len(no_cost) < max(1, len(auto) // 2),
    }
