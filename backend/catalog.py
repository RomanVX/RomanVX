"""Hardcoded article → name/brand catalog."""

CATALOG: dict[str, dict[str, str]] = {
    "BMN-0028": {"name": "SEX FIST 500 мл",              "brand": "Джага"},
    "BMN-0013": {"name": "SEX FIST 200 мл",              "brand": "Джага"},
    "BMN-0035": {"name": "FIST MINT 500 мл",             "brand": "Джага"},
    "BMN-0036": {"name": "FIST MINT 200 мл",             "brand": "Джага"},
    "BMN-0115": {"name": "Спрей клубника 50мл",          "brand": "Джага"},
    "BMN-0116": {"name": "Спрей бабл гам 50мл",          "brand": "Джага"},
    "BMN-0110": {"name": "Спрей ваниль 50мл",            "brand": "Джага"},
    "BMN-0008": {"name": "Anal 200 мл",                  "brand": "Джага"},
    "BMN-0002": {"name": "Universal 200 мл",             "brand": "Джага"},
    "BMN-0106": {"name": "Hybrid 200 мл",                "brand": "Джага"},
    "BMN-0006": {"name": "Hot sex 200 мл",               "brand": "Джага"},
    "BMN-0109": {"name": "Разогревающий hybrid 200 мл",  "brand": "Джага"},
    "BMN-0004": {"name": "Safe sex 200 мл",              "brand": "Джага"},
    "BMN-0069": {"name": "Аромасвеча Искушение",         "brand": "Джага"},
    "BMN-0070": {"name": "Аромасвеча Клубника со сливками", "brand": "Джага"},
    "BMN-0058": {"name": "Очищающий спрей",              "brand": "Джага"},
    "ST-01":    {"name": "pH4 с лактобактериями",        "brand": "Satisfucktion"},
    "ST-02":    {"name": "С перцем фиолетовый",          "brand": "Satisfucktion"},
    "ST-03":    {"name": "Анал синий",                   "brand": "Satisfucktion"},
    "ST-04":    {"name": "Табак-ваниль зеленый",         "brand": "Satisfucktion"},
    "ST-05":    {"name": "С гиалуроновой кислотой",      "brand": "Satisfucktion"},
    "ST-06":    {"name": "С маслом кокоса оранжевый",    "brand": "Satisfucktion"},
    "ST-07":    {"name": "Для фистинга",                 "brand": "Satisfucktion"},
    "AL-01":    {"name": "Очищающий гель",               "brand": "Aloe"},
    "AL-02":    {"name": "Гидрофильное масло",           "brand": "Aloe"},
    "AL-03":    {"name": "Мицеллярная вода",             "brand": "Aloe"},
    "AL-04":    {"name": "Тонер для лица",               "brand": "Aloe"},
    "AL-05":    {"name": "Сыворотка",                    "brand": "Aloe"},
    "AL-06":    {"name": "Крем увлажняющий",             "brand": "Aloe"},
    "AL-07":    {"name": "Крем ночной",                  "brand": "Aloe"},
}

BRAND_ORDER = ["Джага", "Satisfucktion", "Aloe"]


def lookup(article: str) -> dict[str, str]:
    """Return {name, brand} from CATALOG or fallback."""
    return CATALOG.get(article, {"name": article, "brand": "Прочее"})
