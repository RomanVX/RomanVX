"""Generates realistic mock data when no real WB API key is provided.

Демо-кабинет ООО «ФармаЛайн»: БАДы и спортпит: supplements across several
brands/categories, with per-sale finance fields (commission, logistics,
storage, cost) so the financial dashboard has internally-consistent data.
"""
import random
from datetime import datetime, timedelta

random.seed(42)

# (nmId, name, brand, category)
PRODUCTS = [
    (201001, "Омега-3 1000мг 90 капсул", "ФармаЛайн", "Омега"),
    (201002, "Витамин D3 5000 МЕ", "ФармаЛайн", "Витамины"),
    (201003, "Магний B6 форте", "ФармаЛайн", "Минералы"),
    (201004, "Цинк пиколинат 50мг", "ФармаЛайн", "Минералы"),
    (201005, "Коллаген морской 200г", "ФармаЛайн", "Красота"),
    (201006, "Витамин C 1000мг шипучий", "ФармаЛайн", "Витамины"),
    (201007, "Мелатонин 3мг сон", "ФармаЛайн", "Здоровье"),
    (201008, "Железо хелат 25мг", "ФармаЛайн", "Минералы"),
    (201009, "Омега-3 детская со вкусом", "ФармаЛайн Kids", "Омега"),
    (201010, "Мультивитамины детские мишки", "ФармаЛайн Kids", "Витамины"),
    (201011, "Витамин D3 детский капли", "ФармаЛайн Kids", "Витамины"),
    (201012, "Кальций + D3 детский", "ФармаЛайн Kids", "Минералы"),
    (201013, "Протеин сывороточный 900г ваниль", "ФармаЛайн Спорт", "Спортпит"),
    (201014, "Креатин моногидрат 300г", "ФармаЛайн Спорт", "Спортпит"),
    (201015, "BCAA 2:1:1 400г", "ФармаЛайн Спорт", "Спортпит"),
    (201016, "Л-карнитин 3000 жидкий", "ФармаЛайн Спорт", "Спортпит"),
    (201017, "Предтрен энергия 300г", "ФармаЛайн Спорт", "Спортпит"),
    (201018, "Глютамин 300г", "ФармаЛайн Спорт", "Спортпит"),
    (201019, "Пробиотик 10 штаммов", "ФармаЛайн", "Пробиотики"),
    (201020, "Пребиотик инулин 200г", "ФармаЛайн", "Пробиотики"),
    (201021, "Гиалуроновая кислота 150мг", "ФармаЛайн", "Красота"),
    (201022, "Биотин 10000 мкг", "ФармаЛайн", "Красота"),
    (201023, "Куркумин с пиперином", "ФармаЛайн", "Здоровье"),
    (201024, "Коэнзим Q10 100мг", "ФармаЛайн", "Здоровье"),
    (201025, "Витамины группы B комплекс", "ФармаЛайн", "Витамины"),
    (201026, "Селен + цинк иммунитет", "ФармаЛайн", "Минералы"),
    (201027, "Омега-3-6-9 комплекс", "ФармаЛайн", "Омега"),
    (201028, "Лютеин для зрения", "ФармаЛайн", "Здоровье"),
]

WAREHOUSES = ["Коледино", "Электросталь", "Казань", "Тула"]

PRICES = {nm: random.randint(450, 3800) for nm, *_ in PRODUCTS}
# total physical stock per SKU, distributed across warehouses
STOCKS = {nm: random.randint(0, 420) for nm, *_ in PRODUCTS}

DISCOUNTS = [10, 15, 20, 25, 30, 35]


def _base_demand(nm_id: int) -> float:
    """Average daily orders for an SKU (Pareto: top SKUs sell much more)."""
    rank = next(i for i, p in enumerate(PRODUCTS) if p[0] == nm_id)
    return max(0.4, 22 * (0.86 ** rank) + random.uniform(-0.5, 0.5))


def _finance(price_full: int, discount: int) -> dict:
    """Self-consistent per-item finance breakdown."""
    gross = round(price_full * (1 - discount / 100))      # продажи после СПП
    commission = round(gross * 0.15)                       # вознаграждение WB
    delivery = random.randint(45, 130)                     # логистика
    storage = round(gross * 0.015)                         # хранение
    cost = round(price_full * 0.30)                        # себестоимость
    for_pay = gross - commission - delivery - storage      # к перечислению (реализация)
    return {
        "priceWithDisc": gross,
        "commissionRub": commission,
        "deliveryRub": delivery,
        "storageRub": storage,
        "costRub": cost,
        "forPay": for_pay,
        "finishedPrice": gross,
    }


def generate_sales(date_from: datetime, date_to: datetime) -> list[dict]:
    records = []
    current = date_from
    while current <= date_to:
        weekend = 1.25 if current.weekday() >= 5 else 1.0
        for nm_id, name, brand, category in PRODUCTS:
            demand = _base_demand(nm_id)
            qty = max(0, int(random.gauss(demand * weekend, demand * 0.4)))
            for _ in range(qty):
                price_full = PRICES[nm_id]
                discount = random.choice(DISCOUNTS)
                fin = _finance(price_full, discount)
                rec = {
                    "date": current.isoformat(),
                    "lastChangeDate": current.isoformat(),
                    "warehouseName": random.choice(WAREHOUSES),
                    "regionName": random.choice(["Москва", "СПб", "Екатеринбург", "Казань"]),
                    "supplierArticle": f"PL-{nm_id}",
                    "nmId": nm_id,
                    "subject": name,
                    "brand": brand,
                    "category": category,
                    "isCancel": False,
                    "totalPrice": price_full,
                    "discountPercent": discount,
                    "saleID": f"S{nm_id}{current.strftime('%Y%m%d')}{random.randint(1000,9999)}",
                }
                rec.update(fin)
                records.append(rec)
        current += timedelta(days=1)
    return records


def generate_orders(date_from: datetime, date_to: datetime) -> list[dict]:
    records = []
    current = date_from
    while current <= date_to:
        weekend = 1.25 if current.weekday() >= 5 else 1.0
        for nm_id, name, brand, category in PRODUCTS:
            demand = _base_demand(nm_id) * 1.12  # orders > buyouts
            qty = max(0, int(random.gauss(demand * weekend, demand * 0.4)))
            for _ in range(qty):
                price_full = PRICES[nm_id]
                discount = random.choice(DISCOUNTS)
                records.append({
                    "date": current.isoformat(),
                    "lastChangeDate": current.isoformat(),
                    "warehouseName": random.choice(WAREHOUSES),
                    "regionName": random.choice(["Москва", "СПб", "Екатеринбург"]),
                    "supplierArticle": f"PL-{nm_id}",
                    "nmId": nm_id,
                    "subject": name,
                    "brand": brand,
                    "category": category,
                    "isCancel": random.random() < 0.07,  # ~7% возвраты/отмены
                    "totalPrice": price_full,
                    "discountPercent": discount,
                    "priceWithDisc": round(price_full * (1 - discount / 100)),
                    "orderId": random.randint(100000, 999999),
                })
        current += timedelta(days=1)
    return records


# ── Демо-данные для остальных вкладок (финансы/юнитка/маржа/отзывы/цены) ──────
COSTS = {f"PL-{nm}": round(PRICES[nm] * 0.32) for nm, *_ in PRODUCTS}
NAMES = {f"PL-{nm}": name for nm, name, *_ in PRODUCTS}

# комиссия WB по «категориям» демо-кабинета
_COMM = {"Спортпит": 21.5, "Витамины": 24.5, "Омега": 23.0, "Минералы": 23.0,
         "Красота": 24.5, "Здоровье": 23.5, "Пробиотики": 23.0}


def generate_report_detail(date_from: datetime, date_to: datetime) -> list[dict]:
    """Детальный отчёт реализации (сырой формат statistics-api) — понедельно,
    согласован с generate_sales по порядку величин. Кормит P&L/юнитку/маржу."""
    rnd = random.Random(7)
    rows: list[dict] = []
    rrd = 1_000_000
    # выравниваем на понедельник
    cur = date_from - timedelta(days=date_from.weekday())
    while cur <= date_to:
        week_end = cur + timedelta(days=6)
        sale_dt = min(week_end, date_to).strftime("%Y-%m-%dT00:00:00")
        rr_dt = sale_dt
        for nm_id, name, brand, category in PRODUCTS:
            demand = max(0.4, 22 * (0.86 ** next(
                i for i, p in enumerate(PRODUCTS) if p[0] == nm_id)))
            qty = max(0, int(rnd.gauss(demand * 7, demand * 1.5)))
            if qty == 0:
                continue
            price_pre = round(PRICES[nm_id] * 0.78)          # до СПП, после скидки
            cp = _COMM.get(category, 23.0)
            retail_amount = round(price_pre * qty * 0.82)    # что заплатил покупатель
            rrd += 1
            rows.append({
                "rrd_id": rrd, "nm_id": nm_id, "sa_name": f"PL-{nm_id}",
                "brand_name": brand, "subject_name": category,
                "doc_type_name": "Продажа", "supplier_oper_name": "Продажа",
                "sale_dt": sale_dt, "rr_dt": rr_dt, "order_dt": sale_dt,
                "quantity": qty,
                "retail_price_withdisc_rub": price_pre,
                "retail_amount": retail_amount,
                "commission_percent": cp,
                "ppvz_for_pay": round(price_pre * qty * (1 - cp / 100)),
                "acquiring_fee": round(retail_amount * 0.018),
                "delivery_rub": qty * rnd.randint(58, 82),
                "storage_fee": 0, "acceptance": 0, "penalty": 0, "deduction": 0,
            })
            # хранение — отдельной операционной строкой
            rrd += 1
            rows.append({
                "rrd_id": rrd, "nm_id": nm_id, "sa_name": f"PL-{nm_id}",
                "doc_type_name": "", "supplier_oper_name": "Хранение",
                "sale_dt": None, "rr_dt": rr_dt, "quantity": 0,
                "storage_fee": round(qty * 6.5), "delivery_rub": 0,
                "acceptance": 0, "penalty": 0, "deduction": 0,
                "retail_amount": 0, "retail_price_withdisc_rub": 0,
                "ppvz_for_pay": 0, "acquiring_fee": 0,
            })
            if rnd.random() < 0.03:   # редкий штраф
                rrd += 1
                rows.append({
                    "rrd_id": rrd, "nm_id": nm_id, "sa_name": f"PL-{nm_id}",
                    "doc_type_name": "", "supplier_oper_name": "Штраф",
                    "sale_dt": None, "rr_dt": rr_dt, "quantity": 0,
                    "penalty": rnd.randint(300, 1800), "storage_fee": 0,
                    "delivery_rub": 0, "acceptance": 0, "deduction": 0,
                    "retail_amount": 0, "retail_price_withdisc_rub": 0,
                    "ppvz_for_pay": 0, "acquiring_fee": 0,
                })
        cur += timedelta(days=7)
    return rows


_REVIEW_TEXTS = [
    (5, "Отличное качество, заказываю уже третий раз. Упаковка целая, сроки свежие."),
    (5, "Работает! Пью месяц — сон наладился, энергии больше. Рекомендую."),
    (5, "Быстрая доставка, всё как в описании. Спасибо продавцу!"),
    (4, "Хороший состав за свои деньги. Минус звезда за мятую коробку."),
    (5, "Беру для всей семьи, качество стабильное."),
    (3, "Эффекта пока не заметила, пью две недели. Посмотрим дальше."),
    (5, "Лучшее соотношение цена/качество из того, что пробовал."),
    (2, "Пришла банка с повреждённой крышкой. Продавец, решите вопрос!"),
    (5, "Вкус приятный, растворяется хорошо, побочек нет."),
    (4, "Нормально, но хотелось бы объём побольше за эту цену."),
    (1, "Заказала одно — привезли другое. Оформляю возврат."),
    (5, "Проверенный производитель, состав чистый, сертификаты есть."),
]


def generate_reviews(platform=None, limit=500) -> list[dict]:
    """Отзывы для демо-кабинета в формате get_all_reviews()."""
    rnd = random.Random(11)
    out = []
    today = datetime.utcnow()
    for i in range(120):
        nm_id, name, brand, category = PRODUCTS[rnd.randrange(len(PRODUCTS))]
        rating, text = _REVIEW_TEXTS[rnd.randrange(len(_REVIEW_TEXTS))]
        pf = rnd.choices(["WB", "Ozon", "YM"], weights=[70, 22, 8])[0]
        if platform and platform != "all" and pf != platform:
            continue
        answered = rating >= 4 and rnd.random() < 0.8
        out.append({
            "id": f"demo-{i}", "platform": pf, "sku": f"PL-{nm_id}",
            "name": name, "brand": brand, "group": category,
            "rating": rating, "text": text,
            "date": (today - timedelta(days=rnd.randint(0, 60))).strftime("%Y-%m-%d"),
            "answer": "Спасибо за отзыв! Рады, что вам подошло 💚" if answered else "",
            "nm": str(nm_id),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out[:limit]


_PRODUCTOLOG = [
    (["Работает, эффект заметен · 46%", "Качество стабильное · 31%", "Быстрая доставка · 18%"],
     ["Мятая упаковка при доставке · 12%", "Хотят больший объём · 8%"],
     "Усилить упаковку (жалобы на мятые коробки) и добавить фасовку увеличенного объёма — покупатели готовы брать больше."),
    (["Приятный вкус · 38%", "Удобно принимать · 29%", "Помогает со сном · 24%"],
     ["Эффект не сразу · 14%", "Цена высоковата · 9%"],
     "В карточке указать срок накопительного эффекта (2-3 недели) — снимет часть негатива «не работает»."),
    (["Чистый состав · 41%", "Есть сертификаты · 22%", "Заказывают повторно · 19%"],
     ["Крышка открывается туго · 11%"],
     "Проверить партию крышек с поставщиком; в остальном карточка здорова — масштабировать рекламу."),
]


def generate_productolog() -> dict:
    """Готовый ответ «Продуктолога» для демо — без вызовов LLM."""
    rnd = random.Random(5)
    items = []
    for idx, (nm_id, name, brand, category) in enumerate(PRODUCTS[:14]):
        n = rnd.randint(25, 240)
        neg = rnd.randint(2, 12)
        neu = rnd.randint(1, 6)
        pluses, minuses, rec = _PRODUCTOLOG[idx % len(_PRODUCTOLOG)]
        items.append({
            "sku": f"PL-{nm_id}", "name": name, "group": category,
            "count": n, "avg": round(4.5 + rnd.random() * 0.45, 2),
            "pos": 100 - neg - neu, "neu": neu, "neg": neg,
            "pluses": [{"tag": p.split(" · ")[0], "pct": int(p.split(" · ")[1].rstrip("%"))}
                       for p in pluses],
            "minuses": [{"tag": m.split(" · ")[0], "pct": int(m.split(" · ")[1].rstrip("%"))}
                        for m in minuses],
            "recommendation": rec,
            "analyzed": True, "analyzable": True,
            "text_reviews": n - rnd.randint(0, 10),
            "built_at": datetime.utcnow().strftime("%Y-%m-%d"),
            "wb_link": "",
        })
    items.sort(key=lambda x: (-x["neg"], -x["count"]))
    return {"items": items, "building": False, "progress": "", "error": "",
            "pending": 0}


def generate_stocks() -> list[dict]:
    """Distribute each SKU's stock across 1-3 warehouses."""
    records = []
    for nm_id, name, brand, category in PRODUCTS:
        total = STOCKS[nm_id]
        if total == 0:
            continue
        whs = random.sample(WAREHOUSES, k=random.randint(1, 3))
        split = [total // len(whs)] * len(whs)
        split[0] += total - sum(split)
        for wh, qty in zip(whs, split):
            if qty <= 0:
                continue
            records.append({
                "lastChangeDate": datetime.utcnow().isoformat(),
                "warehouseName": wh,
                "supplierArticle": f"PL-{nm_id}",
                "nmId": nm_id,
                "subject": name,
                "brand": brand,
                "category": category,
                "techSize": "0",
                "barcode": f"460000{nm_id}",
                "Price": PRICES[nm_id],
                "Discount": 20,
                "quantityFull": qty,
                "inWayToClient": random.randint(0, 18),
                "inWayFromClient": random.randint(0, 6),
                "quantity": qty,
            })
    return records
