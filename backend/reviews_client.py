"""Clients for fetching reviews from WB, Ozon, YM APIs + Postgres/SQLite storage."""
import asyncio
import logging
import time
from datetime import date, datetime, timedelta

import httpx

from config import (
    WB_API_KEY,
    OZON_CLIENT_ID, OZON_API_KEY,
    YM_API_KEY, YM_BUSINESS_ID,
)
import catalog as cat
import db

_log = logging.getLogger(__name__)

# Маркер: ответ на платформе уже есть, но его текст нам недоступен (Ozon)
ANSWERED_MARK = "✓ Отвечено на платформе"


# ─── DATABASE ────────────────────────────────────────────────────────────────

def _init_db():
    db.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id          TEXT PRIMARY KEY,
            platform    TEXT,
            sku         TEXT,
            name        TEXT,
            brand       TEXT,
            grp         TEXT,
            rating      INTEGER,
            text        TEXT,
            answer      TEXT,
            created_at  TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            review_id   TEXT PRIMARY KEY,
            draft       TEXT,
            status      TEXT,
            updated_at  TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS rating_snapshots (
            snapshot_date TEXT,
            platform      TEXT,
            sku           TEXT,
            rating        REAL,
            count         INTEGER,
            PRIMARY KEY (snapshot_date, platform, sku)
        )
    """)

try:
    _init_db()
except Exception as _e:
    _log.error("reviews _init_db failed (БД недоступна?): %s", _e)


def _enrich(row: dict) -> dict:
    """Add name/brand/group from catalog to a review row."""
    info = cat.lookup(row.get("sku") or "")
    row["name"]  = info.get("name", "")
    row["brand"] = info.get("brand", "")
    row["grp"]   = info.get("group", "")
    return row


def _upsert_reviews(rows: list[dict]):
    if not rows:
        return
    db.executemany(
        "INSERT INTO reviews (id, platform, sku, name, brand, grp, rating, text, answer, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "answer = COALESCE(NULLIF(excluded.answer, ''), reviews.answer)",
        [(r["id"], r["platform"], r["sku"], r["name"], r["brand"], r["grp"],
          r["rating"], r["text"], r["answer"], r["created_at"]) for r in rows],
    )
    _log.info("Upserted %d reviews", len(rows))


def _save_snapshot():
    today = date.today().isoformat()
    rows = db.fetchall(
        "SELECT platform, sku, AVG(rating), COUNT(*) FROM reviews "
        "WHERE rating > 0 GROUP BY platform, sku"
    )
    data = [(today, r[0], r[1], round(float(r[2]), 2), r[3]) for r in rows]
    if db.IS_PG:
        db.executemany(
            "INSERT INTO rating_snapshots (snapshot_date, platform, sku, rating, count) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(snapshot_date, platform, sku) DO UPDATE SET "
            "rating=excluded.rating, count=excluded.count",
            data,
        )
    else:
        db.executemany(
            "INSERT OR REPLACE INTO rating_snapshots VALUES (?,?,?,?,?)", data,
        )


def get_all_reviews(platform=None, limit=500) -> list[dict]:
    sel = ("SELECT id,platform,sku,name,brand,grp,rating,text,created_at,answer "
           "FROM reviews ")
    if platform and platform != "all":
        rows = db.fetchall(sel + "WHERE platform=? ORDER BY created_at DESC LIMIT ?",
                           (platform, limit))
    else:
        rows = db.fetchall(sel + "ORDER BY created_at DESC LIMIT ?", (limit,))
    # nmId — для превью-фото товара WB (артикул → номенклатура)
    import catalog as _cat
    import cost_store as _cs
    art_to_nm = {a: nm for nm, a in _cat.WB_ID_TO_ART.items()}
    nmids = {k: str(v) for k, v in (_cs.get_nmids() or {}).items()}
    def _nm(sku):
        s = _cat.canon(sku or "")
        return art_to_nm.get(s) or nmids.get(s) or ""
    return [{"id": r[0], "platform": r[1], "sku": r[2], "name": r[3],
             "brand": r[4], "group": r[5], "rating": r[6],
             "text": r[7], "date": (r[8] or "")[:10],
             "answer": r[9] or "", "nm": _nm(r[2])} for r in rows]


# ─── DRAFTS (AI-генерация ответов) ─────────────────────────────────────────────

def get_review(review_id: str) -> dict | None:
    r = db.fetchone(
        "SELECT id,platform,sku,name,rating,text,answer FROM reviews WHERE id=?",
        (review_id,),
    )
    if not r:
        return None
    return {"id": r[0], "platform": r[1], "sku": r[2], "name": r[3],
            "rating": r[4], "text": r[5], "answer": r[6] or ""}


def save_draft(review_id: str, draft: str, status="pending"):
    db.execute(
        "INSERT INTO drafts (review_id, draft, status, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(review_id) DO UPDATE SET draft=excluded.draft, "
        "status=excluded.status, updated_at=excluded.updated_at",
        (review_id, draft, status, date.today().isoformat()),
    )


def set_draft_status(review_id: str, status: str):
    db.execute(
        "UPDATE drafts SET status=?, updated_at=? WHERE review_id=?",
        (status, date.today().isoformat(), review_id),
    )


def get_draft(review_id: str) -> dict | None:
    r = db.fetchone(
        "SELECT review_id, draft, status FROM drafts WHERE review_id=?",
        (review_id,),
    )
    return {"review_id": r[0], "draft": r[1], "status": r[2]} if r else None


def get_drafts() -> dict:
    rows = db.fetchall("SELECT review_id, draft, status FROM drafts")
    return {r[0]: {"draft": r[1], "status": r[2]} for r in rows}


def get_unanswered(platform="WB", limit=20) -> list[dict]:
    """Reviews we haven't answered yet and have no draft, with text, newest first."""
    rows = db.fetchall(
        "SELECT r.id, r.platform, r.sku, r.name, r.rating, r.text "
        "FROM reviews r LEFT JOIN drafts d ON d.review_id = r.id "
        "WHERE r.platform=? AND (r.answer IS NULL OR r.answer='') "
        "AND r.text != '' AND d.review_id IS NULL "
        "ORDER BY r.created_at DESC LIMIT ?",
        (platform, limit),
    )
    return [{"id": r[0], "platform": r[1], "sku": r[2], "name": r[3],
             "rating": r[4], "text": r[5]} for r in rows]


async def wb_post_answer(feedback_id: str, text: str) -> tuple[bool, str]:
    """Publish an answer to a WB feedback. Returns (ok, message)."""
    if not WB_API_KEY:
        return False, "WB_API_KEY не задан"
    url = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks/answer"
    headers = {"Authorization": WB_API_KEY, "Content-Type": "application/json"}
    body = {"id": feedback_id, "text": text}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.is_success:
                return True, "опубликовано"
            return False, f"WB {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


async def ozon_post_answer(review_id: str, text: str) -> tuple[bool, str]:
    """Publish an answer to an Ozon review."""
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return False, "OZON_CLIENT_ID / OZON_API_KEY не заданы"
    headers = {"Client-Id": str(OZON_CLIENT_ID), "Api-Key": OZON_API_KEY,
               "Content-Type": "application/json"}
    # review_id stored as "ozon_<id>" — strip prefix
    rid = review_id.removeprefix("ozon_")
    body = {"review_id": rid, "text": text}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api-seller.ozon.ru/v1/review/comment/create",
                headers=headers, json=body,
            )
            if r.is_success:
                return True, "опубликовано"
            return False, f"Ozon {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


async def ym_post_answer(feedback_id: str, text: str) -> tuple[bool, str]:
    """Publish an answer to a YM goods-feedback."""
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return False, "YM_API_KEY / YM_BUSINESS_ID не заданы"
    headers = {"Api-Key": YM_API_KEY, "Content-Type": "application/json"}
    # feedback_id stored as "ym_<id>" — strip prefix
    fid = feedback_id.removeprefix("ym_")
    url = f"https://api.partner.market.yandex.ru/businesses/{YM_BUSINESS_ID}/goods-feedback/comments"
    body = {"feedbackId": int(fid), "comment": {"text": text}}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.is_success:
                return True, "опубликовано"
            return False, f"YM {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


async def post_answer(review_id: str, text: str) -> tuple[bool, str]:
    """Route to the right platform based on review_id prefix."""
    if review_id.startswith("wb_"):
        return await wb_post_answer(review_id[3:], text)
    if review_id.startswith("ozon_"):
        return await ozon_post_answer(review_id, text)
    if review_id.startswith("ym_"):
        return await ym_post_answer(review_id, text)
    return False, f"Неизвестный prefix review_id: {review_id}"


def get_rating_table() -> list[dict]:
    """Ratings per article and per group."""
    # per article
    # MAX(name/brand/grp): эти поля одинаковы в рамках sku, но Postgres требует
    # их в GROUP BY либо под агрегатом (SQLite такой строгости не имеет).
    # SUM(rating) нужен калькулятору рейтинга: по avg×count сумму точно не
    # восстановить (avg округлён до 2 знаков), а формула «сколько 5★ до цели»
    # требует точных S и N.
    art_rows = db.fetchall(
        "SELECT platform, sku, MAX(name), MAX(brand), MAX(grp), ROUND(AVG(rating),2), COUNT(*), SUM(rating) "
        "FROM reviews WHERE rating > 0 GROUP BY platform, sku ORDER BY sku"
    )
    # per group
    grp_rows = db.fetchall(
        "SELECT platform, grp, ROUND(AVG(rating),2), COUNT(*), SUM(rating) "
        "FROM reviews WHERE rating > 0 AND grp != '' GROUP BY platform, grp ORDER BY grp"
    )

    # Pivot articles
    art_table: dict[str, dict] = {}
    for platform, sku, name, brand, grp, avg, cnt, rsum in art_rows:
        avg = float(avg) if avg is not None else None
        if sku not in art_table:
            art_table[sku] = {"sku": sku, "name": name or sku, "brand": brand or "",
                               "group": grp or "",
                               "ozon": None, "wb": None, "ym": None,
                               "ozon_cnt": 0, "wb_cnt": 0, "ym_cnt": 0,
                               "ozon_sum": 0, "wb_sum": 0, "ym_sum": 0}
        key = platform.lower()
        art_table[sku][key] = avg
        art_table[sku][f"{key}_cnt"] = cnt
        art_table[sku][f"{key}_sum"] = int(rsum or 0)

    # Pivot groups
    grp_table: dict[str, dict] = {}
    for platform, grp, avg, cnt, rsum in grp_rows:
        avg = float(avg) if avg is not None else None
        if grp not in grp_table:
            grp_table[grp] = {"group": grp, "ozon": None, "wb": None, "ym": None,
                               "ozon_cnt": 0, "wb_cnt": 0, "ym_cnt": 0,
                               "ozon_sum": 0, "wb_sum": 0, "ym_sum": 0}
        key = platform.lower()
        grp_table[grp][key] = avg
        grp_table[grp][f"{key}_cnt"] = cnt
        grp_table[grp][f"{key}_sum"] = int(rsum or 0)

    return {
        "articles": sorted(art_table.values(), key=lambda x: x["sku"]),
        "groups":   sorted(grp_table.values(), key=lambda x: x["group"]),
    }


def get_rating_dynamics() -> dict:
    """Monthly avg rating from reviews history — overview and per-article breakdown."""
    art_rows = db.fetchall(
        "SELECT substr(created_at,1,7) as month, platform, sku, "
        "ROUND(AVG(rating),2), COUNT(*) "
        "FROM reviews WHERE rating > 0 AND created_at != '' "
        "GROUP BY month, platform, sku ORDER BY month"
    )
    ovr_rows = db.fetchall(
        "SELECT substr(created_at,1,7) as month, platform, "
        "ROUND(AVG(rating),2) "
        "FROM reviews WHERE rating > 0 AND created_at != '' "
        "GROUP BY month, platform ORDER BY month"
    )

    # overview: [{month, ozon, wb, ym}, ...]
    ovr: dict[str, dict] = {}
    for month, platform, avg in ovr_rows:
        ovr.setdefault(month, {"month": month})[platform.lower()] = float(avg) if avg is not None else None

    # by_article: {sku: [{month, ozon, wb, ym}, ...]}
    art: dict[str, dict[str, dict]] = {}
    for month, platform, sku, avg, _ in art_rows:
        art.setdefault(sku, {}).setdefault(month, {"month": month})[platform.lower()] = float(avg) if avg is not None else None

    return {
        "overview": sorted(ovr.values(), key=lambda x: x["month"]),
        "by_article": {
            sku: sorted(months.values(), key=lambda x: x["month"])
            for sku, months in art.items()
        },
    }


def get_answered_pairs(platform="WB", limit=400) -> list[dict]:
    """Review→our answer pairs (only where we actually answered), newest first."""
    rows = db.fetchall(
        "SELECT platform, sku, name, rating, text, answer, created_at FROM reviews "
        "WHERE answer IS NOT NULL AND answer != '' AND answer != ? AND platform = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (ANSWERED_MARK, platform, limit),
    )
    return [{"platform": r[0], "sku": r[1], "name": r[2], "rating": r[3],
             "text": r[4], "answer": r[5], "date": (r[6] or "")[:10]} for r in rows]


def get_answer_stats() -> dict:
    """How many reviews have our answer, per platform."""
    rows = db.fetchall(
        "SELECT platform, COUNT(*), SUM(CASE WHEN answer IS NOT NULL AND answer != '' "
        "THEN 1 ELSE 0 END) FROM reviews GROUP BY platform"
    )
    return {r[0]: {"total": r[1], "answered": r[2] or 0} for r in rows}


def get_stats() -> dict:
    total = db.fetchone("SELECT COUNT(*) FROM reviews")[0]
    by_platform = db.fetchall(
        "SELECT platform, COUNT(*), ROUND(AVG(rating),2) FROM reviews "
        "WHERE rating > 0 GROUP BY platform"
    )
    last_date = db.fetchone("SELECT MAX(created_at) FROM reviews")[0]
    return {
        "total": total,
        "by_platform": {r[0]: {"count": r[1], "avg": float(r[2]) if r[2] is not None else None} for r in by_platform},
        "last_review": (last_date or "")[:10],
    }


# ─── Инкрементальная загрузка ────────────────────────────────────────────────
# Полная перекачка всей истории каждые 15-30 мин — это сотни запросов к Ozon/YM
# (у YM ещё и запрос комментариев на КАЖДЫЙ отзыв). Отзывы уже лежат в БД,
# поэтому обычно догружаем только новое (с перехлёстом 3 дня), а полный скан —
# раз в сутки или по ручному «Обновить».

_FULL_SCAN_EVERY = 24 * 3600


def _incr_cutoff(platform: str) -> str:
    """ISO-дата, старше которой отзывы не качаем (уже есть в БД)."""
    try:
        rows = db.fetchall("SELECT MAX(created_at) FROM reviews WHERE platform = ?",
                           (platform,))
        newest = (rows[0][0] or "") if rows else ""
        if not newest:
            return ""
        return (datetime.fromisoformat(newest[:19]) - timedelta(days=3)).isoformat()
    except Exception:
        return ""


def _answered_ids(platform: str) -> set:
    try:
        rows = db.fetchall("SELECT id FROM reviews WHERE platform = ? AND answer != ''",
                           (platform,))
        return {r[0] for r in rows}
    except Exception:
        return set()


def _full_scan_due(platform: str, force: bool) -> bool:
    if force:
        return True
    import snapshot
    ts = snapshot.load(f"reviews_fullscan_{platform}", 0) or 0
    return time.time() - float(ts) > _FULL_SCAN_EVERY


def _mark_full_scan(platform: str) -> None:
    import snapshot
    snapshot.save(f"reviews_fullscan_{platform}", time.time())


# ─── WB ──────────────────────────────────────────────────────────────────────

async def fetch_wb_reviews():
    if not WB_API_KEY:
        return
    rows = []
    base = "https://feedbacks-api.wildberries.ru"
    headers = {"Authorization": WB_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for answered in (False, True):
                skip = 0
                while True:
                    params = {"isAnswered": str(answered).lower(),
                              "take": 5000, "skip": skip, "order": "dateDesc"}
                    r = await client.get(f"{base}/api/v1/feedbacks", headers=headers, params=params)
                    if not r.is_success:
                        _log.warning("WB feedbacks error: %s %s", r.status_code, r.text[:200])
                        break
                    data = r.json().get("data") or {}
                    feedbacks = data.get("feedbacks") or []
                    if not feedbacks:
                        break
                    for f in feedbacks:
                        details = f.get("productDetails") or {}
                        raw_sku = str(details.get("supplierArticle") or f.get("nmId") or "").strip()
                        # resolve via nmId first if supplierArticle missing
                        nm_id = f.get("nmId")
                        sku = cat.resolve_wb(nm_id) if nm_id else cat.resolve_wb(raw_sku)
                        answer = ((f.get("answer") or {}).get("text") or "").strip()
                        rows.append(_enrich({
                            "id": f"wb_{f['id']}",
                            "platform": "WB",
                            "sku": sku,
                            "rating": int(f.get("productValuation") or 0),
                            "text": (f.get("text") or "").strip(),
                            "answer": answer,
                            "created_at": (f.get("createdDate") or "")[:19],
                        }))
                    skip += len(feedbacks)
                    if len(feedbacks) < 5000:
                        break
                    await asyncio.sleep(0.5)
    except Exception as e:
        _log.warning("WB reviews exception: %s", e)

    _upsert_reviews(rows)
    _log.info("WB: fetched %d feedbacks", len(rows))


# ─── OZON ────────────────────────────────────────────────────────────────────

async def fetch_ozon_reviews(full: bool = True):
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return
    cutoff = "" if full else _incr_cutoff("Ozon")
    rows = []
    headers = {"Client-Id": str(OZON_CLIENT_ID), "Api-Key": OZON_API_KEY,
               "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            last_id = ""
            while True:
                body = {"limit": 100, "sort_dir": "DESC", "status": "ALL"}
                if last_id:
                    body["last_id"] = last_id
                r = await client.post(
                    "https://api-seller.ozon.ru/v1/review/list",
                    headers=headers, json=body,
                )
                if not r.is_success:
                    _log.warning("Ozon reviews error: %s %s", r.status_code, r.text[:200])
                    break
                payload = r.json()
                items = payload.get("reviews") or payload.get("items") or []
                if not items:
                    break
                for rev in items:
                    raw_sku = str(rev.get("sku") or rev.get("offer_id") or "")
                    sku = cat.resolve_ozon(raw_sku) if raw_sku else raw_sku
                    # Ozon не отдаёт текст нашего ответа в списке — судим по статусу
                    status = (rev.get("status") or "").upper()
                    answered = status == "PROCESSED" or (rev.get("comments_amount") or 0) > 0
                    rows.append(_enrich({
                        "id": f"ozon_{rev.get('id') or rev.get('review_id')}",
                        "platform": "Ozon",
                        "sku": sku,
                        "rating": int(rev.get("rating") or 0),
                        "text": (rev.get("text") or rev.get("comment") or "").strip(),
                        "answer": ANSWERED_MARK if answered else "",
                        "created_at": (rev.get("published_at") or rev.get("created_at") or
                                       rev.get("create_at") or "")[:19],
                    }))
                last_id = payload.get("last_id") or ""
                has_next = payload.get("has_next")
                if not last_id or has_next is False or len(items) < 100:
                    break
                # инкремент: дошли до отзывов, которые уже есть в БД — стоп
                if cutoff and rows and rows[-1]["created_at"] and rows[-1]["created_at"] < cutoff:
                    break
                await asyncio.sleep(0.3)
    except Exception as e:
        _log.warning("Ozon reviews exception: %s", e)

    _upsert_reviews(rows)
    _log.info("Ozon: fetched %d reviews%s", len(rows), "" if full else " (инкремент)")


# ─── YM ──────────────────────────────────────────────────────────────────────

async def fetch_ym_reviews(full: bool = True):
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return
    cutoff = "" if full else _incr_cutoff("YM")
    answered = _answered_ids("YM")
    rows = []
    headers = {"Api-Key": YM_API_KEY, "Content-Type": "application/json"}
    url = f"https://api.partner.market.yandex.ru/businesses/{YM_BUSINESS_ID}/goods-feedback"

    fb_ids = []  # (row_index, feedback_id) для подтягивания наших ответов
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            page_token = None
            for _ in range(200):
                params = {"limit": 50}
                if page_token:
                    params["page_token"] = page_token
                r = await client.post(url, headers=headers, params=params, json={})
                if not r.is_success:
                    _log.warning("YM feedback error: %s %s", r.status_code, r.text[:200])
                    break
                result = r.json().get("result") or {}
                feedbacks = result.get("feedbacks") or []
                if not feedbacks:
                    break
                for f in feedbacks:
                    ident = f.get("identifiers") or {}
                    stats = f.get("statistics") or {}
                    descr = f.get("description") or {}
                    parts = [descr.get("comment"), descr.get("advantages"), descr.get("disadvantages")]
                    text = " ".join(p.strip() for p in parts if p).strip()
                    raw_sku = str(ident.get("offerId") or ident.get("shopSku") or "")
                    sku = cat.resolve_ym(raw_sku) if raw_sku else raw_sku
                    fid = f.get("feedbackId") or f.get("id")
                    fb_ids.append((len(rows), fid))
                    rows.append(_enrich({
                        "id": f"ym_{fid}",
                        "platform": "YM",
                        "sku": sku,
                        "rating": int(stats.get("rating") or 0),
                        "text": text,
                        "answer": "",
                        "created_at": (f.get("createdAt") or "")[:19],
                    }))
                page_token = (result.get("paging") or {}).get("nextPageToken")
                if not page_token:
                    break
                # инкремент: дошли до отзывов, которые уже есть в БД — стоп
                if cutoff and rows and rows[-1]["created_at"] and rows[-1]["created_at"] < cutoff:
                    break
                await asyncio.sleep(0.3)

            # Подтягиваем наши ответы (комментарии продавца) — только для
            # отзывов, у которых в БД ещё нет ответа (иначе N+1 по всей истории)
            comments_url = f"{url}/comments"
            fb_ids = [(idx, fid) for idx, fid in fb_ids
                      if rows[idx]["id"] not in answered]
            for idx, fid in fb_ids:
                try:
                    cr = await client.post(comments_url, headers=headers,
                                           json={"feedbackId": int(fid), "limit": 50})
                    if not cr.is_success:
                        continue
                    comments = ((cr.json().get("result") or {}).get("comments")) or []
                    seller = next(
                        (c.get("text", "").strip() for c in comments
                         if (c.get("author") or {}).get("type") in ("BUSINESS", "SHOP", "SELLER")),
                        "",
                    )
                    if seller:
                        rows[idx]["answer"] = seller
                except Exception:
                    pass
                await asyncio.sleep(0.1)
    except Exception as e:
        _log.warning("YM reviews exception: %s", e)

    _upsert_reviews(rows)
    _log.info("YM: fetched %d feedbacks%s", len(rows), "" if full else " (инкремент)")


# ─── REFRESH ──────────────────────────────────────────────────────────────────

_last_refresh = 0.0
_refresh_lock = asyncio.Lock()
REFRESH_INTERVAL = 900  # 15 мин — отзывы подтягиваются сами при открытии вкладки


async def refresh_all(force=False):
    global _last_refresh
    if not force and _last_refresh and time.monotonic() - _last_refresh < REFRESH_INTERVAL:
        return
    async with _refresh_lock:
        if not force and _last_refresh and time.monotonic() - _last_refresh < REFRESH_INTERVAL:
            return
        _last_refresh = time.monotonic()
        full_oz = _full_scan_due("Ozon", force)
        full_ym = _full_scan_due("YM", force)
        await asyncio.gather(
            fetch_wb_reviews(),
            fetch_ozon_reviews(full=full_oz),
            fetch_ym_reviews(full=full_ym),
            return_exceptions=True,
        )
        if full_oz:
            _mark_full_scan("Ozon")
        if full_ym:
            _mark_full_scan("YM")
        _save_snapshot()
        _log.info("Reviews refresh done. Total: %d", get_stats()["total"])
