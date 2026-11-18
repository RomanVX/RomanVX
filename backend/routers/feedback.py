"""Обратная связь: вопросы покупателей (WB, Ozon) и рекламации WB (заявки на возврат).

Вопросы:   WB  GET/PATCH /api/v1/questions (feedbacks-api);
           Ozon /v1/question/list, /v1/question/answer/create (Premium Plus).
           ИИ пишет черновик ответа (правила команды + наши прошлые ответы),
           человек правит и отправляет.
Рекламации: WB GET /api/v1/claims (за 14 дней), PATCH /api/v1/claim.
           ИИ смотрит комментарий и фото покупателя и РЕКОМЕНДУЕТ (одобрить /
           отклонить / запросить фото) с обоснованием и текстом ответа;
           решение и отправку делает человек.
Правила (отдельно для вопросов и рекламаций) — kv `fb_rules`.
"""
import asyncio
import json
import logging
import re
import time as _time
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Body, Query

from config import WB_API_KEY, ANTHROPIC_API_KEY

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])

_WB = "https://feedbacks-api.wildberries.ru"
_WB_CLAIMS = "https://returns-api.wildberries.ru"
_TTL = 600
_cache: dict = {}            # key → (ts, data)
MODEL = "claude-opus-4-8"

CLAIM_ACTIONS = {
    "approve1": "Одобрить с проверкой брака (товар вернётся, проверят на складе)",
    "approve2": "Одобрить и забрать товар",
    "autorefund1": "Вернуть деньги, товар остаётся у покупателя",
    "reject1": "Отклонить: брак не обнаружен (шаблон WB)",
    "reject2": "Отклонить: попросить фото/видео (шаблон WB)",
    "reject3": "Отклонить: направить в сервисный центр (шаблон WB)",
    "rejectcustom": "Отклонить со своим комментарием",
    "approvecc1": "Одобрить с возвратом в магазин продавца",
    "confirmreturngoodcc1": "Подтвердить приёмку товара от покупателя",
}
CLAIM_STATUS = {0: "на рассмотрении", 1: "отказ", 2: "одобрено"}
CLAIM_STATUS_EX = {0: "на рассмотрении", 1: "товар у покупателя (отклонено)",
                   2: "сдаётся на WB → утиль", 5: "товар у покупателя (одобрено)",
                   8: "вернётся в продажу после проверки", 10: "возвращается продавцу"}


# ── правила ──────────────────────────────────────────────────────────────────
def _rules() -> dict:
    import snapshot
    r = snapshot.load("fb_rules", None) or {}
    return {"questions": r.get("questions") or [], "claims": r.get("claims") or []}


@router.get("/rules")
async def get_rules():
    return await asyncio.to_thread(_rules)


@router.post("/rules")
async def save_rules(body: dict = Body(...)):
    """{kind: questions|claims, rules: [строки]} — полный список правил раздела."""
    import snapshot
    kind = (body or {}).get("kind")
    if kind not in ("questions", "claims"):
        return {"error": "kind: questions или claims"}
    rules = [str(x).strip()[:500] for x in (body.get("rules") or []) if str(x).strip()][:50]
    cur = await asyncio.to_thread(_rules)
    cur[kind] = rules
    await asyncio.to_thread(snapshot.save, "fb_rules", cur)
    return cur


# ── Claude ───────────────────────────────────────────────────────────────────
async def _claude(system: str, content, max_tokens=3000, effort="low") -> str:
    from review_ai import _get_client
    resp = await _get_client().messages.create(
        model=MODEL, max_tokens=max_tokens, thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"пустой ответ модели (stop_reason={resp.stop_reason})")
    return text


def _product_info(mp: str, art: str, name: str) -> str:
    import catalog
    info = catalog.lookup(art) if art else {}
    parts = [f"Товар: {name or info.get('name') or art}"]
    if art:
        parts.append(f"Артикул продавца: {art}")
    if info.get("brand") and info["brand"] != "Прочее":
        parts.append(f"Бренд: {info['brand']}")
    return "\n".join(parts)


# ── вопросы ──────────────────────────────────────────────────────────────────
async def _wb_questions(answered: bool, take: int) -> list[dict]:
    if not WB_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.get(f"{_WB}/api/v1/questions", headers={"Authorization": WB_API_KEY},
                        params={"isAnswered": str(answered).lower(), "take": take, "skip": 0,
                                "order": "dateDesc"})
    if r.status_code != 200:
        raise RuntimeError(f"WB вопросы: HTTP {r.status_code} {r.text[:150]}")
    out = []
    for q in ((r.json() or {}).get("data") or {}).get("questions") or []:
        pd = q.get("productDetails") or {}
        out.append({"mp": "WB", "id": q.get("id"), "text": q.get("text") or "",
                    "date": (q.get("createdDate") or "")[:16].replace("T", " "),
                    "answer": ((q.get("answer") or {}).get("text") or ""),
                    "editable": bool((q.get("answer") or {}).get("editable")),
                    "sku": pd.get("nmId"), "art": pd.get("supplierArticle") or "",
                    "name": pd.get("productName") or "", "brand": pd.get("brandName") or "",
                    "warned": bool(q.get("isWarned")), "state": q.get("state")})
    return out


async def _ozon_questions(status: str, pages: int = 5) -> list[dict]:
    import ozon_client as oz
    if not oz.OZON_CLIENT_ID:
        return []
    out, last = [], ""
    since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")
    for _ in range(pages):
        data = await oz._post("/v1/question/list", {
            "filter": {"date_from": since, "date_to": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "status": status},
            "limit": 100, "last_id": last, "sort_dir": "DESC"})
        for q in data.get("questions") or []:
            out.append({"mp": "Ozon", "id": q.get("id"), "text": q.get("text") or "",
                        "date": str(q.get("published_at") or "")[:16].replace("T", " "),
                        "answer": "", "answers_count": q.get("answers_count") or 0,
                        "sku": q.get("sku"), "art": "", "name": "", "status": q.get("status"),
                        "link": q.get("question_link") or q.get("product_url") or ""})
        last = data.get("last_id") or ""
        if not data.get("has_next") or not last:
            break
    return out


async def _ozon_names(qs: list[dict]) -> None:
    """Ozon отдаёт только SKU — подставляем артикул и название."""
    import ozon_client as oz
    skus = sorted({int(q["sku"]) for q in qs if q.get("sku")})
    if not skus:
        return
    m = {}
    for i in range(0, len(skus), 1000):
        try:
            data = await oz._post("/v3/product/info/list", {"sku": skus[i:i + 1000]})
        except Exception as e:
            _log.warning("ozon names: %s", e)
            return
        for it in data.get("items") or []:
            for s in it.get("sources") or []:
                if s.get("sku"):
                    m[int(s["sku"])] = (it.get("offer_id") or "", it.get("name") or "")
            if it.get("sku"):
                m[int(it["sku"])] = (it.get("offer_id") or "", it.get("name") or "")
    for q in qs:
        a = m.get(int(q["sku"] or 0))
        if a:
            q["art"], q["name"] = a


@router.get("/questions")
async def questions(refresh: bool = False):
    hit = _cache.get("q")
    if hit and not refresh and _time.time() - hit[0] < _TTL:
        return hit[1]
    errors, new, done = [], [], []
    try:
        new += await _wb_questions(False, 200)
        done += await _wb_questions(True, 100)
    except Exception as e:
        errors.append(str(e)[:200])
    try:
        oq = await _ozon_questions("UNPROCESSED")
        await _ozon_names(oq)
        new += oq
    except Exception as e:
        msg = str(e)
        errors.append("Ozon вопросы: " + ("нужна подписка Premium Plus" if "403" in msg or "premium" in msg.lower()
                                          else msg[:200]))
    drafts = _drafts()
    for q in new:
        q["draft"] = drafts.get(f"q:{q['id']}")
    res = {"new": new, "answered": done[:100], "errors": errors,
           "fetched": (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m %H:%M")}
    _cache["q"] = (_time.time(), res)
    return res


def _drafts() -> dict:
    import snapshot
    return snapshot.load("fb_drafts", None) or {}


def _save_draft(key: str, val) -> None:
    import snapshot
    d = _drafts()
    if val is None:
        d.pop(key, None)
    else:
        d[key] = val
    if len(d) > 2000:
        for k in list(d)[:len(d) - 2000]:
            d.pop(k)
    snapshot.save("fb_drafts", d)


@router.post("/questions/draft")
async def question_draft(body: dict = Body(...)):
    """{id, mp, text, art, name} → черновик ответа ИИ."""
    if not ANTHROPIC_API_KEY:
        return {"error": "ИИ не подключён (нет ANTHROPIC_API_KEY)"}
    q = body or {}
    rules = (await asyncio.to_thread(_rules))["questions"]
    examples = ""
    hit = _cache.get("q")
    if hit:
        pairs = [x for x in hit[1].get("answered") or [] if x.get("answer")][:12]
        examples = "\n\n".join(f"Вопрос: {x['text']}\nОтвет: {x['answer']}" for x in pairs)
    system = ("Ты отвечаешь от лица продавца на вопросы покупателей о товаре на маркетплейсе. "
              "Отвечай по существу, коротко (2–5 предложений), вежливо и живо, без канцелярита. "
              "Не выдумывай характеристики: если в данных о товаре ответа нет, честно скажи, что "
              "уточним, или предложи посмотреть описание/характеристики в карточке. Не обещай "
              "сроков доставки и скидок. Не упоминай другие магазины и площадки. "
              "Верни ТОЛЬКО текст ответа.")
    if rules:
        system += "\n\nПРАВИЛА КОМАНДЫ (обязательны):\n" + "\n".join(f"- {r}" for r in rules)
    if examples:
        system += "\n\nПРИМЕРЫ НАШИХ ОТВЕТОВ:\n\n" + examples
    user = (_product_info(q.get("mp"), q.get("art") or "", q.get("name") or "")
            + f"\n\nВопрос покупателя: {q.get('text') or ''}\n\nНапиши ответ.")
    try:
        text = await _claude(system, user)
    except Exception as e:
        return {"error": f"Генерация не удалась: {str(e)[:200]}"}
    await asyncio.to_thread(_save_draft, f"q:{q.get('id')}", text)
    return {"draft": text}


@router.post("/questions/answer")
async def question_answer(body: dict = Body(...)):
    """{mp, id, sku, text, reject?} — отправить ответ (или отклонить вопрос WB)."""
    b = body or {}
    text = str(b.get("text") or "").strip()
    if len(text) < 2:
        return {"error": "Пустой ответ"}
    if b.get("mp") == "WB":
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.patch(f"{_WB}/api/v1/questions", headers={"Authorization": WB_API_KEY},
                              json={"id": b.get("id"), "answer": {"text": text},
                                    "state": "none" if b.get("reject") else "wbRu"})
        ok = r.status_code == 200 and not (r.json() or {}).get("error")
        err = "" if ok else f"WB: HTTP {r.status_code} {r.text[:200]}"
    else:
        import ozon_client as oz
        try:
            await oz._post("/v1/question/answer/create",
                           {"question_id": b.get("id"), "sku": int(b.get("sku") or 0), "text": text})
            ok, err = True, ""
        except Exception as e:
            ok, err = False, f"Ozon: {str(e)[:200]}"
    if not ok:
        return {"error": err}
    await asyncio.to_thread(_save_draft, f"q:{b.get('id')}", None)
    hit = _cache.get("q")
    if hit:
        hit[1]["new"] = [x for x in hit[1]["new"] if x["id"] != b.get("id")]
    return {"ok": True}


# ── рекламации WB ────────────────────────────────────────────────────────────
async def _wb_claims(archive: bool) -> tuple[list, int]:
    out, total = [], 0
    async with httpx.AsyncClient(timeout=40) as c:
        for off in range(0, 1000, 200):
            r = await c.get(f"{_WB_CLAIMS}/api/v1/claims", headers={"Authorization": WB_API_KEY},
                            params={"is_archive": str(archive).lower(), "limit": 200, "offset": off})
            if r.status_code != 200:
                raise RuntimeError(f"WB рекламации: HTTP {r.status_code} {r.text[:150]}")
            d = r.json() or {}
            rows = d.get("claims") or []
            total = d.get("total") or total
            out += rows
            if len(rows) < 200:
                break
            await asyncio.sleep(3)
    return out, total


def _claim_view(c: dict, adv: dict) -> dict:
    import catalog
    nm = c.get("nm_id")
    art = catalog.resolve_wb(nm) if nm else ""
    return {"id": c.get("id"), "nm": nm, "art": art if art and art != str(nm) else "",
            "name": c.get("imt_name") or "", "comment": c.get("user_comment") or "",
            "wb_comment": c.get("wb_comment") or "", "price": c.get("price"),
            "date": (c.get("dt") or "")[:16].replace("T", " "),
            "order_date": (c.get("order_dt") or "")[:10], "delivery_date": (c.get("delivery_dt") or "")[:10],
            "status": CLAIM_STATUS.get(c.get("status"), c.get("status")),
            "status_ex": CLAIM_STATUS_EX.get(c.get("status_ex"), c.get("status_ex")),
            "photos": ["https:" + p if p.startswith("//") else p for p in c.get("photos") or []],
            "videos": ["https:" + p if p.startswith("//") else p for p in c.get("video_paths") or []],
            "actions": [{"code": a, "title": CLAIM_ACTIONS.get(a, a)} for a in c.get("actions") or []],
            "origin_info": c.get("origin_id_info") or "", "source": "чат" if c.get("claim_type") == 3 else "портал",
            "advice": adv.get(f"c:{c.get('id')}")}


@router.get("/claims")
async def claims(refresh: bool = False):
    hit = _cache.get("c")
    if hit and not refresh and _time.time() - hit[0] < _TTL:
        return hit[1]
    if not WB_API_KEY:
        return {"open": [], "archive": [], "errors": ["Нет ключа WB"]}
    errors = []
    try:
        op, _ = await _wb_claims(False)
        await asyncio.sleep(3)
        ar, _ = await _wb_claims(True)
    except Exception as e:
        op, ar = [], []
        errors.append(str(e)[:200])
    adv = await asyncio.to_thread(_drafts)
    res = {"open": [_claim_view(c, adv) for c in op],
           "archive": [_claim_view(c, adv) for c in ar[:100]], "errors": errors,
           "fetched": (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m %H:%M")}
    _cache["c"] = (_time.time(), res)
    return res


@router.post("/claims/advise")
async def claim_advise(body: dict = Body(...)):
    """{claim} → рекомендация ИИ: {verdict, action, reason, comment}. Решает человек."""
    if not ANTHROPIC_API_KEY:
        return {"error": "ИИ не подключён (нет ANTHROPIC_API_KEY)"}
    c = (body or {}).get("claim") or {}
    rules = (await asyncio.to_thread(_rules))["claims"]
    acts = [a["code"] for a in c.get("actions") or []]
    system = (
        "Ты — специалист по претензиям продавца на Wildberries. Покупатель подал заявку на возврат "
        "(рекламацию). Оцени её и порекомендуй решение — окончательно решает человек.\n"
        "Как оценивать: соответствует ли претензия товару и фото; виден ли на фото производственный "
        "брак/повреждение/недовложение/не тот товар; не похоже ли на следы использования, "
        "механические повреждения по вине покупателя, «не подошло/передумал» (это не брак — "
        "такие возвраты WB делает по своим правилам); сколько прошло от доставки. Если фото нет "
        "или по ним не понять — рекомендуй запросить фото/видео (reject2). Дешёвый товар с "
        "правдоподобной претензией выгоднее вернуть без возврата товара (autorefund1), чем "
        "спорить и возить. Явный брак — approve1/approve2. Сомнительный — reject1/reject3 или "
        "rejectcustom с вежливым объяснением.\n"
        f"Доступные действия по этой заявке: {', '.join(acts) or 'нет (работать с заявкой нельзя)'}. "
        "Выбирай ТОЛЬКО из них.\n"
        "Ответь строго JSON без пояснений вокруг: {\"verdict\": \"approve\"|\"reject\"|\"clarify\", "
        "\"action\": \"<код действия>\", \"confidence\": \"высокая\"|\"средняя\"|\"низкая\", "
        "\"reason\": \"2–4 предложения для менеджера: почему\", \"comment\": \"текст покупателю "
        "(10–1000 символов) — нужен для rejectcustom/approvecc1, иначе пустая строка\"}")
    if rules:
        system += "\n\nПРАВИЛА КОМАНДЫ (обязательны и важнее общих соображений):\n" + "\n".join(f"- {r}" for r in rules)
    txt = (f"Товар: {c.get('name') or ''} (арт. WB {c.get('nm')}{', ' + c['art'] if c.get('art') else ''})\n"
           f"Цена: {c.get('price')} ₽\nЗаказ: {c.get('order_date')}, доставлен: {c.get('delivery_date') or '—'}, "
           f"заявка: {c.get('date')}\nКомментарий покупателя: {c.get('comment') or '(нет)'}\n"
           f"Фото: {len(c.get('photos') or [])}, видео: {len(c.get('videos') or [])}\n"
           + (f"Служебно от WB: {c['origin_info']}\n" if c.get("origin_info") else ""))
    content = [{"type": "text", "text": txt}]
    for url in (c.get("photos") or [])[:6]:
        if re.match(r"^https://[a-z0-9.-]+\.wbstatic\.net/", url) or re.match(r"^https://[a-z0-9.-]+\.wb\.ru/", url):
            content.append({"type": "image", "source": {"type": "url", "url": url}})
    try:
        raw = await _claude(system, content, max_tokens=6000, effort="medium")
    except Exception as e:
        if len(content) > 1:     # фото не скачались у модели — без них
            try:
                content[0]["text"] += "\n(Фото не удалось открыть — оценивай по тексту.)"
                raw = await _claude(system, content[:1], max_tokens=6000, effort="medium")
            except Exception as e2:
                return {"error": f"ИИ: {str(e2)[:200]}"}
        else:
            return {"error": f"ИИ: {str(e)[:200]}"}
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        adv = json.loads(m.group(0)) if m else {}
    except Exception:
        adv = {}
    if not adv:
        return {"error": "ИИ ответил не по формату", "raw": raw[:500]}
    if adv.get("action") not in acts and acts:
        adv["note"] = f"ИИ предложил недоступное действие «{adv.get('action')}»"
        adv["action"] = ""
    adv["at"] = (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m %H:%M")
    await asyncio.to_thread(_save_draft, f"c:{c.get('id')}", adv)
    hit = _cache.get("c")
    if hit:
        for x in hit[1]["open"]:
            if x["id"] == c.get("id"):
                x["advice"] = adv
    return adv


@router.post("/claims/answer")
async def claim_answer(body: dict = Body(...)):
    """{id, action, comment?} — решение человека уходит в WB."""
    b = body or {}
    action = str(b.get("action") or "")
    if action not in CLAIM_ACTIONS:
        return {"error": "Неизвестное действие"}
    payload = {"id": b.get("id"), "action": action}
    comment = str(b.get("comment") or "").strip()
    if action == "rejectcustom" and len(comment) < 10:
        return {"error": "Для отказа со своим комментарием нужен текст от 10 символов"}
    if comment and action in ("rejectcustom", "approvecc1"):
        payload["comment"] = comment[:1000]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(f"{_WB_CLAIMS}/api/v1/claim", headers={"Authorization": WB_API_KEY}, json=payload)
    if r.status_code not in (200, 204):
        return {"error": f"WB: HTTP {r.status_code} {r.text[:200]}"}
    _log.info("claim %s → %s", b.get("id"), action)
    _cache.pop("c", None)
    return {"ok": True}
