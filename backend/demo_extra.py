"""Демо-данные новых разделов для DEMO_CABINET=1: продвижение Ozon по товарам,
вопросы покупателей, рекламации WB, черновики/советы ИИ (без вызовов ИИ)."""
import random
from datetime import datetime, timedelta

import mock_data as md


class _Src:
    """Ассортимент для демо: DEMO_CABINET — выдуманный (mock_data),
    DEMO_FILL — настоящие товары кабинета из каталога, цифры выдуманные."""
    def __init__(self, products, arts, prices, costs, comm):
        self.PRODUCTS, self._arts, self.PRICES, self.COSTS, self.COMM = products, arts, prices, costs, comm

    def art(self, nm):
        return self._arts[nm]


_src_cache: dict = {}


def S() -> "_Src":
    from config import DEMO_CABINET
    if DEMO_CABINET:
        if "md" not in _src_cache:
            _src_cache["md"] = _Src(md.PRODUCTS, {nm: f"{md.ART}{nm}" for nm, *_ in md.PRODUCTS},
                                    md.PRICES, md.COSTS, md._COMM)
        return _src_cache["md"]
    import catalog
    n = len(catalog.CATALOG)
    hit = _src_cache.get("cat")
    if hit and hit[0] == n:
        return hit[1]
    by_brand: dict = {}
    for art, v in sorted(catalog.CATALOG.items()):
        b = v.get("brand") or "Прочее"
        if b in ("Прочее", "Нет бренда"):
            continue
        by_brand.setdefault(b, []).append((art, v.get("name") or art))
    order = [b for b in catalog.BRAND_ORDER if b in by_brand] or list(by_brand)
    prods, arts, prices, costs = [], {}, {}, {}
    i = 0
    while len(prods) < 28 and any(by_brand.get(b) for b in order):
        for b in order:
            if by_brand.get(b) and len(prods) < 28:
                art, name = by_brand[b].pop(0)
                nm = 700000 + i
                i += 1
                r = random.Random(art)
                prods.append((nm, name[:90], b, b))
                arts[nm] = art
                prices[nm] = r.randint(390, 7900)
                costs[art] = round(prices[nm] * 0.45)
    if not prods:                      # каталог ещё не загрузился
        prods = []
    src = _Src(prods, arts, prices, costs, {})
    _src_cache["cat"] = (n, src)
    return src


def _now():
    return datetime.utcnow() + timedelta(hours=3)


def promo(days: int) -> dict:
    rnd = random.Random(days * 7 + 3)
    d1 = _now().date() - timedelta(days=1)
    d0 = d1 - timedelta(days=days - 1)
    p1, p0 = d0 - timedelta(days=1), d0 - timedelta(days=days)
    today = _now().date()
    acts = [
        {"id": 1, "title": "Чёрная пятница", "start": str(today + timedelta(days=3)), "end": str(today + timedelta(days=10))},
        {"id": 2, "title": "Эластичный бустинг", "start": str(today - timedelta(days=40)), "end": str(today + timedelta(days=40))},
        {"id": 3, "title": "Распродажа 12.12", "start": str(today + timedelta(days=20)), "end": str(today + timedelta(days=26))},
        {"id": 4, "title": "Новогодние подарки", "start": str(today + timedelta(days=28)), "end": str(today + timedelta(days=45))},
    ]
    rows = []
    for i, (nm, name, brand, cat) in enumerate(S().PRODUCTS):
        art = S().art(nm)
        price = S().PRICES[nm]
        base = max(1, int(60 * (0.88 ** i) * days / 7))
        q = max(0, int(rnd.gauss(base, base * 0.25)))
        q0 = max(0, int(rnd.gauss(base, base * 0.3)))
        rev, rev0 = q * price * 0.93, q0 * price * 0.93
        has_ads = i < 18
        drr = rnd.uniform(0.05, 0.22) if has_ads else 0
        drr0 = max(0.02, drr + rnd.uniform(-0.05, 0.05)) if has_ads else 0
        ad, ad0 = rev * drr, rev0 * drr0
        cpo = ad * rnd.uniform(0.3, 0.7)
        ad_rev = ad * rnd.uniform(4, 11) if ad else 0
        comm = S().COMM.get(cat, 22.0) + 3
        logist, storage = rnd.randint(70, 260), rnd.randint(3, 25)
        cogs = S().COSTS[art]
        ad_unit = ad / q if q else 0

        def marg(p):
            if not p:
                return None
            m = p * (1 - comm / 100 - 0.015) - logist - storage - ad_unit - cogs
            return {"rub": round(m), "pct": round(m / p * 100, 1)}

        al = []
        for a in acts:
            r = rnd.random()
            if r < 0.35:
                continue
            disc = rnd.choice([5, 8, 10, 12, 15, 18, 20, 25, 30])
            ap = round(price * (1 - disc / 100))
            st = "участвует" if r > 0.75 else "можно войти"
            al.append({"action_id": a["id"], "title": a["title"], "type": "", "start": a["start"], "end": a["end"],
                       "status": st, "action_price": ap, "discount": float(disc), "margin": marg(ap)})
        tstr = str(today)
        al.sort(key=lambda x: (x["start"] > tstr, x["start"]))
        near = al[0] if al else None
        dyn = lambda a, b: round((a / b - 1) * 100) if b else None
        rows.append({
            "offer": art, "name": name, "group": brand,
            "qty": q, "qty_prev": q0, "qty_dyn": dyn(q, q0),
            "rev": round(rev), "rev_prev": round(rev0), "rev_dyn": dyn(rev, rev0),
            "ad": round(ad), "ad_prev": round(ad0), "ad_dyn": dyn(ad, ad0),
            "ad_cpc": round(ad - cpo), "ad_cpo": round(cpo),
            "views": int(ad * rnd.uniform(8, 20)), "clicks": int(ad * rnd.uniform(0.3, 0.8)),
            "ad_orders": int(ad_rev / price) if price else 0, "ad_rev": round(ad_rev),
            "drr": round(ad / rev * 100, 1) if rev else None,
            "drr_prev": round(ad0 / rev0 * 100, 1) if rev0 else None,
            "roas": round(ad_rev / ad, 2) if ad else None,
            "price": price, "buyer_price": round(price * 0.95), "min_price": round(price * 0.85),
            "actions": al, "actions_total": len(al),
            "promo_price": (near or {}).get("action_price"), "promo_discount": (near or {}).get("discount"),
            "margin": marg(price), "margin_promo": (near or {}).get("margin"), "has_cost": True,
        })
    rows.sort(key=lambda r: -r["rev"])
    T = {k: sum(r[k] or 0 for r in rows) for k in ("qty", "qty_prev", "rev", "rev_prev", "ad", "ad_prev", "ad_rev")}
    T["drr"] = round(T["ad"] / T["rev"] * 100, 1) if T["rev"] else None
    T["drr_prev"] = round(T["ad_prev"] / T["rev_prev"] * 100, 1) if T["rev_prev"] else None
    T["roas"] = round(T["ad_rev"] / T["ad"], 2) if T["ad"] else None
    return {"days": days, "period": [str(d0), str(d1)], "prev": [str(p0), str(p1)], "rows": rows,
            "totals": T, "actions": acts, "truncated": [], "has_costs": True, "warnings": [],
            "built": _now().strftime("%d.%m.%Y %H:%M"), "building": False, "progress": "", "error": ""}


def _pick(brand: str, k: int = 0):
    """Товар бренда (k-й), иначе любой — чтобы примеры были на реальных карточках."""
    P = S().PRODUCTS
    if not P:
        return None
    hits = [p for p in P if p[2] == brand]
    if hits:
        return hits[k % len(hits)]
    return P[(hash(brand) + k) % len(P)]


# (площадка, бренд, вопрос, черновик ответа | None)
_Q = [
    ("WB", "Kallos", "Это оригинал из Венгрии? Есть ли честный знак?",
     "Здравствуйте! Да, это оригинальная продукция Kallos, мы работаем напрямую с официальным поставщиком. Товар промаркирован «Честным знаком» — код на упаковке можно проверить в приложении."),
    ("Ozon", "FuturoHome", "Какая гарантия и куда обращаться, если сломается?",
     "Здравствуйте! На технику FuturoHome действует гарантия производителя, гарантийный талон вложен в коробку. Если что-то пойдёт не так — напишите нам в чат, подскажем ближайший сервис или оформим возврат."),
    ("WB", "InCar", "Подойдёт на Kia Rio 2018?", None),
    ("Ozon", "КРАФТИКИ", "С какого возраста можно? Всё необходимое есть в наборе?", None),
    ("WB", "Bosch", "Это оригинал Bosch? Почему дешевле, чем в магазине?", None),
    ("Ozon", "HELIA-D", "Подходит для чувствительной кожи?", None),
    ("WB", "Nakamichi", "Нужен ли переходник для подключения?", None),
    ("Ozon", "SWAT", "Какой срок годности у товара?", None),
]
_Q_GENERIC = ("Здравствуйте! Спасибо за вопрос. Все характеристики указаны в карточке товара в разделе «Характеристики». "
              "Если не нашли нужное — напишите, уточним у производителя и ответим.")
_ANSWERED = [
    ("Можно ли использовать на окрашенных волосах?", "Здравствуйте! Да, подходит для окрашенных волос и не вымывает цвет."),
    ("Какая гарантия?", "Здравствуйте! Гарантия производителя — 12 месяцев, талон вложен в коробку."),
    ("Есть ли инструкция на русском?", "Здравствуйте! Да, инструкция на русском языке вложена в упаковку."),
]


def questions() -> dict:
    now = _now()
    new = []
    for i, (mp, brand, text, draft) in enumerate(_Q):
        p = _pick(brand, i)
        if not p:
            break
        nm, name, br, _ = p
        new.append({"mp": mp, "id": f"demo-q{i}", "text": text,
                    "date": (now - timedelta(hours=5 + i * 9)).strftime("%Y-%m-%d %H:%M"),
                    "answer": "", "sku": nm if mp == "WB" else nm + 900000000, "art": S().art(nm),
                    "name": name, "brand": br, "warned": False, "draft": draft if i < 2 else None})
    P = S().PRODUCTS
    answered = [{"mp": "WB", "id": f"demo-a{i}", "text": q, "answer": a,
                 "date": (now - timedelta(days=3 + i)).strftime("%Y-%m-%d %H:%M"),
                 "sku": P[i][0], "art": S().art(P[i][0]), "name": P[i][1]}
                for i, (q, a) in enumerate(_ANSWERED) if i < len(P)]
    return {"new": new, "answered": answered, "errors": [], "fetched": now.strftime("%d.%m %H:%M")}


def question_draft(q: dict) -> str:
    i = str(q.get("id") or "")
    if i.startswith("demo-q"):
        try:
            d = _Q[int(i[6:])][3]
            if d:
                return d
        except (ValueError, IndexError):
            pass
    return _Q_GENERIC


_ACTS = [{"code": c, "title": t} for c, t in [
    ("approve1", "Одобрить с проверкой брака (товар вернётся, проверят на складе)"),
    ("approve2", "Одобрить и забрать товар"),
    ("autorefund1", "Вернуть деньги, товар остаётся у покупателя"),
    ("reject1", "Отклонить: брак не обнаружен (шаблон WB)"),
    ("reject2", "Отклонить: попросить фото/видео (шаблон WB)"),
    ("rejectcustom", "Отклонить со своим комментарием")]]
# (бренд, комментарий покупателя, часов назад/3, совет ИИ)
_C = [
    ("SWAT", "Пришло вскрытым, содержимое протекло, коробка мокрая", 1, {"verdict": "approve", "action": "autorefund1", "confidence": "высокая",
      "reason": "Повреждение при доставке правдоподобно, товар недорогой: возить его обратно дороже, чем вернуть деньги. Рекомендую вернуть деньги без возврата товара.", "comment": ""}),
    ("FuturoHome", "Не включается, индикатор не горит", 6, {"verdict": "clarify", "action": "reject2", "confidence": "средняя",
      "reason": "Фото и видео нет, по тексту нельзя отличить брак от неправильного подключения. Стоит попросить видео с попыткой включения.", "comment": ""}),
    ("Bosch", "Сломался через неделю работы, трещина на корпусе", 11, {"verdict": "approve", "action": "approve1", "confidence": "средняя",
      "reason": "На фото трещина по шву корпуса, следов удара нет. Похоже на производственный брак — одобрить с проверкой на складе.", "comment": ""}),
    ("Kallos", "Не подошёл запах, хочу вернуть", 4, {"verdict": "reject", "action": "rejectcustom", "confidence": "высокая",
      "reason": "Это не брак: товар вскрыт, причина — не понравился аромат. Косметика надлежащего качества возврату не подлежит.",
      "comment": "Здравствуйте! Сожалеем, что аромат не понравился. Косметика надлежащего качества после вскрытия возврату не подлежит, поэтому заявку отклоняем. Если заметите проблему с качеством — напишите, разберёмся."}),
]


def claims() -> dict:
    now = _now()
    op = []
    for i, (brand, txt, ago, adv) in enumerate(_C):
        p = _pick(brand, i)
        if not p:
            break
        nm, name, *_ = p
        op.append({"id": f"demo-c{i}", "nm": nm, "art": S().art(nm), "name": name, "comment": txt,
                   "wb_comment": "", "price": S().PRICES[nm], "date": (now - timedelta(hours=ago * 3)).strftime("%Y-%m-%d %H:%M"),
                   "order_date": str((now - timedelta(days=ago + 6)).date()),
                   "delivery_date": str((now - timedelta(days=ago + 2)).date()),
                   "status": "на рассмотрении", "status_ex": "на рассмотрении", "photos": [], "videos": [],
                   "actions": _ACTS, "origin_info": "", "source": "портал",
                   "advice": ({**adv, "at": now.strftime("%d.%m %H:%M")} if i < 2 else None)})
    P = S().PRODUCTS
    arch = [{"id": f"demo-ca{i}", "nm": P[i][0], "art": S().art(P[i][0]), "name": P[i][1],
             "comment": c, "date": (now - timedelta(days=5 + i)).strftime("%Y-%m-%d %H:%M"), "status": st, "status_ex": sx}
            for i, (c, st, sx) in enumerate([("Пришёл не тот цвет", "одобрено", "возвращается продавцу"),
                                             ("Царапина на корпусе", "одобрено", "товар у покупателя (одобрено)"),
                                             ("Передумал", "отказ", "товар у покупателя (отклонено)")]) if i < len(P)]
    return {"open": op, "archive": arch, "errors": [], "fetched": now.strftime("%d.%m %H:%M")}


def claim_advice(c: dict) -> dict:
    i = str(c.get("id") or "")
    if i.startswith("demo-c"):
        try:
            return {**_C[int(i[6:])][3], "at": _now().strftime("%d.%m %H:%M")}
        except (ValueError, IndexError):
            pass
    return {"verdict": "clarify", "action": "reject2", "confidence": "низкая",
            "reason": "Недостаточно данных — попросить фото/видео.", "comment": "", "at": _now().strftime("%d.%m %H:%M")}


# ── Демо для остальных разделов (перехват на уровне HTTP, см. main.py) ──────
def _R(seed):
    return random.Random(seed)


def _arts():
    return [(S().art(nm), nm, name, brand, cat) for nm, name, brand, cat in S().PRODUCTS]


def wb_adv() -> dict:
    rnd = _R(21)
    camps = []
    names = ["АРК Бытовая техника", "АРК Kallos маски", "Поиск Автотовары", "АРК Детское творчество",
             "АРК Инструменты Bosch", "Поиск Увлажнители", "АРК HELIA-D уход", "Каталог Обогреватели"]
    arts = _arts()
    for i, nmn in enumerate(names):
        spend = rnd.randint(18000, 160000)
        drr = rnd.choice([8.5, 11.2, 13.9, 16.4, 21.7, 27.3, 9.8, 14.6])
        rev = round(spend / drr * 100)
        orders = max(1, rev // rnd.randint(1500, 5000))
        views = spend * rnd.randint(2, 4)
        clicks = int(views * rnd.uniform(0.012, 0.035))
        skus = rnd.sample(arts, 3)
        verdict = "good" if drr < 12 else ("bad" if drr > 20 else "ok")
        camps.append({"id": str(20100000 + i), "name": nmn, "type": "Аукцион", "type_id": 9,
                      "status": "🟢 активна" if i < 6 else "⏸ пауза", "active": i < 6,
                      "spend": spend, "views": views, "clicks": clicks, "ctr": round(clicks / views * 100, 1),
                      "cpc": round(spend / clicks, 1), "orders": orders, "revenue": rev, "atbs": orders * 3,
                      "mode": "Ручная", "bids": {"search": 250, "catalog": 250, "cpm": 250},
                      "skus": [{"sku": a[0], "spend": spend // 3, "orders": orders // 3} for a in skus],
                      "cpo": round(spend / orders), "drr": drr, "verdict": verdict,
                      "verdict_why": {"good": f"ДРР {drr}% — реклама окупается, можно масштабировать",
                                      "ok": f"ДРР {drr}% — в норме, следить за ставками",
                                      "bad": f"ДРР {drr}% — реклама дороже маржи, снизить ставки/отключить слабые фразы"}[verdict],
                      "words": [{"phrase": w, "views": rnd.randint(800, 9000), "clicks": rnd.randint(10, 200),
                                 "sum": round(rnd.uniform(300, 6000), 1), "ctr": round(rnd.uniform(0.6, 3.5), 2),
                                 "flag": "waste" if j == 4 else ""}
                                for j, w in enumerate([skus[0][2].split()[0].lower(), skus[0][2].lower()[:28],
                                                       skus[1][2].lower()[:24], f"{skus[0][3].lower()} купить", "подарок мужчине"])]})
    ts = sum(c["spend"] for c in camps)
    tr = sum(c["revenue"] for c in camps)
    return {"campaigns": camps, "days": 28, "total_spend": ts, "total_revenue": tr,
            "total_drr": round(ts / tr * 100, 1), "waste": 7400, "advice": None,
            "building": False, "progress": "", "error": "", "built_at": _now().strftime("%Y-%m-%d %H:%M")}


def oz_funnel() -> dict:
    rnd = _R(31)
    items = []
    for i, (art, nm, name, brand, cat) in enumerate(_arts()):
        search = int(60000 * (0.85 ** i)) + rnd.randint(500, 3000)
        pdp = int(search * rnd.uniform(0.02, 0.07))
        tocart = int(pdp * rnd.uniform(0.05, 0.16))
        orders = int(tocart * rnd.uniform(0.35, 0.7))
        ctr = round(pdp / search * 100, 1)
        cart = round(tocart / pdp * 100, 1) if pdp else None
        buy = round(orders / tocart * 100, 1) if tocart else None
        if ctr < 3:
            b, why = "ctr", f"мало переходов из поиска (CTR {ctr}%) — главное фото и цена в выдаче"
        elif cart and cart < 8:
            b, why = "cart", f"смотрят, но не кладут в корзину ({cart}%) — контент карточки, отзывы, цена"
        else:
            b, why = "ok", "воронка в норме"
        items.append({"sku": art, "name": name, "group": brand, "search": search, "pdp": pdp, "tocart": tocart,
                      "orders": orders, "revenue": orders * S().PRICES[nm], "sessions": pdp,
                      "position": round(rnd.uniform(3, 60), 1), "ctr": ctr, "cart_pct": cart, "buy_pct": buy,
                      "bottleneck": b, "bottleneck_why": why})
    d1 = _now().date() - timedelta(days=1)
    return {"items": items, "days": 28, "period": f"{(d1 - timedelta(days=27)).strftime('%d.%m.%Y')} — {d1.strftime('%d.%m.%Y')}",
            "fetched_at": _now().strftime("%d.%m.%Y %H:%M")}


def _m(rnd, spent, drr):
    om = round(spent / drr * 100) if drr else 0
    views = int(spent * rnd.uniform(1.5, 3))
    clicks = int(views * rnd.uniform(0.02, 0.06))
    orders = max(1, om // 2500)
    return {"views": views, "clicks": clicks, "spent": spent, "orders": orders, "orders_money": om,
            "ctr": round(clicks / views * 100, 2), "cpc": round(spent / max(clicks, 1), 1),
            "cpo": round(spent / orders), "drr": drr, "roas": round(om / spent, 2) if spent else None}


def oz_ads(period: int) -> dict:
    rnd = _R(41 + period)
    d1 = _now().date() - timedelta(days=1)
    d0 = d1 - timedelta(days=period - 1)
    k = period / 7
    camps, skus = [], []
    defs = [("Оплата за заказ: все товары", "Оплата за заказ (поиск)", "SEARCH_PROMO", "за заказ"),
            ("FuturoHome — целевой ДРР 12%", "Оплата за клики", "SKU", "за клики"),
            ("Kallos — трафареты", "Оплата за клики", "SKU", "за клики"),
            ("Автотовары — поиск", "Оплата за клики", "SKU", "за клики"),
            ("КРАФТИКИ — Новый год", "Оплата за клики", "SKU", "за клики")]
    for i, (t, typ, code, pay) in enumerate(defs):
        sp = int(rnd.randint(15000, 70000) * k)
        cur = _m(rnd, sp, rnd.choice([9.6, 11.8, 13.4, 16.9, 22.5]))
        prev = _m(rnd, int(sp * rnd.uniform(0.8, 1.2)), rnd.choice([10.2, 12.5, 14.8, 18.1]))
        camps.append({"id": str(51000000 + i), "title": t, "type": typ, "type_code": code, "state": "Активна",
                      "state_code": "RUNNING", "payment": pay, "created": "2026-09-01", "from": "", "to": "",
                      "running_days": 78, "daily_budget": None, "weekly_budget": None if code == "SEARCH_PROMO" else 14000,
                      "strategy": "TARGET_CIR" if code != "SEARCH_PROMO" else "NO_AUTO_STRATEGY",
                      "cur": cur, "prev": prev, "brands": [], "skus": [], "new_in_period": False})
    for art, nm, name, brand, cat in _arts()[:18]:
        sp = int(rnd.randint(1500, 25000) * k)
        m = _m(rnd, sp, rnd.choice([7.9, 10.4, 12.6, 15.2, 19.8, 26.1]))
        skus.append({"art": art, "sku": str(nm + 900000000), "title": name, "brand": brand, **m,
                     "to_cart": m["orders"] * 3, "gmv": int(m["orders_money"] * 1.4),
                     "general_drr": round(m["drr"] * 0.75, 1), "campaigns": rnd.randint(1, 2)})
    def tot(key):
        cs = [c[key] for c in camps]
        s = {f: sum(c[f] for c in cs) for f in ("views", "clicks", "spent", "orders", "orders_money")}
        s.update(ctr=round(s["clicks"] / s["views"] * 100, 2), cpc=round(s["spent"] / s["clicks"], 1),
                 cpo=round(s["spent"] / s["orders"]), drr=round(s["spent"] / s["orders_money"] * 100, 1),
                 roas=round(s["orders_money"] / s["spent"], 2))
        return s
    brands = {}
    for s in skus:
        b = brands.setdefault(s["brand"], {"brand": s["brand"], "views": 0, "clicks": 0, "spent": 0, "orders": 0,
                                            "orders_money": 0, "skus": 0, "gmv": 0})
        for f in ("views", "clicks", "spent", "orders", "orders_money", "gmv"):
            b[f] += s[f]
        b["skus"] += 1
    tsp = sum(b["spent"] for b in brands.values()) or 1
    for b in brands.values():
        b.update(ctr=round(b["clicks"] / b["views"] * 100, 1), cpc=round(b["spent"] / max(b["clicks"], 1), 1),
                 cpo=round(b["spent"] / max(b["orders"], 1)), drr=round(b["spent"] / b["orders_money"] * 100, 1),
                 roas=round(b["orders_money"] / b["spent"], 2), share=round(b["spent"] / tsp * 100, 1),
                 general_drr=round(b["spent"] / b["gmv"] * 100, 1))
    chart = []
    for i in range(period):
        dd = d0 + timedelta(days=i)
        sp = int(tot("cur")["spent"] / period * rnd.uniform(0.8, 1.2))
        chart.append({"date": str(dd), "spent": sp, "orders_money": int(sp * rnd.uniform(6, 9)), "orders": rnd.randint(20, 60)})
    cur = tot("cur")
    return {"days": period, "period": [str(d0), str(d1)],
            "prev_period": [str(d0 - timedelta(days=period)), str(d0 - timedelta(days=1))],
            "total": {"cur": cur, "prev": tot("prev"), "general_drr": round(cur["drr"] * 0.8, 1)},
            "brands": sorted(brands.values(), key=lambda b: -b["spent"]), "campaigns": camps,
            "skus": sorted(skus, key=lambda s: -s["spent"]), "chart": chart,
            "launches": [{"date": str(d1 - timedelta(days=3)), "title": "КРАФТИКИ — Новый год", "type": "Оплата за клики"}],
            "fetched_at": _now().strftime("%d.%m.%Y %H:%M"), "built_ts": 0, "refreshing": False, "error": ""}


def oz_phrases() -> dict:
    rnd = _R(51)
    items, tv, tc = [], 0, 0
    for art, nm, name, brand, cat in _arts()[:12]:
        base = name.lower().split()
        phr = [" ".join(base[:2]), " ".join(base[:1]), f"{base[0]} {brand.lower()}", f"{base[0]} купить",
               f"{base[0]} недорого", f"{brand.lower()}", f"{' '.join(base[:2])} в подарок", f"лучший {base[0]}"]
        ps = []
        for p in phr:
            v = rnd.randint(300, 9000)
            c = int(v * rnd.uniform(0.01, 0.09))
            ps.append({"phrase": p, "views": v, "clicks": c, "ctr": round(c / v * 100, 1)})
        ps.sort(key=lambda x: -x["views"])
        v, c = sum(p["views"] for p in ps), sum(p["clicks"] for p in ps)
        tv, tc = tv + v, tc + c
        items.append({"sku": str(nm + 900000000), "art": art, "name": name, "views": v, "clicks": c,
                      "ctr": round(c / v * 100, 1), "phrase_count": len(ps), "phrases": ps, "group": brand})
    d1 = _now().date() - timedelta(days=1)
    return {"items": items, "total_views": tv, "total_clicks": tc,
            "period": f"{(d1 - timedelta(days=29)).strftime('%d.%m.%Y')} — {d1.strftime('%d.%m.%Y')}", "days": 30,
            "fetched_at": _now().strftime("%d.%m.%Y %H:%M")}


_CLUSTERS = ["Москва, МО и Дальние регионы", "Санкт-Петербург и СЗО", "Краснодар", "Екатеринбург", "Казань",
             "Новосибирск", "Ростов", "Самара", "Воронеж", "Нижний Новгород"]


def oz_clusters() -> dict:
    rnd = _R(61)
    items = []
    arts = _arts()
    for i, cl in enumerate(_CLUSTERS):
        share = 0.35 * (0.75 ** i)
        skus = []
        for art, nm, name, brand, cat in arts[:14]:
            ads = round(rnd.uniform(0.1, 4) * share * 3, 2)
            st = rnd.choice([0, 0, rnd.randint(1, 40), rnd.randint(20, 160)])
            idc = round(st / ads) if ads else 0
            need = max(0, round(ads * 30 - st))
            grade = "WAS_DEFICIT" if st == 0 else ("избыток" if idc > 120 else "норма")
            skus.append({"sku": art, "name": name, "ads": ads, "stock": st, "idc": idc, "grade": grade,
                         "need": need, "excess": 0})
        stock = sum(s["stock"] for s in skus)
        spd = round(sum(s["ads"] for s in skus), 1)
        cov = round(stock / spd, 1) if spd else 0
        items.append({"cluster": cl, "stock": stock, "spd": spd, "coverage": cov,
                      "need": sum(s["need"] for s in skus), "status": "ok" if cov >= 20 else "weak", "excess": 0,
                      "skus": sorted([s for s in skus if s["ads"] >= 0.3], key=lambda s: -s["need"]),
                      "other_skus": [s for s in skus if s["ads"] < 0.3]})
    return {"items": items, "target_days": 30, "weak": sum(1 for x in items if x["status"] != "ok"),
            "excess_total": 0, "fetched_at": _now().strftime("%d.%m.%Y %H:%M")}


def wb_funnel(days: int) -> dict:
    rnd = _R(71)
    items = []
    for i, (art, nm, name, brand, cat) in enumerate(_arts()):
        opens = int(9000 * (0.86 ** i) * days / 14) + rnd.randint(50, 400)
        carts = int(opens * rnd.uniform(0.05, 0.14))
        orders = int(carts * rnd.uniform(0.3, 0.6))
        buy = int(orders * rnd.uniform(0.6, 0.9))
        po = int(opens * rnd.uniform(0.8, 1.2))
        pord = int(orders * rnd.uniform(0.8, 1.2))
        items.append({"nm": str(nm), "sku": art, "opens": opens, "carts": carts, "orders": orders,
                      "orders_rub": orders * S().PRICES[nm], "buyouts": buy, "cancels": orders - buy,
                      "wishlist": int(opens * 0.03), "cr_cart": round(carts / opens * 100, 1),
                      "cr_order": round(orders / carts * 100, 1) if carts else 0,
                      "cr_buyout": round(buy / orders * 100, 1) if orders else 0,
                      "prev_opens": po, "prev_orders": pord,
                      "prev_cr_cart": round(carts / po * 100 * rnd.uniform(0.85, 1.1), 1),
                      "prev_cr_order": round(rnd.uniform(25, 60), 1)})
    d1 = _now().date() - timedelta(days=1)
    return {"items": items, "days": days, "period": f"{(d1 - timedelta(days=days - 1)).strftime('%d.%m')} — {d1.strftime('%d.%m.%Y')}",
            "history": {"from": str(d1 - timedelta(days=120)), "to": str(d1), "days_stored": 120}, "building": False}


_ACT_DEF = [("OZON", "o1", "Чёрная пятница", "Скидка", 3, 10), ("OZON", "o2", "Эластичный бустинг", "Эластичный бустинг", -40, 40),
            ("WB", "w1", "Распродажа 11.11 — 30.11", "Акция WB", -7, 12), ("OZON", "o3", "Новогодние подарки", "Скидка", 28, 45),
            ("WB", "w2", "Новогодняя распродажа", "Акция WB", 20, 31)]


def promo_calendar() -> dict:
    rnd = _R(81)
    today = _now().date()
    acts = []
    for mp, id_, t, typ, a, b in _ACT_DEF:
        cands = rnd.randint(10, 24)
        part = rnd.randint(2, cands // 2)
        acts.append({"mp": mp, "id": id_, "title": t, "type": typ, "description": t, "start": str(today + timedelta(days=a)),
                     "end": str(today + timedelta(days=b)), "freeze": "", "auto_add_dates": [],
                     "candidates": cands, "participating": part,
                     "counts": {"yes": rnd.randint(3, 8), "maybe": rnd.randint(1, 5), "no": rnd.randint(2, 7), "clear": rnd.randint(0, 4)},
                     "promo_drr": round(rnd.uniform(4, 14), 1), "promo_drr_net": round(rnd.uniform(2, 7), 1),
                     "promo_cost_day": rnd.randint(1500, 9000), "ad_drr_same": round(rnd.uniform(9, 16), 1)})
    return {"actions": acts, "built": _now().strftime("%d.%m.%Y %H:%M"), "errors": [], "building": False, "progress": "",
            "ozon_auto_add_on": 4, "ozon_ads": {"drr": 12.4, "period": [str(today - timedelta(days=28)), str(today - timedelta(days=1))]}}


def promo_action(mp: str, id_: str) -> dict:
    cal = promo_calendar()
    a = next((x for x in cal["actions"] if x["id"] == id_), cal["actions"][0])
    rnd = _R(91 + len(id_))
    items = []
    for art, nm, name, brand, cat in _arts()[:20]:
        price = S().PRICES[nm]
        disc = rnd.choice([5, 8, 10, 12, 15, 20, 25, 30])
        ap = round(price * (1 - disc / 100))
        cost = S().COSTS[art]
        payout = round(ap * 0.72) - 120
        profit = payout - cost
        stock = rnd.randint(0, 300)
        per_day = round(rnd.uniform(0.2, 6), 2)
        sd = round(stock / per_day) if per_day else 999
        if stock == 0:
            rec, txt, why = "no", "Не участвовать", "нет остатка"
        elif profit < 0:
            rec, txt, why = "no", "Не участвовать", f"убыток {profit} ₽ на штуку по цене акции"
        elif sd > 90:
            rec, txt, why = "clear", "Распродать залежи", f"запас на {sd} дн. — акция ускорит оборот"
        elif disc <= 10:
            rec, txt, why = "yes", "Участвовать", f"скидка {disc}% — маржа сохраняется"
        else:
            rec, txt, why = "maybe", "Можно", f"скидка {disc}% — если цель оборот"
        items.append({"art": art, "name": name, "brand": brand, "price": price, "action_price": ap, "rec_price": None,
                      "in_action": rnd.random() < 0.3, "add_mode": "NOT_SET", "stock": stock, "per_day": per_day,
                      "cost": cost, "payout": payout, "auto_add": False, "auto_action": False, "commission_pct": 24.0,
                      "rec": rec, "rec_text": txt, "why": why, "discount": float(disc), "stock_days": sd,
                      "profit": profit, "margin": round(profit / ap * 100, 1), "discount_rub": price - ap,
                      "promo_drr": float(disc), "promo_drr_net": round(disc / 2, 1), "ad_drr": round(rnd.uniform(8, 18), 1),
                      "ad_general_drr": round(rnd.uniform(12, 24), 1), "ad_spent": rnd.randint(500, 9000),
                      "vs_ad": "дешевле рекламы" if disc < 12 else "дороже рекламы"})
    return {**a, "items": items, "ads_total": cal["ozon_ads"]}


def route(path: str, q: dict):
    """Ответ демо для пути или None (пусть отвечает обычный обработчик)."""
    if path == "/api/tools/adv":
        return wb_adv()
    if path == "/api/tools/funnel":
        return oz_funnel()
    if path == "/api/tools/ozads2":
        return oz_ads(int(q.get("period") or 7))
    if path == "/api/tools/ozphrases":
        return oz_phrases()
    if path == "/api/tools/ozon/clusters":
        return oz_clusters()
    if path == "/api/tools/wb_funnel":
        return wb_funnel(int(q.get("days") or 14))
    if path == "/api/promo/calendar":
        return promo_calendar()
    if path == "/api/promo/action":
        return promo_action(q.get("mp") or "", q.get("id") or "")
    return None


# ── Отзывы и продуктолог (для DEMO_FILL, когда реальных отзывов нет) ────────
_COSM = ("Kallos", "Kallos Cosmetics", "HELIA-D")
_RV_COSM = [
    (5, "Волосы мягкие и блестящие после первого применения, объём большой — надолго."),
    (5, "Оригинал, пахнет приятно, пользуюсь не первый год."),
    (4, "Хорошая маска, но аромат резковат."),
    (3, "Эффект слабее, чем ожидала, но за свою цену нормально."),
    (2, "Дозатор подтекает, баночка пришла липкая."),
    (5, ""),
]
_RV = [
    (5, "Всё пришло целым, работает отлично. Упаковка надёжная."),
    (5, "Пользуюсь неделю — всё как в описании, рекомендую."),
    (5, "Быстрая доставка, качество на уровне. Спасибо продавцу!"),
    (4, "Хорошая вещь за свои деньги. Минус звезда за мятую коробку."),
    (5, "Брали в подарок — очень довольны."),
    (3, "В целом нормально, но инструкция только на английском."),
    (5, "Лучшее соотношение цена/качество, сравнивал с аналогами."),
    (2, "Пришёл с царапиной на корпусе. Продавец, решите вопрос!"),
    (4, "Хорошо, но шумит громче, чем ожидал."),
    (1, "Заказывал одно — привезли другое. Оформляю возврат."),
    (5, "Оригинал, есть гарантийный талон и чек."),
    (5, ""),
]
_RV_DRAFT = {
    5: "Спасибо за отзыв! Очень рады, что всё понравилось — будем ждать вас снова 🙂",
    4: "Спасибо за оценку! Жаль, что коробка пришла помятой — передали замечание на склад, усилим упаковку.",
    3: "Спасибо за отзыв! Подскажите, чего не хватило? Русскую инструкцию можем прислать в чат — напишите нам.",
    2: "Здравствуйте! Простите за такую ситуацию. Оформите, пожалуйста, возврат через площадку или напишите в чат — решим вопрос.",
    1: "Здравствуйте! Приносим извинения: это ошибка при комплектации на складе, разбор уже передан. Оформите возврат — деньги вернутся полностью.",
}


def reviews_data() -> dict:
    rnd = random.Random(11)
    P = S().PRODUCTS
    today = _now()
    out = []
    for i in range(90):
        if not P:
            break
        nm, name, brand, cat = P[rnd.randrange(len(P))]
        pool = _RV_COSM if brand in _COSM else _RV
        rating, text = pool[rnd.randrange(len(pool))]
        pf = rnd.choices(["WB", "Ozon"], weights=[60, 40])[0]
        answered = rating >= 4 and rnd.random() < 0.6
        out.append({"id": f"demo-r{i}", "platform": pf, "sku": S().art(nm), "name": name, "brand": brand,
                    "group": brand, "rating": rating, "text": text,
                    "date": (today - timedelta(days=rnd.randint(0, 45))).strftime("%Y-%m-%d"),
                    "answer": _RV_DRAFT[5] if answered else "", "nm": str(nm)})
    out.sort(key=lambda r: r["date"], reverse=True)
    arts, groups = {}, {}
    for r in out:
        k = "wb" if r["platform"] == "WB" else "ozon"
        for tab, key, base in ((arts, r["sku"], {"sku": r["sku"], "name": r["name"], "brand": r["brand"], "group": r["group"]}),
                               (groups, r["group"], {"group": r["group"]})):
            a = tab.setdefault(key, {**base, "ozon": None, "wb": None, "ym": None, "ozon_cnt": 0, "wb_cnt": 0,
                                     "ym_cnt": 0, "ozon_sum": 0, "wb_sum": 0, "ym_sum": 0})
            a[k + "_cnt"] += 1
            a[k + "_sum"] += r["rating"]
            a[k] = round(a[k + "_sum"] / a[k + "_cnt"], 2)
    months = sorted({r["date"][:7] for r in out})
    ovr = [{"month": m, "wb": round(4.5 + random.Random(m).random() * 0.4, 2),
            "ozon": round(4.5 + random.Random(m + "o").random() * 0.4, 2)} for m in months]
    drafts = {r["id"]: {"draft": _RV_DRAFT[r["rating"]], "status": "pending"}
              for r in out[:12] if not r["answer"] and r["text"]}
    byp = {}
    for r in out:
        b = byp.setdefault(r["platform"], {"count": 0, "sum": 0})
        b["count"] += 1
        b["sum"] += r["rating"]
    return {"reviews": out,
            "ratings": {"articles": sorted(arts.values(), key=lambda x: x["sku"]),
                        "groups": sorted(groups.values(), key=lambda x: x["group"])},
            "dynamics": {"overview": ovr, "by_article": {}},
            "stats": {"total": len(out), "by_platform": {k: {"count": v["count"], "avg": round(v["sum"] / v["count"], 2)}
                                                          for k, v in byp.items()},
                      "last_review": out[0]["date"] if out else ""},
            "drafts": drafts, "auto": {"last_run": "", "result": {}, "error": ""}}


def review_draft(review_id: str) -> str:
    for r in reviews_data()["reviews"]:
        if r["id"] == review_id:
            return _RV_DRAFT.get(r["rating"], _RV_DRAFT[5])
    return _RV_DRAFT[5]


def productolog() -> dict:
    rnd = random.Random(5)
    tpl = [
        (["Качество сборки", 44], ["Соответствует описанию", 30], ["Быстрая доставка", 17],
         ["Мятая упаковка при доставке", 13], ["Нет русской инструкции", 9],
         "Добавить русскую инструкцию (QR-код на вкладыше) и усилить упаковку — уйдёт половина негатива."),
        (["Большой объём", 39], ["Приятный аромат", 28], ["Результат после первого применения", 21],
         ["Дозатор подтекает", 12], ["Подделки у других продавцов", 7],
         "В карточке подчеркнуть оригинальность (сертификат, фото партии) и проверить дозаторы у поставщика."),
        (["Простая установка", 36], ["Хорошая цена", 27], ["Покупают в подарок", 18],
         ["Шумит", 11], ["Короткий провод", 8],
         "Указать уровень шума и длину провода в характеристиках — снимет ожидания; масштабировать рекламу."),
    ]
    items = []
    for idx, (nm, name, brand, cat) in enumerate(S().PRODUCTS[:14]):
        n, neg, neu = rnd.randint(25, 240), rnd.randint(2, 12), rnd.randint(1, 6)
        p1, p2, p3, m1, m2, rec = tpl[idx % len(tpl)]
        items.append({"sku": S().art(nm), "name": name, "group": brand, "count": n,
                      "avg": round(4.5 + rnd.random() * 0.45, 2), "pos": 100 - neg - neu, "neu": neu, "neg": neg,
                      "pluses": [{"tag": t, "pct": v} for t, v in (p1, p2, p3)],
                      "minuses": [{"tag": t, "pct": v} for t, v in (m1, m2)],
                      "recommendation": rec, "analyzed": True, "analyzable": True,
                      "text_reviews": n - rnd.randint(0, 10), "built_at": _now().strftime("%Y-%m-%d"), "wb_link": ""})
    items.sort(key=lambda x: (-x["neg"], -x["count"]))
    return {"items": items, "building": False, "progress": "", "error": "", "pending": 0}


# ── DEMO_FILL: пример данных там, где реальных нет ──────────────────────────
FILL_NOTE = {
    "/api/feedback/questions": "Пример данных: вопросы WB появятся после подключения рабочего ключа WB, вопросы Ozon — при подписке Premium Plus.",
    "/api/feedback/claims": "Пример данных: рекламации WB появятся после подключения рабочего ключа WB (доступ «Возвраты покупателями»).",
    "/api/reviews/data": "Пример данных: отзывы WB подтянутся с ключом WB (раздел «Вопросы и отзывы»); отзывы Ozon через API — только с подпиской Ozon «Управление отзывами» или Premium Pro.",
    "/api/tools/productolog": "Пример данных: продуктолог разбирает реальные отзывы WB — нужен рабочий ключ WB.",
    "/api/tools/adv": "Пример данных: реклама WB появится после подключения ключа WB с доступом «Продвижение».",
    "/api/tools/wb_funnel": "Пример данных: воронка WB появится после подключения ключа WB с доступом «Аналитика».",
    "/api/tools/funnel": "Пример данных: воронка Ozon доступна с подпиской Premium.",
    "/api/tools/ozads2": "Пример данных: реклама Ozon появится после добавления ключей Ozon Performance.",
    "/api/tools/ozphrases": "Пример данных: поисковые фразы Ozon появятся после добавления ключей Ozon Performance.",
    "/api/tools/ozon/clusters": "Пример данных: остатки по кластерам Ozon требуют доступа к аналитике Ozon.",
}


def fill_is_empty(path: str, d) -> bool:
    if not isinstance(d, dict):
        return False
    if path == "/api/feedback/questions":
        return not d.get("new") and not d.get("answered")
    if path == "/api/feedback/claims":
        return not d.get("open") and not d.get("archive")
    if path == "/api/reviews/data":
        return not d.get("reviews")
    if path in ("/api/tools/productolog", "/api/tools/funnel", "/api/tools/ozphrases",
                "/api/tools/ozon/clusters", "/api/tools/wb_funnel"):
        return not d.get("items")
    if path == "/api/tools/adv":
        return not d.get("campaigns")
    if path == "/api/tools/ozads2":
        return not d.get("campaigns") and not d.get("skus")
    return False


def fill_route(path: str, q: dict):
    if path == "/api/feedback/questions":
        return questions()
    if path == "/api/feedback/claims":
        return claims()
    if path == "/api/reviews/data":
        return reviews_data()
    if path == "/api/tools/productolog":
        return productolog()
    return route(path, q)
