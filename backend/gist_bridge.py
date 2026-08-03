"""Мост дашборд ↔ домашний агент через секретный GitHub Gist.

Провайдер (МГТС) заблокировал диапазон Render/Cloudflare — домашний ПК не
достаёт до дашборда напрямую. GitHub доступен обоим: сервер публикует
очередь заданий в tasks.json, агент кладёт результаты в results.json.

Env (Render): GIST_BRIDGE_ID, GIST_BRIDGE_TOKEN (classic PAT, scope gist).
Без них мост молчит, прямой канал /niche/pending продолжает работать.
"""
import asyncio
import json
import logging
import os
import time

import httpx

_log = logging.getLogger("gist_bridge")
GIST_ID = os.getenv("GIST_BRIDGE_ID", "").strip()
TOKEN = os.getenv("GIST_BRIDGE_TOKEN", "").strip()
API = "https://api.github.com/gists/"


def configured() -> bool:
    return bool(GIST_ID and TOKEN)


async def loop():
    if not configured():
        return
    from routers import tools as _t
    _log.info("gist-мост включён (%s…)", GIST_ID[:6])
    issued: dict = {}      # query → метка выдачи (для дедупа у агента)
    consumed: dict = {}    # query → ts обработанного результата
    last_push = ""
    async with httpx.AsyncClient(timeout=30, headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json"}) as c:
        while True:
            try:
                pending = list(_t._agent_pending.keys())
                for q in pending:
                    issued.setdefault(q, str(int(time.time())))
                for q in [q for q in issued if q not in pending]:
                    issued.pop(q, None)
                payload = json.dumps(
                    {"queries": pending,
                     "issued": {q: issued[q] for q in pending}},
                    ensure_ascii=False)
                if payload != last_push:
                    await c.patch(API + GIST_ID, json={
                        "files": {"tasks.json": {"content": payload or "{}"}}})
                    last_push = payload
                r = await c.get(API + GIST_ID)
                files = (r.json() or {}).get("files") or {}
                raw = (files.get("results.json") or {}).get("content") or "{}"
                res = json.loads(raw).get("results") or {}
                for q, body in res.items():
                    ts = str(body.get("ts") or "")
                    if not ts or consumed.get(q) == ts:
                        continue
                    consumed[q] = ts
                    _t._agent_pending.pop(q, None)
                    issued.pop(q, None)
                    _t._agent_inbox[q] = (
                        {"error": str(body.get("error"))[:300]}
                        if body.get("error") else
                        {"products": body.get("products") or [],
                         "total": int(body.get("total") or 0)})
                    _log.info("мост: результат %r (%d поз.)", q[:40],
                              len(body.get("products") or []))
            except Exception as e:
                _log.warning("мост: %s", str(e)[:150])
            await asyncio.sleep(6)
