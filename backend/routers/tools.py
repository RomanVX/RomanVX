"""Инструменты WB. Первый: Продуктолог — анализ отзывов по каждому артикулу.

Из всех собранных отзывов WB по SKU строится сводка: сильные и слабые
стороны товара с частотой упоминаний (%), и рекомендация к доработке.
Анализ делает Claude, результат кэшируется в БД и пересобирается в фоне
только для артикулов, где появились новые отзывы.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Query

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
        for i, (sku, revs, n_texts) in enumerate(todo):
            _progress = f"{i + 1}/{len(todo)}: {sku}"
            name = _cat.lookup(sku).get("name", sku)
            try:
                data = await _analyze_sku(sku, name, revs)
            except Exception as e:
                _error = f"{sku}: {str(e)[:200]}"
                _log.warning("productolog %s: %s", sku, e)
                continue
            if not data:
                continue
            await asyncio.to_thread(
                db.execute,
                "INSERT INTO productolog (sku, data, reviews_count, built_at) VALUES (?,?,?,?) "
                "ON CONFLICT (sku) DO UPDATE SET data = excluded.data, "
                "reviews_count = excluded.reviews_count, built_at = excluded.built_at",
                (sku, json.dumps(data, ensure_ascii=False), n_texts,
                 datetime.utcnow().isoformat()))
        _log.info("productolog: обновлено %d SKU", len(todo))
    except Exception as e:
        _error = str(e)[:300]
        _log.error("productolog build: %s", e)
    finally:
        _building = False
        _progress = ""


# держим ссылки на фоновые задачи (иначе GC их убивает)
_bg: set = set()


def _spawn(coro):
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
        end = (datetime.utcnow() + timedelta(hours=3)).date()
        begin = end - timedelta(days=_ADV_DAYS)
        _adv_progress = "статистика кампаний (fullstats, ~минута)"
        stats = await ac.get_fullstats_campaigns(ids, begin.isoformat(), end.isoformat())

        campaigns = []
        for aid in ids:
            m = meta.get(aid) or {}
            s = stats.get(aid) or {}
            spend = round(float(s.get("sum") or 0), 2)
            status = m.get("status")
            if spend <= 0 and status not in (9, 11):
                continue   # старые пустые кампании не показываем
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
            })
        for c in campaigns:
            c["cpo"] = round(c["spend"] / c["orders"]) if c["orders"] else None
            c["drr"] = round(c["spend"] / c["revenue"] * 100, 1) if c["revenue"] else None
            v, why = _adv_verdict(c)
            c["verdict"], c["verdict_why"] = v, why

        # ключевые фразы — по кампаниям с расходом (щадяще, с паузами)
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
            await asyncio.sleep(2)

        # сохраняем
        for c in campaigns:
            await asyncio.to_thread(
                db.execute,
                "INSERT INTO adv_tool (campaign_id, data, built_at) VALUES (?,?,?) "
                "ON CONFLICT (campaign_id) DO UPDATE SET data = excluded.data, built_at = excluded.built_at",
                (c["id"], json.dumps(c, ensure_ascii=False), datetime.utcnow().isoformat()))

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
