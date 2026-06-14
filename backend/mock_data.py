"""Generates realistic mock data when no real WB API key is provided.

Models a Biomed Nutrition seller cabinet: supplements across several
brands/categories, with per-sale finance fields (commission, logistics,
storage, cost) so the financial dashboard has internally-consistent data.
"""
import random
from datetime import datetime, timedelta

random.seed(42)

# (nmId, name, brand, category)
PRODUCTS = [
    (201001, "Омега-3 1000мг 90 капсул", "Biomed", "Омега"),
    (201002, "Витамин D3 5000 МЕ", "Biomed", "Витамины"),
    (201003, "Магний B6 форте", "Biomed", "Минералы"),
    (201004, "Цинк пиколинат 50мг", "Biomed", "Минералы"),
    (201005, "Коллаген морской 200г", "Biomed", "Красота"),
    (201006, "Витамин C 1000мг шипучий", "Biomed", "Витамины"),
    (201007, "Мелатонин 3мг сон", "Biomed", "Здоровье"),
    (201008, "Железо хелат 25мг", "Biomed", "Минералы"),
    (201009, "Омега-3 детская со вкусом", "Biomed Kids", "Омега"),
    (201010, "Мультивитамины детские мишки", "Biomed Kids", "Витамины"),
    (201011, "Витамин D3 детский капли", "Biomed Kids", "Витамины"),
    (201012, "Кальций + D3 детский", "Biomed Kids", "Минералы"),
    (201013, "Протеин сывороточный 900г ваниль", "Biomed Sport", "Спортпит"),
    (201014, "Креатин моногидрат 300г", "Biomed Sport", "Спортпит"),
    (201015, "BCAA 2:1:1 400г", "Biomed Sport", "Спортпит"),
    (201016, "Л-карнитин 3000 жидкий", "Biomed Sport", "Спортпит"),
    (201017, "Предтрен энергия 300г", "Biomed Sport", "Спортпит"),
    (201018, "Глютамин 300г", "Biomed Sport", "Спортпит"),
    (201019, "Пробиотик 10 штаммов", "Biomed", "Пробиотики"),
    (201020, "Пребиотик инулин 200г", "Biomed", "Пробиотики"),
    (201021, "Гиалуроновая кислота 150мг", "Biomed", "Красота"),
    (201022, "Биотин 10000 мкг", "Biomed", "Красота"),
    (201023, "Куркумин с пиперином", "Biomed", "Здоровье"),
    (201024, "Коэнзим Q10 100мг", "Biomed", "Здоровье"),
    (201025, "Витамины группы B комплекс", "Biomed", "Витамины"),
    (201026, "Селен + цинк иммунитет", "Biomed", "Минералы"),
    (201027, "Омега-3-6-9 комплекс", "Biomed", "Омега"),
    (201028, "Лютеин для зрения", "Biomed", "Здоровье"),
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
                    "supplierArticle": f"BM-{nm_id}",
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
                    "supplierArticle": f"BM-{nm_id}",
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
                "supplierArticle": f"BM-{nm_id}",
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
