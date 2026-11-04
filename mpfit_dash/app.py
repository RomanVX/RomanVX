"""Аналитический дашборд МПФИТ (фулфилмент Market Partners).

Отдельный лёгкий сервис Render: свой процесс и память, общая база
mp-postgres со своей схемой (DB_SCHEMA=mpfit). Пул соединений берём из
backend/db.py — тот же проверенный код, что у кабинетов Biomed/ФК.

Env: DATABASE_URL, DB_SCHEMA=mpfit, MPFIT_TOKEN (когда выдадут),
DASH_USER / DASH_PASSWORD (вход в дашборд, Basic Auth).
"""
import asyncio
import base64
import logging
import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import db  # noqa: E402  (backend/db.py: пул Postgres + схема)

import mpfit_client  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
import sync  # noqa: E402

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("mpfit_dash")
HERE = Path(__file__).resolve().parent
app = FastAPI(title="MPFIT Analytics")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

_PUBLIC = ("/health",)
# «Упаковка КИЗ» открыта без пароля (рабочее место упаковщика); финансы и
# остальной дашборд — по-прежнему под Basic Auth
_PUBLIC_PREFIX = ("/kiz", "/api/kiz/", "/static/kiz-parse.js", "/static/bwip-js-min.js")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Финансы внутри — без пароля не пускаем. Пока DASH_PASSWORD не задан,
    сервис закрыт целиком (кроме /health для проверок Render)."""
    if request.url.path in _PUBLIC or request.url.path.startswith(_PUBLIC_PREFIX):
        return await call_next(request)
    user = (os.environ.get("DASH_USER") or "admin").strip()
    pwd = (os.environ.get("DASH_PASSWORD") or "").strip()
    if not pwd:
        return Response("Задайте DASH_PASSWORD в настройках Render", status_code=503)
    auth = request.headers.get("authorization", "")
    ok = False
    if auth.lower().startswith("basic "):
        try:
            u, _, p = base64.b64decode(auth[6:]).decode().partition(":")
            ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, pwd)
        except Exception:
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="MPFIT"'})
    return await call_next(request)


_bg: set = set()


@app.on_event("startup")
async def _startup():
    await asyncio.to_thread(store.init)
    t = asyncio.create_task(sync.loop())
    _bg.add(t)


def _user(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    try:
        return base64.b64decode(auth[6:]).decode().partition(":")[0]
    except Exception:
        return ""


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/status")
async def status():
    """Состояние среды: база, схема, токен МПФИТ и живая проверка API."""
    out: dict = {"db": None, "schema": db.DB_SCHEMA or "public",
                 "mpfit_token": bool(mpfit_client.token()), "mpfit": None}
    try:
        await asyncio.to_thread(
            db.execute, "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        out["db"] = "postgres" if db.IS_PG else "sqlite"
    except Exception as e:
        out["db"] = f"ошибка: {str(e)[:200]}"
    if out["mpfit_token"]:
        try:
            out["mpfit"] = await mpfit_client.ping()
        except Exception as e:
            out["mpfit"] = {"ok": False, "error": str(e)[:300]}
    out["sync"] = {k: v for k, v in sync.state.items()}
    return JSONResponse(out)


async def _t(fn, *a):
    return await asyncio.to_thread(fn, *a)


@app.get("/api/summary")
async def api_summary():
    return {**await _t(report.summary), "sync": sync.state}


@app.get("/api/daily")
async def api_daily(days: int = 60):
    return await _t(report.daily, max(7, min(days, 365)))


@app.get("/api/speed")
async def api_speed(days: int = 30):
    return await _t(report.speed, max(7, min(days, 365)))


@app.get("/api/services")
async def api_services():
    return await _t(report.services)


@app.get("/api/clients")
async def api_clients():
    return await _t(report.clients)


@app.get("/api/finance")
async def api_finance(months: int = 6, ahead: int = 0):
    return await _t(report.finance, max(1, min(months, 24)), max(0, min(ahead, 6)))


@app.get("/api/planfact")
async def api_planfact():
    return await _t(report.plan_fact)


@app.get("/api/plan")
async def api_plan():
    return await _t(store.plan_get)


@app.post("/api/plan")
async def api_plan_set(payload: dict):
    month = str(payload.get("month") or "")[:7]
    for metric in ("orders", "revenue", "expenses", "profit"):
        if metric in payload:
            await _t(store.plan_set, month, metric, payload[metric])
    return {"ok": True}


@app.get("/api/ledger")
async def api_ledger():
    return {"rows": await _t(store.ledger_list), "kinds": store.KINDS, "categories": store.CATEGORIES}


def _check_entry(e: dict):
    from datetime import date
    date.fromisoformat(str(e.get("date"))[:10])
    if e.get("kind") not in store.KINDS:
        raise ValueError("kind")
    if float(e.get("amount")) <= 0:
        raise ValueError("amount")


@app.post("/api/ledger")
async def api_ledger_add(payload: dict, request: Request):
    try:
        _check_entry(payload)
    except Exception:
        return JSONResponse({"error": "проверьте дату, тип и сумму"}, status_code=400)
    return {"id": await _t(store.ledger_add, payload, _user(request))}


@app.put("/api/ledger/{entry_id}")
async def api_ledger_put(entry_id: str, payload: dict):
    try:
        _check_entry(payload)
    except Exception:
        return JSONResponse({"error": "проверьте дату, тип и сумму"}, status_code=400)
    await _t(store.ledger_update, entry_id, payload)
    return {"ok": True}


@app.delete("/api/ledger/{entry_id}")
async def api_ledger_del(entry_id: str):
    await _t(store.ledger_delete, entry_id)
    return {"ok": True}


@app.get("/api/invoices")
async def api_invoices():
    return await _t(report.invoices)


@app.post("/api/invoices/{number}/paid")
async def api_invoice_paid(number: str, payload: dict, request: Request):
    """Отметить оплату счёта: пишем «Оплата от клиента» в журнал со ссылкой на
    счёт. В МПФИТ ничего не меняем — только в своём учёте."""
    inv = {i["number"]: i for i in await _t(report.invoices)}
    i = inv.get(number)
    if not i:
        return JSONResponse({"error": "счёт не найден"}, status_code=404)
    amount = float(payload.get("amount") or i["debt"])
    if amount <= 0:
        return JSONResponse({"error": "по счёту нет долга"}, status_code=400)
    e = {"date": str(payload.get("date") or "")[:10], "kind": "client_payment", "category": "Прочее",
         "amount": amount, "method": payload.get("method") or "rs",
         "note": f"Оплата счёта МПФИТ {number} ({i['company']})", "invoice": number}
    try:
        _check_entry(e)
    except Exception:
        return JSONResponse({"error": "проверьте дату и сумму"}, status_code=400)
    return {"id": await _t(store.ledger_add, e, _user(request))}


@app.get("/api/recurring")
async def api_recurring():
    return await _t(store.recurring_list)


@app.post("/api/recurring")
async def api_recurring_save(payload: dict):
    from datetime import date
    try:
        date.fromisoformat(str(payload.get("start_date")))
        if payload.get("end_date"):
            date.fromisoformat(str(payload["end_date"]))
        assert float(payload.get("amount")) > 0 and payload.get("name")
    except Exception:
        return JSONResponse({"error": "название, сумма и дата начала обязательны"}, status_code=400)
    return {"id": await _t(store.recurring_save, payload)}


@app.delete("/api/recurring/{rid}")
async def api_recurring_del(rid: str):
    await _t(store.recurring_delete, rid)
    return {"ok": True}


@app.get("/api/staff")
async def api_staff(month: str = ""):
    from datetime import datetime, timedelta
    mk = month[:7] or (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m")
    return await _t(report.staff_month, mk)


@app.post("/api/staff")
async def api_staff_save(payload: dict):
    if not str(payload.get("name") or "").strip():
        return JSONResponse({"error": "имя"}, status_code=400)
    return {"id": await _t(store.staff_save, payload)}


@app.post("/api/shifts")
async def api_shift(payload: dict):
    from datetime import date
    d = date.fromisoformat(str(payload.get("date"))[:10]).isoformat()
    await _t(store.shift_set, d, str(payload.get("staff_id")), bool(payload.get("on")))
    return {"ok": True}


@app.post("/api/bank/import")
async def api_bank_import(request: Request):
    """Тело — xlsx-выписка Альфы (как скачана из банка)."""
    import bank
    data = await request.body()
    try:
        return await _t(bank.import_statement, data)
    except Exception as e:
        return JSONResponse({"error": "выписка не прочиталась: " + str(e)[:150]}, status_code=400)


@app.get("/api/bank")
async def api_bank():
    return {"tx": await _t(store.bank_list), "meta": store.kv_get("bank_meta")}


@app.post("/api/arrivals/create")
async def api_arrival_create(payload: dict):
    """Создать заявку на приёмку в МПФИТ (запись в боевую систему!).
    {date: ISO, items: [{product_id, quantity}], confirm: true}. Повтор того же
    набора товаров блокируется, чтобы не создать дубль (force: true — обойти)."""
    import hashlib
    import json as _json
    if payload.get("confirm") is not True:
        return JSONResponse({"error": "нужно confirm: true"}, status_code=400)
    items = [{"product_id": int(i["product_id"]), "quantity": int(i["quantity"])}
             for i in payload.get("items") or [] if int(i.get("quantity") or 0) > 0]
    if not items or not payload.get("date"):
        return JSONResponse({"error": "date и items обязательны"}, status_code=400)
    key = "arrival_" + hashlib.md5(_json.dumps(sorted(items, key=lambda x: x["product_id"])).encode()).hexdigest()[:12]
    if store.kv_get(key) and not payload.get("force"):
        return JSONResponse({"error": "такая заявка уже создана", "arrival": store.kv_get(key)}, status_code=409)
    try:
        res = await mpfit_client.call("POST", "/v1/arrivals/create", {"date": payload["date"], "items": items})
    except Exception as e:
        return JSONResponse({"error": str(e)[:400]}, status_code=502)
    store.kv_set(key, res.get("arrival") or res)
    return res


@app.post("/api/sync")
async def api_sync():
    t = asyncio.create_task(sync.run_once())
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return {"scheduled": True}


def _stock_snapshot(date: str | None) -> dict:
    dates = [r[0] for r in db.fetchall(
        "SELECT DISTINCT snap_date FROM m_stock_snap ORDER BY snap_date DESC")]
    day = date if date in dates else (dates[0] if dates else None)
    if not day:
        return {"date": None, "dates": [], "companies": []}
    names = dict(db.fetchall("SELECT id, name FROM m_companies"))
    by: dict = {}
    taken = ""
    for cid, art, name, bc, free, booked, ts in db.fetchall(
            "SELECT company_id, article, name, barcodes, free, booked, taken_at "
            "FROM m_stock_snap WHERE snap_date = ? ORDER BY article", (day,)):
        by.setdefault(cid, []).append({"article": art, "name": name, "barcodes": bc,
                                       "free": free, "booked": booked})
        taken = max(taken, ts or "")
    comps = [{"id": cid, "name": names.get(cid, str(cid)), "items": items,
              "units": sum(i["free"] + i["booked"] for i in items)}
             for cid, items in sorted(by.items(), key=lambda kv: names.get(kv[0], ""))]
    return {"date": day, "taken_at": taken, "dates": dates, "companies": comps}


@app.get("/api/stocks/snapshot")
async def api_stock_snapshot(date: str | None = None):
    """Последний снимок остатков за день (или за ?date=YYYY-MM-DD)."""
    return await _t(_stock_snapshot, date)


@app.post("/api/stocks/snapshot/refresh")
async def api_stock_snapshot_refresh():
    await sync.sync_stock_snapshot(force=True)
    return await _t(_stock_snapshot, None)


@app.get("/api/stocks/snapshot.xlsx")
async def api_stock_snapshot_xlsx(date: str | None = None):
    """Снимок остатков в Excel: лист на контрагента."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    snap = await _t(_stock_snapshot, date)
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append([f"Остатки ФФ на {snap['date'] or '—'} (снимок {snap.get('taken_at') or '—'} МСК)"])
    ws["A1"].font = Font(bold=True)
    for c in snap["companies"]:
        ws.append([c["name"], len(c["items"]), c["units"]])
    for c in snap["companies"]:
        sh = wb.create_sheet(c["name"][:31].replace("/", "-"))
        sh.append(["Артикул", "Наименование", "Штрихкод", "Свободно", "В брони", "Всего"])
        for cell in sh[1]:
            cell.font = Font(bold=True)
        for i in c["items"]:
            sh.append([i["article"], i["name"], i["barcodes"], i["free"], i["booked"],
                       i["free"] + i["booked"]])
        for col, w in zip("ABCDEF", (28, 60, 18, 10, 10, 10)):
            sh.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    from urllib.parse import quote
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f"attachment; filename*=UTF-8''{quote('Остатки_' + (snap['date'] or '') + '.xlsx')}"})


# ── Упаковка КИЗ: скан КИЗ + штрихкода → этикетка (DataMatrix, ШК, срок годности)

def _kiz_author(request: Request, payload: dict) -> str:
    return (_user(request) or str(payload.get("author") or "")).strip()[:40]


@app.get("/kiz")
async def kiz_page():
    return FileResponse(HERE / "static" / "kiz.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/kiz/lookup")
async def kiz_lookup(gtin: str = "", barcode: str = "", kiz: str = ""):
    """Товар (по ШК/GTIN из снимка остатков), сохранённый срок годности по GTIN
    и печатали ли уже этот КИЗ."""
    prod = await _t(store.kiz_product, barcode.strip(), gtin.strip())
    exp = await _t(store.kiz_expiry_get, gtin.strip()) if gtin else None
    printed = await _t(store.kiz_printed, kiz) if kiz else None
    return {"product": prod, "expiry": exp, "printed": printed}


@app.post("/api/kiz/expiry")
async def kiz_expiry_set(request: Request, payload: dict):
    gtin, expiry = str(payload.get("gtin") or ""), str(payload.get("expiry") or "")
    import re
    if not re.fullmatch(r"\d{14}", gtin) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry):
        return JSONResponse({"error": "нужны gtin (14 цифр) и expiry (ГГГГ-ММ-ДД)"}, status_code=400)
    await _t(store.kiz_expiry_set, gtin, expiry, str(payload.get("product") or ""), _kiz_author(request, payload))
    return {"ok": True}


@app.post("/api/kiz/log")
async def kiz_log_add(request: Request, payload: dict):
    if not payload.get("kiz"):
        return JSONResponse({"error": "нет КИЗ"}, status_code=400)
    return await _t(store.kiz_log_add, payload, _kiz_author(request, payload))


@app.get("/api/kiz/log")
async def kiz_log(d0: str | None = None, d1: str | None = None):
    from datetime import datetime, timedelta
    today = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")
    return {"rows": await _t(store.kiz_log_list, d0 or today, d1 or today)}


@app.get("/api/kiz/log.xlsx")
async def kiz_log_xlsx(d0: str, d1: str):
    import io
    from urllib.parse import quote
    from openpyxl import Workbook
    from openpyxl.styles import Font
    rows = await _t(store.kiz_log_list, d0, d1)
    wb = Workbook()
    ws = wb.active
    ws.title = "Упаковка КИЗ"
    head = ["Время (МСК)", "Контрагент", "Артикул", "Наименование", "Штрихкод", "GTIN",
            "Серийный номер", "Годен до", "Откуда срок", "Сотрудник", "КИЗ"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in reversed(rows):
        ws.append([r["ts"], r["company"], r["article"], r["name"], r["barcode"], r["gtin"],
                   r["serial"], r["expiry"], r["expiry_src"], r["author"],
                   (r["kiz"] or "").replace("\x1d", "<GS>")])
    for col, w in zip("ABCDEFGHIJK", (20, 22, 16, 44, 15, 16, 16, 12, 16, 12, 60)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f"attachment; filename*=UTF-8''{quote(f'Упаковка_КИЗ_{d0}_{d1}.xlsx')}"})


# Разрешены только методы чтения из спеки (list/GET). Создание заказов,
# приёмок, пополнение баланса и правка оплаты сюда не пропускаются.
_READ_POST = {"/v1/services/list", "/v1/products/list", "/v1/products/stocks",
              "/v1/arrivals/list", "/v1/orders/list", "/v1/orders/types/list",
              "/v1/companies/list", "/v1/products/categories/list",
              "/v1/invoices/list", "/v1/analytics/storage/list", "/v1/cim-codes"}


@app.post("/api/probe")
async def probe(payload: dict):
    """Прокси на чтение к API МПФИТ для разработки: {path, body} или
    {path} для GET /v1/orders/{id} и /v1/arrivals/{id}."""
    import re
    path = str(payload.get("path") or "")
    if re.fullmatch(r"/v1/(orders|arrivals)/\d+", path):
        return await mpfit_client.call("GET", path)
    if path not in _READ_POST:
        return JSONResponse({"error": "метод не разрешён (только чтение)"}, status_code=400)
    return await mpfit_client.call("POST", path, payload.get("body") or {"limit": 10, "last_id": 0})


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html",
                        headers={"Cache-Control": "no-cache"})
