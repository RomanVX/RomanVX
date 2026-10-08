"""Хранилище дашборда МПФИТ (схема mpfit в mp-postgres).

Данные МПФИТ копим у себя: история не зависит от API, а отчёты строятся
запросами к своей базе. Все даты — ISO-строки в UTC, кроме полей *_msk.

Таблицы:
  m_companies  — компании (ФФ и селлеры)
  m_orders     — заказы на отгрузку + итог проверки услуг
  m_status_log — первое появление заказа в каждом статусе (скорость по этапам)
  m_invoices   — счета МПФИТ: выставлено / оплачено (это и есть выручка)
  ledger       — ручной журнал: расходы, вложения, поступления (из чата «Финансы ФФ»)
  plan         — план по месяцам: метрика × месяц
  kv           — служебные курсоры синхронизации
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "backend"))  # backend/db.py
import json
import uuid

import db

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)",
    """CREATE TABLE IF NOT EXISTS m_companies (
        id INTEGER PRIMARY KEY, name TEXT, type TEXT, inn TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS m_orders (
        id BIGINT PRIMARY KEY, number TEXT, company_id INTEGER, source TEXT,
        status TEXT, created_at TEXT, plan_at TEXT, shipped_at TEXT,
        units INTEGER, updated_at TEXT,
        svc_checked INTEGER DEFAULT 0, svc_revenue REAL DEFAULT 0,
        svc_cost REAL DEFAULT 0, svc_missing INTEGER DEFAULT 0,
        svc_alerted INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS m_orders_created ON m_orders (created_at)",
    "CREATE INDEX IF NOT EXISTS m_orders_status ON m_orders (status)",
    """CREATE TABLE IF NOT EXISTS m_status_log (
        order_id BIGINT, status TEXT, seen_at TEXT, PRIMARY KEY (order_id, status))""",
    """CREATE TABLE IF NOT EXISTS m_invoices (
        id BIGINT PRIMARY KEY, number TEXT, company_id INTEGER, status TEXT,
        date TEXT, total REAL, paid REAL, ops TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS ledger (
        id TEXT PRIMARY KEY, date TEXT, kind TEXT, category TEXT, amount REAL,
        method TEXT, note TEXT, in_ff INTEGER DEFAULT 1, author TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS plan (
        month TEXT, metric TEXT, value REAL, PRIMARY KEY (month, metric))""",
    # постоянные платежи: начисляются в расходы каждого месяца по дням действия,
    # а сама оплата в журнале (с recurring_id) идёт только в движение денег
    """CREATE TABLE IF NOT EXISTS recurring (
        id TEXT PRIMARY KEY, name TEXT, category TEXT, amount REAL,
        start_date TEXT, end_date TEXT, note TEXT)""",
    # сотрудники склада: оплата = выход за смену + сдельно за единицу отгрузки FBS
    """CREATE TABLE IF NOT EXISTS staff (
        id TEXT PRIMARY KEY, name TEXT, shift_rate REAL, unit_rate REAL,
        active INTEGER DEFAULT 1, since TEXT)""",
    # табель: кто вышел в смену. МПФИТ по API не отдаёт, кто собрал заказ,
    # поэтому отгрузки дня делим поровну между вышедшими
    """CREATE TABLE IF NOT EXISTS shifts (
        date TEXT, staff_id TEXT, PRIMARY KEY (date, staff_id))""",
    # выписка Альфы: сумма со знаком (+ приход, − списание)
    """CREATE TABLE IF NOT EXISTS bank_tx (
        id TEXT PRIMARY KEY, date TEXT, amount REAL, party TEXT, inn TEXT,
        purpose TEXT, kind TEXT, category TEXT)""",
    # хранение по дням (analytics/storage/list) — входит в «К оплате» МПФИТ
    """CREATE TABLE IF NOT EXISTS m_storage (
        id BIGINT PRIMARY KEY, company_id INTEGER, date TEXT, amount REAL)""",
]

# налог к удержанию с выручки (начисляется расходом месяца)
TAX_RATE = 0.07

# колонки, добавленные после первой версии (ALTER без IF NOT EXISTS — для SQLite)
_ADD_COLUMNS = [("ledger", "invoice", "TEXT"), ("ledger", "recurring_id", "TEXT"),
                ("m_invoices", "created_ts", "TEXT"), ("ledger", "staff_id", "TEXT")]

# статусы, после которых «Отгрузка FBS» уже должна стоять
SHIPPED = ("SHIPPED", "DELIVERY", "COMPLETE")
OPEN = ("NEW", "PRODUCTS_RESERVED", "EQUIPMENT", "READY_TO_SHIP", "SHIPPED", "DELIVERY")

KINDS = {"expense": "Расход", "investment": "Вложение", "funding": "Пополнение бюджета",
         "client_payment": "Оплата от клиента", "other_income": "Прочий доход"}
CATEGORIES = ["Аренда", "ФОТ", "Оборудование", "Расходники и упаковка", "Логистика",
              "ПО и сервисы", "Связь", "Хозяйственные", "Налоги", "Прочее"]


def init():
    for sql in SCHEMA:
        db.execute(sql)
    for table, col, typ in _ADD_COLUMNS:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except Exception:
            pass    # колонка уже есть
    seed_ledger()
    seed_recurring()
    fix_v2()
    seed_staff()


def fix_v2():
    """Уточнения владельца 28.09: 5 расходов от 25.09 — с РС; первый месяц
    МПФИТ бесплатный (30.08–29.09), платная подписка 24 990 ₽ — с 30.09."""
    if kv_get("fix_v2"):
        return
    for i in range(18, 23):
        db.execute("UPDATE ledger SET method = 'rs', note = REPLACE(note, ' (способ оплаты не указан)', '') "
                   "WHERE id = ?", (f"seed-{i:03d}",))
    db.execute("UPDATE recurring SET start_date = '2026-09-30', note = ? WHERE id = 'rec-mpfit'",
               ("Первый месяц (30.08–29.09) бесплатно, далее 24 990 ₽/мес",))
    db.execute("UPDATE recurring SET note = ? WHERE id = 'rec-rent'",
               ("Из счёта № 9325: 61 456,27 ₽ за 11 дней сентября, ставка подтверждена",))
    kv_set("fix_v2", True)


def kv_get(k, default=None):
    row = db.fetchone("SELECT v FROM kv WHERE k = ?", (k,))
    return json.loads(row[0]) if row and row[0] else default


def kv_set(k, v):
    db.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
               (k, json.dumps(v, ensure_ascii=False)))


# ── журнал: стартовые записи из чата «Финансы ФФ» (30.08–25.09.2026) ─────────
# Поступления на РС (39 545 и 208 290) — это оплаты счетов МПФИТ: в выручку
# их не пишем (выручка идёт из счетов), держим как client_payment для остатков.
_SEED = [
    ("2026-08-30", "funding", "Прочее", 43850, "cash", "Поступление из бюджета под отчёт", 1, "Roman V"),
    ("2026-08-30", "investment", "Оборудование", 7500, "personal", "Сканер (оплатил Андрей лично)", 1, "Андрей"),
    ("2026-08-30", "expense", "ПО и сервисы", 24990, "cash", "МПФИТ (наличные, завели на РС и оплатили)", 1, "Roman V"),
    ("2026-08-30", "expense", "Хозяйственные", 1250, "cash", "Ключи", 1, "Roman V"),
    ("2026-08-30", "expense", "Хозяйственные", 300, "cash", "Подушка на кресло", 1, "Roman V"),
    ("2026-09-01", "client_payment", "Прочее", 39545, "rs", "Оплата счёта МПФИТ (Субоч Е.П., 31.08)", 1, "Roman V"),
    ("2026-09-04", "expense", "Оборудование", 4500, "cash", "Тележка", 1, "Roman V"),
    ("2026-09-04", "expense", "Оборудование", 910, "cash", "Мышь + коврик", 1, "Roman V"),
    ("2026-09-06", "expense", "Оборудование", 3070, "cash", "Весы и USB-хаб", 1, "Roman V"),
    ("2026-09-07", "expense", "ПО и сервисы", 710, "cash", "Сервер VDS для сайта", 1, "Roman V"),
    ("2026-09-08", "expense", "Связь", 500, "cash", "Оформление номера", 1, "Roman V"),
    ("2026-09-08", "expense", "Хозяйственные", 950, "cash", "Болты, ножки", 1, "Андрей"),
    ("2026-09-20", "expense", "Расходники и упаковка", 380, "cash", "Резинки", 1, "Roman V"),
    ("2026-09-20", "expense", "Расходники и упаковка", 350, "cash", "Скотч", 1, "Roman V"),
    ("2026-09-20", "expense", "Логистика", 350, "cash", "Транспортировка", 1, "Roman V"),
    ("2026-09-20", "expense", "ПО и сервисы", 2386, "cash", "Токены ИИ (не вложения в ФФ)", 0, "Roman V"),
    ("2026-09-20", "client_payment", "Прочее", 208290, "rs", "Оплата счетов МПФИТ (6 счетов 06–15.09)", 1, "Roman V"),
    ("2026-09-20", "expense", "Аренда", 61456.27, "rs", "Аренда помещения за 11 дней сентября, счёт № 9325 Чеховский Печатный Двор", 1, "Андрей"),
    ("2026-09-25", "expense", "ФОТ", 10000, "", "ЗП Володя (способ оплаты не указан)", 1, "Саша К"),
    ("2026-09-25", "expense", "Логистика", 4400, "", "Доставка (способ оплаты не указан)", 1, "Андрей"),
    ("2026-09-25", "expense", "Прочее", 10000, "", "Сертификат, вручён (способ оплаты не указан)", 1, "Андрей"),
    ("2026-09-25", "expense", "Расходники и упаковка", 13090, "", "Коробки (способ оплаты не указан)", 1, "Андрей"),
    ("2026-09-25", "expense", "Оборудование", 28650, "", "Камеры (способ оплаты не указан)", 1, "Андрей"),
]


def seed_ledger():
    if kv_get("ledger_seeded"):
        return
    rows = [(f"seed-{i:03d}", d, k, c, a, m, n, ff, au, "2026-09-28T00:00:00")
            for i, (d, k, c, a, m, n, ff, au) in enumerate(_SEED)]
    db.executemany(
        "INSERT INTO ledger (id, date, kind, category, amount, method, note, in_ff, author, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING", rows)
    kv_set("ledger_seeded", True)


def ledger_add(e: dict, author: str) -> str:
    from datetime import datetime
    i = str(uuid.uuid4())
    db.execute(
        "INSERT INTO ledger (id, date, kind, category, amount, method, note, in_ff, author, created_at, "
        "invoice, recurring_id, staff_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (i, e["date"], e["kind"], e.get("category") or "Прочее", float(e["amount"]),
         e.get("method") or "", e.get("note") or "", 1 if e.get("in_ff", True) else 0,
         author, datetime.utcnow().isoformat(timespec="seconds"),
         e.get("invoice") or None, e.get("recurring_id") or None, e.get("staff_id") or None))
    return i


def ledger_update(i: str, e: dict):
    db.execute("UPDATE ledger SET date=?, kind=?, category=?, amount=?, method=?, note=?, in_ff=?, "
               "recurring_id=? WHERE id=?",
               (e["date"], e["kind"], e.get("category") or "Прочее", float(e["amount"]),
                e.get("method") or "", e.get("note") or "", 1 if e.get("in_ff", True) else 0,
                e.get("recurring_id") or None, i))


def ledger_delete(i: str):
    db.execute("DELETE FROM ledger WHERE id = ?", (i,))


def ledger_list() -> list[dict]:
    rows = db.fetchall("SELECT id, date, kind, category, amount, method, note, in_ff, author, "
                       "invoice, recurring_id, staff_id FROM ledger ORDER BY date DESC, created_at DESC")
    keys = ("id", "date", "kind", "category", "amount", "method", "note", "in_ff", "author",
            "invoice", "recurring_id", "staff_id")
    return [dict(zip(keys, r)) for r in rows]


def plan_get() -> dict:
    out: dict = {}
    for m, metric, v in db.fetchall("SELECT month, metric, value FROM plan"):
        out.setdefault(m, {})[metric] = v
    return out


def plan_set(month: str, metric: str, value):
    if value in (None, ""):
        db.execute("DELETE FROM plan WHERE month = ? AND metric = ?", (month, metric))
    else:
        db.execute("INSERT INTO plan (month, metric, value) VALUES (?,?,?) "
                   "ON CONFLICT (month, metric) DO UPDATE SET value = excluded.value",
                   (month, metric, float(value)))


# ── постоянные платежи ────────────────────────────────────────────────────────
# Аренда: счёт № 9325 — 61 456,27 ₽ за 11 дней сентября (с 20.09) →
# 61 456,27 / 11 × 30 = 167 608 ₽ в месяц. МПФИТ: 24 990 ₽ оплачено 30.08,
# принято помесячно — период подписки уточнить у владельца.
_REC_SEED = [
    ("rec-rent", "Аренда склада, Чехов (Чеховский Печатный Двор)", "Аренда", 167608.01,
     "2026-09-20", None, "Из счёта № 9325: 61 456,27 ₽ за 11 дней сентября. Проверить ставку по договору аренды № 232",
     "seed-017"),
    ("rec-mpfit", "МПФИТ, подписка WMS", "ПО и сервисы", 24990,
     "2026-09-30", None, "Первый месяц (30.08–29.09) бесплатно, далее 24 990 ₽/мес",
     "seed-002"),
]


def seed_recurring():
    if kv_get("recurring_seeded"):
        return
    for rid, name, cat, amt, start, end, note, led in _REC_SEED:
        db.execute("INSERT INTO recurring (id, name, category, amount, start_date, end_date, note) "
                   "VALUES (?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
                   (rid, name, cat, amt, start, end, note))
        db.execute("UPDATE ledger SET recurring_id = ? WHERE id = ?", (rid, led))
    kv_set("recurring_seeded", True)


def recurring_list() -> list[dict]:
    keys = ("id", "name", "category", "amount", "start_date", "end_date", "note")
    return [dict(zip(keys, r)) for r in db.fetchall(
        f"SELECT {', '.join(keys)} FROM recurring ORDER BY start_date")]


def recurring_save(e: dict) -> str:
    i = e.get("id") or f"rec-{uuid.uuid4().hex[:8]}"
    db.execute("INSERT INTO recurring (id, name, category, amount, start_date, end_date, note) "
               "VALUES (?,?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET name = excluded.name, "
               "category = excluded.category, amount = excluded.amount, start_date = excluded.start_date, "
               "end_date = excluded.end_date, note = excluded.note",
               (i, e["name"], e.get("category") or "Прочее", float(e["amount"]),
                e["start_date"], e.get("end_date") or None, e.get("note") or ""))
    return i


def recurring_delete(i: str):
    db.execute("DELETE FROM recurring WHERE id = ?", (i,))
    db.execute("UPDATE ledger SET recurring_id = NULL WHERE recurring_id = ?", (i,))


# ── сотрудники ────────────────────────────────────────────────────────────────
def seed_staff():
    if kv_get("staff_seeded"):
        return
    for sid, name in (("st-vladimir", "Владимир"), ("st-ekaterina", "Екатерина")):
        db.execute("INSERT INTO staff (id, name, shift_rate, unit_rate, active, since) VALUES (?,?,?,?,1,?) "
                   "ON CONFLICT (id) DO NOTHING", (sid, name, 1000, 3, "2026-10-01"))
    kv_set("staff_seeded", True)


def staff_list() -> list[dict]:
    keys = ("id", "name", "shift_rate", "unit_rate", "active", "since")
    return [dict(zip(keys, r)) for r in db.fetchall(
        f"SELECT {', '.join(keys)} FROM staff ORDER BY name")]


def staff_save(e: dict) -> str:
    i = e.get("id") or f"st-{uuid.uuid4().hex[:8]}"
    db.execute("INSERT INTO staff (id, name, shift_rate, unit_rate, active, since) VALUES (?,?,?,?,?,?) "
               "ON CONFLICT (id) DO UPDATE SET name = excluded.name, shift_rate = excluded.shift_rate, "
               "unit_rate = excluded.unit_rate, active = excluded.active, since = excluded.since",
               (i, e["name"], float(e.get("shift_rate") or 0), float(e.get("unit_rate") or 0),
                1 if e.get("active", True) else 0, e.get("since") or None))
    return i


def shifts_get(d0: str, d1: str) -> list[tuple]:
    return db.fetchall("SELECT date, staff_id FROM shifts WHERE date BETWEEN ? AND ?", (d0, d1))


def shift_set(d: str, staff_id: str, on: bool):
    if on:
        db.execute("INSERT INTO shifts (date, staff_id) VALUES (?,?) ON CONFLICT (date, staff_id) DO NOTHING",
                   (d, staff_id))
    else:
        db.execute("DELETE FROM shifts WHERE date = ? AND staff_id = ?", (d, staff_id))


# ── банковская выписка ────────────────────────────────────────────────────────
def bank_upsert(rows: list[tuple]):
    db.executemany(
        "INSERT INTO bank_tx (id, date, amount, party, inn, purpose, kind, category) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT (id) DO UPDATE SET kind = excluded.kind, category = excluded.category", rows)


def bank_list() -> list[dict]:
    keys = ("id", "date", "amount", "party", "inn", "purpose", "kind", "category")
    return [dict(zip(keys, r)) for r in db.fetchall(
        f"SELECT {', '.join(keys)} FROM bank_tx ORDER BY date, id")]


def bank_balance() -> float:
    m = kv_get("bank_meta") or {}
    if m.get("opening") is None:
        return 0.0
    r = db.fetchone("SELECT COALESCE(SUM(amount), 0) FROM bank_tx")
    return float(m["opening"]) + float(r[0] or 0)
