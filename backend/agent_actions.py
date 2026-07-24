"""Руки агента: действия, меняющие кабинет — только через подтверждение.

Агент не выполняет действие сам. Он кладёт заявку в очередь (propose),
владелец подтверждает кнопкой в Telegram или на вкладке, и только тогда
действие уходит в API площадки. Каждое действие пишется в журнал вместе
с прежним значением — чтобы можно было откатить.

Схемы запросов взяты из docs/wb_api/promotion.yaml (WB) — не угадываем.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

import db

_log = logging.getLogger("agent_actions")
_ADV = "https://advert-api.wildberries.ru"


def _init() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS agent_actions (
        id TEXT PRIMARY KEY, created TEXT, kind TEXT, title TEXT,
        payload TEXT, reason TEXT, status TEXT,
        applied TEXT, before TEXT, result TEXT)""")


def _msk() -> str:
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")


# ── очередь заявок ───────────────────────────────────────────────────────────
def propose(kind: str, title: str, payload: dict, reason: str) -> str:
    """Агент предлагает действие. Возвращает id заявки."""
    _init()
    aid = uuid.uuid4().hex[:8]
    db.execute(
        "INSERT INTO agent_actions (id, created, kind, title, payload, reason, status)"
        " VALUES (?,?,?,?,?,?,?)",
        (aid, _msk(), kind, title[:200], json.dumps(payload, ensure_ascii=False),
         reason[:600], "pending"))
    return aid


def pending(limit: int = 20) -> list[dict]:
    _init()
    rows = db.fetchall(
        "SELECT id, created, kind, title, payload, reason, status, applied, result "
        "FROM agent_actions WHERE status = 'pending' ORDER BY created DESC")
    keys = ["id", "created", "kind", "title", "payload", "reason", "status",
            "applied", "result"]
    return [dict(zip(keys, r)) for r in rows[:limit]]


def journal(limit: int = 40) -> list[dict]:
    _init()
    rows = db.fetchall(
        "SELECT id, created, kind, title, payload, reason, status, applied, result "
        "FROM agent_actions ORDER BY created DESC")
    keys = ["id", "created", "kind", "title", "payload", "reason", "status",
            "applied", "result"]
    return [dict(zip(keys, r)) for r in rows[:limit]]


def _get(aid: str) -> dict | None:
    _init()
    r = db.fetchone(
        "SELECT id, created, kind, title, payload, reason, status, applied, "
        "before, result FROM agent_actions WHERE id = ?", (aid,))
    if not r:
        return None
    keys = ["id", "created", "kind", "title", "payload", "reason", "status",
            "applied", "before", "result"]
    d = dict(zip(keys, r))
    d["payload"] = json.loads(d["payload"] or "{}")
    return d


# ── исполнители: минимум логики, максимум точности по спеке ──────────────────
async def _wb_adv(method: str, path: str, **kw) -> dict:
    import httpx
    import advert_client as ac
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.request(method, _ADV + path, headers=ac._headers(), **kw)
    if not r.is_success:
        raise RuntimeError(f"WB {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return {"ok": True}


async def _do_minus_phrases(p: dict) -> tuple[dict, dict]:
    """POST /adv/v0/normquery/set-minus — минус-фразы кампании по артикулу."""
    before = await _wb_adv("POST", "/adv/v0/normquery/get-minus",
                           json={"advert_id": int(p["advert_id"]),
                                 "nm_id": int(p["nm_id"])})
    res = await _wb_adv("POST", "/adv/v0/normquery/set-minus",
                        json={"advert_id": int(p["advert_id"]),
                              "nm_id": int(p["nm_id"]),
                              "norm_queries": list(p["phrases"])})
    return before, res


async def _do_set_bid(p: dict) -> tuple[dict, dict]:
    """PATCH /api/advert/v1/bids — ставка в кампании по артикулу."""
    before = {"bid_kopecks_was": p.get("bid_was")}
    res = await _wb_adv("PATCH", "/api/advert/v1/bids", json={"bids": [{
        "advert_id": int(p["advert_id"]),
        "nm_bids": [{"nm_id": int(p["nm_id"]),
                     "bid_kopecks": int(round(float(p["bid"]) * 100)),
                     "placement": p.get("placement") or "search"}]}]})
    return before, res


async def _do_campaign_state(p: dict) -> tuple[dict, dict]:
    """GET /adv/v0/pause | /adv/v0/start — пауза и запуск кампании."""
    path = "/adv/v0/pause" if p.get("action") == "pause" else "/adv/v0/start"
    before = {"action_was": "start" if p.get("action") == "pause" else "pause"}
    res = await _wb_adv("GET", path, params={"id": int(p["advert_id"])})
    return before, res


async def _do_ozon_price(p: dict) -> tuple[dict, dict]:
    """Цена Ozon: обновление через v1/product/import/prices."""
    import ozon_client
    before = {}
    try:
        cur = await ozon_client.get_prices()
        before = {p["offer_id"]: (cur.get(p["offer_id"]) or {}).get("price")}
    except Exception:
        pass
    res = await ozon_client._post("/v1/product/import/prices", {"prices": [{
        "offer_id": str(p["offer_id"]),
        "price": str(int(p["price"])),
        "old_price": str(int(p.get("old_price") or 0)),
        "auto_action_enabled": p.get("auto_action") or "UNKNOWN"}]})
    return before, res


_EXEC = {
    "minus_phrases": (_do_minus_phrases, "минус-фразы WB"),
    "set_bid": (_do_set_bid, "ставка WB"),
    "campaign_state": (_do_campaign_state, "пауза/запуск кампании WB"),
    "ozon_price": (_do_ozon_price, "цена Ozon"),
}


async def apply(aid: str, who: str = "владелец") -> dict:
    """Подтвердить и выполнить заявку."""
    a = _get(aid)
    if not a:
        return {"error": "заявка не найдена"}
    if a["status"] != "pending":
        return {"error": f"заявка уже {a['status']}"}
    fn = _EXEC.get(a["kind"], (None, ""))[0]
    if not fn:
        return {"error": f"нет исполнителя для {a['kind']}"}
    try:
        before, res = await fn(a["payload"])
        status, result = "done", json.dumps(res, ensure_ascii=False)[:1000]
    except Exception as e:
        before, status, result = {}, "failed", str(e)[:500]
        _log.warning("action %s failed: %s", aid, result)
    await asyncio.to_thread(
        db.execute,
        "UPDATE agent_actions SET status=?, applied=?, before=?, result=? WHERE id=?",
        (status, f"{_msk()} · {who}", json.dumps(before, ensure_ascii=False)[:1000],
         result, aid))
    return {"id": aid, "status": status, "result": result[:300]}


async def reject(aid: str, why: str = "") -> dict:
    a = _get(aid)
    if not a or a["status"] != "pending":
        return {"error": "нечего отклонять"}
    await asyncio.to_thread(
        db.execute,
        "UPDATE agent_actions SET status='rejected', applied=?, result=? WHERE id=?",
        (_msk(), (why or "отклонено владельцем")[:300], aid))
    # отказ — обратная связь агенту: запоминаем как факт, чтобы не предлагал снова
    try:
        import agent_strategist as _st
        _st._init()
        await asyncio.to_thread(
            db.execute, "INSERT INTO strategist_tasks VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], _msk(), "note",
             (f"Владелец отклонил действие «{a['title']}»"
              + (f": {why}" if why else ""))[:300],
             "", "", "open", "", (a.get("reason") or "")[:1000]))
    except Exception as e:
        _log.warning("reject note: %s", e)
    return {"id": aid, "status": "rejected"}


async def notify(aid: str) -> None:
    """Сообщить владельцу о заявке с кнопками подтверждения."""
    import httpx
    import agent_review as _ar
    a = _get(aid)
    if not a or not _ar.TG_BOT_TOKEN:
        return
    text = (f"<b>Предлагаю действие:</b> {a['title']}\n\n{a['reason']}\n\n"
            f"Тип: {_EXEC.get(a['kind'], (None, a['kind']))[1]}")
    kb = {"inline_keyboard": [[
        {"text": "Применить", "callback_data": f"act_ok_{aid}"},
        {"text": "Отклонить", "callback_data": f"act_no_{aid}"}]]}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(
                f"https://api.telegram.org/bot{_ar.TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": _ar.TG_CHAT_ID, "text": text[:3900],
                      "parse_mode": "HTML", "reply_markup": kb})
    except Exception as e:
        _log.warning("notify: %s", e)
