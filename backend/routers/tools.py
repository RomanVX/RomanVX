"""Инструменты WB. Первый: Продуктолог — анализ отзывов по каждому артикулу.

Из всех собранных отзывов WB по SKU строится сводка: сильные и слабые
стороны товара с частотой упоминаний (%), и рекомендация к доработке.
Анализ делает Claude, результат кэшируется в БД и пересобирается в фоне
только для артикулов, где появились новые отзывы.
"""
import asyncio
import json
import logging
from datetime import datetime

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
