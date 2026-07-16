import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from datetime import datetime, timedelta

from routers import dashboard, upload, advert, reviews, finance, tools, docs
import cache
import cost_store
import heavy
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
    # WB — persist_wb срабатывает write-through при каждом реальном обновлении
    # кеша (weekly_orders/stocks_table только что его загрузили), поэтому
    # принудительный invalidate() здесь означал бы двойную выкачку 90 дней.
    try:
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
            await heavy.guard(reviews_client.refresh_all(), "reviews")
            _log.info("reviews refreshed")
        except Exception as exc:
            _log.warning("reviews refresh failed: %s", exc)
        try:
            _log.info("Accumulating sales history...")
            await heavy.guard(_accumulate_sales(), "accumulate_sales")
            _log.info("sales history accumulated")
        except Exception as exc:
            _log.warning("sales accumulation failed: %s", exc)
        await asyncio.sleep(_PREFETCH_INTERVAL)


async def _warm_finance():
    """Прогрев финансов отдельным циклом со стартовой задержкой и паузами:
    на Render free одновременный старт всех сборок (WB детальный, Ozon по
    дням, YM отчёты) забивает CPU/память и роняет инстанс (502)."""
    await asyncio.sleep(180)   # даём подняться основным кешам после деплоя
    from routers import finance as _fin
    while True:
        for name, fn in (("WB", _fin.get_wb_pnl),
                         ("Ozon", _fin.get_ozon_pnl),
                         ("YM", _fin.get_ym_pnl)):
            try:
                await fn(months=6, refresh=False)
                _log.info("finance warm: %s ok", name)
            except Exception as exc:
                _log.warning("finance warm %s failed: %s", name, exc)
            await asyncio.sleep(120)   # сборки стартуют по очереди, не разом
        await asyncio.sleep(_PREFETCH_INTERVAL)


async def _keep_awake():
    """Не даём Render free усыпить сервис (спит после 15 мин без входящих).

    Пингуем собственный внешний URL (запрос идёт через прокси Render и
    считается входящим) и второй кабинет. RENDER_EXTERNAL_URL Render
    подставляет сам. Окно активности — KEEP_AWAKE_HOURS по МСК
    (по умолчанию 7-24), ночью сервис спит и экономит бесплатные часы.
    Выключить: KEEP_AWAKE=0."""
    if os.getenv("KEEP_AWAKE", "1") != "1":
        return
    self_url = os.getenv("KEEP_AWAKE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    from config import OTHER_CABINET_URL
    urls = [u.rstrip("/") + "/api/health"
            for u in (self_url, OTHER_CABINET_URL or "") if u]
    fetch_url = os.getenv("WB_FETCH_URL", "").strip()
    if fetch_url:
        urls.append(fetch_url.rstrip("/") + "/healthz")
    if not urls:
        return
    try:
        lo, hi = (int(x) for x in os.getenv("KEEP_AWAKE_HOURS", "7-24").split("-"))
    except ValueError:
        lo, hi = 7, 24
    import httpx
    klog = logging.getLogger("keep_awake")
    klog.info("keep-awake: %s, окно %d-%d МСК", urls, lo, hi)
    while True:
        msk_hour = (datetime.utcnow() + timedelta(hours=3)).hour
        if lo <= msk_hour < hi:
            for u in urls:
                try:
                    async with httpx.AsyncClient(timeout=25) as c:
                        await c.get(u)
                except Exception as exc:
                    klog.warning("keep-awake ping %s failed: %s", u, exc)
        await asyncio.sleep(600)   # пинг каждые 10 мин (< 15 мин до сна)


async def _agent_weekly():
    """Агент-аналитик: разбор кабинета в Telegram по понедельникам в 9 МСК.

    Дедуп через kv_cache (agent_review_last) — рестарты/два инстанса не
    задваивают отправку."""
    import agent_review
    if not agent_review.configured():
        logging.getLogger("agent_review").info(
            "агент выключен (нет TG_BOT_TOKEN/TG_CHAT_ID)")
        return
    import snapshot as _snap
    while True:
        now = datetime.utcnow() + timedelta(hours=3)
        if now.weekday() == 0 and now.hour == 9:
            wk = now.strftime("%G-W%V")
            last = await asyncio.to_thread(_snap.load, "agent_review_last", "")
            if last != wk:
                res = await agent_review.send_review("WB")
                logging.getLogger("agent_review").info("weekly review: %s", res)
                if res.get("ok"):
                    await asyncio.to_thread(_snap.save, "agent_review_last", wk)
        await asyncio.sleep(1200)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cost_store.init()
    _auth.ensure_bootstrap()
    task = asyncio.create_task(_prefetch_weekly())
    task2 = asyncio.create_task(_warm_finance())
    task3 = asyncio.create_task(_keep_awake())
    task4 = asyncio.create_task(_agent_weekly())
    yield
    task.cancel()
    task2.cancel()
    task3.cancel()
    task4.cancel()


app = FastAPI(title="WB Analytics Dashboard", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# JSON с отзывами/заказами весит сотни КБ — gzip ужимает в 5-10 раз
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(dashboard.router)
app.include_router(upload.router)
app.include_router(advert.router)
app.include_router(reviews.router)
app.include_router(finance.router)
app.include_router(tools.router)
app.include_router(docs.router)

import auth as _auth
app.include_router(_auth.router)
app.include_router(_auth.users_router)


@app.middleware("http")
async def _auth_middleware(request, call_next):
    denied = _auth.check_request(request)
    if denied is not None:
        return denied
    return await call_next(request)


@app.get("/api/health")
def get_health():
    """Диагностика: память процесса (Render free — 512 МБ) и размер тяжёлых кешей."""
    from routers import finance as _fin
    return {
        "ok": True,
        "rss_mb": heavy.rss_mb(),
        "wb_detail_rows": len(_fin._detail_cache.get("rows", [])),
    }


@app.get("/api/cabinet")
def get_cabinet():
    """Конфигурация кабинета: имя, площадки, группировки, ссылка на второй кабинет."""
    from config import (CABINET, CABINET_NAME, CABINET_MARKETPLACES,
                        OTHER_CABINET_URL, OTHER_CABINET_NAME)
    out = {"id": CABINET, "name": CABINET_NAME, "marketplaces": CABINET_MARKETPLACES}
    if CABINET == "fk":
        import catalog_fk
        out["group_order"] = catalog_fk.GROUP_ORDER
        out["brand_order"] = catalog_fk.BRAND_ORDER
        out["subgroups"] = []
    if OTHER_CABINET_URL:
        out["other"] = {"name": OTHER_CABINET_NAME, "url": OTHER_CABINET_URL}
    return out

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if FRONTEND_DIR.exists():
    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(
            str(FRONTEND_DIR / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
