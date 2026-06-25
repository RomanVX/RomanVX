import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from datetime import datetime, timedelta

from routers import dashboard, upload, advert, reviews, finance
import cache
import ozon_client
import reviews_client
import ym_client

_log = logging.getLogger("weekly_prefetch")
_PREFETCH_INTERVAL = 1800  # 30 минут


async def _accumulate_sales():
    """Гарантированно складывает продажи за последние 30 дней в БД (write-through
    внутри fetch-функций). Не зависит от кешей дашборда."""
    dt_to   = datetime.utcnow()
    dt_from = dt_to - timedelta(days=30)
    df, dt = dt_from.strftime("%Y-%m-%d"), dt_to.strftime("%Y-%m-%d")
    # WB — через cache._refresh (persist_wb внутри)
    try:
        cache.invalidate()
        await cache.get_raw_data(dt_from, dt_to)
    except Exception as exc:
        _log.warning("accumulate WB failed: %s", exc)
    # Ozon / YM — get_sales_detail сами вызывают persist_detail
    try:
        await ozon_client.get_sales_detail(df, dt)
    except Exception as exc:
        _log.warning("accumulate Ozon failed: %s", exc)
    try:
        await ym_client.get_sales_detail(df, dt)
    except Exception as exc:
        _log.warning("accumulate YM failed: %s", exc)


async def _prefetch_weekly():
    """Фоновая задача: обновляет кеш weekly_summary каждые 30 минут."""
    await asyncio.sleep(5)  # дать серверу подняться
    while True:
        try:
            _log.info("Prefetching weekly_orders...")
            await dashboard.get_weekly_orders()
            _log.info("weekly_orders cache updated")
        except Exception as exc:
            _log.warning("weekly_orders prefetch failed: %s", exc)
        try:
            _log.info("Prefetching stocks_table...")
            await dashboard.get_stocks_table()
            _log.info("stocks_table cache updated")
        except Exception as exc:
            _log.warning("stocks_table prefetch failed: %s", exc)
        try:
            _log.info("Refreshing reviews...")
            await reviews_client.refresh_all()
            _log.info("reviews refreshed")
        except Exception as exc:
            _log.warning("reviews refresh failed: %s", exc)
        try:
            _log.info("Accumulating sales history...")
            await _accumulate_sales()
            _log.info("sales history accumulated")
        except Exception as exc:
            _log.warning("sales accumulation failed: %s", exc)
        await asyncio.sleep(_PREFETCH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_prefetch_weekly())
    yield
    task.cancel()


app = FastAPI(title="WB Analytics Dashboard", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dashboard.router)
app.include_router(upload.router)
app.include_router(advert.router)
app.include_router(reviews.router)
app.include_router(finance.router)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if FRONTEND_DIR.exists():
    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(
            str(FRONTEND_DIR / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
