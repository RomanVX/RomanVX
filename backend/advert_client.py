"""Async client for Wildberries Advertising (Advert) API.

WB has TWO possible advert API bases; we try both.
Also, the GET /adv/v1/promotion/adverts endpoint returns EITHER:
  - a list of integers  [12345, 67890, ...]
  - a list of objects   [{advertId: 12345, ...}, ...]
depending on WB API version. We handle both.

Requires a separate WB_ADVERT_KEY (set in Render env vars as WB_ADVERT_KEY).
Falls back to WB_API_KEY if WB_ADVERT_KEY is not set.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from config import WB_ADVERT_KEY

_log = logging.getLogger(__name__)

_BASES = [
    "https://advert-api.wildberries.ru",
]

TYPE_NAMES   = {4: "Каталог", 5: "Поиск+Каталог", 6: "Поиск", 7: "Поиск", 8: "Автокампания", 9: "Поиск"}
STATUS_NAMES = {-1: "Удалена", 4: "Готова", 7: "Активна", 8: "Пауза", 9: "Завершена", 11: "Пауза WB"}


def _headers() -> dict:
    return {"Authorization": WB_ADVERT_KEY, "Content-Type": "application/json"}


async def _get(path: str, params: dict | None = None) -> list | dict:
    last_exc = None
    for base in _BASES:
        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(url, headers=_headers(), params=params or {})
                _log.info("[ADVERT] GET %s params=%s → %s body[:200]=%s",
                          url, params, r.status_code, r.text[:200])
                if r.status_code == 204:
                    return []
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            _log.warning("[ADVERT] GET %s FAILED: %s", url, e)
    raise last_exc


async def _post(path: str, body) -> list | dict:
    last_exc = None
    for base in _BASES:
        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(url, headers=_headers(), json=body)
                _log.info("[ADVERT] POST %s payload_len=%d → %s body[:200]=%s",
                          url, len(body) if isinstance(body, list) else 1,
                          r.status_code, r.text[:200])
                if r.status_code == 204:
                    return []
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            _log.warning("[ADVERT] POST %s FAILED: %s", url, e)
    raise last_exc


def _extract_ids(data) -> list[int]:
    """Handle both [int, ...] and [{advertId: int, ...}, ...] response formats."""
    if not isinstance(data, list):
        return []
    ids = []
    for item in data:
        if isinstance(item, int):
            ids.append(item)
        elif isinstance(item, dict):
            aid = item.get("advertId") or item.get("id")
            if aid:
                ids.append(int(aid))
    return ids


async def _ids_for_status(status: int) -> list[int]:
    """GET /adv/v1/promotion/adverts?status=S — returns campaign IDs for that status."""
    try:
        data = await _get("/adv/v1/promotion/adverts",
                          {"status": status, "limit": 100, "offset": 0})
        _log.info("[ADVERT] status=%d raw type=%s raw[:3]=%s",
                  status, type(data).__name__, str(data)[:300])
        ids = _extract_ids(data)
        _log.info("[ADVERT] status=%d → %d IDs extracted", status, len(ids))
        return ids
    except Exception as e:
        _log.warning("[ADVERT] ids_for_status(%s): %s", status, e)
        return []


async def get_all_campaign_ids() -> list[int]:
    """Fetch IDs for active (7), paused (8) and ready (4) campaigns."""
    results = await asyncio.gather(
        _ids_for_status(7),
        _ids_for_status(8),
        _ids_for_status(4),
    )
    seen, ids = set(), []
    for chunk in results:
        for i in chunk:
            if i not in seen:
                seen.add(i)
                ids.append(i)
    _log.info("[ADVERT] get_all_campaign_ids → %d IDs: %s", len(ids), ids[:10])
    return ids


async def get_campaign_details(ids: list[int]) -> list[dict]:
    """POST /adv/v1/promotion/adverts body=[id, ...] → campaign objects."""
    if not ids:
        return []
    result = await _post("/adv/v1/promotion/adverts", ids[:100])
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    return []


def _is_bonus_payment(rec: dict) -> bool:
    """Списание компенсировано WB (промо-бонусы / кэшбэк — поддержка площадки).

    Реальные значения paymentType в /adv/v1/upd: «Счет», «Баланс»,
    «Кэшбэк», «Бонусы» — компенсация приходит как Кэшбэк/Бонусы.
    """
    for key in ("paymentType", "payment_type", "type"):
        v = rec.get(key)
        if isinstance(v, str) and ("бонус" in v.lower() or "кэшбэк" in v.lower()
                                   or "кешбэк" in v.lower()):
            return True
    return False


_spend_lock = asyncio.Lock()
_spend_cache: dict = {}   # key → (monotonic, result)


async def get_spend_by_month(date_from: datetime, date_to: datetime) -> dict[str, dict]:
    """GET /adv/v1/upd — история списаний за рекламу.

    Возвращает {YYYY-MM: {"total": всего, "bonus": из них промо-бонусами}}.
    API отдаёт максимум 31 день за запрос — ходим чанками.
    Одновременные вызовы с одним диапазоном дедуплицируются (lock + кеш 10 мин),
    иначе параллельные сборки P&L ловят 429 от лимитера WB."""
    import time as _t
    key = f"{date_from:%Y-%m-%d}_{date_to:%Y-%m-%d}"
    async with _spend_lock:
        hit = _spend_cache.get(key)
        if hit and _t.monotonic() - hit[0] < 600:
            return hit[1]
        result = await _spend_by_month_impl(date_from, date_to)
        if result:
            _spend_cache.clear()
            _spend_cache[key] = (_t.monotonic(), result)
        return result


async def _spend_by_month_impl(date_from: datetime, date_to: datetime) -> dict[str, dict]:
    spend: dict[str, dict] = {}
    cur = date_from
    while cur <= date_to:
        chunk_end = min(cur + timedelta(days=30), date_to)
        try:
            data = await _get("/adv/v1/upd", {
                "from": cur.strftime("%Y-%m-%d"),
                "to":   chunk_end.strftime("%Y-%m-%d"),
            })
            for rec in data if isinstance(data, list) else []:
                mk = (rec.get("updTime") or "")[:7]
                if not mk:
                    continue
                m = spend.setdefault(mk, {"total": 0.0, "bonus": 0.0, "balance": 0.0})
                amt = float(rec.get("updSum") or 0)
                m["total"] += amt
                if _is_bonus_payment(rec):
                    m["bonus"] += amt
                else:
                    pt = str(rec.get("paymentType") or "").lower()
                    if "баланс" in pt:
                        # списано с баланса продаж — в кабинете сидит в «Удержаниях»
                        m["balance"] += amt
        except Exception as e:
            _log.warning("[ADVERT] upd %s–%s failed: %s", cur.date(), chunk_end.date(), e)
        cur = chunk_end + timedelta(days=1)
    _log.info("[ADVERT] spend by month: %s",
              {k: (round(v["total"]), round(v["bonus"])) for k, v in spend.items()})
    return spend


async def get_all_campaign_ids_ext() -> list[int]:
    """Все ID кампаний (все статусы, вкл. завершённые) — для истории рекламы.

    Канонический источник — GET /adv/v1/promotion/count: возвращает группы
    {type, status, count, advert_list: [{advertId, changeTime}]}.
    Фолбэк — постатусные запросы promotion/adverts.
    """
    seen, ids = set(), []
    global _count_meta
    _count_meta = {}
    try:
        data = await _get("/adv/v1/promotion/count")
        if isinstance(data, dict):
            for grp in data.get("adverts") or []:
                for a in grp.get("advert_list") or []:
                    aid = a.get("advertId")
                    if aid and aid not in seen:
                        seen.add(aid)
                        ids.append(int(aid))
                        _count_meta[int(aid)] = {"type": grp.get("type"),
                                                 "status": grp.get("status")}
    except Exception as e:
        _log.warning("[ADVERT] promotion/count failed: %s", e)
    if not ids:
        for status in (7, 8, 9, 11, 4):
            for i in await _ids_for_status(status):
                if i not in seen:
                    seen.add(i)
                    ids.append(i)
    _log.info("[ADVERT] campaign ids (all statuses): %d", len(ids))
    return ids


async def get_fullstats_nm(ids: list[int], begin: str, end: str) -> dict[int, float]:
    """GET /adv/v3/fullstats за период → {nmId: затраты}.

    Ответ: [{advertId, days: [{date, apps: [{nms: [{nmId, sum}]}]}]}].
    Лимит ~1 req/мин — вызывающий обязан выдерживать паузы между вызовами.
    """
    per_nm: dict[int, float] = {}
    CHUNK = 50
    for ci in range(0, len(ids), CHUNK):
        chunk = ids[ci:ci + CHUNK]
        data = None
        for attempt in range(4):
            try:
                data = await _get("/adv/v3/fullstats", {
                    "ids": ",".join(map(str, chunk)),
                    "beginDate": begin,
                    "endDate": end,
                })
                break
            except Exception as e:
                if "429" in str(e) and attempt < 3:
                    _log.warning("[ADVERT] fullstats 429 — ждём 62с (%d/3)", attempt + 1)
                    await asyncio.sleep(62)
                    continue
                _log.warning("[ADVERT] fullstats %s–%s failed: %s", begin, end, e)
                data = None
                break
        for camp in data if isinstance(data, list) else []:
            for day in camp.get("days") or []:
                for app in day.get("apps") or []:
                    for nm in app.get("nms") or []:
                        nm_id = nm.get("nmId")
                        if nm_id:
                            per_nm[nm_id] = per_nm.get(nm_id, 0.0) + float(nm.get("sum") or 0)
        if ci + CHUNK < len(ids):
            await asyncio.sleep(62)  # rate limit между чанками
    return per_nm


async def get_fullstats(ids: list[int], date_from: datetime, date_to: datetime) -> list[dict]:
    """POST /adv/v2/fullstats — per-campaign aggregated stats."""
    if not ids:
        return []

    dates = []
    cur = date_from
    while cur <= date_to and len(dates) < 31:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    payload = [{"id": i, "dates": dates} for i in ids[:100]]
    _log.info("[ADVERT] fullstats payload preview (first 3): %s", payload[:3])
    try:
        result = await _post("/adv/v2/fullstats", payload)
        _log.info("[ADVERT] fullstats result type=%s len=%s preview=%s",
                  type(result).__name__,
                  len(result) if isinstance(result, list) else "n/a",
                  str(result)[:300])
        return result if isinstance(result, list) else []
    except Exception as e:
        _log.warning("[ADVERT] fullstats error: %s", e)
        return []


_count_meta: dict[int, dict] = {}   # id → {type, status} из promotion/count


def get_count_meta() -> dict[int, dict]:
    return dict(_count_meta)


async def get_campaigns_meta(ids: list[int]) -> dict[int, dict]:
    """{advertId: {name, type, status}} через POST promotion/adverts (чанки по 50)."""
    out: dict[int, dict] = {}
    for ci in range(0, len(ids), 50):
        try:
            data = await _post("/adv/v1/promotion/adverts?order=create&direction=desc",
                               ids[ci:ci + 50])
        except Exception as e:
            _log.warning("[ADVERT] adverts meta failed: %s", e)
            continue
        if not isinstance(data, list) or not data:
            _log.warning("[ADVERT] adverts meta: неожиданный ответ %s", str(data)[:300])
        for c in data if isinstance(data, list) else []:
            aid = c.get("advertId")
            if not aid:
                continue
            meta = {"name": c.get("name") or f"Кампания {aid}",
                    "type": c.get("type"), "status": c.get("status"),
                    "nms": [], "bids": {}}
            # ставки и товары: структура зависит от типа кампании
            ap = c.get("autoParams") or {}
            if ap:
                if ap.get("cpm") is not None:
                    meta["bids"]["cpm"] = ap.get("cpm")
                meta["nms"] += [int(n) for n in (ap.get("nms") or []) if n]
            for up in c.get("unitedParams") or []:
                if up.get("searchCPM") is not None:
                    meta["bids"]["search"] = up.get("searchCPM")
                if up.get("catalogCPM") is not None:
                    meta["bids"]["catalog"] = up.get("catalogCPM")
                meta["nms"] += [int(n) for n in (up.get("nms") or []) if n]
            for p in c.get("params") or []:
                if p.get("price") is not None:
                    meta["bids"]["cpm"] = p.get("price")
                for nm in p.get("nms") or []:
                    v = nm.get("nm") if isinstance(nm, dict) else nm
                    if v:
                        meta["nms"].append(int(v))
            meta["nms"] = list(dict.fromkeys(meta["nms"]))[:60]
            out[int(aid)] = meta
        await asyncio.sleep(1)
    return out


async def get_fullstats_campaigns(ids: list[int], begin: str, end: str) -> dict[int, dict]:
    """GET /adv/v3/fullstats → агрегаты по кампаниям за период.

    {advertId: {views, clicks, sum, orders, sum_price, atbs}} — суммируем
    по дням/платформам. Лимит ~1 req/мин на чанк из 50 id."""
    out: dict[int, dict] = {}
    CHUNK = 50
    for ci in range(0, len(ids), CHUNK):
        chunk = ids[ci:ci + CHUNK]
        data = None
        for attempt in range(4):
            try:
                data = await _get("/adv/v3/fullstats", {
                    "ids": ",".join(map(str, chunk)),
                    "beginDate": begin, "endDate": end,
                })
                break
            except Exception as e:
                if "429" in str(e) and attempt < 3:
                    await asyncio.sleep(62)
                    continue
                _log.warning("[ADVERT] fullstats(campaigns) failed: %s", e)
                data = None
                break
        for camp in data if isinstance(data, list) else []:
            aid = camp.get("advertId")
            if not aid:
                continue
            agg = out.setdefault(int(aid), {"views": 0, "clicks": 0, "sum": 0.0,
                                            "orders": 0, "sum_price": 0.0, "atbs": 0,
                                            "nms": {}})
            for day in camp.get("days") or []:
                for app in day.get("apps") or []:
                    agg["views"] += int(app.get("views") or 0)
                    agg["clicks"] += int(app.get("clicks") or 0)
                    agg["sum"] += float(app.get("sum") or 0)
                    agg["orders"] += int(app.get("orders") or 0)
                    agg["sum_price"] += float(app.get("sum_price") or 0)
                    agg["atbs"] += int(app.get("atbs") or 0)
                    for nm in app.get("nms") or []:
                        nid = nm.get("nmId")
                        if nid:
                            cell = agg["nms"].setdefault(int(nid), {"sum": 0.0, "orders": 0})
                            cell["sum"] += float(nm.get("sum") or 0)
                            cell["orders"] += int(nm.get("orders") or 0)
        if ci + CHUNK < len(ids):
            await asyncio.sleep(62)
    return out


async def get_campaign_words(advert_id: int, ctype: int | None = None) -> list[dict]:
    """Статистика ключевых фраз кампании → [{phrase, views, clicks, ctr, sum}].

    Поисковые/аукционные кампании: GET /adv/v1/stat/words (поле stat — метрики
    по фразам). Автоматические (type 8): GET /adv/v2/auto/stat-words (кластеры,
    только счётчики). Возвращаем нормализованный список, пустой — если тип
    кампании фраз не отдаёт."""
    words: list[dict] = []
    try:
        data = await _get("/adv/v1/stat/words", {"id": advert_id})
        for s in (data.get("stat") or []) if isinstance(data, dict) else []:
            kw = s.get("keyword") or ""
            if not kw or kw == "Всего по кампании":
                continue
            words.append({"phrase": kw,
                          "views": int(s.get("views") or 0),
                          "clicks": int(s.get("clicks") or 0),
                          "ctr": float(s.get("ctr") or 0),
                          "sum": float(s.get("sum") or 0)})
    except Exception:
        pass
    if not words and ctype == 8:
        try:
            data = await _get("/adv/v2/auto/stat-words", {"id": advert_id})
            clusters = (data.get("clusters") or []) if isinstance(data, dict) else []
            for c in clusters:
                words.append({"phrase": c.get("cluster") or "", "views": int(c.get("count") or 0),
                              "clicks": 0, "ctr": 0.0, "sum": 0.0, "cluster": True,
                              "keywords": (c.get("keywords") or [])[:8]})
        except Exception:
            pass
    return words


async def get_campaigns_info(ids: list[int]) -> list[dict]:
    """Детали кампаний: GET /api/advert/v2/adverts?ids=… (актуальный метод,
    единая/ручная ставка; старый POST /adv/v1/promotion/adverts выключен WB).
    Максимум 50 ID за запрос."""
    if not ids:
        return []
    out: list[dict] = []
    chunk = [str(int(i)) for i in ids[:50]]
    try:
        data = await _get("/api/advert/v2/adverts", {"ids": ",".join(chunk)})
        adverts = (data.get("adverts") or []) if isinstance(data, dict) else \
            (data if isinstance(data, list) else [])
        for c in adverts:
            if isinstance(c, dict):
                c.setdefault("advertId", c.get("advertId") or c.get("id"))
                out.append(c)
        _log.info("[ADVERT] advert/v2/adverts → %d кампаний", len(out))
    except Exception as e:
        _log.warning("[ADVERT] advert/v2/adverts: %s", str(e)[:150])
    return out


async def get_cluster_bids(advert_ids: list[int]) -> list[dict]:
    """Ставки поисковых кластеров: POST /adv/v0/normquery/get-bids.
    Возвращает [{advert_id, nm_id, cluster, bid}] — поля нормализуем."""
    if not advert_ids:
        return []
    body = {"items": [{"advert_id": int(i)} for i in advert_ids[:100]]}
    try:
        data = await _post("/adv/v0/normquery/get-bids", body)
    except Exception as e:
        _log.warning("[ADVERT] normquery/get-bids: %s", str(e)[:150])
        return []
    rows = []
    src = (data.get("bids") or []) if isinstance(data, dict) else []
    for b in src:
        if not isinstance(b, dict):
            continue
        subs = b.get("bids") or b.get("norm_queries") or b.get("normQueries")
        if isinstance(subs, list) and subs and isinstance(subs[0], dict):
            for q in subs:
                rows.append({
                    "advert_id": b.get("advert_id") or b.get("advertId"),
                    "nm_id": b.get("nm_id") or b.get("nmId") or q.get("nm_id"),
                    "cluster": q.get("norm_query") or q.get("normQuery")
                               or q.get("cluster") or q.get("name") or "",
                    "bid": q.get("bid") or q.get("cpm") or q.get("price"),
                })
        else:
            rows.append({
                "advert_id": b.get("advert_id") or b.get("advertId"),
                "nm_id": b.get("nm_id") or b.get("nmId"),
                "cluster": b.get("norm_query") or b.get("normQuery")
                           or b.get("cluster") or b.get("name") or "",
                "bid": b.get("bid") or b.get("cpm") or b.get("price"),
            })
    _log.info("[ADVERT] cluster bids: %d строк", len(rows))
    return rows


async def get_cluster_stats(pairs: list[tuple[int, int]],
                            d_from: str, d_to: str) -> list[dict]:
    """Статистика поисковых кластеров: POST /adv/v0/normquery/stats.
    pairs — [(advert_id, nm_id)]; ответ: по каждому кластеру views, clicks,
    ctr, atbs, orders, cpm, spend, avg_pos (по спеке docs/wb_api/promotion.yaml)."""
    if not pairs:
        return []
    body = {"from": d_from, "to": d_to,
            "items": [{"advert_id": int(a), "nm_id": int(n)}
                      for a, n in pairs[:100]]}
    try:
        data = await _post("/adv/v0/normquery/stats", body)
    except Exception as e:
        _log.warning("[ADVERT] normquery/stats: %s", str(e)[:150])
        return []
    rows = []
    for it in (data.get("stats") or []) if isinstance(data, dict) else []:
        aid, nm = it.get("advert_id"), it.get("nm_id")
        for st in it.get("stats") or []:
            rows.append({"advert_id": aid, "nm_id": nm,
                         "cluster": st.get("norm_query") or "",
                         "views": st.get("views"), "clicks": st.get("clicks"),
                         "ctr": st.get("ctr"), "orders": st.get("orders"),
                         "atbs": st.get("atbs"), "cpm": st.get("cpm"),
                         "spend": st.get("spend"), "avg_pos": st.get("avg_pos")})
    _log.info("[ADVERT] cluster stats: %d строк", len(rows))
    return rows
