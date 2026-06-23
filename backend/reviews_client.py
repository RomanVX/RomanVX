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
    YM_API_KEY, YM_CAMPAIGN_ID, YM_BUSINESS_ID,
)

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
            rating      INTEGER,
            text        TEXT,
            created_at  TEXT
        )
    """)
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


def _upsert_reviews(rows: list[dict]):
    if not rows:
        return
    con = sqlite3.connect(DB_PATH)
    con.executemany(
        "INSERT OR IGNORE INTO reviews (id, platform, sku, rating, text, created_at) "
        "VALUES (:id, :platform, :sku, :rating, :text, :created_at)",
        rows,
    )
    con.commit()
    con.close()
    _log.info("Upserted %d reviews", len(rows))


def _save_snapshot():
    """Save daily rating snapshot per platform+sku."""
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
            "SELECT id,platform,sku,rating,text,created_at FROM reviews "
            "WHERE platform=? ORDER BY created_at DESC LIMIT ?",
            (platform, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id,platform,sku,rating,text,created_at FROM reviews "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    con.close()
    return [{"id": r[0], "platform": r[1], "sku": r[2],
             "rating": r[3], "text": r[4], "date": (r[5] or "")[:10]} for r in rows]


def get_rating_table() -> list[dict]:
    """Current avg rating per platform per sku."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT platform, sku, ROUND(AVG(rating),2), COUNT(*) "
        "FROM reviews WHERE rating > 0 GROUP BY platform, sku ORDER BY sku"
    ).fetchall()
    con.close()
    # Pivot: sku → {ozon, wb, ym, count}
    table: dict[str, dict] = {}
    for platform, sku, avg, cnt in rows:
        if sku not in table:
            table[sku] = {"sku": sku, "ozon": None, "wb": None, "ym": None,
                          "ozon_cnt": 0, "wb_cnt": 0, "ym_cnt": 0}
        key = platform.lower()
        table[sku][key] = avg
        table[sku][f"{key}_cnt"] = cnt
    return sorted(table.values(), key=lambda x: x["sku"])


def get_rating_dynamics() -> list[dict]:
    """Return rating dynamics per platform over time."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT snapshot_date, platform, ROUND(AVG(rating),2) "
        "FROM rating_snapshots GROUP BY snapshot_date, platform ORDER BY snapshot_date"
    ).fetchall()
    con.close()
    result: dict[str, dict] = {}
    for dt, platform, avg in rows:
        if dt not in result:
            result[dt] = {"date": dt}
        result[dt][platform.lower()] = avg
    return list(result.values())


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
    """Fetch all WB feedbacks (answered + unanswered)."""
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
                        text = (f.get("text") or "").strip()
                        details = f.get("productDetails") or {}
                        sku = str(details.get("supplierArticle") or f.get("nmId") or "")
                        rows.append({
                            "id": f"wb_{f['id']}",
                            "platform": "WB",
                            "sku": sku,
                            "rating": int(f.get("productValuation") or 0),
                            "text": text,
                            "created_at": (f.get("createdDate") or "")[:19],
                        })
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
    """Fetch all Ozon reviews (requires Premium Plus subscription)."""
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
                    rows.append({
                        "id": f"ozon_{rev.get('id') or rev.get('review_id')}",
                        "platform": "Ozon",
                        "sku": str(rev.get("sku") or rev.get("offer_id") or ""),
                        "rating": int(rev.get("rating") or 0),
                        "text": (rev.get("text") or rev.get("comment") or "").strip(),
                        "created_at": (rev.get("published_at") or rev.get("created_at") or
                                       rev.get("create_at") or "")[:19],
                    })
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
    """Fetch Yandex Market reviews via business goods-feedback API."""
    if not YM_API_KEY or not YM_BUSINESS_ID:
        return
    rows = []
    headers = {"Api-Key": YM_API_KEY, "Content-Type": "application/json"}
    url = f"https://api.partner.market.yandex.ru/businesses/{YM_BUSINESS_ID}/goods-feedback"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            page_token = None
            for _ in range(200):  # safety bound on pages
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
                    rows.append({
                        "id": f"ym_{f.get('feedbackId') or f.get('id')}",
                        "platform": "YM",
                        "sku": str(ident.get("offerId") or ident.get("shopSku") or ""),
                        "rating": int(stats.get("rating") or 0),
                        "text": text,
                        "created_at": (f.get("createdAt") or "")[:19],
                    })
                page_token = (result.get("paging") or {}).get("nextPageToken")
                if not page_token:
                    break
                await asyncio.sleep(0.3)
    except Exception as e:
        _log.warning("YM reviews exception: %s", e)

    _upsert_reviews(rows)
    _log.info("YM: fetched %d feedbacks", len(rows))


# ─── MAIN REFRESH ─────────────────────────────────────────────────────────────

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
