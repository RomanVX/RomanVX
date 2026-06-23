"""Reviews / Ratings upload and display endpoint."""
import logging
import os
from datetime import datetime

from fastapi import APIRouter, UploadFile, File
import openpyxl

router = APIRouter(prefix="/api/reviews", tags=["reviews"])
_log = logging.getLogger(__name__)

# Store in-memory (resets on restart — user re-uploads)
_reviews_data: dict = {}

UPLOAD_PATH = "/tmp/rating.xlsx"


def _parse_rating_xlsx(path: str) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True)
    result = {}

    # === РЕЙТИНГ по склейкам ===
    ws = wb["Рейтинг"]
    ratings = []
    header_found = False
    for row in ws.iter_rows(values_only=True):
        if row[0] == "Названия строк":
            header_found = True
            continue
        if not header_found or not row[0] or row[0] == "Общий итог":
            continue
        ratings.append({
            "group": row[0],
            "ozon":  round(float(row[1]), 2) if row[1] else None,
            "wb":    round(float(row[2]), 2) if row[2] else None,
            "ym":    round(float(row[3]), 2) if row[3] else None,
            "total": round(float(row[4]), 2) if row[4] else None,
        })
    result["ratings"] = ratings

    # === СВОД — отзывы с текстом ===
    ws2 = wb["Свод"]
    reviews = []
    for i, row in enumerate(ws2.iter_rows(values_only=True)):
        if i == 0:
            continue  # header
        if not row[0]:
            continue
        text = (row[3] or "").strip()
        if not text:
            continue  # только с текстом
        dt = row[4]
        reviews.append({
            "platform": row[0],
            "sku":      str(row[5] or ""),
            "name":     str(row[6] or ""),
            "brand":    str(row[7] or ""),
            "group":    str(row[8] or ""),
            "rating":   int(row[2]) if row[2] else 0,
            "text":     text,
            "date":     dt.strftime("%Y-%m-%d") if isinstance(dt, datetime) else str(dt)[:10],
        })
    # Sort by date desc, take latest 300
    reviews.sort(key=lambda x: x["date"], reverse=True)
    result["reviews"] = reviews[:300]

    # === СТАТИСТИКА — всего отзывов и средний рейтинг ===
    total_count = 0
    rating_sum = 0
    platform_counts: dict = {}
    for i, row in enumerate(ws2.iter_rows(values_only=True)):
        if i == 0:
            continue
        if not row[0]:
            continue
        total_count += 1
        rating_sum += int(row[2]) if row[2] else 0
        platform_counts[row[0]] = platform_counts.get(row[0], 0) + 1

    result["stats"] = {
        "total": total_count,
        "avg_rating": round(rating_sum / total_count, 2) if total_count else 0,
        "by_platform": platform_counts,
        "updated": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }

    return result


@router.post("/upload-rating")
async def upload_rating(file: UploadFile = File(...)):
    """Upload Рейтинг.xlsx and parse it."""
    global _reviews_data
    content = await file.read()
    with open(UPLOAD_PATH, "wb") as f:
        f.write(content)
    _reviews_data = _parse_rating_xlsx(UPLOAD_PATH)
    _log.info("Rating file uploaded: %d reviews, %d groups",
              len(_reviews_data.get("reviews", [])),
              len(_reviews_data.get("ratings", [])))
    return {"ok": True, "stats": _reviews_data.get("stats", {})}


@router.get("/reviews-data")
async def get_reviews_data():
    """Return parsed reviews data."""
    if not _reviews_data:
        # Try loading from disk if exists
        if os.path.exists(UPLOAD_PATH):
            try:
                return _parse_rating_xlsx(UPLOAD_PATH)
            except Exception as e:
                _log.warning("Failed to load rating from disk: %s", e)
        return {"ratings": [], "reviews": [], "stats": {}}
    return _reviews_data
