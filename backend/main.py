import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from routers import dashboard, upload, advert

_log = logging.getLogger("weekly_prefetch")
_PREFETCH_INTERVAL = 1800  # 30 минут


async def _prefetch_weekly():
    """Фоновая задача: обновляет кеш weekly_summary каждые 30 минут."""
    await asyncio.sleep(5)  # дать серверу подняться
    while True:
        try:
            _log.info("Prefetching weekly_summary...")
            await dashboard.get_weekly_summary()
            _log.info("weekly_summary cache updated")
        except Exception as exc:
            _log.warning("weekly_summary prefetch failed: %s", exc)
        try:
            _log.info("Prefetching monthly_summary...")
            await dashboard.get_monthly_summary()
            _log.info("monthly_summary cache updated")
        except Exception as exc:
            _log.warning("monthly_summary prefetch failed: %s", exc)
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

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if FRONTEND_DIR.exists():
    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(
            str(FRONTEND_DIR / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
