"""Карточки WB → Ozon: фото и описание с WB дублируются на Ozon.

Пары карточек — по артикулу продавца (WB vendorCode = Ozon offer_id).

Сравнение:
  фото — по содержимому, а не по ссылкам (у WB и Ozon разные CDN): по миниатюре
         каждого фото считается перцептивный хеш (dHash 8×8), фото считаются
         одинаковыми при расстоянии Хэмминга ≤ 10 из 64. Видно, каких фото WB нет
         на Ozon, какие на Ozon лишние и совпадает ли порядок;
  описание — текст WB против характеристики Ozon 4191 «Аннотация».

Перенос (только по команде владельца или в автосинхронизации):
  фото     — POST /v2/product/pictures/import (v1 выключен): ВСЕ фото WB в том
             же порядке (первое — главное). Ozon берёт только JPG/PNG по публичной ссылке,
             а WB отдаёт WEBP — поэтому ссылки ведут на наш конвертер
             /api/tools/img/wb/… (JPG-копия фото WB);
  описание — POST /v1/product/attributes/update, характеристика 4191.
Название не трогаем: на Ozon оно своё (SEO площадки).

Автосинхронизация (раз в 6 ч, по умолчанию выключена): при включении текущее
состояние WB запоминается как исходное — дальше на Ozon уходят только НОВЫЕ
изменения карточек WB (сменили фото/описание → через ≤ 6 ч то же на Ozon).
"""
import asyncio
import hashlib
import io
import logging
import os
import re
import time as _time
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Request

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cardsync", tags=["cardsync"])

_state: dict = {"building": False, "progress": "", "error": "", "data": None, "ts": 0.0}
_TTL = 30 * 60
_HASH_KEY = "cardsync_hashes"     # url(+версия) → dHash hex
_LAST_KEY = "cardsync_last"       # артикул → {sig, at, what}
_AUTO_KEY = "cardsync_auto"       # {"on": bool, "since": "..."}
_ATTR_DESC = 4191
_MATCH = 10                       # порог Хэмминга для «то же фото»
_WB_IMG = re.compile(r"https://(basket-\d+)\.wbbasket\.ru/(vol\d+)/(part\d+)/(\d+)/images/big/(\d+)\.webp")
_bg: set = set()
_job: dict = {"running": False, "progress": "", "results": None, "error": ""}


def _msk() -> str:
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")


def _owner(request: Request):
    import auth as _auth
    sess = _auth._session_of(request) or {}
    if sess.get("role") != "owner":
        raise HTTPException(403, "Только для владельца")
    from config import READ_ONLY
    if READ_ONLY:
        raise HTTPException(403, "Кабинет «только чтение»")


def _kv(key, default):
    import snapshot
    return snapshot.load(key, default) or default


def _kv_save(key, val):
    import snapshot
    snapshot.save(key, val)


# ── перцептивный хеш ─────────────────────────────────────────────────────────

def _dhash(data: bytes) -> str:
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
    px = list(img.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return f"{bits:016x}"


def _ham(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _oz_thumb(url: str) -> str:
    # миниатюра есть только у фото, уже перезалитых Ozon на ir.ozone.ru; пока Ozon
    # обрабатывает загрузку, в карточке стоит наша исходная ссылка — берём её как есть
    if "ozone.ru/" not in url:
        return url
    base, name = url.rsplit("/", 1)
    return f"{base}/wc250/{name}"


async def _hashes(urls: list[tuple[str, str]], cache: dict) -> dict:
    """[(ключ кеша, url миниатюры)] → {ключ: hash}; качает только новые."""
    todo = [(k, u) for k, u in urls if k not in cache]
    sem = asyncio.Semaphore(6)

    async def one(cl, k, u):
        async with sem:
            try:
                r = await cl.get(u)
                if r.status_code == 200:
                    cache[k] = await asyncio.to_thread(_dhash, r.content)
            except Exception as e:
                _log.debug("cardsync hash %s: %s", u, e)

    if todo:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cl:
            await asyncio.gather(*(one(cl, k, u) for k, u in todo))
    return cache


# ── сбор и сравнение ─────────────────────────────────────────────────────────

async def _ozon_items() -> list[dict]:
    import ozon_client as oz
    out, last = [], ""
    for _ in range(20):
        d = await oz._post("/v4/product/info/attributes",
                           {"filter": {"visibility": "ALL"}, "limit": 1000, "last_id": last})
        res = d.get("result") or []
        out += res
        last = d.get("last_id") or ""
        if not last or len(res) < 1000:
            break
    return out


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").replace(" ", " ")).strip()


def _desc_oz(item: dict) -> str:
    for a in item.get("attributes") or []:
        if a.get("id") == _ATTR_DESC and a.get("values"):
            return a["values"][0].get("value") or ""
    return ""


def _sig(wb_hashes: list[str], desc: str) -> str:
    return hashlib.md5(("|".join(wb_hashes) + "#" + _norm(desc)).encode()).hexdigest()


def _compare_photos(w: list[str], o: list[str]) -> dict:
    used = set()
    match = []
    for h in w:
        best, bi = 99, None
        for j, g in enumerate(o):
            if j in used or not g:
                continue
            d = _ham(h, g) if h else 99
            if d < best:
                best, bi = d, j
        if bi is not None and best <= _MATCH:
            used.add(bi)
            match.append(bi)
        else:
            match.append(None)
    missing = sum(1 for m in match if m is None)
    extra = len(o) - len(used)
    same_order = missing == 0 and extra == 0 and match == list(range(len(o)))
    if same_order:
        st = "same"
    elif missing == 0 and extra == 0:
        st = "order"
    else:
        st = "diff"
    return {"status": st, "missing": missing, "extra": extra, "match": match}


async def _build() -> dict:
    import catalog_auto
    _state["progress"] = "карточки WB"
    wb_cards = await catalog_auto._wb_cards()
    _state["progress"] = "карточки Ozon"
    oz_items = await _ozon_items()
    wb = {str(c.get("vendorCode") or "").strip().upper(): c for c in wb_cards if c.get("vendorCode")}
    oz = {str(x.get("offer_id") or "").strip().upper(): x for x in oz_items if x.get("offer_id")}
    pairs = sorted(set(wb) & set(oz))
    cache = await asyncio.to_thread(_kv, _HASH_KEY, {})
    jobs = []
    for a in pairs:
        c = wb[a]
        ver = c.get("updatedAt") or ""
        for i, p in enumerate(c.get("photos") or []):
            jobs.append((f"wb|{c.get('nmID')}|{i}|{ver}", p.get("c246x328") or p.get("big")))
        x = oz[a]
        for u in ([x.get("primary_image")] if x.get("primary_image") else []) + list(x.get("images") or []):
            jobs.append((f"oz|{u}", _oz_thumb(u)))
    _state["progress"] = f"сравниваю фото ({len(jobs)} шт.)"
    await _hashes(jobs, cache)
    # кеш не раздуваем: только ключи текущих карточек
    live = {k for k, _ in jobs}
    cache = {k: v for k, v in cache.items() if k in live}
    await asyncio.to_thread(_kv_save, _HASH_KEY, cache)
    last = await asyncio.to_thread(_kv, _LAST_KEY, {})
    rows = []
    for a in pairs:
        c, x = wb[a], oz[a]
        ver = c.get("updatedAt") or ""
        wph = c.get("photos") or []
        w_h = [cache.get(f"wb|{c.get('nmID')}|{i}|{ver}", "") for i in range(len(wph))]
        o_urls = ([x.get("primary_image")] if x.get("primary_image") else []) + list(x.get("images") or [])
        o_h = [cache.get(f"oz|{u}", "") for u in o_urls]
        ph = _compare_photos(w_h, o_h)
        wd, od = c.get("description") or "", _desc_oz(x)
        rows.append({
            "art": a, "offer_id": x.get("offer_id"), "nm": c.get("nmID"),
            "product_id": x.get("id"), "sku": x.get("sku"),
            "title_wb": c.get("title") or "", "name_oz": x.get("name") or "",
            "wb_photos": [p.get("c246x328") or p.get("big") for p in wph],
            "wb_big": [p.get("big") for p in wph],
            "oz_photos": [_oz_thumb(u) for u in o_urls],
            "photos": ph, "photos_unhashed": sum(1 for h in w_h + o_h if not h),
            "desc_same": _norm(wd) == _norm(od), "desc_wb": wd, "desc_oz": od,
            "wb_updated": ver[:16].replace("T", " "),
            "sig": _sig(w_h, wd), "last": last.get(a),
        })
    only_wb = sorted(set(wb) - set(oz))
    only_oz = sorted(set(oz) - set(wb))
    return {"rows": rows, "only_wb": only_wb, "only_oz": only_oz, "built": _msk(),
            "auto": await asyncio.to_thread(_kv, _AUTO_KEY, {"on": False})}


async def _build_bg():
    _state.update(building=True, error="")
    try:
        _state["data"] = await _build()
        _state["ts"] = _time.monotonic()
    except Exception as e:
        _state["error"] = str(e)[:300]
        _log.warning("cardsync build: %s", e)
    finally:
        _state.update(building=False, progress="")


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


@router.get("/compare")
async def compare(refresh: bool = False):
    fresh = _state["data"] and _time.monotonic() - _state["ts"] < _TTL
    if (refresh or not fresh) and not _state["building"]:
        _spawn(_build_bg())
    if _state["data"] and not refresh:
        return {**_state["data"], "building": _state["building"], "progress": _state["progress"]}
    return {"building": True, "progress": _state["progress"] or "старт", "error": _state["error"]}


# ── перенос ──────────────────────────────────────────────────────────────────

def _public_base() -> str:
    # ссылки для Ozon: его загрузчик скачивает фото сам
    return (os.getenv("CARDSYNC_IMG_BASE") or os.getenv("RENDER_EXTERNAL_URL")
            or "https://wb-dashboard-6wxf.onrender.com").rstrip("/")


def _jpg_links(big_urls: list[str]) -> list[str]:
    out = []
    for u in big_urls[:30]:
        m = _WB_IMG.match(u or "")
        if m:
            b, vol, part, nm, n = m.groups()
            out.append(f"{_public_base()}/api/tools/img/wb/{b}/{vol}/{part}/{nm}/{n}.jpg")
    return out


async def _oz(path: str, body: dict) -> tuple[int, dict]:
    """Запрос в Ozon с телом ответа даже при ошибке (ozon_client._post его теряет)."""
    import ozon_client as oz
    for attempt in range(4):
        r = await oz._http().post(oz._BASE + path, headers=oz._headers(), json=body)
        if r.status_code == 429:
            await asyncio.sleep(3 * (attempt + 1))
            continue
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"message": r.text[:300]}
    return 429, {"message": "лимит запросов Ozon"}


async def _send_batches(path: str, items: list[dict], per: list[dict], field: str):
    """items ↔ per (результаты по товарам): шлём пачками по 100, пишем № задачи."""
    for i in range(0, len(items), 100):
        st, d = await _oz(path, {"items": items[i:i + 100]})
        tid = d.get("task_id") or (d.get("result") or {}).get("task_id")
        for one in per[i:i + 100]:
            if st == 200 and tid:
                one[field] = f"отправлено (задача {tid})"
                one.setdefault("tasks", []).append(int(tid))
            else:
                one[field] = f"ошибка {st}: {str(d.get('message') or d)[:200]}"
        await asyncio.sleep(0.5)


async def _apply(rows: list[dict], what: set) -> list[dict]:
    # Фото: POST /v2/product/pictures/import (v1 Ozon выключил — «obsolete method»).
    # Метода нет в спеке; формат подобран на живом кабинете:
    #   {"items": [{"offer_id", "primary_image", "images": [...]}]} (до 100) → {"task_id"};
    # статус по товарам — /v1/product/import/info.
    res = [{"art": r["art"], "photos": None, "desc": None} for r in rows]
    if "photos" in what:
        from routers import tools as _tools
        items, per, srcs = [], [], []
        for r, one in zip(rows, res):
            links = _jpg_links(r["wb_big"])
            if not links:
                one["photos"] = "нет фото WB"
                continue
            items.append({"offer_id": r.get("offer_id") or r["art"],
                          "primary_image": links[0], "images": links[1:]})
            per.append(one)
            srcs.append([m.groups() for m in (_WB_IMG.match(u or "") for u in r["wb_big"][:30]) if m])
        # Ozon качает фото пачкой сразу после задачи — если перекодировать WEBP→JPG
        # на лету, 512-МБ инстанс не успевает и часть фото (в т.ч. главное) теряется.
        # Поэтому: по 8 товаров; перед отправкой готовим JPG на диске; ждём, пока
        # Ozon обработает задачу, и только потом следующая пачка.
        B = 8
        for i in range(0, len(items), B):
            _job["progress"] = f"фото: готовлю JPG для товаров {i + 1}–{min(i + B, len(items))} из {len(items)}"
            for parts in srcs[i:i + B]:
                for g in parts:
                    try:
                        await _tools.wb_jpg_file(*g, force=True)
                    except Exception as e:
                        _log.warning("cardsync jpg %s: %s", g, e)
            _job["progress"] = f"фото: отправляю в Ozon товары {i + 1}–{min(i + B, len(items))} из {len(items)}"
            await _send_batches("/v2/product/pictures/import", items[i:i + B], per[i:i + B], "photos")
            tids = {t for one in per[i:i + B] for t in one.get("tasks", [])}
            for _ in range(40):                          # до ~6 мин на пачку
                await asyncio.sleep(9)
                pending = False
                for t in tids:
                    st, d = await _oz("/v1/product/import/info", {"task_id": t})
                    its = (d.get("result") or {}).get("items") or []
                    if not its or any(x.get("status") in ("pending", "processing") for x in its):
                        pending = True
                if not pending:
                    break
    if "desc" in what:
        items, per = [], []
        for r, one in zip(rows, res):
            if not _norm(r.get("desc_wb")):
                one["desc"] = "на WB нет описания"
                continue
            items.append({"offer_id": r.get("offer_id") or r["art"], "attributes": [
                {"complex_id": 0, "id": _ATTR_DESC,
                 "values": [{"dictionary_value_id": 0, "value": r["desc_wb"]}]}]})
            per.append(one)
        await _send_batches("/v1/product/attributes/update", items, per, "desc")
    last = await asyncio.to_thread(_kv, _LAST_KEY, {})
    now = _msk()
    for r, one in zip(rows, res):
        ok_p = str(one["photos"] or "").startswith("отправлено")
        ok_d = str(one["desc"] or "").startswith("отправлено")
        if ok_p or ok_d:
            prev = last.get(r["art"]) or {}
            last[r["art"]] = {"sig": r["sig"], "at": now,
                              "what": ", ".join(x for x, ok in (("фото", ok_p), ("описание", ok_d)) if ok)
                              or prev.get("what", "")}
    await asyncio.to_thread(_kv_save, _LAST_KEY, last)
    return res


@router.get("/tasks")
async def tasks(ids: str):
    """Статус задач Ozon по товарам: imported / failed + ошибки и предупреждения модерации."""
    out = []
    for tid in [int(x) for x in re.findall(r"\d+", ids)][:10]:
        st, d = await _oz("/v1/product/import/info", {"task_id": tid})
        r = d.get("result") or {}
        for it in r.get("items") or []:
            errs = [{"level": e.get("level"), "attr": e.get("attribute_name") or "",
                     "text": e.get("description") or e.get("message") or e.get("code")}
                    for e in it.get("errors") or []]
            out.append({"task": tid, "offer_id": it.get("offer_id"), "status": it.get("status"),
                        "errors": errs})
    return {"items": out}


async def _apply_job(rows, what):
    _job.update(running=True, progress="старт", results=None, error="", started=_msk())
    try:
        _job["results"] = await _apply(rows, what)
    except Exception as e:
        _job["error"] = str(e)[:300]
        _log.warning("cardsync apply: %s", e)
    finally:
        _job.update(running=False, progress="", finished=_msk())
        _state["ts"] = 0.0      # следующее открытие пересоберёт сравнение


@router.post("/apply")
async def apply(request: Request, payload: dict):
    """{arts: [...], what: ["photos","desc"]} — перенос с WB на Ozon (в фоне,
    статус — GET /api/cardsync/job)."""
    _owner(request)
    if _job.get("running"):
        raise HTTPException(409, "Перенос уже идёт: " + (_job.get("progress") or ""))
    data = _state.get("data")
    if not data:
        raise HTTPException(409, "Сначала постройте сравнение")
    what = {w for w in (payload.get("what") or []) if w in ("photos", "desc")}
    arts = {str(a).upper() for a in payload.get("arts") or []}
    rows = [r for r in data["rows"] if r["art"] in arts]
    if not rows or not what:
        raise HTTPException(400, "Не выбраны товары или что переносить")
    _spawn(_apply_job(rows, what))
    return {"started": True, "count": len(rows)}


@router.get("/job")
async def job():
    return _job


@router.post("/auto")
async def auto(request: Request, payload: dict):
    """Вкл/выкл автосинхронизацию. При включении текущее состояние WB
    запоминается как исходное — переносятся только будущие изменения."""
    _owner(request)
    on = bool(payload.get("on"))
    if on:
        data = _state.get("data")
        if not data:
            raise HTTPException(409, "Сначала постройте сравнение")
        last = await asyncio.to_thread(_kv, _LAST_KEY, {})
        for r in data["rows"]:
            last.setdefault(r["art"], {"sig": r["sig"], "at": _msk(), "what": "исходное состояние"})
            last[r["art"]]["sig"] = r["sig"]
        await asyncio.to_thread(_kv_save, _LAST_KEY, last)
    st = {"on": on, "since": _msk() if on else ""}
    await asyncio.to_thread(_kv_save, _AUTO_KEY, st)
    if _state.get("data"):
        _state["data"]["auto"] = st
    return st


async def loop():
    """Раз в 6 ч: если автосинхронизация включена — переносим изменённые на WB карточки."""
    from config import READ_ONLY, CABINET_MARKETPLACES
    if READ_ONLY or not {"WB", "OZON"} <= set(CABINET_MARKETPLACES):
        return
    await asyncio.sleep(900)
    while True:
        try:
            st = await asyncio.to_thread(_kv, _AUTO_KEY, {"on": False})
            if st.get("on"):
                data = await _build()
                _state["data"], _state["ts"] = data, _time.monotonic()
                changed = [r for r in data["rows"]
                           if (r.get("last") or {}).get("sig") != r["sig"]
                           and not r["photos_unhashed"]]
                if changed:
                    what = {"photos", "desc"}
                    res = await _apply(changed, what)
                    _log.info("cardsync auto: %s", [(x["art"], x["photos"], x["desc"]) for x in res])
                    try:
                        import agent_review as _ar
                        await _ar.tg_send("<b>WB → Ozon: карточки обновлены</b>\n" + "\n".join(
                            f"{x['art']}: фото — {x['photos'] or '—'}; описание — {x['desc'] or '—'}"
                            for x in res[:20]))
                    except Exception:
                        pass
        except Exception as e:
            _log.warning("cardsync loop: %s", e)
        await asyncio.sleep(6 * 3600)
