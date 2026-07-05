import os
import logging
from dotenv import load_dotenv

load_dotenv()  # no-op on Render (env vars already set), safe to keep

WB_API_KEY: str    = os.getenv("WB_API_KEY", "").strip()
WB_ADVERT_KEY: str = os.getenv("WB_ADVERT_KEY", WB_API_KEY).strip()

OZON_CLIENT_ID: str = os.getenv("OZON_CLIENT_ID", "").strip()
OZON_API_KEY: str   = os.getenv("OZON_API_KEY", "").strip()

YM_API_KEY: str      = os.getenv("YM_API_KEY", "").strip()
YM_CAMPAIGN_ID: str  = os.getenv("YM_CAMPAIGN_ID", "").strip()
YM_BUSINESS_ID: str  = os.getenv("YM_BUSINESS_ID", "").strip()

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()

# ── Кабинет ─────────────────────────────────────────────────────────────────
# Один деплой = один кабинет; кабинет задаётся переменными окружения.
# biomed — Biomed Nutrition (WB+Ozon+YM), fk — Фабрика красоты/Миагра (WB+Ozon)
CABINET: str = os.getenv("CABINET", "biomed").strip().lower()
_CAB_DEFAULTS = {
    "biomed": {"name": "Biomed Nutrition", "marketplaces": ["WB", "OZON", "YM"]},
    "fk":     {"name": "Фабрика красоты",  "marketplaces": ["WB", "OZON"]},
}
CABINET_NAME: str = os.getenv("CABINET_NAME", "").strip() \
    or _CAB_DEFAULTS.get(CABINET, {}).get("name", CABINET)
CABINET_MARKETPLACES: list = _CAB_DEFAULTS.get(CABINET, {}).get("marketplaces", ["WB", "OZON", "YM"])
if not YM_API_KEY and "YM" in CABINET_MARKETPLACES:
    CABINET_MARKETPLACES = [m for m in CABINET_MARKETPLACES if m != "YM"]
# ссылка на второй кабинет (другой деплой) — для карточки на экране выбора
OTHER_CABINET_URL: str  = os.getenv("OTHER_CABINET_URL", "").strip()
OTHER_CABINET_NAME: str = os.getenv("OTHER_CABINET_NAME", "").strip() \
    or ("Biomed Nutrition" if CABINET == "fk" else "Фабрика красоты")

USE_MOCK: bool = not WB_API_KEY or WB_API_KEY == "your_wildberries_api_key_here"
USE_ADVERT_MOCK: bool = not WB_ADVERT_KEY or WB_ADVERT_KEY == "your_wildberries_api_key_here"

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)
_log.info("WB Analytics mode: %s", "MOCK" if USE_MOCK else f"LIVE (key …{WB_API_KEY[-6:]})")
_log.info("WB Advert mode: %s", "MOCK" if USE_ADVERT_MOCK else f"LIVE (key …{WB_ADVERT_KEY[-6:]})")
_log.info("Ozon: %s", "configured" if OZON_CLIENT_ID else "not configured")
_log.info("YM: %s", "configured" if YM_API_KEY else "not configured")

