"""Рынок WB по поисковым запросам: исследования, которые собирает бот на ПК
владельца (agent/wb_market.py --agent, домашний IP — серверные WB банит).

Исследование (job) = список запросов. Бот забирает задания
(GET /api/market/pending?token=WB_AGENT_TOKEN), по каждому запросу снимает
подсказки WB и выдачу (до 100–200 товаров: цена покупателя, рейтинг, отзывы,
остаток, реклама) и отдаёт в POST /api/market/ingest. Если в исследовании
включены подсказки (expand=N), первые N подсказок к исходным запросам
добавляются в него как новые запросы.

Анализ (GET /api/market/job/{id}):
  queries      — по запросу: найдено товаров, цены, отзывы и остатки топа,
                 доля рекламы, лидеры-бренды, оценка продаж/мес по приросту
                 отзывов между замерами (как в «Спросе WB»);
  ingredients  — активные компоненты: в скольких карточках топа встречаются
                 (по названию), сколько у них отзывов (≈ продажи), доля
                 отзывов, цены, лучшие места, лидер; плюс размер выдачи по
                 запросам с этим компонентом;
  hints        — подсказки WB (что ищут рядом).
Хранение: kv `mkt_jobs` (список), `mkt_<job>_<md5(q)[:10]>` (выдача запроса),
снимки отзывов — общая таблица niche_snapshots (оценка продаж между замерами).
"""
import asyncio
import hashlib
import os
import re
import statistics as st
import time
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/market", tags=["market"])
_JOBS = "mkt_jobs"
_ISSUE_TTL = 600            # выданный боту запрос, не вернувшийся за 10 мин, выдаём снова
_REVIEW_RATE = 0.05         # ~5% покупателей оставляют отзыв (как в «Спросе WB»)
_agent_seen = {"ts": 0.0, "host": ""}

# активные компоненты: ключ → (название, регулярка по названию товара)
ACTIVES = {
    "selenium": ("Сульфид селена", r"сульфид\w*\s+селен|дисульфид|selen"),
    "climbazole": ("Климбазол", r"климбазол|climbazol"),
    "piroctone": ("Пироктон оламин", r"пироктон|октопирокс|piroctone|octopirox"),
    "ketoconazole": ("Кетоконазол", r"кетоконазол|ketoconazol|низорал"),
    "zinc": ("Цинк пиритион", r"пиритион|pyrithion|\bцинк"),
    "tar": ("Дёготь", r"д[её]г[оё]т|д[её]гтярн"),
    "salicylic": ("Салициловая кислота", r"салицил"),
    "copper": ("Трипептиды меди", r"пептид\w*\s+меди|медн\w*\s+пептид|ghk|copper\s*pept"),
    "oleoresin": ("Олеорезин / перец", r"олеорезин|oleoresin|капсикум|capsicum|красн\w*\s+перц|перцов"),
    "capixyl": ("Капиксил", r"капиксил|capixyl"),
    "procapil": ("Прокапил", r"прокапил|procapil"),
    "redensyl": ("Редензил", r"редензил|redensyl"),
    "minoxidil": ("Миноксидил", r"миноксидил|minoxidil"),
    "aminexil": ("Аминексил", r"аминексил|aminexil"),
    "caffeine": ("Кофеин", r"кофеин|caffein"),
    "niacin": ("Никотиновая кислота", r"никотинов|ниацин|niacin"),
    "biotin": ("Биотин", r"биотин|biotin"),
    "keratin": ("Кератин", r"кератин|keratin"),
    "rosemary": ("Розмарин", r"розмарин|rosemary"),
    "burdock": ("Репейное масло", r"репейн|лопух|burdock"),
    "panthenol": ("Пантенол", r"пантенол|panthenol"),
}
_ACT_RE = {k: re.compile(rx, re.I) for k, (_, rx) in ACTIVES.items()}

PRESETS = {
    "hair": {
        "title": "Волосы: перхоть и выпадение — активные компоненты",
        "queries": [
            # категории (где считаем доли компонентов)
            "шампунь от перхоти", "средство от перхоти", "шампунь от выпадения волос",
            "шампунь для роста волос", "сыворотка для роста волос", "лосьон от выпадения волос",
            "бальзам от выпадения волос", "маска для роста волос",
            # компоненты из ТЗ
            "шампунь с сульфидом селена", "сульфид селена", "шампунь с климбазолом", "климбазол",
            "шампунь пироктон оламин", "пироктон оламин", "пептиды меди для волос",
            "трипептид меди", "олеорезин для волос", "капиксил", "сыворотка с капиксилом",
            "прокапил", "сыворотка с прокапилом",
            # для сравнения: что ещё качает
            "шампунь с кетоконазолом", "шампунь цинк пиритион", "дегтярный шампунь",
            "миноксидил", "редензил", "шампунь с кофеином", "никотиновая кислота для волос",
        ],
        "category": ["шампунь от перхоти", "средство от перхоти", "шампунь от выпадения волос",
                     "шампунь для роста волос", "сыворотка для роста волос",
                     "лосьон от выпадения волос", "бальзам от выпадения волос",
                     "маска для роста волос"],
        "expand": 3, "pages": 1,
    },
}


# ── хранение ──────────────────────────────────────────────────────────────
def _kv(key, default):
    import snapshot
    return snapshot.load(key, default) or default


def _save(key, val):
    import snapshot
    snapshot.save(key, val)


def _qkey(job: str, q: str) -> str:
    return f"mkt_{job}_{hashlib.md5(q.encode()).hexdigest()[:10]}"


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", str(q or "").strip().lower())


def _owner(request: Request):
    import auth as _auth
    if (_auth._session_of(request) or {}).get("role") != "owner":
        raise HTTPException(403, "Только для владельца")


def _token(token: str):
    want = os.getenv("WB_AGENT_TOKEN", "")
    if not want or token != want:
        raise HTTPException(401, "bad token")


def _now() -> str:
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")


# ── бот ───────────────────────────────────────────────────────────────────
@router.get("/pending")
async def pending(token: str = "", limit: int = 5):
    """Задания боту: [{job, query, pages, hints}] — по одному запросу."""
    _token(token)
    _agent_seen["ts"] = time.time()
    jobs = await asyncio.to_thread(_kv, _JOBS, {"jobs": []})
    now, out, dirty = time.time(), [], False
    for j in jobs["jobs"]:
        if j.get("status") == "done":
            continue
        for q, s in j["queries"].items():
            if s["status"] == "done" or (s["status"] == "issued" and now - s.get("ts", 0) < _ISSUE_TTL):
                continue
            s.update(status="issued", ts=now); dirty = True
            out.append({"job": j["id"], "query": q, "pages": j.get("pages", 1),
                        "hints": bool(j.get("expand")) and s.get("src") == "seed"})
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    if dirty:
        await asyncio.to_thread(_save, _JOBS, jobs)
    return {"tasks": out}


@router.post("/ingest")
async def ingest(payload: dict, token: str = ""):
    """{job, query, total, rows:[{pos,nm,name,brand,supplier,price,rating,feedbacks,stock,ad}],
    hints:[...], error?}"""
    _token(token)
    _agent_seen["ts"] = time.time()
    jid, q = str(payload.get("job") or ""), _norm(payload.get("query"))
    jobs = await asyncio.to_thread(_kv, _JOBS, {"jobs": []})
    j = next((x for x in jobs["jobs"] if x["id"] == jid), None)
    if not j or q not in j["queries"]:
        raise HTTPException(404, "нет такого задания")
    s = j["queries"][q]
    if payload.get("error"):
        s.update(status="error", error=str(payload["error"])[:200], ts=time.time())
    else:
        rows = [{k: r.get(k) for k in ("pos", "nm", "name", "brand", "supplier", "price",
                                        "rating", "feedbacks", "stock", "ad")}
                for r in (payload.get("rows") or [])[:200] if r.get("nm")]
        hints = [str(h)[:80] for h in (payload.get("hints") or [])][:15]
        await asyncio.to_thread(_save, _qkey(jid, q), {
            "query": q, "total": payload.get("total"), "rows": rows, "hints": hints,
            "at": _now()})
        await asyncio.to_thread(_snap_feedbacks, q, rows)
        s.update(status="done", ts=time.time(), n=len(rows), total=payload.get("total"))
        s.pop("error", None)
        # подсказки → новые запросы исследования
        for h in hints[: int(j.get("expand") or 0)] if s.get("src") == "seed" else []:
            hq = _norm(h)
            if hq and hq not in j["queries"]:
                j["queries"][hq] = {"status": "new", "src": "hint", "parent": q}
    if all(x["status"] in ("done", "error") for x in j["queries"].values()):
        j["status"], j["finished"] = "done", _now()
    await asyncio.to_thread(_save, _JOBS, jobs)
    return {"ok": True}


def _snap_feedbacks(q: str, rows: list):
    """Снимок отзывов в niche_snapshots — по приросту между замерами оцениваем продажи."""
    import db
    today = datetime.utcnow().date().isoformat()
    data = [(q, int(r["nm"]), int(r.get("feedbacks") or 0), float(r.get("price") or 0), today)
            for r in rows if r.get("nm")]
    if not data:
        return
    try:
        db.executemany(
            "INSERT INTO niche_snapshots (query, nm_id, feedbacks, price, snap_date) "
            "VALUES (?,?,?,?,?) ON CONFLICT (query, nm_id, snap_date) DO UPDATE "
            "SET feedbacks = excluded.feedbacks, price = excluded.price", data)
    except Exception:
        pass


def _sales_est(q: str, rows: list) -> dict:
    """nm → продажи/мес по приросту отзывов с первого замера (≥3 дней назад)."""
    import db
    today = datetime.utcnow().date()
    try:
        prev = db.fetchall("SELECT nm_id, feedbacks, snap_date FROM niche_snapshots "
                           "WHERE query = ? AND snap_date < ? ORDER BY snap_date",
                           (q, today.isoformat()))
    except Exception:
        return {}
    first = {}
    for nm, fb, d in prev:
        first.setdefault(int(nm), (int(fb or 0), str(d)[:10]))
    out = {}
    for r in rows:
        b = first.get(int(r["nm"]))
        if not b:
            continue
        days = (today - datetime.fromisoformat(b[1]).date()).days
        if days >= 3:
            out[int(r["nm"])] = round(max(0, (r.get("feedbacks") or 0) - b[0]) / days * 30 / _REVIEW_RATE)
    return out


# ── владелец ──────────────────────────────────────────────────────────────
@router.get("/presets")
async def presets(request: Request):
    _owner(request)
    return {k: {"title": v["title"], "queries": v["queries"], "expand": v["expand"]}
            for k, v in PRESETS.items()}


@router.post("/job")
async def create_job(request: Request, payload: dict):
    """{title, queries:[...], expand: 0..5, pages: 1|2, category:[...]} или {preset: "hair"}."""
    _owner(request)
    p = dict(PRESETS.get(payload.get("preset") or "", {}))
    p.update({k: v for k, v in payload.items() if k != "preset" and v not in (None, "", [])})
    qs = []
    for q in p.get("queries") or []:
        n = _norm(q)
        if n and n not in qs:
            qs.append(n)
    if not qs:
        raise HTTPException(400, "нет запросов")
    if len(qs) > 120:
        raise HTTPException(400, "не больше 120 запросов за раз")
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "title": str(p.get("title") or qs[0])[:120], "created": _now(),
           "status": "new", "expand": max(0, min(5, int(p.get("expand") or 0))),
           "pages": max(1, min(2, int(p.get("pages") or 1))),
           "category": [_norm(c) for c in p.get("category") or []],
           "queries": {q: {"status": "new", "src": "seed"} for q in qs}}
    jobs = await asyncio.to_thread(_kv, _JOBS, {"jobs": []})
    jobs["jobs"] = [job] + jobs["jobs"][:29]
    await asyncio.to_thread(_save, _JOBS, jobs)
    return {"ok": True, "id": jid}


@router.post("/job/{jid}/rerun")
async def rerun(request: Request, jid: str):
    """Пересобрать все запросы (повторный замер — даёт оценку продаж по приросту отзывов)."""
    _owner(request)
    jobs = await asyncio.to_thread(_kv, _JOBS, {"jobs": []})
    j = next((x for x in jobs["jobs"] if x["id"] == jid), None)
    if not j:
        raise HTTPException(404)
    for s in j["queries"].values():
        s.update(status="new"); s.pop("error", None)
    j["status"] = "new"
    await asyncio.to_thread(_save, _JOBS, jobs)
    return {"ok": True}


@router.delete("/job/{jid}")
async def delete_job(request: Request, jid: str):
    _owner(request)
    jobs = await asyncio.to_thread(_kv, _JOBS, {"jobs": []})
    jobs["jobs"] = [x for x in jobs["jobs"] if x["id"] != jid]
    await asyncio.to_thread(_save, _JOBS, jobs)
    return {"ok": True}


@router.get("/jobs")
async def list_jobs(request: Request):
    _owner(request)
    jobs = await asyncio.to_thread(_kv, _JOBS, {"jobs": []})
    out = []
    for j in jobs["jobs"]:
        qs = j["queries"].values()
        out.append({k: j.get(k) for k in ("id", "title", "created", "status", "finished", "expand")}
                   | {"total": len(qs), "done": sum(s["status"] == "done" for s in qs),
                      "errors": sum(s["status"] == "error" for s in qs)})
    age = time.time() - _agent_seen["ts"] if _agent_seen["ts"] else None
    return {"jobs": out, "agent_online": age is not None and age < 120,
            "agent_seen_sec": round(age) if age is not None else None}


def _med(xs):
    xs = [x for x in xs if x]
    return round(st.median(xs)) if xs else None


@router.get("/job/{jid}")
async def job_report(request: Request, jid: str):
    _owner(request)
    jobs = await asyncio.to_thread(_kv, _JOBS, {"jobs": []})
    j = next((x for x in jobs["jobs"] if x["id"] == jid), None)
    if not j:
        raise HTTPException(404)
    data = {}
    for q, s in j["queries"].items():
        if s["status"] == "done":
            d = await asyncio.to_thread(_kv, _qkey(jid, q), None)
            if d:
                data[q] = d
    return await asyncio.to_thread(_analyze, j, data)


def _analyze(j: dict, data: dict) -> dict:
    queries, hints = [], []
    for q, s in j["queries"].items():
        d = data.get(q)
        row = {"query": q, "status": s["status"], "src": s.get("src"), "parent": s.get("parent"),
               "error": s.get("error")}
        if d:
            rows = d["rows"]
            top = rows[:30]
            sales = _sales_est(q, rows[:100])
            brands = {}
            for r in rows[:100]:
                b = brands.setdefault(r.get("brand") or "—", {"n": 0, "fb": 0, "sales": 0, "best": r["pos"]})
                b["n"] += 1; b["fb"] += r.get("feedbacks") or 0; b["sales"] += sales.get(int(r["nm"]), 0)
            fb_all = sum(b["fb"] for b in brands.values()) or 1
            leaders = sorted(({"brand": k, "cards": v["n"], "feedbacks": v["fb"],
                               "share": round(v["fb"] / fb_all * 100, 1), "best": v["best"],
                               "sales_est": v["sales"] or None}
                              for k, v in brands.items()), key=lambda x: -x["feedbacks"])[:10]
            acts = sorted({ACTIVES[k][0] for r in rows[:100] for k, rx in _ACT_RE.items()
                           if rx.search(r.get("name") or "")})
            row.update({
                "total": d.get("total"), "n": len(rows), "at": d.get("at"),
                "price_med": _med([r.get("price") for r in rows]),
                "price_p25": round(st.quantiles([r["price"] for r in rows if r.get("price")], n=4)[0])
                if sum(1 for r in rows if r.get("price")) >= 4 else None,
                "price_p75": round(st.quantiles([r["price"] for r in rows if r.get("price")], n=4)[2])
                if sum(1 for r in rows if r.get("price")) >= 4 else None,
                "fb_top30": sum(r.get("feedbacks") or 0 for r in top),
                "fb_med30": _med([r.get("feedbacks") for r in top]),
                "rating30": round(st.mean([r["rating"] for r in top if r.get("rating")] or [0]), 2),
                "stock30": sum(r.get("stock") or 0 for r in top) if any(r.get("stock") is not None for r in top) else None,
                "ads30": round(sum(1 for r in top if r.get("ad")) / len(top) * 100) if top else None,
                "sales_est100": sum(sales.values()) or None,
                "leaders": leaders, "actives": acts,
                "top": [{**r, "sales_est": sales.get(int(r["nm"]))} for r in rows[:20]],
            })
            for i, h in enumerate(d.get("hints") or [], 1):
                hints.append({"query": q, "hint": h, "rank": i})
        queries.append(row)

    # активные компоненты: доли в топе категорийных запросов
    cat = [q for q in (j.get("category") or []) if q in data] or \
          [q for q in data if not any(rx.search(q) for rx in _ACT_RE.values())]
    seen, pool = set(), []
    for q in cat:
        for r in data[q]["rows"][:100]:
            if r["nm"] not in seen:
                seen.add(r["nm"]); pool.append(r)
    fb_pool = sum(r.get("feedbacks") or 0 for r in pool) or 1
    ingr = []
    for k, (title, _) in ACTIVES.items():
        rx = _ACT_RE[k]
        hit = [r for r in pool if rx.search(r.get("name") or "")]
        own_q = [x for x in queries if x.get("total") is not None and rx.search(x["query"])]
        lead = {}
        for r in hit:
            lead[r.get("brand") or "—"] = lead.get(r.get("brand") or "—", 0) + (r.get("feedbacks") or 0)
        ingr.append({
            "key": k, "title": title, "cards": len(hit),
            "feedbacks": sum(r.get("feedbacks") or 0 for r in hit),
            "share": round(sum(r.get("feedbacks") or 0 for r in hit) / fb_pool * 100, 1),
            "price_med": _med([r.get("price") for r in hit]),
            "best_pos": min((r["pos"] for r in hit), default=None),
            "leader": max(lead, key=lead.get) if lead else None,
            "queries": [{"query": x["query"], "total": x["total"], "fb_top30": x.get("fb_top30")}
                        for x in own_q],
            "in_hints": sum(1 for h in hints if rx.search(h["hint"])),
        })
    ingr = [x for x in ingr if x["cards"] or x["queries"] or x["in_hints"]]
    ingr.sort(key=lambda x: (-x["feedbacks"], -(x["cards"])))
    return {"job": {k: j.get(k) for k in ("id", "title", "created", "status", "finished", "expand")},
            "category": cat, "pool_cards": len(pool),
            "queries": queries, "ingredients": ingr, "hints": hints}
