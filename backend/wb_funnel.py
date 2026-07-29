"""Воронка продаж WB (nm-report, seller-analytics-api, нужна подписка Джем).

Показы карточки → корзина → заказ → выкуп по каждому артикулу и дню.
Отчёт асинхронный: создаём задание (DETAIL_HISTORY_REPORT), ждём SUCCESS,
скачиваем ZIP с CSV и складываем строки в вечную таблицу wb_funnel —
история не ограничена окном WB. Лимит методов — 3 запроса/мин.
"""
import asyncio
import csv
import io
import logging
import uuid
import zipfile
from datetime import datetime, timedelta

import db

_log = logging.getLogger("wb_funnel")

_lock = asyncio.Lock()


def _init():
    db.execute("""CREATE TABLE IF NOT EXISTS wb_funnel (
        dt TEXT, nm TEXT, sku TEXT,
        opens INTEGER, carts INTEGER, orders_cnt INTEGER, orders_rub REAL,
        buyouts INTEGER, buyouts_rub REAL, cancels INTEGER, wishlist INTEGER,
        PRIMARY KEY (dt, nm))""")


async def fetch(days: int = 28) -> dict:
    """Собирает воронку за последние `days` дней и upsert-ит в wb_funnel."""
    import wb_client
    _init()
    dt_to = (datetime.utcnow() + timedelta(hours=3)).date() - timedelta(days=1)
    dt_from = dt_to - timedelta(days=days - 1)
    rid = str(uuid.uuid4())
    async with _lock:
        st, resp = await wb_client.analytics_post(
            "/api/v2/nm-report/downloads",
            {"id": rid, "reportType": "DETAIL_HISTORY_REPORT",
             "userReportName": "dashboard funnel",
             "params": {"startDate": dt_from.strftime("%Y-%m-%d"),
                        "endDate": dt_to.strftime("%Y-%m-%d"),
                        "timezone": "Europe/Moscow",
                        "aggregationLevel": "day",
                        "skipDeletedNm": True}})
        if st != 200:
            return {"error": f"создание отчёта → {st}: {str(resp)[:200]}"}
        # статусы: WAITING → PROCESSING → SUCCESS / FAILED; лимит 3 req/мин
        for _ in range(20):
            await asyncio.sleep(25)
            try:
                data = await wb_client._get(
                    f"{wb_client.ANALYTICS_BASE}/api/v2/nm-report/downloads",
                    {"filter[downloadIds]": rid})
            except Exception as e:
                _log.warning("funnel status: %s", str(e)[:150])
                continue
            rows = (data or {}).get("data") or []
            status = rows[0].get("status") if rows else ""
            if status == "SUCCESS":
                break
            if status == "FAILED":
                return {"error": "WB не смог сгенерировать отчёт (FAILED)"}
        else:
            return {"error": "отчёт не собрался за 8 минут — попробуй позже"}
        resp = await wb_client._http().get(
            f"{wb_client.ANALYTICS_BASE}/api/v2/nm-report/downloads/file/{rid}",
            headers=wb_client._headers())
        if not resp.is_success:
            return {"error": f"скачивание → {resp.status_code}: {resp.text[:200]}"}
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        raw = zf.read(zf.namelist()[0]).decode("utf-8-sig")
    except Exception as e:
        return {"error": f"не смог распаковать отчёт: {str(e)[:200]}"}

    try:
        import catalog as _cat
        nm_to_art = {str(k): v for k, v in
                     getattr(_cat, "WB_ID_TO_ART", {}).items()}
    except Exception:
        nm_to_art = {}

    def _i(v):
        try:
            return int(float(str(v).replace(",", ".")))
        except (TypeError, ValueError):
            return 0

    reader = csv.DictReader(io.StringIO(raw))
    rows_out = []
    for r in reader:
        nm = str(r.get("nmID") or "").strip()
        dt = str(r.get("dt") or "")[:10]
        if not nm or not dt:
            continue
        rows_out.append((
            dt, nm, nm_to_art.get(nm, ""),
            _i(r.get("openCardCount")), _i(r.get("addToCartCount")),
            _i(r.get("ordersCount")), _i(r.get("ordersSumRub")),
            _i(r.get("buyoutsCount")), _i(r.get("buyoutsSumRub")),
            _i(r.get("cancelCount")), _i(r.get("addToWishlist"))))
    if not rows_out:
        return {"error": "отчёт пуст — проверь подписку Джем",
                "csv_head": raw[:300]}

    def _save():
        _init()
        db.executemany(
            "INSERT INTO wb_funnel (dt, nm, sku, opens, carts, orders_cnt, "
            "orders_rub, buyouts, buyouts_rub, cancels, wishlist) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (dt, nm) DO UPDATE SET sku=excluded.sku, "
            "opens=excluded.opens, carts=excluded.carts, "
            "orders_cnt=excluded.orders_cnt, orders_rub=excluded.orders_rub, "
            "buyouts=excluded.buyouts, buyouts_rub=excluded.buyouts_rub, "
            "cancels=excluded.cancels, wishlist=excluded.wishlist",
            rows_out)
    await asyncio.to_thread(_save)
    _log.info("wb_funnel: %d строк за %s..%s", len(rows_out), dt_from, dt_to)
    return {"rows": len(rows_out), "from": str(dt_from), "to": str(dt_to)}


def summary(days: int = 14, sku: str = "") -> dict:
    """Воронка по SKU за период + сравнение с предыдущим таким же периодом.

    Конверсии считаем сами из сумм (persented-поля WB по дням не складываются).
    """
    _init()
    today = (datetime.utcnow() + timedelta(hours=3)).date()
    cur_from = today - timedelta(days=days)
    prev_from = cur_from - timedelta(days=days)

    def _load(d_from, d_to):
        where = "dt >= ? AND dt < ?"
        params = [str(d_from), str(d_to)]
        if sku:
            where += " AND (sku = ? OR nm = ?)"
            params += [sku, sku]
        rows = db.fetchall(
            f"SELECT nm, MAX(sku), SUM(opens), SUM(carts), SUM(orders_cnt), "
            f"SUM(orders_rub), SUM(buyouts), SUM(buyouts_rub), SUM(cancels), "
            f"SUM(wishlist) FROM wb_funnel WHERE {where} GROUP BY nm",
            tuple(params))
        out = {}
        for r in rows:
            out[str(r[0])] = {
                "sku": r[1] or str(r[0]), "opens": int(r[2] or 0),
                "carts": int(r[3] or 0), "orders": int(r[4] or 0),
                "orders_rub": round(float(r[5] or 0)),
                "buyouts": int(r[6] or 0),
                "buyouts_rub": round(float(r[7] or 0)),
                "cancels": int(r[8] or 0), "wishlist": int(r[9] or 0)}
        return out

    cur = _load(cur_from, today)
    prev = _load(prev_from, cur_from)

    def _pct(a, b):
        return round(a / b * 100, 1) if b else None

    items = []
    for nm, c in cur.items():
        p = prev.get(nm) or {}
        items.append({
            "nm": nm, "sku": c["sku"],
            "opens": c["opens"], "carts": c["carts"], "orders": c["orders"],
            "orders_rub": c["orders_rub"], "buyouts": c["buyouts"],
            "cancels": c["cancels"], "wishlist": c["wishlist"],
            "cr_cart": _pct(c["carts"], c["opens"]),
            "cr_order": _pct(c["orders"], c["carts"]),
            "cr_buyout": _pct(c["buyouts"], c["orders"] or 0),
            "prev_opens": p.get("opens"), "prev_orders": p.get("orders"),
            "prev_cr_cart": _pct(p.get("carts", 0), p.get("opens", 0)),
            "prev_cr_order": _pct(p.get("orders", 0), p.get("carts", 0)),
        })
    items.sort(key=lambda x: -x["opens"])
    dates = db.fetchone("SELECT MIN(dt), MAX(dt), COUNT(DISTINCT dt) FROM wb_funnel")
    return {"items": items, "days": days,
            "period": f"{cur_from}..{today - timedelta(days=1)}",
            "history": {"from": dates[0], "to": dates[1],
                        "days_stored": dates[2]} if dates and dates[0] else None}


def daily(sku: str, days: int = 28) -> list[dict]:
    """Динамика воронки одного артикула по дням."""
    _init()
    d_from = (datetime.utcnow() + timedelta(hours=3)).date() - timedelta(days=days)
    rows = db.fetchall(
        "SELECT dt, SUM(opens), SUM(carts), SUM(orders_cnt), SUM(buyouts) "
        "FROM wb_funnel WHERE (sku = ? OR nm = ?) AND dt >= ? "
        "GROUP BY dt ORDER BY dt", (sku, sku, str(d_from)))
    return [{"dt": r[0], "opens": int(r[1] or 0), "carts": int(r[2] or 0),
             "orders": int(r[3] or 0), "buyouts": int(r[4] or 0)} for r in rows]
