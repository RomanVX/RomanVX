"""Репрайсер: управление ценовой политикой по цене для покупателя.

Конфиг на артикул: целевая цена для покупателя + минималки Ozon/WB.
Три пресета уровней (low/mid/high) из выгрузок озоновского репрайсера.
Экспорт XLSX в формате шаблона Ozon (загружается в их ЛК как есть).
Стратег видит конфиг и кладёт ПРЕДЛОЖЕНИЯ — применяет их только владелец.
Сам модуль цены НЕ меняет ни на одной площадке."""
import asyncio
import json
import logging
import os
from datetime import datetime

import db

_log = logging.getLogger("repricer")
_SEED = os.path.join(os.path.dirname(__file__), "data", "repricer_seed.json")


def _init():
    db.execute("""CREATE TABLE IF NOT EXISTS repricer_cfg (
        art TEXT PRIMARY KEY, name TEXT, sku_ozon TEXT, nm_wb TEXT,
        target REAL, min_ozon REAL, min_wb REAL, active INTEGER,
        updated TEXT)""")
    row = db.fetchone("SELECT COUNT(*) FROM repricer_cfg")
    if row and row[0] == 0:
        _seed()


def _seed():
    """Первичное наполнение из выгрузок озоновского репрайсера (пресет mid)."""
    try:
        data = json.load(open(_SEED, encoding="utf-8"))
    except Exception as e:
        _log.warning("seed: %s", e)
        return
    import snapshot as _snap
    _snap.save("repricer_presets", data["presets"])
    mid = data["presets"].get("mid") or {}
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    for art, m in data["products"].items():
        p = mid.get(art) or {}
        db.execute(
            "INSERT INTO repricer_cfg VALUES (?,?,?,?,?,?,?,?,?)",
            (art, m.get("name", ""), m.get("sku_ozon", ""), m.get("nm_wb", ""),
             p.get("target"), p.get("min_ozon"), p.get("min_wb"),
             1 if p.get("active") else 0, now))
    _log.info("repricer: засеяно %d артикулов (пресет mid)", len(data["products"]))


def cfg_load() -> list[dict]:
    _init()
    rows = db.fetchall(
        "SELECT art, name, sku_ozon, nm_wb, target, min_ozon, min_wb, active, updated "
        "FROM repricer_cfg ORDER BY art")
    keys = ["art", "name", "sku_ozon", "nm_wb", "target", "min_ozon",
            "min_wb", "active", "updated"]
    return [dict(zip(keys, r)) for r in rows]


def cfg_set(art: str, **kw) -> bool:
    _init()
    # минималка — служебное поле репрайсера маркетплейса: держим её
    # автоматически чуть выше целевой цены, руками не ведём
    if kw.get("target") and kw.get("min_ozon") is None and kw.get("min_wb") is None:
        kw["min_ozon"] = kw["min_wb"] = round(float(kw["target"]) * 1.05)
    fields, vals = [], []
    for k in ("target", "min_ozon", "min_wb", "active", "name"):
        if k in kw and kw[k] is not None:
            fields.append(f"{k} = ?")
            vals.append(kw[k])
    if not fields:
        return False
    fields.append("updated = ?")
    vals.append(datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
    vals.append(art)
    db.execute(f"UPDATE repricer_cfg SET {', '.join(fields)} WHERE art = ?", vals)
    return True


def presets_load() -> dict:
    import snapshot as _snap
    return _snap.load("repricer_presets", None) or {}


def preset_apply(name: str) -> int:
    """Переключить весь конфиг на пресет low/mid/high."""
    p = presets_load().get(name)
    if not p:
        return 0
    n = 0
    for art, v in p.items():
        if cfg_set(art, target=v.get("target"), min_ozon=v.get("min_ozon"),
                   min_wb=v.get("min_wb"), active=1 if v.get("active") else 0):
            n += 1
    _log.info("repricer: применён пресет %s (%d)", name, n)
    return n


# ── предложения стратега (pending до решения владельца) ──────────────────────
def proposals_load() -> list[dict]:
    import snapshot as _snap
    return _snap.load("repricer_proposals", None) or []


def proposals_save(items: list[dict]) -> None:
    import snapshot as _snap
    _snap.save("repricer_proposals", items[:50])


def propose(items: list[dict], reason: str) -> int:
    """Стратег кладёт предложения: [{art, target, min_wb?, min_ozon?, why}]."""
    cur = proposals_load()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    known = {c["art"] for c in cfg_load()}
    n = 0
    for it in items[:30]:
        art = str(it.get("art") or "").strip()
        if art not in known:
            continue
        cur = [p for p in cur if p.get("art") != art]   # новое перекрывает старое
        cur.append({"art": art, "target": it.get("target"),
                    "min_wb": it.get("min_wb"), "min_ozon": it.get("min_ozon"),
                    "why": str(it.get("why") or reason)[:400], "created": now})
        n += 1
    proposals_save(cur)
    return n


def proposal_apply(art: str) -> bool:
    """Владелец принимает предложение — переносится в конфиг, а стратегу
    ставится задача проверить эффект через неделю (отслеживание решений)."""
    from datetime import timedelta
    cur = proposals_load()
    for p in cur:
        if p.get("art") == art:
            cfg_set(art, target=p.get("target"), min_wb=p.get("min_wb"),
                    min_ozon=p.get("min_ozon"))
            proposals_save([x for x in cur if x.get("art") != art])
            try:
                import uuid
                check = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d")
                db.execute(
                    "INSERT INTO strategist_tasks VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12],
                     datetime.utcnow().strftime("%Y-%m-%d %H:%M"), "hypothesis",
                     f"Цена {art}: владелец принял цель {p.get('target')} ₽ — проверить эффект",
                     f"темп продаж и маржа {art} не ухудшились при цели {p.get('target')} ₽",
                     check, "open", "",
                     (p.get("why") or "")[:500]))
            except Exception as e:
                _log.warning("apply task: %s", e)
            return True
    return False


async def sync_targets_to_fact() -> int:
    """Цели = фактические цены для покупателя из кабинетов (Ozon приоритетнее,
    WB — оценка через СПП). Стартовая точка «как есть» для анализа стратега."""
    ov = await overview()
    n = 0
    for c in ov["items"]:
        fact = c.get("oz_buyer_now") or c.get("wb_buyer_now")
        if fact and cfg_set(c["art"], target=round(float(fact))):
            n += 1
    _log.info("repricer: цели синхронизированы с фактом (%d)", n)
    return n


def proposal_reject(art: str) -> bool:
    cur = proposals_load()
    nxt = [x for x in cur if x.get("art") != art]
    proposals_save(nxt)
    return len(nxt) != len(cur)


# ── обзор для стратега и вкладки: конфиг × факт × юнитка ─────────────────────
_live_cache: dict = {}
_live_ts: float = 0.0


async def overview(refresh: bool = False, include_margin: bool = True) -> dict:
    """Конфиг + текущие цены WB/Ozon (кеш 10 мин) + маржа при целевой цене.
    include_margin=False — быстрый режим без ожидания юнитки (холодный старт)."""
    import time as _t
    import agent_review as _ar
    from routers import tools as _tools
    global _live_cache, _live_ts
    cfg = await asyncio.to_thread(cfg_load)
    if not refresh and _live_cache and _t.monotonic() - _live_ts < 600:
        live, oz = _live_cache["live"], _live_cache["oz"]
    else:
        live, oz = {}, {}
        try:
            import wb_client
            live = await wb_client.get_current_prices()
        except Exception as e:
            _log.warning("overview prices: %s", e)
        try:
            import ozon_client
            oz = await ozon_client.get_prices()
        except Exception as e:
            _log.warning("overview ozon prices: %s", e)
        if live or oz:
            _live_cache = {"live": live, "oz": oz}
            _live_ts = _t.monotonic()
    margin = {}
    if include_margin:
        try:
            data = await _tools.get_margin(mp="WB")
            for b in data.get("items") or []:
                margin[b.get("sku")] = b
        except Exception as e:
            _log.warning("overview margin: %s", e)
    # коэффициент buyer/seller стабилен у SKU — кешируем на случай, когда
    # юнитка не собрана (иначе «для покупателя» по WB пустеет)
    import snapshot as _snap
    ratios = await asyncio.to_thread(_snap.load, "repricer_ratios", None) or {}
    ratios_dirty = False
    import catalog as _cat
    # Ozon API не отдаёт витринную цену с механиками соплатежа — оцениваем
    # через коэффициент покупатель/продавец из выгрузок озоновского репрайсера
    try:
        oz_ratios = json.load(open(_SEED, encoding="utf-8")).get("oz_ratios") or {}
    except Exception:
        oz_ratios = {}
    out = []
    for c in cfg:
        b = margin.get(c["art"])
        row = dict(c)
        row["brand"] = _cat.lookup(c["art"]).get("brand", "")
        lp = live.get(c["art"]) or {}
        row["wb_seller_now"] = lp.get("discounted")
        op = oz.get(c["art"]) or {}
        row["oz_price_now"] = op.get("price")
        ozr = oz_ratios.get(c["art"])
        if ozr:
            row["oz_seller_per_buyer"] = round(1 / ozr, 4)
        mp = op.get("marketing_price")
        if mp and op.get("price") and mp < op["price"]:
            row["oz_buyer_now"] = mp                     # цена с акциями из API
        elif ozr and op.get("price"):
            row["oz_buyer_now"] = round(op["price"] * ozr)   # оценка по соплатежу
        else:
            row["oz_buyer_now"] = op.get("price")
        ratio = None
        if b and b.get("price0") and b.get("buyer0"):
            ratio = b["buyer0"] / b["price0"]
            if abs(ratios.get(c["art"], 0) - ratio) > 1e-6:
                ratios[c["art"]] = ratio
                ratios_dirty = True
        else:
            ratio = ratios.get(c["art"])
        if b:
            q = b.get("qty_f") if b.get("qty_f") is not None else b.get("qty_m")
            row["qty_month"] = round(q or 0)
        if ratio:
            row["seller_per_buyer"] = round(1 / ratio, 4)   # выручка = qty × цена× это
            row["wb_buyer_now"] = round((lp.get("discounted") or 0) * ratio) or None
            if c.get("target"):
                need_seller = c["target"] / ratio
                row["wb_seller_needed"] = round(need_seller)
                if b:
                    mm = _ar._margin_math({**b, "price0": need_seller,
                                           "buyer0": c["target"]})
                    row["margin_at_target"] = mm.get("margin_pct")
                    row["profit_at_target"] = mm.get("profit_unit")
        out.append(row)
    if ratios_dirty:
        await asyncio.to_thread(_snap.save, "repricer_ratios", ratios)
    return {"items": out, "proposals": proposals_load(),
            "presets": {k: len(v) for k, v in presets_load().items()}}


def export_ozon_xlsx() -> bytes:
    """XLSX в формате шаблона репрайсера Ozon — загрузить в их ЛК как есть."""
    import io
    import openpyxl
    cfg = cfg_load()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Настройка репрайсера"
    ws.append(["Товар", None, None, None, "Ozon", None, None,
               "Wildberries", None, None, "Активация стратегии", None, None, "Ошибки"])
    ws.append(["Название товара на Ozon", "SKU Ozon", "Артикул Ozon", "Артикул WB",
               "Текущая цена с учетом акции на Ozon, руб.",
               "Текущая цена для покупателя на Ozon, руб.",
               "Минимальная цена на Ozon, руб.",
               "Текущая цена на Wildberries, руб.",
               "Текущая цена для покупателя на Wildberries, руб.",
               "Минимальная цена на Wildberries, руб.",
               "Целевое значение цены для покупателя, руб.",
               "Активировать стратегию репрайсера",
               "Участие товара в стратегиях репрайсера", None])
    ws.append(["Нередактируемое"] * 6 + ["Редактируемое", "Нередактируемое",
               "Нередактируемое", "Редактируемое", "Редактируемое",
               "Редактируемое", "Нередактируемое", "Нередактируемое"])
    ws.append(["—"] * 14)
    for c in cfg:
        ws.append([c["name"], c["sku_ozon"], c["art"], c["nm_wb"],
                   None, None, c["min_ozon"], None, None, c["min_wb"],
                   c["target"], "ДА" if c["active"] else "НЕТ",
                   "Стратегия по цене для покупателя", None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
