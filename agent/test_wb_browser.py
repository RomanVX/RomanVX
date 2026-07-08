"""Тест v2: перехват СОБСТВЕННЫХ запросов страницы WB + чтение из DOM.

Свой fetch с самодельным x-queryid WB отклоняет (403) — не хватает
токена, который считает их JS. Поэтому даём странице сделать запрос
самой и слушаем ответы u-search; плюс читаем карточки из DOM.

Запуск (PowerShell):
    py -m pip install playwright
    py -m playwright install chromium
    py test_wb_browser.py
"""
import asyncio
import urllib.parse

from playwright.async_api import async_playwright

QUERY = "крем для лица с цинком"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


async def run(headless: bool):
    print(f"\n=== Запуск браузера (headless={headless}) ===")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, args=[
            "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            locale="ru-RU", timezone_id="Europe/Moscow", user_agent=UA)
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()

        seen = []          # (appType, статус, число товаров, url)

        async def on_resp(r):
            if "u-search" in r.url and "/search" in r.url:
                at = "?"
                if "appType=" in r.url:
                    at = r.url.split("appType=")[1].split("&")[0]
                try:
                    j = await r.json()
                    n = len((j.get("data") or {}).get("products") or [])
                except Exception:
                    n = -1
                seen.append((at, r.status, n))

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        url = ("https://www.wildberries.ru/catalog/0/search.aspx?search="
               + urllib.parse.quote(QUERY))
        print("Открываю страницу поиска WB...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("goto:", str(e)[:120])
        await page.wait_for_timeout(6000)   # дать JS дозагрузить выдачу

        print("Ответы u-search, которые сделала сама страница:")
        for at, st, n in seen:
            print(f"   appType={at}  HTTP {st}  товаров={n}")

        # запасной вариант — карточки из DOM
        try:
            dom = await page.evaluate(
                r"""() => document.querySelectorAll(
                    'article.product-card, [data-nm-id], div.product-card').length""")
        except Exception:
            dom = 0
        print(f"Карточек товара в DOM: {dom}")

        best = max((n for _, _, n in seen), default=0)
        if best > 0 or dom > 0:
            print(f"✅ ВЫДАЧА ЕСТЬ (запрос/DOM). Здесь можно ставить браузерный агент.")
        else:
            print("❌ выдачи нет — см. статусы выше.")
        await browser.close()
        return best > 0 or dom > 0


async def main():
    ok = await run(headless=True)
    if not ok:
        print("\nHeadless не дал выдачу — пробую с видимым окном (headed)...")
        await run(headless=False)


if __name__ == "__main__":
    asyncio.run(main())
