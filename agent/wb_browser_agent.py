"""Браузерный агент сбора выдачи WB — для машины с ЖИЛЫМ IP (всегда вкл).

Голый HTTP-запрос WB режет (429/403), но настоящий Chrome на жилом IP
отрисовывает карточки товаров. Агент держит браузер, опрашивает дашборд
(/api/tools/niche/pending), для каждого запроса открывает поиск WB,
читает товары из DOM и отдаёт в /api/tools/niche/ingest.

Установка (один раз, PowerShell):
    py -m pip install playwright httpx
    py -m playwright install chromium

Запуск:
    set DASHBOARD_URL=https://wb-dashboard-6wxf.onrender.com
    set WB_AGENT_TOKEN=<тот же токен, что в дашборде WB_AGENT_TOKEN>
    py wb_browser_agent.py

Оставь окно открытым (или добавь в автозагрузку — см. README).
"""
import asyncio
import json
import os
import time
import urllib.parse

import httpx
from playwright.async_api import async_playwright

DASHBOARD = os.getenv("DASHBOARD_URL", "https://wb-dashboard-6wxf.onrender.com").rstrip("/")
TOKEN = os.getenv("WB_AGENT_TOKEN", "")
# Мост через GitHub Gist: провайдер блокирует Render — если заданы эти два
# env, агент общается с дашбордом через gist (см. инструкцию в чате)
GIST_ID = os.getenv("GIST_BRIDGE_ID", "").strip()
GIST_TOKEN = os.getenv("GIST_BRIDGE_TOKEN", "").strip()
GH_API = "https://api.github.com/gists/"


def _gh_hdr():
    return {"Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json"}
POLL_SEC = 4
# системный прокси Windows игнорируем: битый прокси (след Tailscale и т.п.)
# даёт WinError 10060, хотя браузер работает — ходим напрямую
# и принудительный IPv4: битый v6-маршрут (Tailscale) даёт тот же 10060,
# браузер умеет откатываться на v4, httpx — заставляем явно
_HTTP = httpx.Client(timeout=30, trust_env=False,
                     transport=httpx.HTTPTransport(local_address="0.0.0.0"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# JS читает карточки товаров прямо из отрисованной страницы WB
_DOM_JS = r"""
() => {
    const out = [];
    const cards = document.querySelectorAll('article.product-card, div.product-card, [data-nm-id]');
    cards.forEach(c => {
        const nm = c.getAttribute('data-nm-id')
            || (c.querySelector('a[href*="/catalog/"]')?.href.match(/catalog\/(\d+)\//)?.[1]);
        if (!nm) return;
        const txt = s => (c.querySelector(s)?.textContent || '').trim();
        const priceRaw = txt('.price__lower-price, ins.price__lower-price, .product-card__price');
        const price = parseInt((priceRaw.match(/[\d\s]+/)?.[0] || '').replace(/\s/g,'')) || null;
        const fbRaw = txt('.product-card__count, .product-card__feedback');
        const fb = parseInt((fbRaw.match(/\d[\d\s]*/)?.[0] || '').replace(/\s/g,'')) || 0;
        const rtRaw = txt('.address-rate-mini, .product-card__rating, [class*="rating"]');
        const rt = parseFloat((rtRaw.match(/[\d.,]+/)?.[0] || '0').replace(',','.')) || 0;
        // реальный URL заглавного фото/превью прямо из карточки
        const img = c.querySelector('img.j-thumbnail, img.product-card__img, img');
        const photo = img ? (img.src || img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
        const name = txt('.product-card__name, .goods-name').replace(/^\/\s*/,'');
        // пропускаем рекламные/пустые вставки (нет названия и данных)
        if (!name && !fb && !price) return;
        out.push({
            nm: parseInt(nm),
            name: name,
            brand: txt('.product-card__brand, .brand-name'),
            price: price, rating: rt, feedbacks: fb,
            photo: photo, subject_id: null,
            supplier: txt('.product-card__brand-name') || '',
        });
    });
    return out;
}
"""


async def _search(page, query: str, limit: int = 60):
    url = ("https://www.wildberries.ru/catalog/0/search.aspx?search="
           + urllib.parse.quote(query))
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # ждём появления карточек (до ~20с), затем даём дорисоваться
    for _ in range(20):
        try:
            n = await page.evaluate(
                "() => document.querySelectorAll('[data-nm-id], .product-card').length")
        except Exception:
            n = 0
        if n > 0:
            break
        await page.wait_for_timeout(1000)
    await page.wait_for_timeout(1500)
    items = await page.evaluate(_DOM_JS)
    items = [p for p in (items or []) if p.get("nm")][:limit]
    return items


async def _own_prices(page, nm_csv: str):
    """Клиентские цены своих карточек: fetch к card.wb.ru прямо из браузера
    (жилой IP + куки анти-бота) — это цена, которую видит покупатель, с СПП."""
    out = []
    nms = [n for n in nm_csv.split(",") if n.strip().isdigit()]
    for i in range(0, len(nms), 100):
        chunk = ";".join(nms[i:i + 100])
        url = ("https://card.wb.ru/cards/v2/detail?appType=1&curr=rub"
               "&dest=-1257786&spp=30&nm=" + chunk)
        data = await page.evaluate(
            "(u) => fetch(u, {credentials:'include'}).then(r => r.json())"
            ".catch(e => ({err: String(e)}))", url)
        products = ((data or {}).get("data") or {}).get("products") or []
        for p in products:
            sizes = p.get("sizes") or []
            price = ((sizes[0].get("price") or {}) if sizes else {})
            total = price.get("product") or price.get("total") or 0
            if p.get("id") and total:
                out.append({"nm": int(p["id"]),
                            "client": round(total / 100)})
        await page.wait_for_timeout(800)
    return out


async def main():
    print(f"WB браузер-агент запущен. Дашборд: {DASHBOARD}")
    print("Держу браузер, опрашиваю очередь. Не закрывай это окно.\n")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled", "--no-sandbox"])
        ctx = await browser.new_context(
            locale="ru-RU", timezone_id="Europe/Moscow", user_agent=UA,
            viewport={"width": 1366, "height": 900})
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()
        # прогрев: один заход на WB, чтобы получить куки анти-бота
        try:
            await page.goto("https://www.wildberries.ru/", timeout=60000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass

        done: dict = {}
        while True:
            issued = {}
            try:
                if GIST_ID:
                    g = _HTTP.get(GH_API + GIST_ID, headers=_gh_hdr()).json()
                    tasks = json.loads(((g.get("files") or {}).get("tasks.json")
                                        or {}).get("content") or "{}")
                    issued = tasks.get("issued") or {}
                    queries = [q for q in tasks.get("queries") or []
                               if done.get(q) != issued.get(q, "")]
                else:
                    r = _HTTP.get(f"{DASHBOARD}/api/tools/niche/pending",
                                  params={"token": TOKEN})
                    queries = r.json().get("queries") or []
            except Exception as e:
                print("нет связи:", str(e)[:120])
                await asyncio.sleep(POLL_SEC)
                continue
            for q in queries:
                try:
                    if q.startswith("nmprice:"):
                        print("→ снимаю клиентские цены своих карточек")
                        items = await _own_prices(page, q.split(":", 1)[1])
                        body = {"query": q, "products": items, "total": len(items)}
                        print(f"   получено цен: {len(items)}")
                    else:
                        print(f"→ собираю выдачу: {q!r}")
                        items = await _search(page, q)
                        body = {"query": q, "products": items, "total": len(items)}
                        print(f"   получено {len(items)} товаров")
                except Exception as e:
                    body = {"query": q, "error": str(e)[:200]}
                    print("   ошибка:", str(e)[:160])
                try:
                    if GIST_ID:
                        g = _HTTP.get(GH_API + GIST_ID, headers=_gh_hdr()).json()
                        raw = ((g.get("files") or {}).get("results.json")
                               or {}).get("content") or "{}"
                        res = json.loads(raw) if raw.strip() else {}
                        res.setdefault("results", {})[q] = {
                            **{k: v for k, v in body.items() if k != "query"},
                            "ts": str(int(time.time()))}
                        # держим только последние 6 результатов — gist не пухнет
                        keys = list(res["results"])
                        for old in keys[:-6]:
                            res["results"].pop(old, None)
                        _HTTP.patch(GH_API + GIST_ID, headers=_gh_hdr(), json={
                            "files": {"results.json": {"content":
                                json.dumps(res, ensure_ascii=False)}}})
                        done[q] = issued.get(q, "")
                    else:
                        _HTTP.post(f"{DASHBOARD}/api/tools/niche/ingest",
                                   params={"token": TOKEN}, json=body)
                except Exception as e:
                    print("   не отдал результат:", str(e)[:120])
            await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
