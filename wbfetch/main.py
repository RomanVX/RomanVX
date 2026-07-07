"""wb-fetch: головной браузер для публичной выдачи WB.

Публичный поиск WB закрыт анти-ботом (JS-челлендж + куки, привязанные к
IP): серверные запросы получают заглушку или 429. Этот сервис открывает
страницу поиска wildberries.ru в headless-Chrome через резидентский
прокси — как обычный посетитель — и перехватывает JSON выдачи, который
страница получает сама (__internal/u-search, v18).

Эндпоинты:
  GET /healthz                  — жив ли сервис (и браузер)
  GET /search?query=&token=     — выдача: {ok, total, products:[...]}

ENV: FETCH_TOKEN (общий секрет с основным приложением),
     WB_SEARCH_PROXY (http://user:pass@host:port — тот же слот, что у
     основного приложения, куки анти-бота привязаны к IP).
"""
import asyncio
import logging
import os
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("wbfetch")

TOKEN = os.getenv("FETCH_TOKEN", "").strip()
PROXY = os.getenv("WB_SEARCH_PROXY", "").strip()

app = FastAPI(title="wb-fetch")

_pw = None
_browser = None
_ctx = None
_lock = asyncio.Lock()


def _proxy_cfg():
    if not PROXY:
        return None
    u = urlparse(PROXY)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = u.username
        cfg["password"] = u.password or ""
    return cfg


async def _ensure_browser():
    global _pw, _browser, _ctx
    if _ctx is not None:
        return
    from playwright.async_api import async_playwright
    _log.info("Запускаем chromium (proxy=%s)...", "да" if PROXY else "нет")
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        proxy=_proxy_cfg(),
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-gpu", "--disable-extensions",
              # экономия памяти: инстанс 512МБ, страница WB тяжёлая
              "--blink-settings=imagesEnabled=false",
              "--mute-audio", "--disable-background-networking",
              "--js-flags=--max-old-space-size=192"],
    )
    _ctx = await _browser.new_context(
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1366, "height": 850},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/149.0.0.0 Safari/537.36"),
    )
    _log.info("chromium готов")


async def _reset_browser():
    global _pw, _browser, _ctx
    for obj, closer in ((_ctx, "close"), (_browser, "close"), (_pw, "stop")):
        try:
            if obj is not None:
                await getattr(obj, closer)()
        except Exception:
            pass
    _pw = _browser = _ctx = None


def _norm_products(data: dict, limit: int) -> tuple[list, int]:
    prods = (data.get("data") or {}).get("products") or []
    out = []
    for p in prods[:limit]:
        price = None
        for sz in p.get("sizes") or []:
            pr = (sz.get("price") or {}).get("product")
            if pr:
                price = pr / 100
                break
        if price is None and p.get("salePriceU"):
            price = p["salePriceU"] / 100
        out.append({
            "nm": p.get("id"),
            "name": p.get("name") or "",
            "brand": p.get("brand") or "",
            "price": round(price) if price else None,
            "rating": p.get("reviewRating") or p.get("rating") or 0,
            "feedbacks": int(p.get("feedbacks") or 0),
            "subject_id": p.get("subjectId"),
            "supplier": p.get("supplier") or "",
        })
    total = int((data.get("data") or {}).get("total") or len(out))
    return out, total


@app.get("/healthz")
async def healthz():
    return {"ok": True, "browser": _ctx is not None}


@app.get("/search")
async def search(query: str, token: str = "", limit: int = 60):
    if TOKEN and token != TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    query = (query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    async with _lock:               # один браузер — по одному поиску за раз
        last_err = ""
        for attempt in (1, 2):
            page = None
            try:
                await _ensure_browser()
                page = await _ctx.new_page()
                url = ("https://www.wildberries.ru/catalog/0/search.aspx?search="
                       + quote(query))

                # не грузим картинки/шрифты/медиа — для JSON выдачи они не
                # нужны, а память 512МБ (страница WB без этого не влезает)
                async def _block(route):
                    if route.request.resource_type in ("image", "media", "font"):
                        await route.abort()
                    else:
                        await route.continue_()
                await page.route("**/*", _block)

                # сначала открываем страницу поиска — браузер проходит
                # JS-челлендж и получает анти-бот-куки в контекст
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)

                # затем сами делаем fetch к u-search с appType=64 ИЗ страницы:
                # куки/заголовки подставит браузер, а appType страница по
                # умолчанию шлёт 1 (и получает preset-заглушку с products=0)
                q_enc = quote(query)
                search_url = (
                    "https://www.wildberries.ru/__internal/u-search/exactmatch/"
                    "ru/common/v18/search?appType=64&curr=rub&dest=-1257786"
                    "&hide_dtype=15&lang=ru&locale=ru&resultset=catalog"
                    "&sort=popular&spp=30&suppressSpellcheck=false&query=" + q_enc)
                data = await page.evaluate(
                    """async (u) => {
                        const r = await fetch(u, {headers: {'x-requested-with': 'XMLHttpRequest'}});
                        try { return await r.json(); } catch(e) { return {__status: r.status}; }
                    }""", search_url)
                prods = (data.get("data") or {}).get("products") or [] if isinstance(data, dict) else []
                if not prods:
                    last_err = f"appType=64 из браузера вернул products=0 (raw: {str(data)[:200]})"
                    _log.warning("search %r attempt %d: %s", query, attempt, last_err)
                    await _reset_browser()
                    continue
                products, total = _norm_products(data, limit)
                if products:
                    _log.info("search %r: %d товаров (total %d)", query, len(products), total)
                    return {"ok": True, "total": total, "products": products}
                last_err = "страница ответила, но товаров в JSON нет"
                _log.warning("search %r attempt %d: %s (%s)", query, attempt,
                             last_err, "; ".join(seen[-5:]))
            except Exception as e:
                last_err = str(e)[:300]
                _log.warning("search %r attempt %d: %s", query, attempt, last_err)
                await _reset_browser()   # свежий браузер на вторую попытку
            finally:
                try:
                    if page is not None:
                        await page.close()
                except Exception:
                    pass
        return {"ok": False, "error": last_err}
