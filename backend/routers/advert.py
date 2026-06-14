from datetime import datetime, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Query, HTTPException

import advert_client
from config import USE_MOCK, USE_ADVERT_MOCK

router = APIRouter(prefix="/api/advert", tags=["advert"])

_DATE_FROM = Query(description="YYYY-MM-DD")
_DATE_TO   = Query(description="YYYY-MM-DD")
_DAYS      = Query(ge=1, le=90)


def _range(date_from, date_to, days):
    dt_to   = datetime.fromisoformat(date_to)   if date_to   else datetime.utcnow()
    dt_from = datetime.fromisoformat(date_from) if date_from else dt_to - timedelta(days=days)
    return dt_from, dt_to


def _aggregate_stats(stats_data: list[dict]) -> dict[int, dict]:
    result = {}
    for s in stats_data:
        cid = s.get("advertId")
        if cid is None:
            continue
        agg = {"views": 0, "clicks": 0, "sum": 0.0, "orders": 0, "sum_price": 0.0}
        for day in s.get("days", []):
            for app in day.get("apps", []):
                for nm in app.get("nm", []):
                    agg["views"]     += nm.get("views",     0)
                    agg["clicks"]    += nm.get("clicks",    0)
                    agg["sum"]       += nm.get("sum",       0.0)
                    agg["orders"]    += nm.get("orders",    0)
                    agg["sum_price"] += nm.get("sum_price", 0.0)
        result[cid] = agg
    return result


@router.get("/campaigns")
async def get_campaigns(
    date_from: Annotated[Optional[str], _DATE_FROM] = None,
    date_to:   Annotated[Optional[str], _DATE_TO]   = None,
    days:      Annotated[int, _DAYS]                = 30,
):
    if USE_ADVERT_MOCK:
        return {"campaigns": [], "mock": True,
                "hint": "Задайте WB_ADVERT_KEY в переменных окружения (Render → Environment)"}

    dt_from, dt_to = _range(date_from, date_to, days)

    try:
        ids = await advert_client.get_all_campaign_ids()
    except Exception as e:
        raise HTTPException(502, f"WB Advert API (ids): {e}")

    if not ids:
        return {"campaigns": []}

    try:
        details  = await advert_client.get_campaign_details(ids)
        stats_data = await advert_client.get_fullstats(ids, dt_from, dt_to)
    except Exception as e:
        raise HTTPException(502, f"WB Advert API (details/stats): {e}")

    detail_map = {d["advertId"]: d for d in details if "advertId" in d}
    stats_map  = _aggregate_stats(stats_data)

    campaigns = []
    for cid in ids:
        d = detail_map.get(cid, {})
        s = stats_map.get(cid, {})

        views     = s.get("views",     0)
        clicks    = s.get("clicks",    0)
        spend     = s.get("sum",       0.0)
        orders    = s.get("orders",    0)
        revenue   = s.get("sum_price", 0.0)

        ctr = round(clicks / views  * 100, 2) if views   else 0.0
        cpc = round(spend  / clicks,       2) if clicks  else 0.0
        cpo = round(spend  / orders,       2) if orders  else 0.0
        drr = round(spend  / revenue * 100, 2) if revenue else 0.0
        cr  = round(orders / clicks  * 100, 2) if clicks  else 0.0

        campaigns.append({
            "id":          cid,
            "name":        d.get("name", f"Кампания {cid}"),
            "type":        advert_client.TYPE_NAMES.get(d.get("type"), f"Тип {d.get('type','?')}"),
            "status":      advert_client.STATUS_NAMES.get(d.get("status"), "—"),
            "status_code": d.get("status", 0),
            "views":       views,
            "clicks":      clicks,
            "ctr":         ctr,
            "cpc":         cpc,
            "spend":       round(spend, 2),
            "orders":      orders,
            "revenue":     round(revenue, 2),
            "cr":          cr,
            "cpo":         cpo,
            "drr":         drr,
        })

    campaigns.sort(key=lambda x: x["spend"], reverse=True)
    return {"campaigns": campaigns}
