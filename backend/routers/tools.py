"""Инструменты WB. Первый: Продуктолог — анализ отзывов по каждому артикулу.

Из всех собранных отзывов WB по SKU строится сводка: сильные и слабые
стороны товара с частотой упоминаний (%), и рекомендация к доработке.
Анализ делает Claude, результат кэшируется в БД и пересобирается в фоне
только для артикулов, где появились новые отзывы.
"""
import asyncio
import os
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

import db
from config import ANTHROPIC_API_KEY

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tools", tags=["tools"])

_building = False
_error = ""
_progress = ""

_MODEL = "claude-opus-4-8"
_MAX_REVIEWS_PER_SKU = 120
_MIN_TEXTS = 3          # меньше 3 текстовых отзывов — анализировать нечего


def _init_table():
    db.execute("CREATE TABLE IF NOT EXISTS productolog "
               "(sku TEXT PRIMARY KEY, data TEXT, reviews_count INTEGER, built_at TEXT)")


def _wb_reviews_by_sku() -> dict[str, list]:
    """{sku: [(rating, text, date)]} — все отзывы WB из БД."""
    rows = db.fetchall(
        "SELECT sku, rating, text, created_at FROM reviews "
        "WHERE platform = 'WB' AND sku IS NOT NULL AND sku != '' "
        "ORDER BY created_at DESC")
    out: dict[str, list] = {}
    for sku, rating, text, dt in rows:
        out.setdefault(sku, []).append((int(rating or 0), (text or "").strip(), dt))
    return out


def _stats(reviews: list) -> dict:
    n = len(reviews)
    pos = sum(1 for r, _, _ in reviews if r >= 4)
    neu = sum(1 for r, _, _ in reviews if r == 3)
    neg = n - pos - neu
    avg = round(sum(r for r, _, _ in reviews) / n, 2) if n else 0
    return {"count": n, "avg": avg,
            "pos": round(pos / n * 100) if n else 0,
            "neu": round(neu / n * 100) if n else 0,
            "neg": round(neg / n * 100) if n else 0}


async def _analyze_sku(sku: str, name: str, reviews: list) -> dict | None:
    """LLM-анализ отзывов одного артикула → {pluses, minuses, recommendation}."""
    texts = [(r, t) for r, t, _ in reviews if t][:_MAX_REVIEWS_PER_SKU]
    if len(texts) < _MIN_TEXTS:
        return None
    body = "\n".join(f"[{r}★] {t[:400]}" for r, t in texts)
    prompt = f"""Ты — продуктолог маркетплейс-селлера. Проанализируй отзывы покупателей о товаре «{name}» (артикул {sku}).

ОТЗЫВЫ ({len(texts)} шт., в скобках оценка):
{body}

Верни СТРОГО JSON без пояснений и без markdown:
{{
 "pluses":  [{{"tag": "короткая формулировка плюса (2-4 слова)", "pct": число % отзывов с этим плюсом}}],
 "minuses": [{{"tag": "короткая формулировка минуса", "pct": число}}],
 "recommendation": "1-3 предложения: что конкретно доработать в продукте/упаковке/карточке"
}}
Правила: до 6 плюсов и до 6 минусов, сортируй по частоте, pct — целое число (доля отзывов, где тема упомянута), формулировки конкретные («Течёт крышка», а не «Плохое качество»). Если минусов нет — пустой список и рекомендация «Существенных проблем в отзывах нет»."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    msg = await client.messages.create(
        model=_MODEL, max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except (ValueError, TypeError):
        _log.warning("productolog %s: не распарсился ответ LLM", sku)
        return None
    return {"pluses": data.get("pluses") or [],
            "minuses": data.get("minuses") or [],
            "recommendation": str(data.get("recommendation") or "")}


def _set_progress(msg: str) -> None:
    global _progress
    _progress = msg


async def _build_bg(force: bool = False) -> None:
    global _building, _error, _progress
    if _building:
        return
    _building = True
    _error = ""
    try:
        if not ANTHROPIC_API_KEY:
            _error = "ANTHROPIC_API_KEY не настроен"
            return
        import catalog as _cat
        _init_table()
        by_sku = await asyncio.to_thread(_wb_reviews_by_sku)
        cached = {r[0]: r[2] for r in await asyncio.to_thread(
            db.fetchall, "SELECT sku, data, reviews_count FROM productolog")}
        # пересобираем только SKU, где отзывов стало заметно больше
        todo = []
        for sku, revs in by_sku.items():
            n_texts = sum(1 for _, t, _ in revs if t)
            if n_texts < _MIN_TEXTS:
                continue
            if not force and sku in cached and n_texts <= (cached[sku] or 0) + 2:
                continue
            todo.append((sku, revs, n_texts))
        todo.sort(key=lambda x: -x[2])
        total = len(todo)
        done = 0
        errors = 0
        sem = asyncio.Semaphore(4)   # 4 товара анализируем параллельно

        async def _one(sku, revs, n_texts):
            nonlocal done, errors
            name = _cat.lookup(sku).get("name", sku)
            async with sem:
                try:
                    data = await _analyze_sku(sku, name, revs)
                except Exception as e:
                    errors += 1
                    _log.warning("productolog %s: %s", sku, e)
                    return f"{sku}: {str(e)[:150]}"
                if data:
                    await asyncio.to_thread(
                        db.execute,
                        "INSERT INTO productolog (sku, data, reviews_count, built_at) VALUES (?,?,?,?) "
                        "ON CONFLICT (sku) DO UPDATE SET data = excluded.data, "
                        "reviews_count = excluded.reviews_count, built_at = excluded.built_at",
                        (sku, json.dumps(data, ensure_ascii=False), n_texts,
                         datetime.utcnow().isoformat()))
            return None

        async def _tracked(sku, revs, n_texts):
            nonlocal done
            err = await _one(sku, revs, n_texts)
            done += 1
            _set_progress(f"проанализировано {done} из {total}"
                          + (f", ошибок {errors}" if errors else ""))
            return err

        errs = await asyncio.gather(*[_tracked(s, r, n) for s, r, n in todo])
        errs = [e for e in errs if e]
        if errs:
            _error = f"{len(errs)} арт. с ошибкой (напр. {errs[0]})"
        _log.info("productolog: обновлено %d SKU, ошибок %d", total - len(errs), len(errs))
    except Exception as e:
        _error = str(e)[:300]
        _log.error("productolog build: %s", e)
    finally:
        _building = False
        _progress = ""


# держим ссылки на фоновые задачи (иначе GC их убивает)
_bg: set = set()


def _spawn(coro):
    # тяжёлый шлюз тут сознательно НЕ используется: сборки инструментов
    # (LLM-анализ, fullstats 1 req/мин) длятся десятки минут, но памяти
    # почти не потребляют — под heavy.guard они заблокировали бы финансы
    t = asyncio.get_event_loop().create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


@router.get("/productolog")
async def get_productolog(refresh: bool = Query(default=False)):
    """Продуктолог: анализ отзывов WB по артикулам."""
    import catalog as _cat
    _init_table()
    if refresh and not _building:
        _spawn(_build_bg(force=False))
    by_sku = await asyncio.to_thread(_wb_reviews_by_sku)
    rows = await asyncio.to_thread(
        db.fetchall, "SELECT sku, data, built_at FROM productolog")
    analyzed = {r[0]: (json.loads(r[1] or "{}"), r[2]) for r in rows}
    items = []
    for sku, revs in by_sku.items():
        st = _stats(revs)
        if st["count"] < _MIN_TEXTS:
            continue
        info = _cat.lookup(sku)
        a, built = analyzed.get(sku, ({}, None))
        items.append({
            "sku": sku, "name": info.get("name", sku), "group": info.get("brand", ""),
            **st,
            "pluses": a.get("pluses") or [],
            "minuses": a.get("minuses") or [],
            "recommendation": a.get("recommendation") or "",
            "analyzed": bool(a), "built_at": (built or "")[:10],
        })
    # проблемные сверху: доля негатива, затем количество отзывов
    items.sort(key=lambda x: (-x["neg"], -x["count"]))
    pending = sum(1 for it in items if not it["analyzed"])
    if pending and not _building:
        _spawn(_build_bg(force=False))
    return {"items": items, "building": _building, "progress": _progress,
            "error": _error, "pending": pending}


@router.get("/productolog/export")
async def export_productolog():
    """Выгрузка продуктолога в Excel: артикул, наименование, отзывы,
    рейтинг, плюсы/минусы/рекомендация — для исследования «что реабилитировать»."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from fastapi.responses import StreamingResponse

    data = await get_productolog(refresh=False)
    items = data.get("items", [])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Продуктолог"
    headers = ["Артикул", "Наименование", "Группа", "Кол-во отзывов",
               "Рейтинг карточки", "% негатива", "Плюсы (тема · %)",
               "Минусы (тема · %)", "Рекомендация к доработке"]
    ws.append(headers)
    hfill = PatternFill("solid", fgColor="4F46E5")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = hfill
        c.alignment = Alignment(vertical="center", wrap_text=True)

    def _chips(lst):
        return "\n".join(f"{p.get('tag','')} · {p.get('pct','')}%" for p in (lst or []))

    for it in items:
        ws.append([
            it.get("sku", ""), it.get("name", ""), it.get("group", ""),
            it.get("count", 0),
            round(it.get("avg", 0), 2) if it.get("avg") is not None else "",
            f'{it.get("neg", 0)}%',
            _chips(it.get("pluses")) if it.get("analyzed") else "⏳ анализируется",
            _chips(it.get("minuses")) if it.get("analyzed") else "",
            it.get("recommendation", "") if it.get("analyzed") else "",
        ])
    widths = [12, 34, 14, 13, 15, 11, 34, 34, 55]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = "productolog.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ══ Остатки по кластерам (федеральным округам) ════════════════════════════════

_CLUSTER_KEYS = [
    ("Центральный",      ["колед", "подольск", "электрост", "тула", "белые столбы",
                          "радумл", "вёшки", "вешки", "обухово", "чехов", "рязан",
                          "котовск", "голицын", "пушкино", "внуково", "москв",
                          "иваново", "ярослав", "воронеж", "белгород", "домодедово",
                          "сабурово", "черная грязь", "чёрная грязь", "щербинка"]),
    ("Северо-Западный",  ["санкт", "уткина", "шушары", "псков", "спб", "новосаратовка",
                          "петербург", "калининград", "мурманс"]),
    ("Южный",            ["краснодар", "тихорецк", "невинномыс", "волгоград", "ростов",
                          "астрахан", "крым", "симферопол", "ставропол"]),
    ("Приволжский",      ["казан", "самара", "новосемейкино", "сарапул", "уфа",
                          "нижний новгород", "пенза", "саратов", "чебоксар", "оренбург",
                          "киров", "перм", "ижевск"]),
    ("Уральский",        ["екатеринбург", "испытателей", "перспективн", "челябинск",
                          "тюмен", "курган", "сургут"]),
    ("Сибирский",        ["новосибир", "омск", "красноярск", "кемеров", "иркутск",
                          "барнаул", "томск"]),
    ("Дальневосточный",  ["хабаровск", "владивосток", "артём", "артем", "благовещенск"]),
]
_CLUSTER_ORDER = [c for c, _ in _CLUSTER_KEYS] + ["Прочее"]


def _cluster_of(name: str) -> str:
    low = (name or "").lower()
    for cluster, keys in _CLUSTER_KEYS:
        if any(k in low for k in keys):
            return cluster
    return "Прочее"


def _okrug_cluster(okrug: str) -> str:
    """«Центральный федеральный округ» → «Центральный»."""
    low = (okrug or "").lower()
    for cluster, _ in _CLUSTER_KEYS:
        if low.startswith(cluster.lower().split("-")[0][:6]):
            return cluster
    for cluster, _ in _CLUSTER_KEYS:
        if cluster.lower() in low:
            return cluster
    return "Прочее"


_clusters_cache: dict = {}
_clusters_ts: float = 0.0


@router.get("/clusters")
async def get_clusters(refresh: bool = Query(default=False)):
    """Остатки и продажи WB по кластерам (федеральным округам складов)."""
    import time as _time
    global _clusters_cache, _clusters_ts
    if not refresh and _clusters_cache and _time.monotonic() - _clusters_ts < 1800:
        return _clusters_cache

    from datetime import timedelta
    import analytics
    import cache
    DAYS = 28
    dt_to = datetime.utcnow()
    sales, _orders, stocks = await cache.get_raw_data(dt_to - timedelta(days=DAYS), dt_to)

    agg: dict[str, dict] = {c: {"stock": 0, "sold": 0, "loc_hit": 0, "loc_total": 0}
                            for c in _CLUSTER_ORDER}
    # SKU-разрез: остаток на складах кластера и СПРОС покупателей кластера
    sku_stock: dict = {c: {} for c in _CLUSTER_ORDER}   # кластер → sku → шт на складах
    sku_demand: dict = {c: {} for c in _CLUSTER_ORDER}  # кластер → sku → выкупов покупателями округа
    for s in stocks or []:
        cl = _cluster_of(s.get("warehouseName"))
        agg[cl]["stock"] += int(s.get("quantity") or 0)
        sku = (s.get("supplierArticle") or "").strip()
        if sku:
            sku_stock[cl][sku] = sku_stock[cl].get(sku, 0) + int(s.get("quantity") or 0)
    for r in sales or []:
        if not analytics._is_sale(r):
            continue
        wh_cl = _cluster_of(r.get("warehouseName"))
        agg[wh_cl]["sold"] += 1
        buyer = _okrug_cluster(r.get("oblastOkrugName") or r.get("regionName") or "")
        if buyer != "Прочее":
            agg[buyer]["loc_total"] += 1
            if buyer == wh_cl:
                agg[buyer]["loc_hit"] += 1
            sku = (r.get("supplierArticle") or "").strip()
            if sku:
                sku_demand[buyer][sku] = sku_demand[buyer].get(sku, 0) + 1

    TARGET_DAYS = 30
    items = []
    for c in _CLUSTER_ORDER:
        a = agg[c]
        if a["stock"] == 0 and a["sold"] == 0:
            continue
        spd = round(a["sold"] / DAYS, 2)
        coverage = round(a["stock"] / spd, 1) if spd > 0 else None
        need = max(0, round((TARGET_DAYS - (coverage or 0)) * spd)) if spd > 0 else 0
        if spd == 0:
            status = "no_sales"
        elif coverage < 7:
            status = "urgent"
        elif coverage < 15:
            status = "warn"
        elif coverage > 90:
            status = "over"
        else:
            status = "ok"
        loc = round(a["loc_hit"] / a["loc_total"] * 100) if a["loc_total"] else None
        # что именно везти: покрытие по SKU от СПРОСА округа (не от отгрузок)
        import catalog as _cat
        skus = []
        for sku, dem in sku_demand[c].items():
            d_spd = dem / DAYS
            if d_spd <= 0:
                continue
            st_here = sku_stock[c].get(sku, 0)
            cov = round(st_here / d_spd, 1)
            sku_need = max(0, round((TARGET_DAYS - cov) * d_spd))
            if sku_need > 0:
                skus.append({"sku": _cat.canon(sku), "name": _cat.lookup(sku).get("name", sku),
                             "demand_spd": round(d_spd, 2), "stock": st_here,
                             "coverage": cov, "need": sku_need})
        skus.sort(key=lambda x: -x["need"])
        items.append({"cluster": c, "stock": a["stock"], "spd": spd,
                      "coverage": coverage, "need": need, "status": status,
                      "localization": loc, "demand": a["loc_total"],
                      "need_by_demand": sum(s["need"] for s in skus),
                      "skus": skus[:15]})

    total_hit = sum(a["loc_hit"] for a in agg.values())
    total_dem = sum(a["loc_total"] for a in agg.values())
    result = {
        "items": items,
        "days": DAYS,
        "target_days": TARGET_DAYS,
        "localization_total": round(total_hit / total_dem * 100, 1) if total_dem else None,
        "weak": sum(1 for it in items if it["status"] in ("urgent", "warn")),
        "fetched_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC"),
    }
    _clusters_cache = result
    _clusters_ts = _time.monotonic()
    return result


# ══ Контроль рекламы WB ═══════════════════════════════════════════════════════

_ADV_TYPES = {4: "Каталог", 5: "Карточка", 6: "Поиск", 7: "Рекомендации",
              8: "Авто", 9: "Аукцион"}
_ADV_STATUS = {4: "готова", 7: "завершена", 8: "отказ", 9: "🟢 активна", 11: "⏸ пауза"}

_adv_building = False
_adv_error = ""
_adv_progress = ""
_ADV_DAYS = 28


def _adv_init():
    db.execute("CREATE TABLE IF NOT EXISTS adv_tool "
               "(campaign_id BIGINT PRIMARY KEY, data TEXT, built_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS kv_cache (k TEXT PRIMARY KEY, v TEXT)")


def _adv_verdict(c: dict) -> tuple[str, str]:
    """Правило-вердикт по кампании: (код, пояснение)."""
    spend, revenue, clicks = c["spend"], c["revenue"], c["clicks"]
    if spend < 100:
        return "idle", "почти нет расхода"
    if revenue <= 0:
        return "waste", f"потрачено {round(spend)} ₽ без заказов"
    drr = spend / revenue * 100
    if drr > 30:
        return "bad", f"ДРР {round(drr)}% — реклама дороже, чем приносит"
    if drr > 15:
        return "warn", f"ДРР {round(drr)}% — на грани"
    return "good", f"ДРР {round(drr)}% — эффективна"


async def _adv_build_bg(force: bool = False) -> None:
    global _adv_building, _adv_error, _adv_progress
    if _adv_building:
        return
    _adv_building = True
    _adv_error = ""
    try:
        import advert_client as ac
        _adv_init()
        _adv_progress = "список кампаний"
        ids = await ac.get_all_campaign_ids_ext()
        if not ids:
            _adv_error = "Кампании не найдены (проверьте права токена «Продвижение»)"
            return
        meta = await ac.get_campaigns_meta(ids)
        # фолбэк: тип/статус из promotion/count, если детали кампаний пусты
        cnt_meta = ac.get_count_meta()
        for aid, cm in cnt_meta.items():
            m = meta.setdefault(aid, {"name": f"Кампания {aid}", "nms": [], "bids": {}})
            if m.get("type") is None:
                m["type"] = cm.get("type")
            if m.get("status") is None:
                m["status"] = cm.get("status")
        end = (datetime.utcnow() + timedelta(hours=3)).date()
        begin = end - timedelta(days=_ADV_DAYS)
        _adv_progress = "статистика кампаний (fullstats, ~минута)"
        stats = await ac.get_fullstats_campaigns(ids, begin.isoformat(), end.isoformat())

        import catalog as _cat
        campaigns = []
        for aid in ids:
            m = meta.get(aid) or {}
            s = stats.get(aid) or {}
            spend = round(float(s.get("sum") or 0), 2)
            status = m.get("status")
            if spend <= 0 and status not in (9, 11):
                continue   # старые пустые кампании не показываем
            # какие SKU продвигает: nm-разбивка расходов из fullstats,
            # фолбэк — товары из настроек кампании
            sku_spend: dict[str, dict] = {}
            for nid, cell in (s.get("nms") or {}).items():
                art = _cat.resolve_wb(nid)
                sc = sku_spend.setdefault(art, {"sum": 0.0, "orders": 0})
                sc["sum"] += cell.get("sum", 0)
                sc["orders"] += cell.get("orders", 0)
            if not sku_spend:
                for nid in m.get("nms") or []:
                    sku_spend.setdefault(_cat.resolve_wb(nid), {"sum": 0.0, "orders": 0})
            skus = sorted(
                [{"sku": k, "spend": round(v["sum"]), "orders": v["orders"]}
                 for k, v in sku_spend.items()], key=lambda x: -x["spend"])[:12]
            campaigns.append({
                "id": aid, "name": m.get("name") or str(aid),
                "type": _ADV_TYPES.get(m.get("type"), str(m.get("type") or "")),
                "type_id": m.get("type"),
                "status": _ADV_STATUS.get(status, str(status or "")),
                "active": status == 9,
                "spend": spend,
                "views": s.get("views") or 0,
                "clicks": s.get("clicks") or 0,
                "ctr": round((s.get("clicks") or 0) / s["views"] * 100, 2) if s.get("views") else 0,
                "cpc": round(spend / s["clicks"], 1) if s.get("clicks") else None,
                "orders": s.get("orders") or 0,
                "revenue": round(float(s.get("sum_price") or 0)),
                "atbs": s.get("atbs") or 0,
                "mode": "Автоматическая" if m.get("type") == 8 else "Ручная",
                "bids": m.get("bids") or {},
                "skus": skus,
            })
        for c in campaigns:
            c["cpo"] = round(c["spend"] / c["orders"]) if c["orders"] else None
            c["drr"] = round(c["spend"] / c["revenue"] * 100, 1) if c["revenue"] else None
            v, why = _adv_verdict(c)
            c["verdict"], c["verdict_why"] = v, why

        def _save(c: dict):
            db.execute(
                "INSERT INTO adv_tool (campaign_id, data, built_at) VALUES (?,?,?) "
                "ON CONFLICT (campaign_id) DO UPDATE SET data = excluded.data, built_at = excluded.built_at",
                (c["id"], json.dumps(c, ensure_ascii=False), datetime.utcnow().isoformat()))

        # сохраняем сразу (без фраз) — рестарты сервера не теряют прогресс
        for c in campaigns:
            await asyncio.to_thread(_save, c)
        _log.info("adv tool: %d кампаний сохранено, собираем фразы", len(campaigns))

        # ключевые фразы — по кампаниям с расходом (щадяще, с паузами),
        # каждая кампания дозаписывается в БД по мере готовности
        with_spend = sorted([c for c in campaigns if c["spend"] > 100],
                            key=lambda x: -x["spend"])[:15]
        for i, c in enumerate(with_spend):
            _adv_progress = f"фразы {i + 1}/{len(with_spend)}: {c['name'][:30]}"
            try:
                words = await ac.get_campaign_words(c["id"], c.get("type_id"))
            except Exception:
                words = []
            for w in words:
                # кандидат в минус: тратит, почти не кликают, заказов не видно
                w["flag"] = ("minus" if w.get("sum", 0) > 300 and w.get("ctr", 0) < 1.5
                             else "hot" if w.get("clicks", 0) >= 20 and w.get("ctr", 0) >= 4
                             else "")
            words.sort(key=lambda w: -(w.get("sum") or w.get("views") or 0))
            c["words"] = words[:40]
            await asyncio.to_thread(_save, c)
            await asyncio.sleep(2)

        # LLM-совет по оптимизации (одним вызовом)
        try:
            if ANTHROPIC_API_KEY and campaigns:
                _adv_progress = "советы Claude"
                table = "\n".join(
                    f"- {c['name']} [{c['type']}, {c['status']}]: расход {c['spend']}₽, "
                    f"показы {c['views']}, клики {c['clicks']} (CTR {c['ctr']}%), "
                    f"заказы {c['orders']} на {c['revenue']}₽, ДРР {c['drr'] or '—'}%"
                    for c in sorted(campaigns, key=lambda x: -x["spend"])[:20])
                waste_words = []
                for c in campaigns:
                    for w in c.get("words") or []:
                        if w.get("flag") == "minus":
                            waste_words.append(f"«{w['phrase']}» ({c['name'][:25]}): {round(w['sum'])}₽, CTR {w['ctr']}%")
                prompt = f"""Ты — специалист по рекламе Wildberries. Данные кампаний селлера за {_ADV_DAYS} дней:
{table}

Фразы-кандидаты в минус (тратят, не кликаются):
{chr(10).join(waste_words[:25]) or 'нет'}

Дай 3-6 конкретных рекомендаций по оптимизации: что остановить/минусовать, куда перераспределить бюджет, где потенциал масштабирования. Формат: маркированный список, каждая рекомендация 1-2 предложения с цифрами. Без воды и вступлений."""
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
                msg = await client.messages.create(model=_MODEL, max_tokens=900,
                                                   messages=[{"role": "user", "content": prompt}])
                advice = msg.content[0].text.strip()
                await asyncio.to_thread(
                    db.execute,
                    "INSERT INTO kv_cache (k, v) VALUES ('adv_advice', ?) "
                    "ON CONFLICT (k) DO UPDATE SET v = excluded.v",
                    (json.dumps({"text": advice, "at": datetime.utcnow().isoformat()},
                                ensure_ascii=False),))
        except Exception as e:
            _log.warning("adv advice: %s", e)
        _log.info("adv tool built: %d campaigns", len(campaigns))
    except Exception as e:
        _adv_error = str(e)[:300]
        _log.error("adv tool build: %s", e)
    finally:
        _adv_building = False
        _adv_progress = ""


@router.get("/adv")
async def get_adv(refresh: bool = Query(default=False)):
    """Контроль рекламы: кампании, ДРР, ключевые фразы, советы."""
    _adv_init()
    rows = await asyncio.to_thread(db.fetchall, "SELECT data, built_at FROM adv_tool")
    campaigns = []
    newest = ""
    for data, built in rows:
        try:
            campaigns.append(json.loads(data))
            newest = max(newest, built or "")
        except ValueError:
            pass
    stale = True
    if newest:
        try:
            stale = (datetime.utcnow() - datetime.fromisoformat(newest)).total_seconds() > 6 * 3600
        except ValueError:
            pass
    if (refresh or stale or not campaigns) and not _adv_building:
        _spawn(_adv_build_bg())
    advice = None
    row = await asyncio.to_thread(db.fetchone, "SELECT v FROM kv_cache WHERE k = 'adv_advice'")
    if row:
        try:
            advice = json.loads(row[0])
        except ValueError:
            pass
    campaigns.sort(key=lambda c: (-c.get("active", False), -(c.get("spend") or 0)))
    total_spend = round(sum(c.get("spend") or 0 for c in campaigns))
    total_rev = round(sum(c.get("revenue") or 0 for c in campaigns))
    waste = round(sum(c.get("spend") or 0 for c in campaigns if c.get("verdict") == "waste"))
    return {"campaigns": campaigns, "days": _ADV_DAYS,
            "total_spend": total_spend, "total_revenue": total_rev,
            "total_drr": round(total_spend / total_rev * 100, 1) if total_rev else None,
            "waste": waste,
            "advice": advice,
            "building": _adv_building, "progress": _adv_progress, "error": _adv_error,
            "built_at": newest[:16].replace("T", " ")}


# ══ Калькулятор ниши: выходить с товаром или нет ═══════════════════════════════

_NICHE_REVIEW_RATE = 0.04   # ~4% покупателей оставляют отзыв (отраслевая оценка)


def _niche_init():
    db.execute("CREATE TABLE IF NOT EXISTS niche_snapshots "
               "(query TEXT, nm_id BIGINT, feedbacks INTEGER, price REAL, "
               "snap_date TEXT, PRIMARY KEY (query, nm_id, snap_date))")
    db.execute("CREATE TABLE IF NOT EXISTS niche_history "
               "(query TEXT PRIMARY KEY, data TEXT, built_at TEXT)")


_niche_last_body = ""   # сырое тело последнего ответа поиска (для диагностики)

# WB жёстко банит IP за очереди запросов; после серии 429 бан «липкий»
# и держится часами. Темп по умолчанию — 1 запрос в 45 сек (можно менять
# переменной окружения WB_SEARCH_INTERVAL без деплоя).
_wb_search_lock = asyncio.Lock()
_wb_search_last: float = 0.0
# 45с было под сожжённый серверный IP; на резидентском прокси хватает 15с
_WB_SEARCH_INTERVAL = float(os.getenv("WB_SEARCH_INTERVAL", "15") or 15)


async def _wb_throttle():
    global _wb_search_last
    import time as _t
    async with _wb_search_lock:
        wait = _WB_SEARCH_INTERVAL - (_t.monotonic() - _wb_search_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _wb_search_last = _t.monotonic()


def _parse_loose(text: str) -> dict | None:
    """Максимально терпеливый разбор ответа WB search.

    Тело бывает испорчено: прокси оставляет маркеры chunked-передачи,
    а анти-бот WB дописывает к metadata-JSON хвост «Not Found». Пробуем
    по очереди: чистый json → чистка чанков → raw_decode (берёт первый
    валидный JSON-объект и игнорирует мусорный хвост)."""
    import re as _re
    for candidate in (text, _re.sub(r"\r?\n[0-9a-fA-F]{1,6}\r?\n", "", text)):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate.lstrip())
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    return None


async def _wb_search_preset(client, params: dict, meta_text: str, ver: str = "v13") -> dict | None:
    """Preset-ответ WB: metadata несёт catalog_value=preset=N — сама выдача
    отдаётся тем же эндпоинтом при повторе с параметром preset."""
    import re as _re
    global _niche_last_body
    m = _re.search(r'"catalog_value":"preset=(\d+)"', meta_text)
    if not m:
        return None
    preset_id = m.group(1)
    for drop_query in (False, True):
        p2 = dict(params)
        p2["preset"] = preset_id
        if drop_query:
            p2.pop("query", None)
        # без троттлинга: браузер шлёт follow-up мгновенно, пауза только
        # протухшивает токены и провоцирует анти-бот на приманку
        await asyncio.sleep(1.0)
        try:
            r = await client.get(
                f"https://search.wb.ru/exactmatch/ru/common/{ver}/search", params=p2)
            _niche_last_body += (f"\n\n=== PRESET {preset_id}{' (без query)' if drop_query else ''}"
                                 f" → HTTP {r.status_code} ===\n" + r.text[:1500])
            if not r.is_success:
                continue
            payload = _parse_loose(r.text)
            if payload and ((payload.get("data") or {}).get("products")):
                return payload
        except Exception as e:
            _niche_last_body += f"\n\n=== PRESET → EXC {str(e)[:200]} ==="
    return None


async def _wb_search_stage2(client, params: dict, meta_text: str, ver: str = "v13") -> dict | None:
    """Анти-бот WB: metadata-ответ несёт токены qv/kcl — повторяем запрос
    с ними. Иногда и второй ответ metadata-только (с новыми токенами),
    поэтому идём до 3 переходов, каждый раз со свежими qv/kcl."""
    import re as _re
    global _niche_last_body
    body = meta_text
    for hop in range(1, 4):
        qv = _re.search(r'"qv":"([^"]+)"', body)
        if not qv:
            return None
        p2 = dict(params)
        p2["qv"] = qv.group(1)
        kcl = _re.search(r'"kcl":"([^"]+)"', body)
        if kcl:
            p2["kcl"] = kcl.group(1)
        # qv-токен короткоживущий: повтор должен уйти сразу, как в браузере
        await asyncio.sleep(0.7)
        try:
            r = await client.get(
                f"https://search.wb.ru/exactmatch/ru/common/{ver}/search", params=p2)
            _niche_last_body += f"\n\n=== STAGE2 hop{hop} qv → HTTP {r.status_code} ===\n" + r.text[:1500]
            if not r.is_success:
                return None
            payload = _parse_loose(r.text)
            if payload and ((payload.get("data") or {}).get("products")):
                return payload
            # metadata снова — берём свежие токены из нового тела и повторяем
            new_body = r.text
            if new_body == body:
                return None
            body = new_body
        except Exception as e:
            _niche_last_body += f"\n\n=== STAGE2 hop{hop} qv → EXC {str(e)[:200]} ==="
            return None
    return None


async def _wb_public_search(query: str, limit: int = 60) -> tuple[list[dict], int]:
    """Публичная выдача WB по запросу → (товары, всего найдено).

    Тот же эндпоинт, что использует сайт wildberries.ru (и все сервисы
    аналитики). Формат периодически меняется — пробуем v5 и v4."""
    import httpx
    global _niche_last_err, _niche_last_body
    _niche_last_err = ""
    import os
    proxy = os.getenv("WB_SEARCH_PROXY", "").strip() or None

    # Сайт WB (июль 2026) получает выдачу через v18 c appType=64 — старый
    # v13/appType=1 отдаёт всем preset-заглушку с qv (снято с DevTools).
    # Пробуем варианты от нового к старому; для старого остаётся qv-цепочка.
    variants = [("v18", 64), ("v13", 64), ("v13", 1)]
    env_ver = os.getenv("WB_SEARCH_VER", "").strip()
    if env_ver:
        variants.insert(0, (env_ver, int(os.getenv("WB_SEARCH_APPTYPE", "64") or 64)))

    def _mk_params(ver: str, apptype: int) -> dict:
        p = {"ab_testing": "false", "appType": apptype, "curr": "rub",
             "dest": -1257786, "sort": "popular", "resultset": "catalog",
             "page": 1, "spp": 30, "lang": "ru", "locale": "ru",
             "suppressSpellcheck": "false", "query": query}
        if ver >= "v18":
            p["hide_dtype"] = 15   # как шлёт сайт
        else:
            p["reg"] = 1
            p["regions"] = "80,38,83,4,64,33,68,70,30,40,86,75,69,1,66,110,22,31,48,71,114"
        return p

    async with httpx.AsyncClient(timeout=30, proxy=proxy, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "Accept": "*/*", "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/catalog/0/search.aspx",
            "x-requested-with": "XMLHttpRequest",
            "Accept-Encoding": "identity"}) as client:
        _niche_last_body = ""
        for ver, apptype in variants:
            params = _mk_params(ver, apptype)
            tag = f"{ver}/appType={apptype}"
            try:
                await _wb_throttle()
                r = await client.get(
                    f"https://search.wb.ru/exactmatch/ru/common/{ver}/search", params=params)
            except Exception as e:
                _niche_last_err = f"{tag}: {str(e)[:120]}"
                continue
            _niche_last_body += f"\n\n=== {tag} → HTTP {r.status_code} ===\n" + r.text[:4000]
            if r.status_code == 429:
                _niche_last_err = ("HTTP 429 — WB ограничивает IP; дайте инструменту "
                                   "пару минут отдыха")
                break   # с этого IP сейчас всё будет 429 — варианты не помогут
            if not r.is_success:
                _niche_last_err = f"{tag}: HTTP {r.status_code}"
                continue
            try:
                txt = r.text
                payload = _parse_loose(txt)
                data = (payload.get("data") or {}) if payload else {}
                products = data.get("products") or []
                if not products and '"qv"' in txt:
                    p2 = await _wb_search_stage2(client, params, txt, ver)
                    if p2:
                        data = p2.get("data") or {}
                        products = data.get("products") or []
                if not products and '"catalog_value":"preset=' in txt:
                    p3 = await _wb_search_preset(client, params, txt, ver)
                    if p3:
                        data = p3.get("data") or {}
                        products = data.get("products") or []
                if products:
                    total = int(data.get("total") or len(products))
                    out = []
                    for p in products[:limit]:
                        price = None
                        for sz in p.get("sizes") or []:
                            pr = (sz.get("price") or {}).get("product")
                            if pr:
                                price = pr / 100
                                break
                        if price is None and p.get("salePriceU"):
                            price = p["salePriceU"] / 100
                        out.append({
                            "nm": p.get("id"),
                            "name": p.get("name") or "",
                            "brand": p.get("brand") or "",
                            "price": round(price) if price else None,
                            "rating": p.get("reviewRating") or p.get("rating") or 0,
                            "feedbacks": int(p.get("feedbacks") or 0),
                            "subject_id": p.get("subjectId"),
                            "supplier": p.get("supplier") or "",
                        })
                    if out:
                        return out, total
                    _niche_last_err = f"{tag}: пустая выдача"
                elif '"qv"' in txt or '"preset=' in txt:
                    _niche_last_err = (f"{tag}: только анти-бот metadata — пробуем "
                                       "следующий вариант")
                else:
                    _niche_last_err = f"{tag}: пустая выдача"
            except Exception as e:
                _log.warning("wb public search %s: %s", tag, e)
                _niche_last_err = f"{tag}: {str(e)[:120]}"
    return [], 0


_niche_last_err = ""


@router.get("/niche/last_response", include_in_schema=False)
async def niche_last_response():
    """Сырое тело последнего ответа поиска WB — без нового запроса."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_niche_last_body or "ещё не было запросов")


@router.get("/niche/proxycheck", include_in_schema=False)
async def niche_proxycheck():
    """Каким выходным IP ходим в WB: маскированный прокси из env + egress-IP.

    Проверка идёт на нейтральный ipify (не WB), лимиты не жжёт."""
    import httpx
    import os
    proxy = os.getenv("WB_SEARCH_PROXY", "").strip() or None
    masked = None
    if proxy:
        # прячем пароль: http://user:pass@host:port → http://user:***@host:port
        import re as _re
        masked = _re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", proxy)
    out = {"proxy_env": masked or "не задан (прямое соединение)"}
    try:
        async with httpx.AsyncClient(timeout=20, proxy=proxy) as client:
            r = await client.get("https://api.ipify.org?format=json")
            out["egress_ip"] = r.json().get("ip")
        try:
            async with httpx.AsyncClient(timeout=20, proxy=proxy) as client:
                g = await client.get(f"http://ip-api.com/json/{out['egress_ip']}?fields=country,isp")
                out.update(g.json())
        except Exception:
            pass
    except Exception as e:
        out["error"] = f"через прокси не удалось выйти в сеть: {str(e)[:200]}"
    return out


@router.get("/niche/debug", include_in_schema=False)
async def niche_debug(query: str = Query(default="крем для лица"),
                      full: bool = Query(default=False)):
    """Диагностика публичного поиска WB: статусы по версиям API."""
    import httpx
    import os
    proxy = os.getenv("WB_SEARCH_PROXY", "").strip() or None
    out = {"proxy": "настроен" if proxy else "нет"}
    params = {"ab_testing": "false", "appType": 1, "curr": "rub", "dest": -1257786,
              "sort": "popular", "resultset": "catalog", "page": 1, "spp": 30,
              "lang": "ru", "locale": "ru", "reg": 1,
              "regions": "80,38,83,4,64,33,68,70,30,40,86,75,69,1,66,110,22,31,48,71,114",
              "query": query}
    async with httpx.AsyncClient(timeout=20, proxy=proxy, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.wildberries.ru/",
            "Accept-Encoding": "gzip, deflate"}) as client:
        for ver in ("v13",):   # только один запрос — очереди выжигают лимит WB
            try:
                await _wb_throttle()
                r = await client.get(f"https://search.wb.ru/exactmatch/ru/common/{ver}/search",
                                     params=params)
                body = r.text[:300]
                n = 0
                try:
                    n = len((r.json().get("data") or {}).get("products") or [])
                except Exception:
                    pass
                out[ver] = {"status": r.status_code, "products": n,
                            "body_head": r.text[:4000] if full else body}
            except Exception as e:
                out[ver] = {"error": str(e)[:200]}
    return out


_commission_cache: dict = {}


async def _wb_commission(subject_id: int | None) -> float | None:
    """Комиссия WB (FBW, %) по предмету — официальный API тарифов."""
    global _commission_cache
    if not subject_id:
        return None
    if not _commission_cache:
        try:
            import httpx
            from config import WB_API_KEY
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    "https://common-api.wildberries.ru/api/v1/tariffs/commission",
                    headers={"Authorization": WB_API_KEY}, params={"locale": "ru"})
                for rep in (r.json().get("report") or []):
                    sid = rep.get("subjectID")
                    if sid:
                        _commission_cache[int(sid)] = float(
                            rep.get("kgvpMarketplace") or rep.get("paidStorageKgvp") or 0)
        except Exception as e:
            _log.warning("wb tariffs: %s", e)
            _commission_cache = {0: 0.0}
    return _commission_cache.get(int(subject_id))


_niche_job: dict = {"status": "idle"}

# ── Локальный агент (сбор выдачи WB с домашнего IP пользователя) ──────────────
# WB банит IP дата-центров/прокси, но домашний IP пользователя пускает.
# Агент на ПК пользователя опрашивает /niche/pending, тянет выдачу и
# отдаёт её в /niche/ingest. Очередь и «почтовый ящик» — в памяти.
_agent_pending: dict = {}     # query -> запрошено (monotonic)
_agent_inbox: dict = {}       # query -> {products, total} или {error}


@router.get("/niche/pending")
async def niche_pending(token: str = ""):
    """Список запросов, ждущих сбора локальным агентом."""
    if os.getenv("WB_AGENT_TOKEN", "") and token != os.getenv("WB_AGENT_TOKEN", ""):
        raise HTTPException(status_code=401, detail="bad token")
    now = _t_mono()
    # чистим протухшие (агент офлайн > 5 мин)
    for q in [q for q, ts in _agent_pending.items() if now - ts > 300]:
        _agent_pending.pop(q, None)
    return {"queries": list(_agent_pending.keys())}


@router.post("/niche/ingest")
async def niche_ingest(payload: dict, token: str = ""):
    """Агент отдаёт собранную выдачу: {query, products:[...], total} или {query, error}."""
    if os.getenv("WB_AGENT_TOKEN", "") and token != os.getenv("WB_AGENT_TOKEN", ""):
        raise HTTPException(status_code=401, detail="bad token")
    query = str(payload.get("query") or "").strip().lower()
    if not query:
        raise HTTPException(status_code=400, detail="query required")
    _agent_pending.pop(query, None)
    if payload.get("error"):
        _agent_inbox[query] = {"error": str(payload["error"])[:300]}
    else:
        _agent_inbox[query] = {"products": payload.get("products") or [],
                               "total": int(payload.get("total") or 0)}
    return {"ok": True}


def _t_mono() -> float:
    import time as _t
    return _t.monotonic()


async def _wb_agent_search(query: str) -> tuple[list, int]:
    """Ставит запрос в очередь агента и ждёт результат (агент на ПК юзера)."""
    global _niche_last_err
    _agent_inbox.pop(query, None)
    _agent_pending[query] = _t_mono()
    for _ in range(60):            # ~3 мин ожидания результата от агента
        await asyncio.sleep(3)
        res = _agent_inbox.pop(query, None)
        if res is None:
            continue
        if res.get("error"):
            _niche_last_err = f"агент: {res['error']}"
            return [], 0
        return res.get("products") or [], int(res.get("total") or 0)
    _agent_pending.pop(query, None)
    _niche_last_err = ("локальный агент не ответил за 3 мин — запущен ли он на ПК "
                       "и открыт ли доступ к WB?")
    return [], 0


def _niche_relevant(products: list, query: str) -> bool:
    """Анти-бот WB иногда отдаёт «приманку» — 1-2 случайных товара (MacBook
    на запрос про крем). Хоть какие-то товары должны содержать слова запроса."""
    q_tokens = [w[:max(4, len(w) - 2)].lower() for w in query.split() if len(w) > 3]
    if not q_tokens:
        return True
    return any(any(t in (p.get("name") or "").lower() for t in q_tokens)
               for p in products)


async def _wb_fetch_search(query: str, limit: int = 60) -> tuple[list, int]:
    """Выдача через сервис wb-fetch (headless-Chrome проходит анти-бот WB).

    Холодный старт браузера на free-CPU занимает до 2-3 минут — таймаут
    щедрый, прогресс виден в стадии задачи."""
    global _niche_last_err, _niche_last_body
    import os
    fetch_url = os.getenv("WB_FETCH_URL", "").strip()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=300) as fc:
            fr = await fc.get(fetch_url.rstrip("/") + "/search",
                              params={"query": query, "limit": limit,
                                      "token": os.getenv("WB_FETCH_TOKEN", "")})
            fj = fr.json()
            if fj.get("ok") and fj.get("products"):
                _niche_last_body = (f"WB-FETCH (headless): {len(fj['products'])} "
                                    f"товаров, total={fj.get('total')}")
                return fj["products"], int(fj.get("total") or len(fj["products"]))
            _niche_last_err = f"wb-fetch: {fj.get('error') or f'HTTP {fr.status_code}, пусто'}"
            _niche_last_body = f"WB-FETCH ответ: {str(fj)[:1200]}"
    except Exception as e:
        _niche_last_err = f"wb-fetch недоступен: {str(e)[:150]}"
        _niche_last_body = f"WB-FETCH exc: {str(e)[:400]}"
    return [], 0


async def _niche_job_run(payload: dict, query: str) -> None:
    """Фоновый анализ ниши с автоповторами.

    Многоступенчатая схема (qv/preset, троттлинг, ретраи при приманке)
    занимает больше 100с — лимита запроса на Render, поэтому анализ идёт
    фоном, а фронт опрашивает /niche/status."""
    global _niche_job, _niche_last_err
    import os
    try:
        # Локальный агент (домашний IP пользователя) — приоритетный путь:
        # WB банит IP серверов/прокси, но домашний адрес пускает.
        if os.getenv("WB_AGENT_MODE", "").strip() == "1":
            for attempt in (1, 2):
                _niche_job["stage"] = (f"ждём локальный агент — сбор выдачи WB "
                                       f"с вашего ПК (попытка {attempt}/2)")
                products, total = await _wb_agent_search(query)
                if products and not _niche_relevant(products, query):
                    _niche_last_err = "агент получил нерелевантную выдачу"
                    products = []
                if products:
                    _niche_job["stage"] = "выдача получена — считаем метрики и вердикт"
                    await _analyze_niche_impl(payload, query, products, total)
                    _niche_job = {"status": "done", "query": query}
                    return
                if attempt == 1:
                    await asyncio.sleep(5)
            _niche_job = {"status": "error", "query": query,
                          "error": _niche_last_err or "агент не дал выдачу"}
            return

        # wb-fetch настроен → он единственный путь: прямые запросы всё равно
        # получают 429/заглушку, а перебор только затягивает и жжёт лимиты
        if os.getenv("WB_FETCH_URL", "").strip():
            for attempt in (1, 2):
                _niche_job["stage"] = (f"wb-fetch: браузер открывает WB "
                                       f"(попытка {attempt}/2; холодный старт — до 2-3 мин)")
                products, total = await _wb_fetch_search(query)
                if products and not _niche_relevant(products, query):
                    _niche_last_err = "браузер получил нерелевантную выдачу"
                    products = []
                if products:
                    _niche_job["stage"] = "выдача получена — считаем метрики и вердикт"
                    await _analyze_niche_impl(payload, query, products, total)
                    _niche_job = {"status": "done", "query": query}
                    return
                if attempt == 1:
                    _niche_job["stage"] = f"{_niche_last_err} — повтор через 45с"
                    await asyncio.sleep(45)
            _niche_job = {"status": "error", "query": query,
                          "error": (_niche_last_err or "wb-fetch не дал выдачу")
                          + ". Смотрите лог сервиса wb-fetch в Render."}
            return

        last_reason = ""
        for attempt in range(1, 4):
            _niche_job["stage"] = f"запрашиваем выдачу WB (попытка {attempt}/3)"
            products, total = await _wb_public_search(query)
            if products and not _niche_relevant(products, query):
                last_reason = "WB подсовывает нерелевантную приманку вместо выдачи"
                products = []
            elif not products:
                last_reason = _niche_last_err or "пустая выдача"
            if products:
                _niche_job["stage"] = "выдача получена — считаем метрики и вердикт"
                await _analyze_niche_impl(payload, query, products, total)
                _niche_job = {"status": "done", "query": query}
                return
            if "429" in last_reason:
                break   # бан по IP держится часами — повторы его только продлевают
            if attempt < 3:
                _niche_job["stage"] = f"{last_reason} — пауза и новая попытка ({attempt}/3)"
                await asyncio.sleep(30)
        hint = (" WB ограничивает IP: подождите час-другой или смените IP прокси "
                "в ЛК proxy6." if "429" in last_reason else
                " Если повторяется на каждой попытке — WB пометил IP прокси как бота; "
                "надёжное лечение — резидентский прокси.")
        _niche_job = {"status": "error", "query": query, "error": last_reason + hint}
    except Exception as e:
        _log.warning("niche job: %s", e)
        _niche_job = {"status": "error", "query": query, "error": str(e)[:300]}


@router.post("/niche")
async def analyze_niche(payload: dict):
    """Скоринг ниши по поисковому запросу WB.

    {query, price?, cost?, logistics?} → запускает фоновый анализ
    (конкуренты, метрики ниши, юнит-прикидка, вердикт Claude); статус —
    в /niche/status, результат — в /niche/get."""
    query = str(payload.get("query") or "").strip().lower()
    if not query:
        raise HTTPException(status_code=400, detail="Введите поисковый запрос")
    _niche_init()
    global _niche_job
    if _niche_job.get("status") == "running":
        return {"building": True, "query": _niche_job.get("query"),
                "stage": _niche_job.get("stage", "")}
    _niche_job = {"status": "running", "query": query, "stage": "старт"}
    _spawn(_niche_job_run(payload, query))
    return {"building": True, "query": query, "stage": "старт"}


@router.get("/niche/status")
async def niche_status():
    """Состояние фонового анализа ниши."""
    return dict(_niche_job)


async def _analyze_niche_impl(payload: dict, query: str,
                              products: list, total: int) -> dict:
    today = datetime.utcnow().date().isoformat()
    # прошлые замеры — для оценки продаж по приросту отзывов
    prev_rows = await asyncio.to_thread(
        db.fetchall,
        "SELECT nm_id, feedbacks, snap_date FROM niche_snapshots "
        "WHERE query = ? AND snap_date < ? ORDER BY snap_date", (query, today))
    prev: dict[int, tuple] = {}
    for nm, fb, dt_ in prev_rows:
        prev[int(nm)] = (int(fb or 0), dt_)   # берём самый старый→последняя запись перезапишет? нет: оставляем первый
    # (первый замер по nm — самая старая точка)
    first_seen: dict[int, tuple] = {}
    for nm, fb, dt_ in prev_rows:
        if int(nm) not in first_seen:
            first_seen[int(nm)] = (int(fb or 0), dt_)

    sales_est_available = False
    for p in products:
        p["sales_month_est"] = None
        base = first_seen.get(int(p["nm"] or 0))
        if base:
            fb0, dt0 = base
            try:
                days = (datetime.utcnow().date() - datetime.fromisoformat(dt0).date()).days
            except ValueError:
                days = 0
            if days >= 3:
                delta = max(0, p["feedbacks"] - fb0)
                p["sales_month_est"] = round(delta / days * 30 / _NICHE_REVIEW_RATE)
                sales_est_available = True

    # сохраняем сегодняшний снимок
    snap_rows = [(query, int(p["nm"]), p["feedbacks"], float(p["price"] or 0), today)
                 for p in products if p.get("nm")]
    await asyncio.to_thread(
        db.executemany,
        "INSERT INTO niche_snapshots (query, nm_id, feedbacks, price, snap_date) "
        "VALUES (?,?,?,?,?) ON CONFLICT (query, nm_id, snap_date) DO UPDATE "
        "SET feedbacks = excluded.feedbacks, price = excluded.price", snap_rows)

    # ── метрики ниши ──
    prices = sorted(p["price"] for p in products if p["price"])
    med_price = prices[len(prices) // 2] if prices else 0
    top30 = products[:30]
    fb_total = sum(p["feedbacks"] for p in top30)
    brand_fb: dict[str, int] = {}
    for p in top30:
        brand_fb[p["brand"]] = brand_fb.get(p["brand"], 0) + p["feedbacks"]
    top3_share = round(sum(sorted(brand_fb.values(), reverse=True)[:3]) / fb_total * 100) if fb_total else 0
    newcomers = sum(1 for p in top30 if p["feedbacks"] < 50)
    avg_rating = round(sum(float(p["rating"] or 0) for p in top30) / len(top30), 2) if top30 else 0

    # ── юнит-прикидка ──
    price = float(payload.get("price") or 0) or med_price
    cost = float(payload.get("cost") or 0)
    logistics = float(payload.get("logistics") or 70)
    subj = next((p["subject_id"] for p in top30 if p.get("subject_id")), None)
    commission_pct = await _wb_commission(subj)
    unit = None
    if price:
        comm = price * (commission_pct or 19) / 100
        profit = price - comm - logistics - cost
        unit = {"price": round(price), "commission_pct": commission_pct or 19,
                "commission": round(comm), "logistics": round(logistics),
                "cost": round(cost),
                "profit": round(profit),
                "margin": round(profit / price * 100) if price else 0}

    # ── вердикт Claude ──
    verdict = None
    try:
        if ANTHROPIC_API_KEY:
            comp = "\n".join(
                f"- {p['brand']} «{p['name'][:50]}»: {p['price']}₽, ★{p['rating']}, отзывов {p['feedbacks']}"
                + (f", ~{p['sales_month_est']} прод/мес" if p.get("sales_month_est") else "")
                for p in top30[:20])
            prompt = f"""Ты — аналитик маркетплейсов. Оцени, стоит ли селлеру выходить на Wildberries с товаром по запросу «{query}».

НИША: найдено {total} товаров. Медианная цена {med_price}₽. Средний рейтинг топ-30: {avg_rating}. Суммарно отзывов у топ-30: {fb_total}. Доля топ-3 брендов: {top3_share}% (монополизация). Карточек с <50 отзывов в топ-30: {newcomers} (шанс новичку).
{'ЮНИТ-ПРИКИДКА: цена ' + str(unit['price']) + '₽, комиссия ' + str(unit['commission_pct']) + '%, логистика ' + str(unit['logistics']) + '₽, себес ' + str(unit['cost']) + '₽ → прибыль ' + str(unit['profit']) + '₽ (' + str(unit['margin']) + '%)' if unit and cost else ''}

ТОП КОНКУРЕНТОВ:
{comp}

Дай вердикт: 1) итог одной строкой (ИДТИ / ИДТИ ОСТОРОЖНО / НЕ ИДТИ + почему); 2) 3-4 пункта обоснования с цифрами; 3) при каких условиях заходить (цена, на что давить). Кратко, без воды."""
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            msg = await client.messages.create(model=_MODEL, max_tokens=800,
                                               messages=[{"role": "user", "content": prompt}])
            verdict = msg.content[0].text.strip()
    except Exception as e:
        _log.warning("niche verdict: %s", e)

    result = {
        "query": query, "total": total,
        "median_price": med_price,
        "price_min": prices[0] if prices else None,
        "price_max": prices[-1] if prices else None,
        "avg_rating": avg_rating,
        "feedbacks_top30": fb_total,
        "top3_brand_share": top3_share,
        "newcomers_top30": newcomers,
        "sales_est_available": sales_est_available,
        "unit": unit,
        "verdict": verdict,
        "products": products[:30],
        "analyzed_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC"),
    }
    await asyncio.to_thread(
        db.execute,
        "INSERT INTO niche_history (query, data, built_at) VALUES (?,?,?) "
        "ON CONFLICT (query) DO UPDATE SET data = excluded.data, built_at = excluded.built_at",
        (query, json.dumps(result, ensure_ascii=False), datetime.utcnow().isoformat()))
    return result


@router.get("/niche/history")
async def niche_history():
    """Последние проанализированные ниши."""
    _niche_init()
    rows = await asyncio.to_thread(
        db.fetchall, "SELECT query, built_at FROM niche_history ORDER BY built_at DESC")
    return {"items": [{"query": r[0], "built_at": (r[1] or "")[:16].replace("T", " ")}
                      for r in rows[:20]]}


@router.get("/niche/get")
async def niche_get(query: str = Query(...)):
    """Сохранённый анализ ниши (без пересчёта)."""
    _niche_init()
    row = await asyncio.to_thread(
        db.fetchone, "SELECT data FROM niche_history WHERE query = ?", (query.strip().lower(),))
    if not row:
        raise HTTPException(status_code=404, detail="Нет сохранённого анализа")
    return json.loads(row[0])


# ══ Воронка Ozon (Premium-аналитика) ═══════════════════════════════════════════

_funnel_cache: dict = {}
_funnel_ts: float = 0.0

# метрики Premium-аналитики Ozon: воронка от показа до заказа
_FUNNEL_METRICS = ["hits_view_search", "hits_view_pdp", "hits_tocart_pdp",
                   "ordered_units", "revenue", "position_category", "session_view"]


def _funnel_bottleneck(m: dict) -> tuple[str, str]:
    """Где товар теряет продажи: (код, пояснение)."""
    search = m.get("hits_view_search") or 0
    pdp = m.get("hits_view_pdp") or 0
    tocart = m.get("hits_tocart_pdp") or 0
    orders = m.get("ordered_units") or 0
    if search < 300:
        return "visibility", "мало показов — усилить SEO карточки/рекламу"
    ctr = pdp / search * 100 if search else 0
    if ctr < 2:
        return "ctr", f"показы есть, в карточку идут {ctr:.1f}% — главное фото/цена в выдаче"
    cart = tocart / pdp * 100 if pdp else 0
    if cart < 5:
        return "cart", f"карточку смотрят, в корзину кладут {cart:.1f}% — контент/отзывы/цена"
    buy = orders / tocart * 100 if tocart else 0
    if buy < 25:
        return "checkout", f"из корзины выкупают {buy:.0f}% — сроки доставки/конкуренты в корзине"
    return "ok", "воронка здоровая"


@router.get("/funnel")
async def get_funnel(refresh: bool = Query(default=False)):
    """Воронка Ozon по SKU: показы → карточка → корзина → заказ (Premium)."""
    import time as _t
    global _funnel_cache, _funnel_ts
    if not refresh and _funnel_cache and _t.monotonic() - _funnel_ts < 6 * 3600:
        return _funnel_cache

    # холодный старт: последняя воронка из БД (свежесть 10 мин), потом обновим
    if not refresh and not _funnel_cache:
        import snapshot as _snapmod
        snap = await asyncio.to_thread(_snapmod.load, "oz_funnel", None)
        if snap:
            _funnel_cache = snap
            _funnel_ts = _t.monotonic() - 6 * 3600 + 600
            return snap

    import ozon_client
    import catalog as _cat
    from datetime import date
    d_to = date.today().isoformat()
    d_from = (date.today() - timedelta(days=28)).isoformat()
    try:
        rows = await ozon_client.get_analytics_data(d_from, d_to, _FUNNEL_METRICS)
    except Exception as e:
        msg = str(e)[:300]
        hint = (" Метрики воронки доступны с подпиской Ozon Premium — проверьте, "
                "что она активна и у API-ключа есть роль «Аналитика»."
                if "403" in msg or "premium" in msg.lower() else "")
        return {"items": [], "error": msg + hint}

    items = []
    for r in rows:
        m = r["metrics"]
        if not any(m.values()):
            continue
        sku = _cat.resolve_ozon(r["sku"]) if str(r["sku"]).isdigit() else str(r["sku"])
        search = m.get("hits_view_search") or 0
        pdp = m.get("hits_view_pdp") or 0
        tocart = m.get("hits_tocart_pdp") or 0
        orders = m.get("ordered_units") or 0
        code, why = _funnel_bottleneck(m)
        items.append({
            "sku": sku,
            "name": _cat.lookup(sku).get("name") or r["name"] or sku,
            "group": _cat.lookup(sku).get("brand", ""),
            "search": int(search), "pdp": int(pdp), "tocart": int(tocart),
            "orders": int(orders),
            "revenue": round(m.get("revenue") or 0),
            "sessions": int(m.get("session_view") or 0),
            "position": round(m.get("position_category") or 0),
            "ctr": round(pdp / search * 100, 1) if search else None,
            "cart_pct": round(tocart / pdp * 100, 1) if pdp else None,
            "buy_pct": round(orders / tocart * 100, 1) if tocart else None,
            "bottleneck": code, "bottleneck_why": why,
        })
    order = {"visibility": 0, "ctr": 1, "cart": 2, "checkout": 3, "ok": 4}
    items.sort(key=lambda x: (order.get(x["bottleneck"], 9), -x["revenue"]))
    result = {"items": items, "days": 28,
              "fetched_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")}
    _funnel_cache = result
    _funnel_ts = _t.monotonic()
    import snapshot as _snapmod
    await asyncio.to_thread(_snapmod.save, "oz_funnel", result)
    return result
