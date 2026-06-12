import random
from datetime import datetime, timedelta

random.seed(42)

PRODUCTS = [
    (101001, "Кроссовки спортивные мужские"),
    (101002, "Футболка базовая белая"),
    (101003, "Джинсы slim fit синие"),
    (101004, "Куртка зимняя с капюшоном"),
    (101005, "Платье летнее цветочное"),
    (101006, "Рюкзак городской 30л"),
    (101007, "Кепка бейсбол чёрная"),
    (101008, "Носки хлопковые набор 5пар"),
    (101009, "Свитер оверсайз серый"),
    (101010, "Шорты пляжные мужские"),
    (101011, "Блузка шёлковая женская"),
    (101012, "Брюки классические чёрные"),
    (101013, "Кеды белые холщовые"),
    (101014, "Пальто осеннее бежевое"),
    (101015, "Топ спортивный женский"),
    (101016, "Комбинезон детский 80р"),
    (101017, "Шапка вязаная зимняя"),
    (101018, "Перчатки кожаные чёрные"),
    (101019, "Шарф тёплый клетчатый"),
    (101020, "Ремень кожаный мужской"),
    (101021, "Сумка шоппер бежевая"),
    (101022, "Очки солнцезащитные"),
    (101023, "Майка мужская белая"),
    (101024, "Леггинсы спортивные"),
    (101025, "Толстовка с капюшоном"),
    (101026, "Юбка миди в горошек"),
    (101027, "Пижама фланелевая"),
    (101028, "Халат махровый белый"),
    (101029, "Купальник слитный"),
    (101030, "Джемпер кашемировый"),
    (101031, "Бомбер молодёжный"),
    (101032, "Сарафан льняной"),
    (101033, "Брюки спортивные"),
    (101034, "Рубашка клетчатая"),
    (101035, "Жилет утеплённый"),
    (101036, "Боди женское"),
    (101037, "Гетры детские"),
    (101038, "Плащ непромокаемый"),
    (101039, "Водолазка тонкая"),
    (101040, "Шорты джинсовые"),
    (101041, "Поло мужское"),
    (101042, "Лонгслив базовый"),
    (101043, "Туника пляжная"),
    (101044, "Кардиган длинный"),
    (101045, "Ветровка лёгкая"),
    (101046, "Комбинезон спортивный"),
    (101047, "Футболка с принтом"),
    (101048, "Джинсы женские"),
    (101049, "Костюм спортивный"),
    (101050, "Анорак горнолыжный"),
]

PRICES = {nm_id: random.randint(500, 8000) for nm_id, _ in PRODUCTS}
STOCKS = {nm_id: random.randint(0, 300) for nm_id, _ in PRODUCTS}


def _base_demand(nm_id: int) -> float:
    rank = PRODUCTS.index(next(p for p in PRODUCTS if p[0] == nm_id))
    return max(0.3, 20 * (0.85 ** rank) + random.uniform(-0.5, 0.5))


def generate_sales(date_from: datetime, date_to: datetime) -> list[dict]:
    records = []
    current = date_from
    while current <= date_to:
        for nm_id, name in PRODUCTS:
            demand = _base_demand(nm_id)
            weekday_factor = 1.3 if current.weekday() >= 5 else 1.0
            qty = max(0, int(random.gauss(demand * weekday_factor, demand * 0.4)))
            if qty == 0:
                current += timedelta(days=1)
                continue
            price = PRICES[nm_id]
            for _ in range(qty):
                records.append({
                    "date": current.isoformat(),
                    "lastChangeDate": current.isoformat(),
                    "warehouseName": random.choice(["Коледино", "Электросталь", "Казань"]),
                    "regionName": random.choice(["Москва", "СПб", "Екатеринбург", "Казань"]),
                    "supplierArticle": f"ART-{nm_id}",
                    "nmId": nm_id,
                    "subject": name,
                    "category": "Одежда",
                    "brand": "MyBrand",
                    "isCancel": False,
                    "totalPrice": price,
                    "discountPercent": random.choice([10, 15, 20, 25, 30]),
                    "finishedPrice": int(price * 0.8),
                    "priceWithDisc": int(price * 0.8),
                    "saleID": f"S{nm_id}{current.strftime('%Y%m%d')}{random.randint(1000,9999)}",
                    "forPay": int(price * 0.7),
                })
        current += timedelta(days=1)
    return records


def generate_orders(date_from: datetime, date_to: datetime) -> list[dict]:
    records = []
    current = date_from
    while current <= date_to:
        for nm_id, name in PRODUCTS:
            demand = _base_demand(nm_id) * 1.1
            weekday_factor = 1.3 if current.weekday() >= 5 else 1.0
            qty = max(0, int(random.gauss(demand * weekday_factor, demand * 0.4)))
            if qty == 0:
                current += timedelta(days=1)
                continue
            price = PRICES[nm_id]
            for _ in range(qty):
                records.append({
                    "date": current.isoformat(),
                    "lastChangeDate": current.isoformat(),
                    "warehouseName": random.choice(["Коледино", "Электросталь", "Казань"]),
                    "regionName": random.choice(["Москва", "СПб", "Екатеринбург"]),
                    "supplierArticle": f"ART-{nm_id}",
                    "nmId": nm_id,
                    "subject": name,
                    "category": "Одежда",
                    "brand": "MyBrand",
                    "isCancel": random.random() < 0.08,
                    "totalPrice": price,
                    "discountPercent": random.choice([10, 15, 20, 25, 30]),
                    "finishedPrice": int(price * 0.8),
                    "priceWithDisc": int(price * 0.8),
                    "orderId": random.randint(100000, 999999),
                })
        current += timedelta(days=1)
    return records


def generate_stocks() -> list[dict]:
    records = []
    for nm_id, name in PRODUCTS:
        qty = STOCKS[nm_id]
        if qty == 0:
            continue
        records.append({
            "lastChangeDate": datetime.utcnow().isoformat(),
            "warehouseName": "Коледино",
            "supplierArticle": f"ART-{nm_id}",
            "nmId": nm_id,
            "subject": name,
            "category": "Одежда",
            "brand": "MyBrand",
            "techSize": "0",
            "barcode": f"460000{nm_id}",
            "Price": PRICES[nm_id],
            "Discount": 20,
            "isSupply": True,
            "isRealization": False,
            "quantityFull": qty,
            "quantityNotInOrders": max(0, qty - random.randint(0, 10)),
            "inWayToClient": random.randint(0, 15),
            "inWayFromClient": random.randint(0, 5),
            "quantity": qty,
        })
    return records
