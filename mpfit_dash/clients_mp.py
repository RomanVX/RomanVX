"""Клиенты фулфилмента: продажи из их кабинетов WB/Ozon (по их API-ключам).

Зачем: видеть, сколько заказов клиента уходит со складов маркетплейсов (FBO),
сколько через FBS и с каких складов продавца (наш Чехов или чужие фулфилменты),
средние продажи и динамику по периодам.

Ключи клиентов хранятся в БД зашифрованными (Fernet). Ключ шифрования —
env KEYS_SECRET, а если не задан — выводится из DASH_PASSWORD (тогда при
смене пароля дашборда ключи клиентов придётся ввести заново).

Данные:
  WB   — statistics-api /api/v1/supplier/orders: 1 строка = 1 единица товара,
         warehouseType «Склад WB» = FBO, «Склад продавца» = FBS, warehouseName.
         Хранится 90 дней; лимит 1 запрос в минуту.
  Ozon — /v2/posting/fbo/list (analytics_data.warehouse_name) и
         /v3/posting/fbs/list (delivery_method.warehouse).
Храним агрегаты по дню (МСК) × площадка × модель × склад — mp_orders_daily.
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "backend"))
import asyncio
import base64
import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta

import httpx

import db

_log = logging.getLogger("clients_mp")
FIRST_DAYS = 90            # первый сбор — вся доступная история WB
REFRESH_DAYS = 14          # дальше пересобираем последние 2 недели (отмены меняются)
EVERY_SEC = 3 * 3600
OURS_HINTS = ("чехов",)    # склады продавца с таким именем считаем нашими по умолчанию

state: dict = {"running": False, "last_run": "", "clients": {}}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS mp_keys (
        id TEXT PRIMARY KEY, client TEXT, mp TEXT, client_id TEXT, secret TEXT,
        created_at TEXT, last_ok TEXT, last_error TEXT)""",
    """CREATE TABLE IF NOT EXISTS mp_orders_daily (
        client TEXT, mp TEXT, day TEXT, model TEXT, warehouse TEXT,
        qty INTEGER, sum REAL, cancels INTEGER,
        PRIMARY KEY (client, mp, day, model, warehouse))""",
    "CREATE TABLE IF NOT EXISTS mp_wh_flags (warehouse TEXT PRIMARY KEY, ours INTEGER)",
]


def init():
    for sql in SCHEMA:
        db.execute(sql)


# ── шифрование ключей ───────────────────────────────────────────────────────

def _fernet():
    from cryptography.fernet import Fernet
    secret = (os.environ.get("KEYS_SECRET") or "").strip() \
        or "dash:" + (os.environ.get("DASH_PASSWORD") or "").strip()
    key = base64.urlsafe_b64encode(hashlib.sha256(("mpfit-keys|" + secret).encode()).digest())
    return Fernet(key)


def _enc(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode()


def _dec(s: str) -> str:
    return _fernet().decrypt(s.encode()).decode()


# ── ключи ───────────────────────────────────────────────────────────────────

def keys_list() -> list[dict]:
    rows = db.fetchall("SELECT id, client, mp, client_id, created_at, last_ok, last_error "
                       "FROM mp_keys ORDER BY client, mp")
    out = []
    for i, c, mp, cid, cr, ok, err in rows:
        out.append({"id": i, "client": c, "mp": mp,
                    "client_id": cid or "", "created_at": cr, "last_ok": ok, "last_error": err})
    return out


def key_add(client: str, mp: str, secret: str, client_id: str = "") -> str:
    client, mp = client.strip(), mp.strip().lower()
    if not client or mp not in ("wb", "ozon") or not secret.strip():
        raise ValueError("нужны клиент, площадка (wb/ozon) и ключ")
    if mp == "ozon" and not client_id.strip():
        raise ValueError("для Ozon нужен Client-Id")
    # один ключ на клиента и площадку — старый заменяем
    db.execute("DELETE FROM mp_keys WHERE client = ? AND mp = ?", (client, mp))
    i = uuid.uuid4().hex[:12]
    db.execute("INSERT INTO mp_keys (id, client, mp, client_id, secret, created_at) VALUES (?,?,?,?,?,?)",
               (i, client, mp, client_id.strip(), _enc(secret.strip()),
                datetime.utcnow().isoformat(timespec="seconds")))
    return i


def key_delete(i: str):
    row = db.fetchall("SELECT client, mp FROM mp_keys WHERE id = ?", (i,))
    db.execute("DELETE FROM mp_keys WHERE id = ?", (i,))
    if row:
        db.execute("DELETE FROM mp_orders_daily WHERE client = ? AND mp = ?", row[0])


def _keys_full() -> list[dict]:
    rows = db.fetchall("SELECT id, client, mp, client_id, secret FROM mp_keys")
    out = []
    for i, c, mp, cid, sec in rows:
        try:
            out.append({"id": i, "client": c, "mp": mp, "client_id": cid, "secret": _dec(sec)})
        except Exception:
            _mark(i, err="ключ не расшифровывается (сменился пароль дашборда?) — введите заново")
    return out


def _mark(i: str, ok: bool = False, err: str = ""):
    now = datetime.utcnow().isoformat(timespec="seconds")
    if ok:
        db.execute("UPDATE mp_keys SET last_ok = ?, last_error = '' WHERE id = ?", (now, i))
    else:
        if "X-Client-Secret" in err:
            err = ("WB: это сервисный токен (выпущен для стороннего сервиса), без секрета сервиса "
                   "он не работает. Нужен обычный токен: ЛК WB → Доступ к API → «Базовый» "
                   "(или «Персональный»), категории Маркетплейс, Контент, Статистика, Цены — только чтение")
        db.execute("UPDATE mp_keys SET last_error = ? WHERE id = ?", (err[:300], i))


# ── сбор: WB ────────────────────────────────────────────────────────────────

def _msk_day(ts: str) -> str:
    """'2026-11-05T21:30:00' (МСК у WB) → '2026-11-05'; ISO с Z (UTC, Ozon) → день МСК."""
    if not ts:
        return ""
    if ts.endswith("Z") or "+00:00" in ts:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00").split(".")[0].replace("+00:00", ""))
        return (d + timedelta(hours=3)).strftime("%Y-%m-%d")
    return ts[:10]


async def _wb_orders(token: str, date_from: str) -> list[dict]:
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
    rows: list[dict] = []
    cursor = date_from
    async with httpx.AsyncClient(timeout=120) as cl:
        for page in range(20):
            r = await cl.get(url, params={"dateFrom": cursor, "flag": 0},
                             headers={"Authorization": token})
            if r.status_code == 429:
                await asyncio.sleep(65)
                continue
            if r.status_code == 401:
                raise RuntimeError("WB: ключ не подходит (401) — нужен доступ к «Статистике»")
            if not r.is_success:
                raise RuntimeError(f"WB orders {r.status_code}: {r.text[:200]}")
            part = r.json() or []
            rows += part
            if len(part) < 80000:
                break
            cursor = part[-1].get("lastChangeDate") or cursor
            await asyncio.sleep(61)        # лимит statistics-api: 1 запрос в минуту
    return rows


def _agg_wb(rows: list[dict], since_day: str) -> dict:
    agg: dict = {}
    seen = set()
    for r in rows:
        sid = r.get("srid") or ""
        if sid and sid in seen:
            continue
        seen.add(sid)
        day = _msk_day(r.get("date") or "")
        if not day or day < since_day:
            continue
        model = "FBS" if "продавца" in (r.get("warehouseType") or "").lower() else "FBO"
        wh = (r.get("warehouseName") or "—").strip()
        k = (day, model, wh)
        a = agg.setdefault(k, [0, 0.0, 0])
        a[0] += 1
        a[1] += float(r.get("priceWithDisc") or 0)
        a[2] += 1 if r.get("isCancel") else 0
    return agg


# Запасной путь, если у ключа нет «Статистики», а есть «Маркетплейс» (часто
# клиенты дают фулфилменту именно такой ключ): только FBS-сборочные задания
# со складов продавца — FBO-заказов так не видно.
WB_MP = "https://marketplace-api.wildberries.ru"


async def _wb_fbs_agg(token: str, since: datetime) -> dict:
    agg: dict = {}
    async with httpx.AsyncClient(timeout=90) as cl:
        h = {"Authorization": token}
        r = await cl.get(WB_MP + "/api/v3/warehouses", headers=h)
        if not r.is_success:
            raise RuntimeError(f"WB warehouses {r.status_code}: {r.text[:150]}")
        names = {w.get("id"): (w.get("name") or str(w.get("id"))) for w in r.json() or []}
        now = datetime.utcnow()
        start = since
        while start < now:                         # не больше 30 дней за запрос
            end = min(start + timedelta(days=29), now)
            nxt = 0
            for _ in range(200):
                r = await cl.get(WB_MP + "/api/v3/orders", headers=h, params={
                    "limit": 1000, "next": nxt,
                    "dateFrom": int(start.timestamp()), "dateTo": int(end.timestamp())})
                if r.status_code == 429:
                    await asyncio.sleep(10)
                    continue
                if not r.is_success:
                    raise RuntimeError(f"WB orders FBS {r.status_code}: {r.text[:150]}")
                data = r.json() or {}
                orders = data.get("orders") or []
                for o in orders:
                    day = _msk_day(o.get("createdAt") or "")
                    wh = names.get(o.get("warehouseId"), str(o.get("warehouseId") or "—"))
                    a = agg.setdefault((day, "FBS", wh), [0, 0.0, 0])
                    a[0] += 1
                    a[1] += float(o.get("convertedPrice") or o.get("price") or 0) / 100
                nxt = data.get("next") or 0
                if len(orders) < 1000 or not nxt:
                    break
                await asyncio.sleep(0.4)
            start = end
    return agg


# ── сбор: Ozon ──────────────────────────────────────────────────────────────

async def _oz_post(cl, cid, key, path, body):
    for attempt in range(4):
        r = await cl.post("https://api-seller.ozon.ru" + path, json=body,
                          headers={"Client-Id": cid, "Api-Key": key})
        if r.status_code == 429:
            await asyncio.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"Ozon: ключ не подходит ({r.status_code})")
        if not r.is_success:
            raise RuntimeError(f"Ozon {path} {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError("Ozon: 429, лимит запросов")


def _oz_amount(products) -> float:
    s = 0.0
    for p in products or []:
        try:
            s += float(p.get("price") or 0) * int(p.get("quantity") or 1)
        except Exception:
            pass
    return s


def _oz_qty(products) -> int:
    return sum(int(p.get("quantity") or 1) for p in products or []) or 1


async def _ozon_agg(cid: str, key: str, since: datetime) -> dict:
    agg: dict = {}
    def add(day, model, wh, qty, amount, cancelled):
        a = agg.setdefault((day, model, (wh or "—").strip()), [0, 0.0, 0])
        a[0] += qty
        a[1] += amount
        a[2] += qty if cancelled else 0
    now = datetime.utcnow()
    async with httpx.AsyncClient(timeout=90) as cl:
        start = since
        while start < now:                         # окна по 30 дней
            end = min(start + timedelta(days=30), now)
            f_since, f_to = start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            offset = 0
            while True:                            # FBO
                data = await _oz_post(cl, cid, key, "/v2/posting/fbo/list", {
                    "dir": "ASC", "filter": {"since": f_since, "to": f_to, "status": ""},
                    "limit": 1000, "offset": offset, "translit": True,
                    "with": {"analytics_data": True, "financial_data": False}})
                res = data.get("result") or []
                for p in res:
                    add(_msk_day(p.get("created_at") or ""), "FBO",
                        (p.get("analytics_data") or {}).get("warehouse_name"),
                        _oz_qty(p.get("products")), _oz_amount(p.get("products")),
                        p.get("status") == "cancelled")
                if len(res) < 1000:
                    break
                offset += 1000
            offset = 0
            while True:                            # FBS / rFBS
                data = await _oz_post(cl, cid, key, "/v3/posting/fbs/list", {
                    "dir": "ASC", "filter": {"since": f_since, "to": f_to},
                    "limit": 1000, "offset": offset,
                    "with": {"analytics_data": False, "financial_data": False}})
                res = (data.get("result") or {}).get("postings") or []
                for p in res:
                    dm = p.get("delivery_method") or {}
                    add(_msk_day(p.get("in_process_at") or p.get("created_at") or ""), "FBS",
                        dm.get("warehouse"), _oz_qty(p.get("products")), _oz_amount(p.get("products")),
                        p.get("status") == "cancelled")
                if not (data.get("result") or {}).get("has_next"):
                    break
                offset += 1000
            start = end
    return agg


# ── запись ──────────────────────────────────────────────────────────────────

def _save(client: str, mp: str, agg: dict, since_day: str):
    db.execute("DELETE FROM mp_orders_daily WHERE client = ? AND mp = ? AND day >= ?",
               (client, mp, since_day))
    rows = [(client, mp, d, m, w, a[0], round(a[1], 2), a[2])
            for (d, m, w), a in agg.items() if d >= since_day]
    if rows:
        db.executemany("INSERT INTO mp_orders_daily (client, mp, day, model, warehouse, qty, sum, cancels) "
                       "VALUES (?,?,?,?,?,?,?,?)", rows)


def _has_history(client: str, mp: str) -> bool:
    r = db.fetchall("SELECT COUNT(*) FROM mp_orders_daily WHERE client = ? AND mp = ?", (client, mp))
    return bool(r and r[0][0])


async def collect_one(k: dict) -> dict:
    days = REFRESH_DAYS if _has_history(k["client"], k["mp"]) else FIRST_DAYS
    since = datetime.utcnow() - timedelta(days=days)
    since_day = (since + timedelta(hours=3)).strftime("%Y-%m-%d")
    note = ""
    if k["mp"] == "wb":
        try:
            rows = await _wb_orders(k["secret"], since.strftime("%Y-%m-%dT00:00:00"))
            agg = _agg_wb(rows, since_day)
        except RuntimeError as e:
            if "403" not in str(e) and "401" not in str(e):
                raise
            agg = await _wb_fbs_agg(k["secret"], since)
            note = ("частично: у ключа нет категории «Статистика» — собраны только FBS-заказы "
                    "со складов продавца, заказов со складов WB (FBO) не видно")
    else:
        agg = await _ozon_agg(k["client_id"], k["secret"], since)
    await asyncio.to_thread(_save, k["client"], k["mp"], agg, since_day)
    return {"days": days, "rows": sum(a[0] for a in agg.values()), "note": note}


async def collect_all(only_id: str | None = None):
    if state["running"]:
        return
    state["running"] = True
    try:
        for k in await asyncio.to_thread(_keys_full):
            if only_id and k["id"] != only_id:
                continue
            try:
                res = await collect_one(k)
                await asyncio.to_thread(_mark, k["id"], True)
                if res.get("note"):
                    await asyncio.to_thread(_mark, k["id"], False, "⚠ " + res["note"])
                state["clients"][f'{k["client"]}/{k["mp"]}'] = res
            except Exception as e:
                await asyncio.to_thread(_mark, k["id"], False, str(e))
                _log.warning("%s/%s: %s", k["client"], k["mp"], str(e)[:200])
        state["last_run"] = (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m %H:%M МСК")
    finally:
        state["running"] = False


async def loop():
    await asyncio.sleep(60)
    while True:
        try:
            await collect_all()
        except Exception as e:
            _log.warning("сбор: %s", str(e)[:200])
        await asyncio.sleep(EVERY_SEC)


# ── отчёт ───────────────────────────────────────────────────────────────────

def _ours_map() -> dict:
    return {w: bool(o) for w, o in db.fetchall("SELECT warehouse, ours FROM mp_wh_flags")}


def set_ours(warehouse: str, ours: bool):
    db.execute("DELETE FROM mp_wh_flags WHERE warehouse = ?", (warehouse,))
    db.execute("INSERT INTO mp_wh_flags (warehouse, ours) VALUES (?, ?)", (warehouse, 1 if ours else 0))


def _is_ours(model: str, wh: str, flags: dict) -> bool:
    if model != "FBS":
        return False
    if wh in flags:
        return flags[wh]
    return any(h in wh.lower() for h in OURS_HINTS)


def report(client: str, days: int = 30) -> dict:
    """Период — последние N полных дней (без сегодняшнего) + такой же период до него."""
    today = (datetime.utcnow() + timedelta(hours=3)).date()
    d1 = today - timedelta(days=1)
    d0 = d1 - timedelta(days=days - 1)
    p1 = d0 - timedelta(days=1)
    p0 = p1 - timedelta(days=days - 1)
    rows = db.fetchall("SELECT mp, day, model, warehouse, qty, sum, cancels FROM mp_orders_daily "
                       "WHERE client = ? AND day >= ? AND day <= ?", (client, p0.isoformat(), d1.isoformat()))
    flags = _ours_map()

    def bucket(lo, hi):
        tot = {"qty": 0, "sum": 0.0, "cancels": 0, "fbo": 0, "fbs": 0, "ours": 0}
        for mp, day, model, wh, q, s, c in rows:
            if lo <= day <= hi:
                tot["qty"] += q; tot["sum"] += s; tot["cancels"] += c
                tot["fbo" if model == "FBO" else "fbs"] += q
                if _is_ours(model, wh, flags):
                    tot["ours"] += q
        return tot

    cur = bucket(d0.isoformat(), d1.isoformat())
    prev = bucket(p0.isoformat(), p1.isoformat())
    # склады за текущий период
    whs: dict = {}
    for mp, day, model, wh, q, s, c in rows:
        if d0.isoformat() <= day <= d1.isoformat():
            w = whs.setdefault((mp, model, wh), {"mp": mp.upper(), "model": model, "warehouse": wh,
                                                 "qty": 0, "sum": 0.0, "cancels": 0})
            w["qty"] += q; w["sum"] += s; w["cancels"] += c
    wh_list = sorted(whs.values(), key=lambda w: -w["qty"])
    for w in wh_list:
        w["share"] = round(w["qty"] / cur["qty"] * 100, 1) if cur["qty"] else 0
        w["per_day"] = round(w["qty"] / days, 1)
        w["sum"] = round(w["sum"])
        w["ours"] = _is_ours(w["model"], w["warehouse"], flags)
    # по дням: FBO / FBS (наш склад) / FBS (другие)
    labels, fbo, fbs_ours, fbs_other = [], [], [], []
    by_day: dict = {}
    for mp, day, model, wh, q, s, c in rows:
        if day >= d0.isoformat():
            b = by_day.setdefault(day, [0, 0, 0])
            if model == "FBO":
                b[0] += q
            elif _is_ours(model, wh, flags):
                b[1] += q
            else:
                b[2] += q
    d = d0
    while d <= d1:
        b = by_day.get(d.isoformat(), [0, 0, 0])
        labels.append(d.strftime("%d.%m")); fbo.append(b[0]); fbs_ours.append(b[1]); fbs_other.append(b[2])
        d += timedelta(days=1)
    by_mp = {}
    for mp, day, model, wh, q, s, c in rows:
        if day >= d0.isoformat():
            m = by_mp.setdefault(mp.upper(), {"qty": 0, "fbo": 0, "fbs": 0, "sum": 0.0})
            m["qty"] += q; m["sum"] += s; m["fbo" if model == "FBO" else "fbs"] += q
    for m in by_mp.values():
        m["sum"] = round(m["sum"])
    last = db.fetchall("SELECT MAX(day) FROM mp_orders_daily WHERE client = ?", (client,))
    notes = [f"{mp.upper()}: {err[2:]}" for mp, err in db.fetchall(
        "SELECT mp, last_error FROM mp_keys WHERE client = ?", (client,)) if (err or "").startswith("⚠")]
    return {"client": client, "days": days, "period": [d0.isoformat(), d1.isoformat()],
            "prev_period": [p0.isoformat(), p1.isoformat()], "cur": cur, "prev": prev,
            "warehouses": wh_list, "by_mp": by_mp,
            "daily": {"labels": labels, "fbo": fbo, "fbs_ours": fbs_ours, "fbs_other": fbs_other},
            "last_day": last[0][0] if last else None, "notes": notes}


def clients() -> list[str]:
    return [c for (c,) in db.fetchall("SELECT DISTINCT client FROM mp_keys ORDER BY client")]


# ── Диагностика «почему на WB нет остатка» (только чтение) ──────────────────

async def wb_stock_diag(key_id: str, q: str) -> dict:
    """По ключу клиента: карточка (nmID/баркод → chrtID), склады продавца и
    остаток товара на каждом, плюс остаток на складах WB (если есть «Статистика»).
    Ничего не меняет в кабинете — только GET/POST-методы чтения."""
    k = next((x for x in _keys_full() if x["id"] == key_id), None)
    if not k or k["mp"] != "wb":
        return {"error": "нет такого WB-ключа"}
    h = {"Authorization": k["secret"]}
    q = (q or "").strip()
    out: dict = {"client": k["client"], "q": q, "steps": []}

    def step(name, ok, info):
        out["steps"].append({"step": name, "ok": ok, "info": info})

    async with httpx.AsyncClient(timeout=60) as cl:
        # 1. карточка
        card = None
        r = await cl.post("https://content-api.wildberries.ru/content/v2/get/cards/list", headers=h,
                          json={"settings": {"cursor": {"limit": 100},
                                             "filter": {"textSearch": q, "withPhoto": -1}}})
        if r.is_success:
            cards = (r.json() or {}).get("cards") or []
            for c in cards:
                skus = [s for z in c.get("sizes") or [] for s in z.get("skus") or []]
                if q in (str(c.get("nmID")), c.get("vendorCode")) or q in skus:
                    card = c
                    break
            card = card or (cards[0] if cards else None)
            if card:
                step("карточка", True, {
                    "nmID": card.get("nmID"), "vendorCode": card.get("vendorCode"),
                    "title": card.get("title"), "subject": card.get("subjectName"),
                    "sizes": [{"chrtID": z.get("chrtID"), "skus": z.get("skus")} for z in card.get("sizes") or []],
                    "updatedAt": card.get("updatedAt")})
            else:
                step("карточка", False, "в контенте не найдена по " + q)
        else:
            step("карточка", False, f"content-api {r.status_code}: {r.text[:150]}")
        chrts = [z.get("chrtID") for z in (card or {}).get("sizes") or [] if z.get("chrtID")]
        # 2. склады продавца
        r = await cl.get(WB_MP + "/api/v3/warehouses", headers=h)
        whs = r.json() if r.is_success else []
        step("склады продавца", r.is_success, whs if r.is_success else f"{r.status_code}: {r.text[:150]}")
        # 3. остаток на каждом складе продавца
        if chrts and r.is_success:
            res = []
            for w in whs or []:
                rs = await cl.post(f"{WB_MP}/api/v3/stocks/{w['id']}", headers=h, json={"chrtIds": chrts})
                res.append({"warehouse": w.get("name"), "id": w.get("id"), "status": rs.status_code,
                            "stocks": (rs.json() or {}).get("stocks") if rs.is_success else rs.text[:150]})
                await asyncio.sleep(0.3)
            step("остатки на складах продавца (FBS)", True, res)
        # 4. остаток на складах WB (FBO)
        if card:
            r = await cl.get("https://statistics-api.wildberries.ru/api/v1/supplier/stocks", headers=h,
                             params={"dateFrom": "2020-01-01T00:00:00"})
            if r.is_success:
                rows = [x for x in r.json() or [] if x.get("nmId") == card.get("nmID")]
                step("остатки на складах WB (FBO)", True,
                     [{"warehouse": x.get("warehouseName"), "qty": x.get("quantity"),
                       "inWay": (x.get("inWayToClient") or 0) + (x.get("inWayFromClient") or 0)} for x in rows])
            else:
                step("остатки на складах WB (FBO)", False, f"{r.status_code}: {r.text[:120]}")
            # 5. цена (без цены товар на витрине не показывается)
            r = await cl.get("https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter",
                             headers=h, params={"limit": 10, "filterNmID": card.get("nmID")})
            if r.is_success:
                g = ((r.json() or {}).get("data") or {}).get("listGoods") or []
                step("цена", bool(g), g[0] if g else "цены нет")
            else:
                step("цена", False, f"{r.status_code}: {r.text[:120]}")
    return out


# ── Сверка остатков: МПФИТ (наш склад) ↔ склады продавца клиента в WB ───────

def _company_for(client: str) -> int | None:
    """Компания МПФИТ по ИНН или названию из подписи ключа."""
    import re as _re
    rows = db.fetchall("SELECT id, name, inn FROM m_companies")
    for inn in _re.findall(r"\d{10,12}", client or ""):
        for i, n, ci in rows:
            if (ci or "") == inn:
                return i
    low = (client or "").lower()
    for i, n, ci in rows:
        if n and (n.lower() in low or low in n.lower()):
            return i
    return None


async def wb_stock_recon(key_id: str, company_id: int | None = None) -> dict:
    """Все товары клиента: свободный остаток в МПФИТ против остатка, который
    видит WB на складах продавца (по chrtID). Только чтение с обеих сторон."""
    import mpfit_client as mp
    k = next((x for x in _keys_full() if x["id"] == key_id), None)
    if not k or k["mp"] != "wb":
        return {"error": "нет такого WB-ключа"}
    cid = company_id or await asyncio.to_thread(_company_for, k["client"])
    if not cid:
        return {"error": "не нашёл компанию МПФИТ для ключа — передайте company_id"}
    # МПФИТ
    prods = {p["id"]: p for p in await mp.paginate("/v1/products/list", flt={"company_id": cid},
                                                    limit=100, max_pages=200)}
    free: dict[int, int] = {}
    for s in await mp.paginate("/v1/products/stocks", flt={"company_id": cid}, limit=100, max_pages=200):
        free[s["product_id"]] = sum(int(x.get("free") or 0) for x in s.get("stocks") or [])
    h = {"Authorization": k["secret"]}
    async with httpx.AsyncClient(timeout=90) as cl:
        # WB: все карточки → баркод → (chrtID, nmID, артикул)
        by_bc: dict[str, dict] = {}
        cursor: dict = {"limit": 100}
        for _ in range(200):
            r = await cl.post("https://content-api.wildberries.ru/content/v2/get/cards/list", headers=h,
                              json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
            if r.status_code == 429:
                await asyncio.sleep(10); continue
            if not r.is_success:
                return {"error": f"WB content {r.status_code}: {r.text[:150]}"}
            d = r.json() or {}
            cards = d.get("cards") or []
            for c in cards:
                for z in c.get("sizes") or []:
                    for bc in z.get("skus") or []:
                        by_bc[str(bc)] = {"chrt": z.get("chrtID"), "nm": c.get("nmID"),
                                          "vc": c.get("vendorCode")}
            cur = d.get("cursor") or {}
            if len(cards) < 100 or not cur.get("nmID"):
                break
            cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
            await asyncio.sleep(0.7)
        r = await cl.get(WB_MP + "/api/v3/warehouses", headers=h)
        if not r.is_success:
            return {"error": f"WB warehouses {r.status_code}: {r.text[:150]}"}
        whs = r.json() or []
        chrts = sorted({v["chrt"] for v in by_bc.values() if v.get("chrt")})
        wb_st: dict[int, dict[str, int]] = {}      # chrt → {склад: остаток}
        for w in whs:
            for i in range(0, len(chrts), 1000):
                for _ in range(3):
                    rs = await cl.post(f"{WB_MP}/api/v3/stocks/{w['id']}", headers=h,
                                       json={"chrtIds": chrts[i:i + 1000]})
                    if rs.status_code != 429:
                        break
                    await asyncio.sleep(5)
                if rs.is_success:
                    for s in (rs.json() or {}).get("stocks") or []:
                        wb_st.setdefault(s["chrtId"], {})[w["name"]] = int(s.get("amount") or 0)
                await asyncio.sleep(0.25)
    ours = [w["name"] for w in whs if any(x in w["name"].lower() for x in OURS_HINTS)]
    rows = []
    for pid, p in prods.items():
        f = free.get(pid, 0)
        bcs = [str(b) for b in ((p.get("external_barcodes") or {}).get("wb") or [])] + \
              [str(b) for b in p.get("barcodes") or []]
        hit = next((by_bc[b] for b in bcs if b in by_bc), None)
        st = wb_st.get(hit["chrt"], {}) if hit else {}
        our_wb = sum(v for n, v in st.items() if n in ours)
        other = {n: v for n, v in st.items() if n not in ours and v}
        if not hit:
            status = "нет карточки WB с таким баркодом" if f else ""
        elif not any(n in ours for n in st):
            status = "остаток в WB не загружен" if f else ""
        elif our_wb != f:
            status = "расходится"
        else:
            status = "ок"
        if not f and not our_wb and not other and status in ("", "ок"):
            continue                                    # пусто с обеих сторон
        rows.append({"article": p.get("article"), "name": p.get("name"), "barcode": ", ".join(bcs[:2]),
                     "nm": hit and hit["nm"], "wb_vendor": hit and hit["vc"], "mpfit_free": f,
                     "wb_ours": our_wb if hit and any(n in ours for n in st) else None,
                     "wb_other": other, "diff": (our_wb - f) if hit else None, "status": status})
    order = {"нет карточки WB с таким баркодом": 0, "остаток в WB не загружен": 1, "расходится": 2, "ок": 3, "": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -(r["mpfit_free"] or 0)))
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"] or "—"] = summary.get(r["status"] or "—", 0) + 1
    return {"client": k["client"], "company_id": cid, "our_warehouses": ours,
            "all_warehouses": [w["name"] for w in whs], "summary": summary, "rows": rows,
            "at": (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M МСК")}
