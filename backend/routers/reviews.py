"""Reviews endpoints — auto-fetch from WB / Ozon / YM APIs."""
import logging

from fastapi import APIRouter, BackgroundTasks, Body, Query

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
        "drafts":   rc.get_drafts(),
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


@router.post("/draft")
async def make_draft(id: str = Query(...)):
    """Generate (or regenerate) an AI draft reply for one review."""
    review = rc.get_review(id)
    if not review:
        return {"error": "Отзыв не найден"}
    draft = await review_ai.generate_reply(review, platform=review["platform"])
    if not draft:
        return {"error": "Не удалось сгенерировать (проверьте ANTHROPIC_API_KEY)"}
    rc.save_draft(id, draft, status="pending")
    return {"id": id, "draft": draft, "status": "pending"}


@router.post("/draft-batch")
async def make_drafts(platform: str = Query("WB"), limit: int = Query(20)):
    """Generate drafts for up to `limit` unanswered reviews without a draft."""
    todo = rc.get_unanswered(platform=platform, limit=limit)
    made = 0
    for review in todo:
        draft = await review_ai.generate_reply(review, platform=platform)
        if draft:
            rc.save_draft(review["id"], draft, status="pending")
            made += 1
    return {"generated": made, "requested": len(todo)}


@router.post("/approve")
async def approve_draft(
    id: str = Query(...),
    publish: bool = Query(True),
    body: dict = Body(default=None),
):
    """Approve a draft (optionally edited). If publish and WB review — post to WB."""
    d = rc.get_draft(id)
    if not d:
        return {"error": "Черновик не найден"}
    text = ((body or {}).get("text") or d["draft"] or "").strip()
    if not text:
        return {"error": "Текст ответа пустой"}
    published, msg = False, ""
    if publish and id.startswith("wb_"):
        feedback_id = id[3:]
        published, msg = await rc.wb_post_answer(feedback_id, text)
        if not published:
            return {"id": id, "status": "pending", "published": False, "error": msg}
    rc.save_draft(id, text, status="approved")
    return {"id": id, "status": "approved", "published": published, "message": msg}


@router.post("/decline")
async def decline_draft(id: str = Query(...)):
    """Mark a draft as declined."""
    rc.set_draft_status(id, "declined")
    return {"id": id, "status": "declined"}
