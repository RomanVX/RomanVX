"""Локальный агент сбора выдачи WB — запускается на компьютере пользователя.

WB банит IP серверов/прокси (дашборд получает 498), но домашний IP
пускает. Агент опрашивает дашборд, забирает запросы калькулятора ниши,
тянет выдачу WB с вашего домашнего IP и отдаёт результат обратно.

Запуск:
    pip install httpx
    set DASHBOARD_URL=https://wb-dashboard-6wxf.onrender.com   (Windows: set, macOS/Linux: export)
    set WB_AGENT_TOKEN=<тот же токен, что в дашборде>
    python wb_local_agent.py

Оставьте окно открытым — агент работает, пока запущен.
"""
import os
import time
import datetime
import urllib.parse

import httpx

DASHBOARD = os.getenv("DASHBOARD_URL", "https://wb-dashboard-6wxf.onrender.com").rstrip("/")
TOKEN = os.getenv("WB_AGENT_TOKEN", "")
POLL_SEC = 4

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


def _wb_search(query: str) -> tuple[list, int]:
    """Тянет выдачу WB с домашнего IP: получаем _wbauid, строим x-queryid,
    запрашиваем v18/appType=64 (тот же путь, что использует сайт WB)."""
    with httpx.Client(timeout=40, headers={"User-Agent": _UA,
                      "Accept-Language": "ru,en;q=0.9"}, follow_redirects=True) as c:
        # 1) главная — получаем куку _wbauid (httpOnly, но httpx её сохранит)
        c.get("https://www.wildberries.ru/")
        wbauid = c.cookies.get("_wbauid", "")
        # 2) x-queryid = qid + _wbauid + время МСК
        ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%Y%m%d%H%M%S")
        qid = "qid" + wbauid + ts
        u = ("https://search.wb.ru/exactmatch/ru/common/v18/search"
             "?appType=64&curr=rub&dest=-1257786&hide_dtype=15&lang=ru&locale=ru"
             "&resultset=catalog&sort=popular&spp=30&suppressSpellcheck=false&query="
             + urllib.parse.quote(query))
        r = c.get(u, headers={"x-queryid": qid, "x-requested-with": "XMLHttpRequest",
                              "Origin": "https://www.wildberries.ru",
                              "Referer": "https://www.wildberries.ru/"})
        data = r.json()
        prods = (data.get("data") or {}).get("products") or []
        out = []
        for p in prods[:60]:
            price = None
            for sz in p.get("sizes") or []:
                pr = (sz.get("price") or {}).get("product")
                if pr:
                    price = pr / 100
                    break
            out.append({
                "nm": p.get("id"), "name": p.get("name") or "",
                "brand": p.get("brand") or "",
                "price": round(price) if price else None,
                "rating": p.get("reviewRating") or p.get("rating") or 0,
                "feedbacks": int(p.get("feedbacks") or 0),
                "subject_id": p.get("subjectId"), "supplier": p.get("supplier") or "",
            })
        return out, int((data.get("data") or {}).get("total") or len(out))


def main():
    print(f"WB-агент запущен. Дашборд: {DASHBOARD}")
    print("Опрашиваю очередь запросов. Не закрывайте это окно.\n")
    while True:
        try:
            r = httpx.get(f"{DASHBOARD}/api/tools/niche/pending",
                          params={"token": TOKEN}, timeout=30)
            queries = r.json().get("queries") or []
        except Exception as e:
            print("нет связи с дашбордом:", str(e)[:120])
            time.sleep(POLL_SEC)
            continue
        for q in queries:
            print(f"→ собираю выдачу: {q!r}")
            try:
                products, total = _wb_search(q)
                body = {"query": q, "products": products, "total": total}
                print(f"   получено {len(products)} товаров (всего {total})")
            except Exception as e:
                body = {"query": q, "error": str(e)[:200]}
                print("   ошибка:", str(e)[:160])
            try:
                httpx.post(f"{DASHBOARD}/api/tools/niche/ingest",
                           params={"token": TOKEN}, json=body, timeout=30)
            except Exception as e:
                print("   не отдал результат:", str(e)[:120])
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
