"""Документы по товарам: сертификаты Ozon (по API) + ручной реестр (WB и общие).

Ozon отдаёт по API загруженные сертификаты/декларации, их статусы, сроки
и привязки к товарам. У WB товарной сертификации в API нет — для него
ручной реестр: тип, номер, срок действия, покрытые артикулы. Система
подсвечивает истекающие документы и артикулы без покрытия.
"""
import asyncio
import json
import logging
import time as _time
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

import db

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/docs", tags=["docs"])

_EXPIRE_SOON_DAYS = 60


def _init_tables():
    db.execute("CREATE TABLE IF NOT EXISTS docs_manual ("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, doc_type TEXT, number TEXT, "
               "title TEXT, valid_to TEXT, skus TEXT, note TEXT)"
               if not db.IS_PG else
               "CREATE TABLE IF NOT EXISTS docs_manual ("
               "id SERIAL PRIMARY KEY, doc_type TEXT, number TEXT, "
               "title TEXT, valid_to TEXT, skus TEXT, note TEXT)")
    if db.IS_PG:
        # sequence после переезда БД мог отстать от скопированных id
        try:
            db.execute("SELECT setval(pg_get_serial_sequence('docs_manual', 'id'), "
                       "COALESCE((SELECT MAX(id) FROM docs_manual), 0) + 1, false)")
        except Exception:
            pass


def _expiry_status(valid_to: str) -> str:
    """ok | soon | expired | unknown."""
    if not valid_to:
        return "unknown"
    try:
        d = datetime.fromisoformat(valid_to[:10]).date()
    except ValueError:
        return "unknown"
    today = datetime.utcnow().date()
    if d < today:
        return "expired"
    if d <= today + timedelta(days=_EXPIRE_SOON_DAYS):
        return "soon"
    return "ok"


# ── Ozon: сертификаты из API (кэш 6ч) ────────────────────────────────────────

_oz_cache: dict = {}
_oz_ts: float = 0.0


def _first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


async def _fetch_ozon_certs() -> dict:
    import ozon_client
    import catalog as _cat
    raw = await ozon_client.get_certificates()
    certs = []
    for c in raw:
        number = str(_first(c, "certificate_number", "number", "id", default=""))
        cert = {
            "number": number,
            "name": _first(c, "certificate_name", "name", default=number),
            "type": _first(c, "type_code", "type", "certificate_type", default=""),
            "status": _first(c, "status_code", "status", default=""),
            "valid_to": str(_first(c, "expire_date", "valid_to", "expire_at", default=""))[:10],
            "products": [],
        }
        cert["expiry"] = _expiry_status(cert["valid_to"])
        if number:
            try:
                prods = await ozon_client.get_certificate_products(number)
                for p in prods:
                    pid = _first(p, "offer_id", "sku", "product_id")
                    if pid:
                        art = _cat.resolve_ozon(pid) if str(pid).isdigit() else _cat.canon(str(pid))
                        cert["products"].append(art)
                await asyncio.sleep(0.3)
            except Exception:
                pass
        certs.append(cert)
    # покрытие: артикулы каталога Ozon без действующего документа
    covered = {a for c in certs for a in c["products"]
               if c["expiry"] in ("ok", "soon") }
    all_oz = sorted(set(_cat.OZON_ID_TO_ART.values()))
    uncovered = [a for a in all_oz if a not in covered]
    return {"certs": certs, "uncovered": uncovered, "total_products": len(all_oz),
            "fetched_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")}


@router.get("/summary")
async def get_docs_summary(refresh: bool = Query(default=False)):
    """Сводка: сертификаты Ozon + ручной реестр + покрытие артикулов."""
    global _oz_cache, _oz_ts
    _init_tables()
    # холодный старт: сертификаты из БД сразу (N+1 запросов к Ozon по 0.3с
    # может превысить 100с лимит Render), свежее подтянется по refresh/TTL
    if not refresh and not _oz_cache:
        import snapshot as _snapmod
        snap = await asyncio.to_thread(_snapmod.load, "ozon_certs", None)
        if snap:
            _oz_cache = snap
            _oz_ts = _time.monotonic() - 6 * 3600 + 600  # свежесть 10 мин
    if refresh or not _oz_cache or _time.monotonic() - _oz_ts > 6 * 3600:
        try:
            _oz_cache = await _fetch_ozon_certs()
            _oz_ts = _time.monotonic()
            import snapshot as _snapmod
            await asyncio.to_thread(_snapmod.save, "ozon_certs", _oz_cache)
        except Exception as e:
            _log.warning("ozon certs: %s", e)
            if not _oz_cache:
                _oz_cache = {"certs": [], "uncovered": [], "total_products": 0,
                             "error": str(e)[:200]}

    import catalog as _cat
    rows = await asyncio.to_thread(
        db.fetchall,
        "SELECT id, doc_type, number, title, valid_to, skus, note FROM docs_manual ORDER BY valid_to")
    manual = []
    covered_manual: set = set()
    for r in rows:
        skus = [s.strip() for s in (r[5] or "").replace("\n", ",").split(",") if s.strip()]
        skus = [_cat.canon(s) for s in skus]
        st = _expiry_status(r[4] or "")
        manual.append({"id": r[0], "doc_type": r[1], "number": r[2], "title": r[3],
                       "valid_to": r[4], "skus": skus, "note": r[6] or "",
                       "expiry": st})
        if st in ("ok", "soon"):
            covered_manual.update(skus)
    # непокрытые артикулы всего каталога (для ручного реестра / WB)
    all_arts = sorted(_cat.CATALOG.keys())
    uncovered_manual = [a for a in all_arts if a not in covered_manual]
    return {
        "ozon": _oz_cache,
        "manual": manual,
        "manual_uncovered": uncovered_manual,
        "manual_covered": len(covered_manual),
        "catalog_total": len(all_arts),
        "expire_soon_days": _EXPIRE_SOON_DAYS,
    }


@router.get("/matrix")
async def get_docs_matrix():
    """Матрица покрытия: каждый SKU × (реестр документов, привязка Ozon).

    Статусы: ok / soon (истекает ≤60 дн) / expired / none."""
    _init_tables()
    import catalog as _cat
    import cost_store as _cs

    # ручной реестр: SKU → лучший документ
    rows = await asyncio.to_thread(
        db.fetchall,
        "SELECT doc_type, number, valid_to, skus FROM docs_manual")
    best: dict = {}
    _rank = {"ok": 3, "soon": 2, "expired": 1}
    for doc_type, number, valid_to, skus in rows:
        st = _expiry_status(valid_to or "")
        for s in (skus or "").replace("\n", ",").split(","):
            s = _cat.canon(s.strip())
            if not s:
                continue
            cur = best.get(s)
            if not cur or _rank.get(st, 0) > _rank.get(cur["status"], 0):
                best[s] = {"status": st, "doc_type": doc_type,
                           "number": number, "valid_to": valid_to}

    # Ozon: SKU → статус привязки из API-кеша
    oz: dict = {}
    for c in (_oz_cache.get("certs") or []):
        for a in c.get("products", []):
            cur = oz.get(a)
            st = c.get("expiry", "unknown")
            if not cur or _rank.get(st, 0) > _rank.get(cur, 0):
                oz[a] = st

    names = _cs.get_names()
    items = []
    for art in sorted(_cat.CATALOG.keys()):
        info = _cat.lookup(art)
        reg = best.get(art)
        items.append({
            "sku": art,
            "name": names.get(art) or info.get("name", ""),
            "group": info.get("brand", ""),
            "registry": reg or {"status": "none"},
            "ozon": oz.get(art, "none"),
            "on_ozon": art in set(_cat.OZON_ID_TO_ART.values()),
        })
    # проблемные сверху: нет документа / истёк / истекает
    order = {"none": 0, "expired": 1, "soon": 2, "unknown": 3, "ok": 4}
    items.sort(key=lambda x: order.get(x["registry"]["status"], 5))
    summary = {
        "total": len(items),
        "no_doc": sum(1 for i in items if i["registry"]["status"] == "none"),
        "expired": sum(1 for i in items if i["registry"]["status"] == "expired"),
        "soon": sum(1 for i in items if i["registry"]["status"] == "soon"),
        "ozon_unbound": sum(1 for i in items if i["on_ozon"]
                            and i["ozon"] in ("none", "expired")),
    }
    return {"items": items, "summary": summary}


@router.post("/manual")
async def add_manual_doc(payload: dict):
    """Добавить документ: {doc_type, number, title, valid_to, skus, note}."""
    _init_tables()
    doc_type = str(payload.get("doc_type") or "Декларация").strip()
    number = str(payload.get("number") or "").strip()
    title = str(payload.get("title") or "").strip()
    valid_to = str(payload.get("valid_to") or "").strip()[:10]
    skus = str(payload.get("skus") or "").strip()
    if not number and not title:
        raise HTTPException(status_code=400, detail="Укажите номер или название документа")
    await asyncio.to_thread(
        db.execute,
        "INSERT INTO docs_manual (doc_type, number, title, valid_to, skus, note) VALUES (?,?,?,?,?,?)",
        (doc_type, number, title, valid_to, skus, str(payload.get("note") or "")))
    return {"ok": True}


@router.delete("/manual/{doc_id}")
async def delete_manual_doc(doc_id: int):
    await asyncio.to_thread(db.execute, "DELETE FROM docs_manual WHERE id = ?", (doc_id,))
    return {"ok": True}
