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
import os
import urllib.parse

import httpx
from playwright.async_api import async_playwright

DASHBOARD = os.getenv("DASHBOARD_URL", "https://wb-dashboard-6wxf.onrender.com").rstrip("/")
TOKEN = os.getenv("WB_AGENT_TOKEN", "")
POLL_SEC = 4
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

        while True:
            try:
                r = httpx.get(f"{DASHBOARD}/api/tools/niche/pending",
                              params={"token": TOKEN}, timeout=30)
                queries = r.json().get("queries") or []
            except Exception as e:
                print("нет связи с дашбордом:", str(e)[:120])
                await asyncio.sleep(POLL_SEC)
                continue
            for q in queries:
                print(f"→ собираю выдачу: {q!r}")
                try:
                    items = await _search(page, q)
                    body = {"query": q, "products": items, "total": len(items)}
                    print(f"   получено {len(items)} товаров")
                except Exception as e:
                    body = {"query": q, "error": str(e)[:200]}
                    print("   ошибка:", str(e)[:160])
                try:
                    httpx.post(f"{DASHBOARD}/api/tools/niche/ingest",
                               params={"token": TOKEN}, json=body, timeout=30)
                except Exception as e:
                    print("   не отдал результат:", str(e)[:120])
            await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
