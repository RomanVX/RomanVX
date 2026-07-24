"""Ущерб от пожаров на складах WB: сколько денег сгорело по себестоимости.

Пожары июля 2026 (Шушары, Электросталь, Котовск, Краснодар, Невинномысск).
Считаем по остаткам из отчёта платного хранения WB на дату пожара, умноженным
на себестоимость. Розница — справочно, для суммы претензии.

ВАЖНО по Электростали: часть остатков WB формально вернул в кабинет продавца,
но физически сгорело всё. Поэтому возвращённое НЕ вычитаем — считаем полный
объём, что и отражено в поле note.

Данные лежат в БД и правятся: строки можно догрузить файлом остатков по
складу, себестоимость подтягивается из cost_store.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import db

_log = logging.getLogger("damage")

_SEED = os.path.join(os.path.dirname(__file__), "data", "damage_seed.json")

# Склады, по которым ведём учёт. Порядок = порядок вывода на экране.
WAREHOUSES = ["Шушары", "Электросталь", "Котовск", "Краснодар", "Невинномысск"]

_NOTES = {
    "Электросталь": "Остатки на 17.07 + принятая поставка 580 шт. "
                    "Возвращённое WB в кабинет НЕ вычитаем — физически "
                    "сгорело всё.",
    "Котовск": "Остатки из отчёта платного хранения, срез 18.07.2026.",
}


def _init() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS damage (
        id TEXT PRIMARY KEY, warehouse TEXT, sku TEXT, nm TEXT, name TEXT,
        qty INTEGER, cost REAL, retail REAL, source TEXT, note TEXT,
        updated TEXT)""")
    try:      # таблица могла быть создана до появления колонки источника
        db.execute("ALTER TABLE damage ADD COLUMN source TEXT")
    except Exception:
        pass


def _msk() -> str:
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")


def _seed_if_empty() -> None:
    """Заливаем расчёты, уже сделанные по отчётам WB.

    Ключ включает источник: один артикул может гореть и в складских
    остатках, и в поставке, принятой перед пожаром (AL-01: 30 + 440 шт).
    Без этого вторая строка затиралась и сумма занижалась.
    """
    _init()
    try:
        rows = json.load(open(_SEED, encoding="utf-8"))
    except Exception as e:
        _log.warning("seed: %s", e)
        return
    seeded = {r["wh"] for r in rows}
    marks = ",".join("?" * len(seeded))
    have = db.fetchone(
        f"SELECT COUNT(*) FROM damage WHERE warehouse IN ({marks})",
        tuple(seeded))
    have_n = int((have or [0])[0] or 0)
    if have_n == len(rows):
        return
    if have_n:      # была залита урезанная версия — переписываем начисто
        _log.warning("damage: пересев (было %d строк, ожидается %d)",
                     have_n, len(rows))
        for wh in seeded:
            db.execute("DELETE FROM damage WHERE warehouse = ?", (wh,))
    now = _msk()
    for i, r in enumerate(rows):
        src = r.get("source") or ""
        db.execute(
            "INSERT INTO damage (id, warehouse, sku, nm, name, qty, cost, "
            "retail, source, note, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (id) DO NOTHING",
            (f"{r['wh']}|{r['sku']}|{i}", r["wh"], r["sku"], r.get("nm", ""),
             r.get("name", ""), int(r["qty"]), float(r.get("cost") or 0),
             float(r.get("retail") or 0), src, _NOTES.get(r["wh"], ""), now))
    _log.info("damage: залито %d позиций", len(rows))


def rows(warehouse: str = "") -> list[dict]:
    _seed_if_empty()
    where, params = [], []
    if warehouse:
        where.append("warehouse = ?"); params.append(warehouse)
    q = ("SELECT warehouse, sku, nm, name, qty, cost, retail, source, note, "
         "updated FROM damage"
         + (" WHERE " + " AND ".join(where) if where else "")
         + " ORDER BY warehouse, qty DESC")
    keys = ["warehouse", "sku", "nm", "name", "qty", "cost", "retail",
            "source", "note", "updated"]
    out = []
    for r in db.fetchall(q, tuple(params)):
        d = dict(zip(keys, r))
        d["cost_total"] = round((d["qty"] or 0) * (d["cost"] or 0))
        d["retail_total"] = round((d["qty"] or 0) * (d["retail"] or 0))
        out.append(d)
    return out


def summary() -> dict:
    """Свод по складам + итог. Склады без данных показываем пустыми строками —
    видно, что расчёт по ним ещё не сделан, а не что ущерба нет."""
    all_rows = rows()
    by_wh: dict = {}
    for r in all_rows:
        g = by_wh.setdefault(r["warehouse"], {
            "warehouse": r["warehouse"], "skus": 0, "qty": 0,
            "cost_total": 0, "retail_total": 0, "note": r.get("note") or "",
            "updated": r.get("updated")})
        g["skus"] += 1
        g["qty"] += r["qty"] or 0
        g["cost_total"] += r["cost_total"]
        g["retail_total"] += r["retail_total"]
    order = {w: i for i, w in enumerate(WAREHOUSES)}
    for w in WAREHOUSES:
        by_wh.setdefault(w, {"warehouse": w, "skus": 0, "qty": 0,
                             "cost_total": 0, "retail_total": 0,
                             "note": "расчёт ещё не загружен", "updated": None})
    warehouses = sorted(by_wh.values(),
                        key=lambda x: order.get(x["warehouse"], 99))
    no_cost = [r["sku"] for r in all_rows if not r.get("cost")]
    # контроль: сходится ли с исходными расчётами по файлам WB
    control = {}
    try:
        for r in json.load(open(_SEED, encoding="utf-8")):
            c = control.setdefault(r["wh"], {"qty": 0, "cost_total": 0})
            c["qty"] += int(r["qty"])
            c["cost_total"] += int(r["qty"]) * float(r.get("cost") or 0)
        for w in by_wh.values():
            exp = control.get(w["warehouse"])
            if exp:
                w["control_qty"] = exp["qty"]
                w["control_cost"] = round(exp["cost_total"])
                w["matches"] = (w["qty"] == exp["qty"]
                                and abs(w["cost_total"] - exp["cost_total"]) < 1)
    except Exception:
        pass
    return {
        "warehouses": warehouses,
        "total": {
            "skus": len({r["sku"] for r in all_rows}),
            "qty": sum(r["qty"] or 0 for r in all_rows),
            "cost_total": sum(r["cost_total"] for r in all_rows),
            "retail_total": sum(r["retail_total"] for r in all_rows),
        },
        "rows": all_rows,
        "missing_cost": sorted(set(no_cost)),
        "pending": [w["warehouse"] for w in warehouses if not w["qty"]],
    }


async def upload(warehouse: str, raw: bytes) -> dict:
    """Файл остатков по складу (выгрузка «Платное хранение» WB или свой xlsx):
    берём артикул и количество, себестоимость подставляем свою."""
    import io
    import openpyxl
    _init()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    except Exception as e:
        return {"error": f"не смог открыть файл: {str(e)[:200]}"}
    try:
        import cost_store
        costs = cost_store.get_costs() or {}
    except Exception:
        costs = {}
    try:
        import catalog as _cat
        nm_by_art = {v.upper(): k for k, v in
                     getattr(_cat, "WB_ID_TO_ART", {}).items()}
    except Exception:
        nm_by_art = {}

    added, skipped = 0, 0
    now = _msk()
    for ws in wb:
        hdr_i, hdr = -1, []
        for i, row in enumerate(ws.iter_rows(min_row=1, max_row=15,
                                             values_only=True)):
            cells = [str(c or "").strip().lower() for c in row]
            if any("артикул" in c for c in cells) and \
                    any(("кол" in c or "остат" in c or "шт" in c) for c in cells):
                hdr_i, hdr = i, cells
                break
        if hdr_i < 0:
            continue

        def col(*words, exclude=()):
            for j, h in enumerate(hdr):
                if any(w in h for w in words) and not any(x in h for x in exclude):
                    return j
            return -1

        c_art = col("артикул", exclude=("wb", "nmid"))
        c_qty = col("кол", "остат", "шт", exclude=("штрих",))
        c_name = col("назван", "предмет")
        c_cost = col("себес")
        c_ret = col("розниц", "цена")
        if c_art < 0 or c_qty < 0:
            continue
        for row in ws.iter_rows(min_row=hdr_i + 2, values_only=True):
            art = str(row[c_art] or "").strip()
            if not art or art.upper().startswith("ИТОГО"):
                continue
            try:
                qty = int(float(str(row[c_qty]).strip()))
            except (TypeError, ValueError):
                skipped += 1
                continue
            if qty <= 0:
                continue
            cost = 0.0
            if c_cost >= 0 and row[c_cost]:
                try:
                    cost = float(row[c_cost])
                except (TypeError, ValueError):
                    cost = 0.0
            if not cost:
                cost = float(costs.get(art) or costs.get(art.upper()) or 0)
            retail = 0.0
            if c_ret >= 0 and row[c_ret]:
                try:
                    retail = float(row[c_ret])
                except (TypeError, ValueError):
                    retail = 0.0
            name = str(row[c_name] or "")[:120] if c_name >= 0 else ""
            db.execute(
                "INSERT INTO damage (id, warehouse, sku, nm, name, qty, cost, "
                "retail, source, note, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (id) DO UPDATE SET qty=excluded.qty, "
                "cost=excluded.cost, retail=excluded.retail, "
                "name=excluded.name, updated=excluded.updated",
                (f"{warehouse}|{art}|upload", warehouse, art,
                 nm_by_art.get(art.upper(), ""), name, qty, cost, retail,
                 "загружено файлом", _NOTES.get(warehouse, ""), now))
            added += 1
    if not added:
        return {"error": "не нашёл строк с артикулом и количеством — "
                         "проверь, что это выгрузка остатков"}
    return {"warehouse": warehouse, "added": added, "skipped": skipped}


def clear(warehouse: str) -> dict:
    _init()
    db.execute("DELETE FROM damage WHERE warehouse = ?", (warehouse,))
    return {"cleared": warehouse}
