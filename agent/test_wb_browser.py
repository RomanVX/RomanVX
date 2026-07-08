"""Тест: достаётся ли выдача WB через НАСТОЯЩИЙ браузер на этой машине.

Голый HTTP-запрос WB режет (429), но браузер проходит JS-проверку.
Этот тест открывает страницу поиска WB в Chrome (Playwright), проходит
анти-бот и из контекста страницы забирает выдачу v18/appType=64.

Запуск (Windows PowerShell):
    py -m pip install playwright
    py -m playwright install chromium
    py test_wb_browser.py

✅ «N товаров» — браузер на этой машине проходит, сюда можно ставить
   браузерный агент 24/7.
❌ пусто/ошибка — пришли вывод, разберёмся.
"""
import asyncio
import datetime
import urllib.parse

from playwright.async_api import async_playwright

QUERY = "крем для лица с цинком"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            locale="ru-RU", timezone_id="Europe/Moscow", user_agent=UA)
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()

        url = ("https://www.wildberries.ru/catalog/0/search.aspx?search="
               + urllib.parse.quote(QUERY))
        print("Открываю страницу поиска WB...")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        data = await page.evaluate(
            r"""async (query) => {
                const wbauid = (document.cookie.match(/_wbauid=([^;]+)/)||[])[1]||'';
                const n = new Date(), p = x => String(x).padStart(2,'0');
                const ts = ''+n.getFullYear()+p(n.getMonth()+1)+p(n.getDate())
                         +p(n.getHours())+p(n.getMinutes())+p(n.getSeconds());
                const qid = 'qid'+wbauid+ts;
                const u = 'https://www.wildberries.ru/__internal/u-search/exactmatch/'
                        +'ru/common/v18/search?appType=64&curr=rub&dest=-1257786'
                        +'&hide_dtype=15&lang=ru&locale=ru&resultset=catalog'
                        +'&sort=popular&spp=30&suppressSpellcheck=false&query='
                        +encodeURIComponent(query);
                try {
                    const r = await fetch(u, {headers:{'x-queryid':qid,
                        'x-requested-with':'XMLHttpRequest'}});
                    const j = await r.json().catch(()=>null);
                    return {status:r.status, wbauid:wbauid,
                            n:((j&&j.data&&j.data.products)||[]).length};
                } catch(e){ return {status:-1, error:String(e), wbauid:wbauid}; }
            }""", QUERY)

        print("Результат:", data)
        if data and data.get("n"):
            print(f"✅ РАБОТАЕТ — {data['n']} товаров. Сюда можно ставить браузерный агент 24/7.")
        else:
            print("❌ выдача не пришла. wbauid:",
                  "есть" if data.get("wbauid") else "НЕТ", "| status:", data.get("status"))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
