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
_MODEL = "claude-opus-4-8"
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
    """Полный цикл: собрать → разобрать. Зовётся планировщиком."""
    added = await fetch_wb()
    analyzed = await analyze_new()
    return {"added": added, "analyzed": analyzed}
