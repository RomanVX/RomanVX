"""Тест: пускает ли WB выдачу с этого IP.

Запусти на своём сервере (или ПК):
    pip install httpx
    python test_wb_ip.py

Если увидишь «✅ РАБОТАЕТ: N товаров» — WB пускает этот IP, сюда можно
ставить агент 24/7. Если «❌ ЗАБЛОКИРОВАН» — это дата-центровый IP, WB
его режет (как Render), сервер не подойдёт, нужен домашний/жилой IP.
"""
import datetime
import urllib.parse

import httpx

QUERY = "крем для лица с цинком"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


def main():
    with httpx.Client(timeout=30, headers={"User-Agent": UA,
                      "Accept-Language": "ru,en;q=0.9"},
                      follow_redirects=True) as c:
        # 1) какой у нас внешний IP и чей он
        try:
            ip = c.get("https://api.ipify.org").text
            who = c.get(f"http://ip-api.com/json/{ip}?fields=country,isp,org").json()
            print(f"IP: {ip} | {who.get('country')} | {who.get('isp')} / {who.get('org')}")
        except Exception as e:
            print("IP определить не удалось:", e)

        # 2) получаем куку _wbauid и собираем x-queryid по формуле WB
        c.get("https://www.wildberries.ru/")
        wbauid = c.cookies.get("_wbauid", "")
        ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%Y%m%d%H%M%S")
        qid = "qid" + wbauid + ts
        u = ("https://search.wb.ru/exactmatch/ru/common/v18/search"
             "?appType=64&curr=rub&dest=-1257786&hide_dtype=15&lang=ru&locale=ru"
             "&resultset=catalog&sort=popular&spp=30&suppressSpellcheck=false&query="
             + urllib.parse.quote(QUERY))
        r = c.get(u, headers={"x-queryid": qid, "x-requested-with": "XMLHttpRequest",
                              "Origin": "https://www.wildberries.ru",
                              "Referer": "https://www.wildberries.ru/"})
        n = 0
        try:
            n = len((r.json().get("data") or {}).get("products") or [])
        except Exception:
            pass
        print(f"HTTP {r.status_code}, товаров: {n}")
        if n > 0:
            print("✅ РАБОТАЕТ — WB пускает этот IP, сюда можно ставить агент 24/7.")
        else:
            print("❌ ЗАБЛОКИРОВАН — WB режет этот IP (дата-центр). "
                  "Нужен домашний/жилой IP; сервер не подойдёт.")
            print("   Ответ WB (начало):", r.text[:200].replace("\n", " "))


if __name__ == "__main__":
    main()
