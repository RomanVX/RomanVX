"""Reviews endpoints — auto-fetch from WB / Ozon / YM APIs."""
import logging

from fastapi import APIRouter, BackgroundTasks, Query

import reviews_client as rc
import review_ai

router = APIRouter(prefix="/api/reviews", tags=["reviews"])
_log = logging.getLogger(__name__)


@router.get("/data")
async def get_reviews_data(
    background_tasks: BackgroundTasks,
    platform: str = Query("all"),
    limit: int = Query(5000),
):
    """Return reviews, ratings, dynamics, stats. Triggers a background refresh if stale."""
    background_tasks.add_task(rc.refresh_all)
    return {
        "reviews":  rc.get_all_reviews(platform=platform, limit=limit),
        "ratings":  rc.get_rating_table(),
        "dynamics": rc.get_rating_dynamics(),
        "stats":    rc.get_stats(),
    }


@router.post("/refresh")
async def force_refresh():
    """Force refresh from all platforms now."""
    await rc.refresh_all(force=True)
    return {"ok": True, "stats": rc.get_stats()}


@router.get("/answer-stats")
async def answer_stats():
    """How many reviews already have our saved answer, per platform."""
    return rc.get_answer_stats()


@router.post("/analyze-style")
async def analyze_style(platform: str = Query("WB"), sample: int = Query(300)):
    """Analyze our past answers and return a reusable style guide."""
    return await review_ai.analyze_style(platform=platform, sample=sample)
