"""Рынок WB по поисковым запросам — запускается на компьютере владельца.

Что делает: для каждого запроса из queries.txt открывает поиск WB в
настоящем Chrome/Edge (домашний IP, серверные IP WB банит), перехватывает
данные выдачи, считает сводку и сохраняет Excel рядом со скриптом:

  «Сводка»  — по запросу: найдено товаров, цены для покупателя (медиана,
              разброс), рейтинг и отзывы топа, доля рекламы, остатки топа,
              наша лучшая позиция и наши карточки;
  «Выдача»  — все товары по местам: бренд, продавец, цена, рейтинг,
              отзывы, остаток, реклама, «наш»;
  «Бренды»  — доли брендов в топе (по числу карточек и по отзывам).

Установка (один раз, PowerShell):
    py -m pip install playwright openpyxl
    (браузер ставить не нужно — берётся установленный Chrome или Edge)

Запуск:
    py wb_market.py                 — запросы из queries.txt, 1 страница (100 шт.)
    py wb_market.py --pages 2       — глубже, 200 товаров на запрос
    py wb_market.py --show          — с окном браузера (если WB что-то спрашивает)

queries.txt — по одному запросу в строке (# — комментарий).
ours.txt    — наши бренды/продавцы/артикулы WB, по одному в строке:
              по ним считается «наша позиция» (необязательно).

Режим бота (задания и результат — в дашборде, «Wildberries → Рынок по запросам»):
    setx WB_AGENT_TOKEN "<токен из Render, переменная WB_AGENT_TOKEN>"   (один раз)
    py wb_market.py --agent
    Адрес дашборда по умолчанию — biomed.marketpartners.ru (доступен без VPN),
    запасной — wb-dashboard на Render; свой — переменная DASHBOARD_URL.

VPN: WB должен открываться напрямую (иначе выдача другая или блок) —
выключите VPN или пустите wildberries.ru мимо него.
"""
import argparse
import asyncio
import json
import os
import re
import statistics as st
import sys
import time
import urllib.parse
from datetime import datetime

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("Нет playwright: py -m pip install playwright openpyxl")
try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("Нет openpyxl: py -m pip install openpyxl")

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
TOP = 30          # «топ» для средних по рейтингу/отзывам/рекламе


# ── входные файлы ─────────────────────────────────────────────────────────
def _lines(name: str) -> list[str]:
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8-sig") as f:
        return [s.strip() for s in f if s.strip() and not s.strip().startswith("#")]


def _ours():
    marks = _lines("ours.txt")
    nms = {int(m) for m in marks if m.isdigit()}
    words = [m.lower() for m in marks if not m.isdigit()]
    return nms, words


# ── разбор ответа поиска WB ───────────────────────────────────────────────
def _products(js) -> tuple[list, int | None]:
    """Товары и «найдено всего» из JSON поиска (формат у WB плавает)."""
    if not isinstance(js, dict):
        return [], None
    d = js.get("data") if isinstance(js.get("data"), dict) else js
    prods = d.get("products") or []
    total = d.get("total") or js.get("total") \
        or (js.get("metadata") or {}).get("total")
    return (prods if isinstance(prods, list) else []), total


def _price(p) -> int | None:
    for s in p.get("sizes") or []:
        pr = s.get("price") or {}
        v = pr.get("product") or pr.get("total")
        if v:
            return round(v / 100)
    v = p.get("salePriceU") or p.get("priceU")
    return round(v / 100) if v else None


def _row(p, pos: int) -> dict:
    return {
        "pos": pos,
        "nm": p.get("id"),
        "name": p.get("name") or "",
        "brand": p.get("brand") or "",
        "supplier": p.get("supplier") or "",
        "price": _price(p),
        "rating": p.get("reviewRating") or p.get("nmReviewRating") or p.get("rating") or 0,
        "feedbacks": p.get("feedbacks") or p.get("nmFeedbacks") or 0,
        "stock": p.get("totalQuantity"),
        "ad": bool(p.get("log")),            # рекламные места WB помечает полем log
    }


# DOM-запасной путь: если перехват JSON не сработал
_DOM_JS = r"""
() => [...document.querySelectorAll('article.product-card, [data-nm-id]')].map(c => {
    const nm = c.getAttribute('data-nm-id')
        || (c.querySelector('a[href*="/catalog/"]')?.href.match(/catalog\/(\d+)\//)?.[1]);
    const t = s => (c.querySelector(s)?.textContent || '').trim();
    const num = (s, re) => (t(s).match(re)?.[0] || '').replace(/\s/g, '');
    return {
        id: nm ? parseInt(nm) : null,
        name: t('.product-card__name, .goods-name').replace(/^\/\s*/, ''),
        brand: t('.product-card__brand, .brand-name'),
        priceDom: parseInt(num('.price__lower-price, ins.price__lower-price', /[\d\s]+/)) || null,
        rating: parseFloat((t('.address-rate-mini, .product-card__rating').match(/[\d.,]+/)?.[0] || '0')
                           .replace(',', '.')) || 0,
        feedbacks: parseInt(num('.product-card__count, .product-card__feedback', /\d[\d\s]*/)) || 0,
        adDom: /реклама|буст/i.test(c.innerText || ''),
    };
}).filter(x => x.id)
"""


async def _confirm_adult(page):
    try:
        return await page.evaluate("""() => {
            const b = [...document.querySelectorAll('button, a')]
                .find(x => /да|исполнилось|18|продолжить|подтвер/i.test(x.textContent || ''));
            if (b) { b.click(); return true; } return false; }""")
    except Exception:
        return False


def _hint_texts(js) -> list[str]:
    """Тексты подсказок из JSON автодополнения WB (формат плавает — ищем строки)."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            for k in ("name", "query", "text", "value"):
                v = x.get(k)
                if isinstance(v, str) and 1 < len(v) < 80:
                    out.append(v.strip()); break
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(js)
    return out


async def _suggest(page, query: str) -> list[str]:
    """Подсказки WB к запросу: вводим его в строку поиска, как покупатель,
    и забираем выпадающий список (сеть или DOM)."""
    caught = []

    async def on_resp(r):
        u = r.url.lower()
        if ("suggest" in u or "hint" in u) and r.status == 200:
            try:
                caught.append(await r.json())
            except Exception:
                pass
    page.on("response", on_resp)
    try:
        box = page.locator("#searchInput, input[type=search], input[name=search]").first
        await box.click(timeout=15000)
        await box.fill("")
        await box.type(query, delay=90)
        await page.wait_for_timeout(2500)
        hints = []
        for js in caught:
            hints += _hint_texts(js)
        if not hints:
            hints = await page.evaluate("""() => [...document.querySelectorAll(
                '.autocomplete__item, [class*="autocomplete"] li, [class*="suggest"] li')]
                .map(x => (x.textContent || '').trim()).filter(Boolean)""") or []
        await box.fill("")
    except Exception as e:
        print(f"   подсказки не снялись: {str(e)[:80]}")
        hints = []
    finally:
        page.remove_listener("response", on_resp)
    seen, out = set(), []
    for h in hints:
        k = h.lower()
        if k != query.lower() and k not in seen:
            seen.add(k); out.append(h)
    return out[:15]


async def _search(page, query: str, pages: int) -> tuple[list, int | None]:
    rows, total = [], None
    for pg in range(1, pages + 1):
        caught = []

        async def on_resp(r):
            u = r.url
            if ("search" in u and ("wb.ru" in u or "wildberries" in u)
                    and "suggest" not in u and r.status == 200):
                try:
                    caught.append(await r.json())
                except Exception:
                    pass
        page.on("response", on_resp)
        url = ("https://www.wildberries.ru/catalog/0/search.aspx?search="
               + urllib.parse.quote(query) + (f"&page={pg}" if pg > 1 else ""))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"   страница {pg} не открылась: {str(e)[:100]}")
        prods = []
        for i in range(20):                              # ждём ответ поиска до ~20с
            for js in caught:                            # первый ответ с товарами — эта страница
                pp, tt = _products(js)
                if pp:
                    prods, total = pp, total or tt
                    break
            if prods:
                break
            if i == 6 and await _confirm_adult(page):    # заглушка 18+
                await page.wait_for_timeout(2000)
            await page.wait_for_timeout(1000)
        page.remove_listener("response", on_resp)
        base = len(rows)
        if prods:
            rows += [_row(p, base + i + 1) for i, p in enumerate(prods)]
        else:                                            # запасной путь: DOM
            for _ in range(10):
                await page.evaluate("() => window.scrollBy(0, window.innerHeight * 2)")
                await page.wait_for_timeout(600)
            dom = await page.evaluate(_DOM_JS) or []
            for i, p in enumerate(dom):
                r = _row(p, base + i + 1)
                r["price"], r["ad"] = p.get("priceDom"), p.get("adDom")
                rows.append(r)
            if dom:
                print(f"   стр. {pg}: данные из страницы (без остатков)")
        if total is None:                                # «Найдено N товаров» из текста
            try:
                txt = await page.evaluate("() => document.body.innerText.slice(0, 3000)")
                m = re.search(r"(\d[\d\s ]*)\s+товар", txt or "")
                total = int(re.sub(r"\D", "", m.group(1))) if m else None
            except Exception:
                pass
        if len(prods or []) < 90 and pg < pages:         # страница неполная — дальше пусто
            break
        await page.wait_for_timeout(1500)
    return rows, total


# ── сводка ────────────────────────────────────────────────────────────────
def _is_ours(r, nms, words) -> bool:
    if r["nm"] in nms:
        return True
    hay = f"{r['brand']} {r['supplier']}".lower()
    return any(w in hay for w in words)


def _summary(q, rows, total, nms, words) -> dict:
    prices = [r["price"] for r in rows if r["price"]]
    top = rows[:TOP]
    ours = [r for r in rows if r["ours"]]
    q_ = lambda xs, k: round(st.quantiles(xs, n=4)[k]) if len(xs) >= 4 else None
    stock = [r["stock"] for r in top if r["stock"] is not None]
    return {
        "Запрос": q, "Найдено товаров": total, "Собрано": len(rows),
        "Цена медиана": round(st.median(prices)) if prices else None,
        "Цена 25%": q_(prices, 0), "Цена 75%": q_(prices, 2),
        "Цена мин": min(prices) if prices else None,
        f"Рейтинг топ-{TOP}": round(st.mean([r["rating"] for r in top if r["rating"]] or [0]), 2),
        f"Отзывы топ-{TOP} медиана": round(st.median([r["feedbacks"] for r in top] or [0])),
        f"Реклама в топ-{TOP}, %": round(sum(r["ad"] for r in top) / len(top) * 100) if top else None,
        f"Остаток топ-{TOP}, шт": sum(stock) if stock else None,
        "Брендов в выдаче": len({r["brand"] for r in rows if r["brand"]}),
        "Наша лучшая позиция": ours[0]["pos"] if ours else None,
        "Наших карточек": len(ours),
        "Наши (артикул:место)": ", ".join(f"{r['nm']}:{r['pos']}" for r in ours[:10]),
    }


def _brands(q, rows) -> list[dict]:
    agg = {}
    for r in rows:
        b = r["brand"] or "—"
        a = agg.setdefault(b, {"n": 0, "fb": 0, "best": r["pos"]})
        a["n"] += 1; a["fb"] += r["feedbacks"] or 0
    fb_all = sum(a["fb"] for a in agg.values()) or 1
    out = [{"Запрос": q, "Бренд": b, "Карточек": a["n"],
            "Доля карточек, %": round(a["n"] / len(rows) * 100, 1),
            "Отзывов": a["fb"], "Доля отзывов, %": round(a["fb"] / fb_all * 100, 1),
            "Лучшее место": a["best"]} for b, a in agg.items()]
    return sorted(out, key=lambda x: -x["Отзывов"])[:25]


def _sheet(wb, title, rows, first=False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    if not rows:
        return ws
    head = list(rows[0])
    ws.append(head)
    for r in rows:
        ws.append([r.get(h) for h in head])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="4B5563")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for i, h in enumerate(head, 1):
        w = max(len(str(h)), *(len(str(r.get(h) or "")) for r in rows[:200]))
        ws.column_dimensions[get_column_letter(i)].width = min(60, max(8, w + 2))
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def _save(summ, allrows, brands, hints) -> str:
    wb = Workbook()
    _sheet(wb, "Сводка", summ, first=True)
    _sheet(wb, "Выдача", [{
        "Запрос": r["q"], "Место": r["pos"], "Артикул WB": r["nm"], "Название": r["name"],
        "Бренд": r["brand"], "Продавец": r["supplier"], "Цена покупателя": r["price"],
        "Рейтинг": r["rating"], "Отзывы": r["feedbacks"], "Остаток": r["stock"],
        "Реклама": "да" if r["ad"] else "", "Наш": "да" if r["ours"] else "",
    } for r in allrows])
    _sheet(wb, "Бренды", brands)
    _sheet(wb, "Подсказки", hints)
    name = f"wb_market_{datetime.now():%Y-%m-%d_%H-%M}.xlsx"
    path = os.path.join(HERE, name)
    wb.save(path)
    return path


# ── режим бота: задания из дашборда («Wildberries → Рынок по запросам») ───
DASHBOARDS = [u.rstrip("/") for u in os.getenv(
    "DASHBOARD_URL", "https://biomed.marketpartners.ru,https://wb-dashboard-6wxf.onrender.com"
).split(",") if u.strip()]
TOKEN = os.getenv("WB_AGENT_TOKEN", "")


def _api(method: str, path: str, body=None, timeout=40):
    """Запрос к дашборду; пробуем адреса по очереди (российский прокси, потом Render)."""
    import urllib.request
    last = None
    for base in list(DASHBOARDS):
        url = f"{base}{path}{'&' if '?' in path else '?'}token={urllib.parse.quote(TOKEN)}"
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "wb-market-agent"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if base != DASHBOARDS[0]:                 # рабочий адрес — первым
                    DASHBOARDS.remove(base); DASHBOARDS.insert(0, base)
                return json.loads(r.read().decode() or "{}")
        except Exception as e:
            last = e
            if getattr(e, "code", None) == 401:
                raise SystemExit("Дашборд не принял токен — проверьте WB_AGENT_TOKEN.")
    raise ConnectionError(str(last)[:150])


async def agent(page, nms, words):
    if not TOKEN:
        sys.exit("Задайте токен: setx WB_AGENT_TOKEN \"<токен из Render>\" и откройте новое окно.")
    print("Режим бота: жду задания из дашборда (Wildberries → Рынок по запросам). "
          "Окно не закрывать, Ctrl+C — выход.")
    last_err = ""
    while True:
        try:
            tasks = _api("GET", "/api/market/pending?limit=3").get("tasks") or []
            if last_err:
                print("связь с дашбордом восстановлена"); last_err = ""
        except ConnectionError as e:
            if str(e) != last_err:
                print("нет связи с дашбордом:", e); last_err = str(e)
            await asyncio.sleep(20)
            continue
        for t in tasks:
            q, t0 = t["query"], time.time()
            body = {"job": t["job"], "query": q}
            try:
                if t.get("hints"):
                    await page.goto("https://www.wildberries.ru/", timeout=60000)
                    await page.wait_for_timeout(1200)
                    body["hints"] = await _suggest(page, q)
                rows, total = await _search(page, q, int(t.get("pages") or 1))
                for r in rows:
                    r["ad"] = bool(r.get("ad"))
                body.update(rows=rows, total=total)
                print(f"→ {q!r}: {len(rows)} товаров, найдено {total}"
                      + (f", подсказок {len(body['hints'])}" if t.get("hints") else "")
                      + f" ({time.time() - t0:.0f} с)")
            except Exception as e:
                body["error"] = str(e)[:200]
                print(f"→ {q!r}: ошибка {str(e)[:120]}")
            try:
                _api("POST", "/api/market/ingest", body, timeout=60)
            except Exception as e:
                print("   не отдал результат:", str(e)[:120])
            await page.wait_for_timeout(1500)
        await asyncio.sleep(5 if tasks else 15)


# ── запуск ────────────────────────────────────────────────────────────────
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=1, help="страниц выдачи на запрос (по 100)")
    ap.add_argument("--show", action="store_true", help="показать окно браузера")
    ap.add_argument("--expand", type=int, default=0, metavar="N",
                    help="добавить к разбору до N подсказок WB на каждый запрос")
    ap.add_argument("queries", nargs="*", help="запросы (иначе — queries.txt)")
    ap.add_argument("--agent", action="store_true",
                    help="режим бота: брать запросы из дашборда и отдавать туда результат")
    a = ap.parse_args()
    queries = a.queries or _lines("queries.txt")
    if a.agent:
        queries = ["(бот)"]
    if not queries:
        with open(os.path.join(HERE, "queries.txt"), "w", encoding="utf-8") as f:
            f.write("# по одному запросу в строке\nсмазка для фистинга\nспрей для горла\n")
        sys.exit("Заполните queries.txt (создал пример рядом со скриптом) и запустите снова.")
    nms, words = _ours()
    print(f"Запросов: {len(queries)}, страниц на запрос: {a.pages}. "
          f"Наши метки: {len(nms)} артикулов, {len(words)} брендов/продавцов.")
    summ, allrows, brands = [], [], []
    async with async_playwright() as pw:
        args = ["--disable-blink-features=AutomationControlled"]
        browser = None
        for ch in ("chrome", "msedge", ""):
            try:
                browser = await pw.chromium.launch(headless=not a.show, args=args,
                                                   **({"channel": ch} if ch else {}))
                break
            except Exception:
                continue
        if browser is None:
            sys.exit("Не нашёл ни Chrome, ни Edge.")
        ctx = await browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow",
                                        user_agent=UA, viewport={"width": 1366, "height": 900})
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()
        try:
            await page.goto("https://www.wildberries.ru/", timeout=60000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print("WB не открылся (VPN?):", str(e)[:120])
        if a.agent:
            await agent(page, nms, words)
            return
        # 1) поиск запросов: подсказки WB к каждому исходному запросу
        hints_rows, known = [], {q.lower() for q in queries}
        print("Собираю подсказки WB (связанные запросы)…")
        for q in list(queries):
            try:
                await page.goto("https://www.wildberries.ru/", timeout=60000)
                await page.wait_for_timeout(1500)
            except Exception:
                pass
            hs = await _suggest(page, q)
            print(f"   {q!r}: {len(hs)} подсказок")
            for j, h in enumerate(hs, 1):
                hints_rows.append({"Исходный запрос": q, "Подсказка WB": h, "Место": j,
                                   "Разобран": "да" if j <= a.expand else ""})
                if j <= a.expand and h.lower() not in known:
                    known.add(h.lower()); queries.append(h)
        # 2) рынок по каждому запросу
        for i, q in enumerate(queries, 1):
            t0 = time.time()
            print(f"[{i}/{len(queries)}] {q!r} …", end=" ", flush=True)
            try:
                rows, total = await _search(page, q, a.pages)
            except Exception as e:
                print("ошибка:", str(e)[:120])
                continue
            for r in rows:
                r["q"], r["ours"] = q, _is_ours(r, nms, words)
            s = _summary(q, rows, total, nms, words)
            summ.append(s); allrows += rows; brands += _brands(q, rows) if rows else []
            print(f"{len(rows)} товаров, найдено {total}, медиана {s['Цена медиана']} ₽, "
                  f"наша позиция {s['Наша лучшая позиция'] or '—'} ({time.time() - t0:.0f} с)")
            await page.wait_for_timeout(2000)
        await browser.close()
    path = _save(summ, allrows, brands, hints_rows)
    print(f"\nГотово: {path}")
    try:
        os.startfile(path)                                # Windows: сразу открыть
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
