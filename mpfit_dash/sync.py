"""Фоновая синхронизация МПФИТ → своя база.

Каждый проход:
  1. компании;
  2. новые заказы по курсору last_id + перепроверка открытых заказов по ids;
     смена статуса пишется в m_status_log (время этапов считаем сами —
     в API у заказа нет отметок времени по этапам);
  3. услуги: у отгруженных заказов открываем карточку и проверяем, стоит ли
     «Отгрузка FBS». Её сотрудник прожимает галочкой на экране отгрузки;
     нет услуги у отгруженного заказа → деньги не выставятся в счёт;
  4. счета (выручка: выставлено/оплачено).
Лимит API 120 запросов в минуту — карточки заказов проверяем порциями.
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "backend"))  # backend/db.py
import asyncio
import logging
import os
from datetime import datetime, timedelta

import db
import mpfit_client as mp
import store

_log = logging.getLogger("mpfit_sync")
state = {"last_run": "", "error": "", "orders": 0, "svc_backlog": 0, "running": False}

SVC_BATCH = 100            # карточек за проход (≈ минута лимита API)
RECHECK_DAYS = 3           # сколько дней перепроверять заказ без услуги


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _utc(s: str | None) -> str | None:
    if not s:
        return None
    return s.replace("Z", "").replace(".000000", "")[:19]


def _msk_to_utc(s: str | None) -> str | None:
    """shipment_date_actual приходит строкой «26.08.2026 18:34» по Москве."""
    if not s:
        return None
    try:
        return (datetime.strptime(s, "%d.%m.%Y %H:%M") - timedelta(hours=3)).isoformat(timespec="seconds")
    except ValueError:
        return None


def _row(o: dict) -> tuple:
    units = sum(int(it.get("quantity") or 0) for it in o.get("items") or [])
    return (o["id"], o.get("number"), o.get("company_id"), o.get("source"), o.get("status"),
            _utc(o.get("created_at")), _utc(o.get("shipment_date")),
            _msk_to_utc(o.get("shipment_date_actual")), units, _utc(o.get("updated_at")))


_UPSERT = (
    "INSERT INTO m_orders (id, number, company_id, source, status, created_at, plan_at, "
    "shipped_at, units, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT (id) DO UPDATE SET status = excluded.status, plan_at = excluded.plan_at, "
    "shipped_at = excluded.shipped_at, units = excluded.units, updated_at = excluded.updated_at")


def _save_orders(orders: list[dict], log_status: bool):
    if not orders:
        return
    prev = {}
    if log_status:
        ids = [o["id"] for o in orders]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            prev.update(dict(db.fetchall(f"SELECT id, status FROM m_orders WHERE id IN ({q})", chunk)))
    db.executemany(_UPSERT, [_row(o) for o in orders])
    if log_status:
        now = _now()
        changed = [(o["id"], o["status"], now) for o in orders if prev.get(o["id"]) != o.get("status")]
        db.executemany("INSERT INTO m_status_log (order_id, status, seen_at) VALUES (?,?,?) "
                       "ON CONFLICT (order_id, status) DO NOTHING", changed)


async def sync_companies():
    rows = await mp.paginate("/v1/companies/list", limit=100, max_pages=10)
    db.executemany(
        "INSERT INTO m_companies (id, name, type, inn, created_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT (id) DO UPDATE SET name = excluded.name, type = excluded.type",
        [(c["id"], c.get("name"), c.get("type"), c.get("inn"), _utc(c.get("created_at"))) for c in rows])


async def sync_orders():
    first = store.kv_get("orders_cursor") is None
    cursor = store.kv_get("orders_cursor", 0) or 0
    # 1) новые заказы по курсору (id растут)
    for _ in range(200):
        res = (await mp.call("POST", "/v1/orders/list",
                             {"limit": 200, "last_id": cursor})).get("result") or {}
        data = res.get("data") or []
        _save_orders(data, log_status=not first)
        if not data or res.get("last_id") is None:
            break
        cursor = res["last_id"]
        store.kv_set("orders_cursor", cursor)
    # 2) открытые заказы — перепроверяем статус
    open_ids = [r[0] for r in db.fetchall(
        f"SELECT id FROM m_orders WHERE status IN ({','.join('?' * len(store.OPEN))}) "
        "AND created_at > ?", (*store.OPEN, (datetime.utcnow() - timedelta(days=20)).isoformat()))]
    for i in range(0, len(open_ids), 200):
        res = (await mp.call("POST", "/v1/orders/list",
                             {"limit": 200, "last_id": 0,
                              "filter": {"ids": open_ids[i:i + 200]}})).get("result") or {}
        _save_orders(res.get("data") or [], log_status=True)
    state["orders"] = db.fetchone("SELECT COUNT(*) FROM m_orders")[0]


def _services(detail: dict) -> tuple[float, float, bool]:
    """(выручка ₽, себестоимость ₽, нет ли «Отгрузки FBS» хотя бы у одного товара)."""
    rev = cost = 0.0
    missing = False
    for it in detail.get("items") or []:
        svcs = it.get("order_item_services") or []
        if not any("отгрузка" in ((s.get("service") or {}).get("name") or "").lower() for s in svcs):
            missing = True
        for s in svcs:
            rev += (s.get("price") or 0) / 100
            cost += (s.get("cost_price") or 0) / 100
    for s in detail.get("order_services") or []:
        n = int(s.get("count") or 1)
        rev += (s.get("price") or 0) / 100 * n
        cost += (s.get("cost_price") or 0) / 100 * n
    return rev, cost, missing


async def check_services():
    """Карточки отгруженных заказов: сначала непроверенные (свежие вперёд),
    затем перепроверка тех, где услуги не было, — вдруг дожали."""
    ph = ",".join("?" * len(store.SHIPPED))
    todo = [r[0] for r in db.fetchall(
        f"SELECT id FROM m_orders WHERE status IN ({ph}) AND svc_checked = 0 "
        "ORDER BY created_at DESC LIMIT ?", (*store.SHIPPED, SVC_BATCH))]
    recheck_from = (datetime.utcnow() - timedelta(days=RECHECK_DAYS)).isoformat()
    left = SVC_BATCH - len(todo)
    if left > 0:
        todo += [r[0] for r in db.fetchall(
            "SELECT id FROM m_orders WHERE svc_missing = 1 AND shipped_at > ? LIMIT ?",
            (recheck_from, min(left, 30)))]
    for oid in todo:
        try:
            det = (await mp.call("GET", f"/v1/orders/{oid}")).get("result") or {}
        except Exception as e:
            _log.warning("order %s: %s", oid, str(e)[:120])
            continue
        rev, cost, missing = _services(det)
        db.execute("UPDATE m_orders SET svc_checked = 1, svc_revenue = ?, svc_cost = ?, "
                   "svc_missing = ? WHERE id = ?", (rev, cost, 1 if missing else 0, oid))
        await asyncio.sleep(0.4)
    state["svc_backlog"] = db.fetchone(
        f"SELECT COUNT(*) FROM m_orders WHERE status IN ({ph}) AND svc_checked = 0", store.SHIPPED)[0]


async def sync_invoices():
    rows = await mp.paginate("/v1/invoices/list", limit=200, max_pages=50)
    import json
    db.executemany(
        "INSERT INTO m_invoices (id, number, company_id, status, date, total, paid, ops, updated_at, created_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET status = excluded.status, "
        "total = excluded.total, paid = excluded.paid, ops = excluded.ops, updated_at = excluded.updated_at, "
        "created_ts = excluded.created_ts",
        [(i["id"], i.get("number"), i.get("company_id"), i.get("status"),
          (i.get("date_of_creation") or i.get("created_at") or "")[:10],
          (i.get("total") or 0) / 100, (i.get("total_paid") or 0) / 100,
          json.dumps([{"name": o.get("name"), "qty": o.get("quantity"), "price": (o.get("price") or 0) / 100,
                       "total": (o.get("total") or 0) / 100} for o in i.get("operations") or []],
                     ensure_ascii=False),
          _utc(i.get("updated_at")), _utc(i.get("created_at"))) for i in rows])


async def alert_missing():
    """Telegram: отгруженные сегодня-вчера заказы без «Отгрузки FBS».
    Нужны env TG_BOT_TOKEN и TG_CHAT_ID; без них просто пропускаем."""
    tok, chat = os.environ.get("TG_BOT_TOKEN", ""), os.environ.get("TG_CHAT_ID", "")
    if not tok or not chat:
        return
    since = (datetime.utcnow() - timedelta(days=2)).isoformat()
    rows = db.fetchall(
        "SELECT o.id, o.number, c.name FROM m_orders o LEFT JOIN m_companies c ON c.id = o.company_id "
        "WHERE o.svc_missing = 1 AND o.svc_alerted = 0 AND o.shipped_at > ? ORDER BY o.shipped_at", (since,))
    if not rows:
        return
    lines = [f"⚠️ Не прожата «Отгрузка FBS»: {len(rows)} заказ(ов)"]
    lines += [f"• №{n} — {c or '—'}" for _, n, c in rows[:30]]
    if len(rows) > 30:
        lines.append(f"… и ещё {len(rows) - 30}")
    lines.append("Проставьте услугу в МПФИТ, иначе она не попадёт в счёт.")
    import httpx
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": "\n".join(lines)})
    if r.is_success:
        ids = [r_[0] for r_ in rows]
        for i in range(0, len(ids), 500):
            ch = ids[i:i + 500]
            db.execute(f"UPDATE m_orders SET svc_alerted = 1 WHERE id IN ({','.join('?' * len(ch))})", ch)


async def run_once():
    if state["running"] or not mp.token():
        return
    state["running"] = True
    try:
        await sync_companies()
        await sync_orders()
        await sync_invoices()
        await check_services()
        await alert_missing()
        state["error"] = ""
    except Exception as e:
        state["error"] = str(e)[:300]
        _log.warning("sync: %s", e)
    finally:
        state["running"] = False
        state["last_run"] = (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m %H:%M")


async def loop():
    await asyncio.sleep(5)
    while True:
        await run_once()
        # пока догоняем историю услуг — чаще, потом раз в 5 минут
        await asyncio.sleep(90 if state.get("svc_backlog") else 300)
