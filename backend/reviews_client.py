"""Clients for fetching reviews from WB, Ozon, YM APIs + SQLite storage."""
import asyncio
import logging
import sqlite3
import time
from datetime import date

import httpx

from config import (
    WB_API_KEY,
    OZON_CLIENT_ID, OZON_API_KEY,
    YM_API_KEY, YM_BUSINESS_ID,
)
import catalog as cat

_log = logging.getLogger(__name__)
DB_PATH = "/tmp/reviews.db"


# ─── DATABASE ────────────────────────────────────────────────────────────────

def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
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
    # migrate old schema if name column missing
    cols = {r[1] for r in con.execute("PRAGMA table_info(reviews)").fetchall()}
    for col, typ in [("name", "TEXT"), ("brand", "TEXT"), ("grp", "TEXT"), ("answer", "TEXT")]:
        if col not in cols:
            con.execute(f"ALTER TABLE reviews ADD COLUMN {col} {typ}")
    con.execute("""
        CREATE TABLE IF NOT EXISTS rating_snapshots (
            snapshot_date TEXT,
            platform      TEXT,
            sku           TEXT,
            rating        REAL,
            count         INTEGER,
            PRIMARY KEY (snapshot_date, platform, sku)
        )
    """)
    con.commit()
    con.close()

_init_db()


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
    con = sqlite3.connect(DB_PATH)
    con.executemany(
        "INSERT INTO reviews (id, platform, sku, name, brand, grp, rating, text, answer, created_at) "
        "VALUES (:id, :platform, :sku, :name, :brand, :grp, :rating, :text, :answer, :created_at) "
        "ON CONFLICT(id) DO UPDATE SET "
        "answer = COALESCE(NULLIF(excluded.answer, ''), reviews.answer)",
        rows,
    )
    con.commit()
    con.close()
    _log.info("Upserted %d reviews", len(rows))


def _save_snapshot():
    today = date.today().isoformat()
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT platform, sku, AVG(rating), COUNT(*) FROM reviews "
        "WHERE rating > 0 GROUP BY platform, sku"
    ).fetchall()
    con.executemany(
        "INSERT OR REPLACE INTO rating_snapshots VALUES (?,?,?,?,?)",
        [(today, r[0], r[1], round(r[2], 2), r[3]) for r in rows],
    )
    con.commit()
    con.close()


def get_all_reviews(platform=None, limit=500) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    if platform and platform != "all":
        rows = con.execute(
            "SELECT id,platform,sku,name,brand,grp,rating,text,created_at FROM reviews "
            "WHERE platform=? ORDER BY created_at DESC LIMIT ?",
            (platform, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id,platform,sku,name,brand,grp,rating,text,created_at FROM reviews "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    con.close()
    return [{"id": r[0], "platform": r[1], "sku": r[2], "name": r[3],
             "brand": r[4], "group": r[5], "rating": r[6],
             "text": r[7], "date": (r[8] or "")[:10]} for r in rows]


def get_rating_table() -> list[dict]:
    """Ratings per article and per group."""
    con = sqlite3.connect(DB_PATH)
    # per article
    art_rows = con.execute(
        "SELECT platform, sku, name, brand, grp, ROUND(AVG(rating),2), COUNT(*) "
        "FROM reviews WHERE rating > 0 GROUP BY platform, sku ORDER BY sku"
    ).fetchall()
    # per group
    grp_rows = con.execute(
        "SELECT platform, grp, ROUND(AVG(rating),2), COUNT(*) "
        "FROM reviews WHERE rating > 0 AND grp != '' GROUP BY platform, grp ORDER BY grp"
    ).fetchall()
    con.close()

    # Pivot articles
    art_table: dict[str, dict] = {}
    for platform, sku, name, brand, grp, avg, cnt in art_rows:
        if sku not in art_table:
            art_table[sku] = {"sku": sku, "name": name or sku, "brand": brand or "",
                               "group": grp or "",
                               "ozon": None, "wb": None, "ym": None,
                               "ozon_cnt": 0, "wb_cnt": 0, "ym_cnt": 0}
        key = platform.lower()
        art_table[sku][key] = avg
        art_table[sku][f"{key}_cnt"] = cnt

    # Pivot groups
    grp_table: dict[str, dict] = {}
    for platform, grp, avg, cnt in grp_rows:
        if grp not in grp_table:
            grp_table[grp] = {"group": grp, "ozon": None, "wb": None, "ym": None,
                               "ozon_cnt": 0, "wb_cnt": 0, "ym_cnt": 0}
        key = platform.lower()
        grp_table[grp][key] = avg
        grp_table[grp][f"{key}_cnt"] = cnt

    return {
        "articles": sorted(art_table.values(), key=lambda x: x["sku"]),
        "groups":   sorted(grp_table.values(), key=lambda x: x["group"]),
    }


def get_rating_dynamics() -> dict:
    """Monthly avg rating from reviews history — overview and per-article breakdown."""
    con = sqlite3.connect(DB_PATH)
    art_rows = con.execute(
        "SELECT strftime('%Y-%m', created_at) as month, platform, sku, "
        "ROUND(AVG(rating),2), COUNT(*) "
        "FROM reviews WHERE rating > 0 AND created_at != '' "
        "GROUP BY month, platform, sku ORDER BY month"
    ).fetchall()
    ovr_rows = con.execute(
        "SELECT strftime('%Y-%m', created_at) as month, platform, "
        "ROUND(AVG(rating),2) "
        "FROM reviews WHERE rating > 0 AND created_at != '' "
        "GROUP BY month, platform ORDER BY month"
    ).fetchall()
    con.close()

    # overview: [{month, ozon, wb, ym}, ...]
    ovr: dict[str, dict] = {}
    for month, platform, avg in ovr_rows:
        ovr.setdefault(month, {"month": month})[platform.lower()] = avg

    # by_article: {sku: [{month, ozon, wb, ym}, ...]}
    art: dict[str, dict[str, dict]] = {}
    for month, platform, sku, avg, _ in art_rows:
        art.setdefault(sku, {}).setdefault(month, {"month": month})[platform.lower()] = avg

    return {
        "overview": sorted(ovr.values(), key=lambda x: x["month"]),
        "by_article": {
            sku: sorted(months.values(), key=lambda x: x["month"])
            for sku, months in art.items()
        },
    }


def get_answered_pairs(platform="WB", limit=400) -> list[dict]:
    """Review→our answer pairs (only where we actually answered), newest first."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT platform, sku, name, rating, text, answer, created_at FROM reviews "
        "WHERE answer IS NOT NULL AND answer != '' AND platform = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (platform, limit),
    ).fetchall()
    con.close()
    return [{"platform": r[0], "sku": r[1], "name": r[2], "rating": r[3],
             "text": r[4], "answer": r[5], "date": (r[6] or "")[:10]} for r in rows]


def get_answer_stats() -> dict:
    """How many reviews have our answer, per platform."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT platform, COUNT(*), SUM(CASE WHEN answer IS NOT NULL AND answer != '' "
        "THEN 1 ELSE 0 END) FROM reviews GROUP BY platform"
    ).fetchall()
    con.close()
    return {r[0]: {"total": r[1], "answered": r[2] or 0} for r in rows}


def get_stats() -> dict:
    con = sqlite3.connect(DB_PATH)
    total = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    by_platform = con.execute(
        "SELECT platform, COUNT(*), ROUND(AVG(rating),2) FROM reviews "
        "WHERE rating > 0 GROUP BY platform"
    ).fetchall()
    last_date = con.execute("SELECT MAX(created_at) FROM reviews").fetchone()[0]
    con.close()
    return {
        "total": total,
        "by_platform": {r[0]: {"count": r[1], "avg": r[2]} for r in by_platform},
        "last_review": (last_date or "")[:10],
    }


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

async def fetch_ozon_reviews():
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        return
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
                    rows.append(_enrich({
                        "id": f"ozon_{rev.get('id') or rev.get('review_id')}",
                        "platform": "Ozon",
                        "sku": sku,
                        "rating": int(rev.get("rating") or 0),
                        "text": (rev.get("text") or rev.get("comment") or "").strip(),
                        "answer": "",
                        "created_at": (rev.get("published_at") or rev.get("created_at") or
                                       rev.get("create_at") or "")[:19],
                    }))
                last_id = payload.get("last_id") or ""
                has_next = payload.get("has_next")
                if not last_id or has_next is False or len(items) < 100:
                    break
                await asyncio.sleep(0.3)
    except Exception as e:
        _log.warning("Ozon reviews exception: %s", e)

    _upsert_reviews(rows)
    _log.info("Ozon: fetched %d reviews", len(rows))


# ─── YM ──────────────────────────────────────────────────────────────────────

async def fetch_ym_reviews():
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return
    rows = []
    headers = {"Api-Key": YM_API_KEY, "Content-Type": "application/json"}
    url = f"https://api.partner.market.yandex.ru/businesses/{YM_BUSINESS_ID}/goods-feedback"

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
                    rows.append(_enrich({
                        "id": f"ym_{f.get('feedbackId') or f.get('id')}",
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
                await asyncio.sleep(0.3)
    except Exception as e:
        _log.warning("YM reviews exception: %s", e)

    _upsert_reviews(rows)
    _log.info("YM: fetched %d feedbacks", len(rows))


# ─── REFRESH ──────────────────────────────────────────────────────────────────

_last_refresh = 0.0
_refresh_lock = asyncio.Lock()
REFRESH_INTERVAL = 3600  # 1 hour


async def refresh_all(force=False):
    global _last_refresh
    if not force and _last_refresh and time.monotonic() - _last_refresh < REFRESH_INTERVAL:
        return
    async with _refresh_lock:
        if not force and _last_refresh and time.monotonic() - _last_refresh < REFRESH_INTERVAL:
            return
        _last_refresh = time.monotonic()
        await asyncio.gather(
            fetch_wb_reviews(),
            fetch_ozon_reviews(),
            fetch_ym_reviews(),
            return_exceptions=True,
        )
        _save_snapshot()
        _log.info("Reviews refresh done. Total: %d", get_stats()["total"])
