"""Новости площадок: сбор, хранение, разбор «чем это грозит нам».

Источники:
  WB      — официальный GET /api/communications/v2/news (новости портала).
  Кабинет — наши собственные события: изменение комиссии, тарифов, статусов.
Ozon отдельного метода новостей не имеет (только push на публичный URL),
поэтому его изменения ловим своими сторожами и кладём сюда же.

Каждая новость один раз прогоняется через Claude в связке с нашим
ассортиментом: важность (critical/important/background) и вывод, как это
касается нас. Разбор хранится в БД — повторно не платим.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

import db
from config import ANTHROPIC_API_KEY

_log = logging.getLogger("news")
_MODEL = "claude-haiku-4-5"   # классификация новостей: Opus не нужен
_COMMON = "https://common-api.wildberries.ru"


def _init() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS news (
        id TEXT PRIMARY KEY, source TEXT, published TEXT, title TEXT,
        body TEXT, tags TEXT, url TEXT,
        importance TEXT, impact TEXT, effective_date TEXT,
        analyzed TEXT, created TEXT)""")


def _msk() -> datetime:
    return datetime.utcnow() + timedelta(hours=3)


# ── сбор ─────────────────────────────────────────────────────────────────────
async def fetch_wb(days: int = 30) -> int:
    """Новости портала WB. Лимит метода — 1 запрос в минуту, зовём редко."""
    import httpx
    import wb_client
    since = (_msk() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get(f"{_COMMON}/api/communications/v2/news",
                            headers=wb_client._headers(), params={"from": since})
        if not r.is_success:
            _log.warning("wb news %s: %s", r.status_code, r.text[:200])
            return 0
        items = (r.json() or {}).get("data") or []
    except Exception as e:
        _log.warning("wb news: %s", e)
        return 0
    _init()
    added = 0
    for it in items:
        nid = f"wb_{it.get('id')}"
        if db.fetchone("SELECT id FROM news WHERE id = ?", (nid,)):
            continue
        tags = ", ".join(t.get("name", "") for t in (it.get("types") or []))
        db.execute(
            "INSERT INTO news (id, source, published, title, body, tags, url, created)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (nid, "WB", str(it.get("date") or "")[:19],
             (it.get("header") or "")[:400], (it.get("content") or "")[:6000],
             tags[:200], "https://seller.wildberries.ru/news",
             _msk().strftime("%Y-%m-%d %H:%M")))
        added += 1
    return added


def add_internal(key: str, title: str, body: str, source: str = "Кабинет",
                 effective: str = "") -> bool:
    """Наше собственное событие (сменилась комиссия, тариф, статус)."""
    _init()
    nid = f"in_{key}"
    if db.fetchone("SELECT id FROM news WHERE id = ?", (nid,)):
        return False
    db.execute(
        "INSERT INTO news (id, source, published, title, body, tags, url,"
        " effective_date, created) VALUES (?,?,?,?,?,?,?,?,?)",
        (nid, source, _msk().strftime("%Y-%m-%d %H:%M"), title[:400],
         body[:6000], "наш кабинет", "", effective[:10],
         _msk().strftime("%Y-%m-%d %H:%M")))
    return True


async def detect_ozon() -> int:
    """Ozon не отдаёт новости порталом — ловим его изменения сами:
    новые акции с датами автодобавления, сдвиг тарифов коробов."""
    import snapshot as _snap
    import ozon_client
    added = 0
    # 1) акции: появилась новая — это и есть «новость Ozon» с деньгами внутри
    try:
        acts = await ozon_client.get_actions()
        seen = set(await asyncio.to_thread(_snap.load, "news_oz_actions", None) or [])
        for a in acts:
            aid = str(a.get("id"))
            if not aid or aid in seen:
                continue
            seen.add(aid)
            auto = (a.get("auto_add_dates") or [])
            body = (f"{a.get('description') or ''}\n\n"
                    f"Период: {str(a.get('date_start'))[:10]} — "
                    f"{str(a.get('date_end'))[:10]}. "
                    f"Подходит наших товаров: {int(a.get('potential_products_count') or 0)}, "
                    f"уже участвует: {int(a.get('participating_products_count') or 0)}.")
            if auto:
                body += (f"\nАвтодобавление товаров: "
                         f"{', '.join(str(d)[:10] for d in auto[:3])} — "
                         "если не хотим, товары нужно исключить заранее.")
            if add_internal(f"oz_act_{aid}",
                            f"Ozon: акция «{a.get('title')}»", body,
                            source="Ozon",
                            effective=str(a.get("date_start") or "")[:10]):
                added += 1
        await asyncio.to_thread(_snap.save, "news_oz_actions", sorted(seen)[-400:])
    except Exception as e:
        _log.warning("ozon actions: %s", str(e)[:200])
    # 2) автоакции: Ozon включил автодобавление у товара — тихий срез маржи
    try:
        prices = await ozon_client.get_prices()
        on = sorted(a for a, p in (prices or {}).items()
                    if p.get("auto_action_enabled") or p.get("auto_action"))
        prev = set(await asyncio.to_thread(_snap.load, "news_oz_auto", None) or [])
        new_on = [a for a in on if a not in prev]
        if new_on and prev:      # первый прогон только запоминает состояние
            key = datetime.utcnow().strftime("%Y%m%d%H")
            if add_internal(
                    f"oz_auto_{key}",
                    "Ozon: включилось автодобавление в акции",
                    "Появилось автодобавление у SKU: " + ", ".join(new_on[:15]) +
                    ".\nOzon может сам опустить цену — проверь, что маржа "
                    "выдержит, или исключи товары из автодобавления.",
                    source="Ozon"):
                added += 1
        await asyncio.to_thread(_snap.save, "news_oz_auto", on)
    except Exception as e:
        _log.warning("ozon auto-action: %s", str(e)[:200])
    return added


# ── разбор влияния ───────────────────────────────────────────────────────────
async def _our_context() -> str:
    """Кто мы: категории, площадки, чувствительные места — для оценки влияния."""
    try:
        import catalog as _cat
        groups = sorted({v.get("articleGroup") or "" for v in
                         getattr(_cat, "SKU_MAP", {}).values()} - {""})
    except Exception:
        groups = []
    return ("Мы — селлер Biomed Nutrition: собственное производство, "
            "~40 SKU, площадки WB, Ozon, Яндекс.Маркет. Категории: "
            + (", ".join(groups[:12]) if groups else "лубриканты, косметика, БАДы")
            + ". Модель FBO/FBW, активная реклама на обеих площадках, "
            "репрайсер, регулярные поставки через ПВЗ.")


async def analyze_new(limit: int = 12) -> int:
    """Прогнать неразобранные новости через модель: важность + вывод для нас."""
    _init()
    rows = db.fetchall(
        "SELECT id, source, published, title, body, tags FROM news "
        "WHERE analyzed IS NULL OR analyzed = '' ORDER BY published DESC")
    rows = rows[:limit]
    if not rows or not ANTHROPIC_API_KEY:
        return 0
    ctx = await _our_context()
    items = [{"id": r[0], "источник": r[1], "дата": r[2],
              "заголовок": r[3], "текст": (r[4] or "")[:1800], "теги": r[5]}
             for r in rows]
    system = (
        "Ты — аналитик селлера маркетплейсов. Оцениваешь новости площадок: "
        "что реально меняется и как это касается конкретно нас.\n" + ctx + "\n\n"
        "На каждую новость верни JSON-объект: id, importance "
        "(critical — бьёт по деньгам или требует действия в ближайшие дни; "
        "important — стоит учесть в планах; background — к сведению), "
        "impact — 1-2 предложения ЖИВЫМ языком: чем это грозит или что даёт "
        "именно нам, с конкретикой (какие категории, какие процессы). "
        "Если новость нас не касается — так и напиши, importance=background. "
        "effective_date — дата вступления в силу в формате YYYY-MM-DD, если "
        "она есть в тексте, иначе пустая строка.\n"
        "Ответ — ТОЛЬКО массив JSON, без пояснений и markdown.")
    try:
        import anthropic
        from config import ai_gate; ai_gate()   # экономия: только отзывы
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model=_MODEL, max_tokens=2500, system=system,
            messages=[{"role": "user", "content": json.dumps(items,
                                                             ensure_ascii=False)}])
        txt = msg.content[0].text.strip()
        if txt.startswith("```"):
            txt = txt.split("```")[1].lstrip("json").strip()
        verdicts = json.loads(txt)
    except Exception as e:
        _log.warning("analyze: %s", str(e)[:200])
        return 0
    now = _msk().strftime("%Y-%m-%d %H:%M")
    n = 0
    for v in verdicts if isinstance(verdicts, list) else []:
        imp = str(v.get("importance") or "background")
        if imp not in ("critical", "important", "background"):
            imp = "background"
        db.execute(
            "UPDATE news SET importance=?, impact=?, effective_date=?, analyzed=? "
            "WHERE id=?",
            (imp, str(v.get("impact") or "")[:900],
             str(v.get("effective_date") or "")[:10], now, str(v.get("id"))))
        n += 1
    return n


# ── чтение ───────────────────────────────────────────────────────────────────
def listing(days: int = 45, source: str = "", importance: str = "") -> list[dict]:
    _init()
    since = (_msk() - timedelta(days=days)).strftime("%Y-%m-%d")
    where, params = ["published >= ?"], [since]
    if source:
        where.append("source = ?"); params.append(source)
    if importance:
        where.append("importance = ?"); params.append(importance)
    rows = db.fetchall(
        "SELECT id, source, published, title, body, tags, url, importance, "
        "impact, effective_date FROM news WHERE " + " AND ".join(where) +
        " ORDER BY published DESC", tuple(params))
    keys = ["id", "source", "published", "title", "body", "tags", "url",
            "importance", "impact", "effective_date"]
    out = [dict(zip(keys, r)) for r in rows[:200]]
    for o in out:
        o["body"] = (o["body"] or "")[:1500]
    return out


def upcoming() -> list[dict]:
    """Календарь: что вступает в силу и когда."""
    _init()
    today = _msk().strftime("%Y-%m-%d")
    rows = db.fetchall(
        "SELECT id, source, title, effective_date, importance FROM news "
        "WHERE effective_date >= ? AND effective_date != '' "
        "ORDER BY effective_date", (today,))
    return [dict(zip(["id", "source", "title", "effective_date", "importance"], r))
            for r in rows[:20]]


# ── утренняя сводка ──────────────────────────────────────────────────────────
async def morning_digest(hours: int = 26) -> str:
    """Что изменилось за сутки + выводы. Пусто — значит новостей нет."""
    _init()
    since = (_msk() - timedelta(hours=hours)).strftime("%Y-%m-%d")
    rows = db.fetchall(
        "SELECT source, published, title, importance, impact, effective_date "
        "FROM news WHERE published >= ? ORDER BY "
        "CASE importance WHEN 'critical' THEN 0 WHEN 'important' THEN 1 "
        "ELSE 2 END, published DESC", (since,))
    if not rows:
        return ""
    fresh = [dict(zip(["source", "published", "title", "importance", "impact",
                       "effective"], r)) for r in rows]
    hot = [f for f in fresh if f["importance"] in ("critical", "important")]
    lines = [f"<b>Новости площадок за сутки</b> — {len(fresh)} шт"
             + (f", важных {len(hot)}" if hot else "")]
    for f in (hot or fresh)[:6]:
        mark = "ВАЖНО" if f["importance"] == "critical" else \
               "Стоит учесть" if f["importance"] == "important" else "К сведению"
        lines.append(f"\n<b>{f['source']} · {mark}</b>\n{f['title'][:200]}"
                     + (f"\n{f['impact']}" if f.get("impact") else "")
                     + (f"\nВступает в силу: {f['effective']}"
                        if f.get("effective") else ""))
    rest = len(fresh) - len(hot or fresh[:6])
    if rest > 0:
        lines.append(f"\nОстальные {rest} — фоновые, лежат во вкладке «Новости».")
    up = upcoming()
    if up:
        lines.append("\n<b>Скоро вступает в силу:</b>\n" + "\n".join(
            f"{u['effective_date']} — {u['title'][:120]}" for u in up[:4]))
    return "\n".join(lines)


async def refresh_all() -> dict:
    """Полный цикл: собрать WB + изменения Ozon → разобрать влияние."""
    added = await fetch_wb()
    added += await detect_ozon()
    analyzed = await analyze_new()
    return {"added": added, "analyzed": analyzed}
