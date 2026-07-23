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
        try:
            import agent_review as _agent
            await _agent.accumulate_prices()
        except Exception as exc:
            _log.warning("price accumulation failed: %s", exc)
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


async def _competitors_daily():
    """Дашборд конкурентов: суточный срез выдачи WB через домашний агент.
    Окно 11-21 МСК (ПК включён), дедуп по дню через kv_cache."""
    import snapshot as _snap
    from routers import tools as _tools
    await asyncio.sleep(600)   # даём прогреться основным сборкам
    while True:
        now = datetime.utcnow() + timedelta(hours=3)
        today = now.strftime("%Y-%m-%d")
        if 11 <= now.hour < 21:
            last = await asyncio.to_thread(_snap.load, "competitors_last_day", "")
            if last != today:
                try:
                    res = await _tools.competitors_collect_daily()
                    if res.get("ok"):
                        await asyncio.to_thread(_snap.save, "competitors_last_day", today)
                except Exception as e:
                    logging.getLogger("competitors").warning("daily failed: %s", e)
        await asyncio.sleep(1800)


async def _slot_watcher():
    """Охота за таймслотами поставки Ozon: опрос каждые 7 минут, пока включена."""
    from routers import tools as _tools
    await asyncio.sleep(300)
    while True:
        try:
            await _tools.slot_watch_tick()
        except Exception as e:
            logging.getLogger("slot_watch").warning("tick: %s", e)
        await asyncio.sleep(420)


async def _bid_history_daily():
    """История рекламных кластеров: суточный срез в БД (13:00 МСК, дедуп)."""
    import snapshot as _snap
    from routers import tools as _tools
    await asyncio.sleep(1500)
    while True:
        now = datetime.utcnow() + timedelta(hours=3)
        today = now.strftime("%Y-%m-%d")
        if now.hour >= 13:
            last = await asyncio.to_thread(_snap.load, "bid_history_last", "")
            if last != today:
                try:
                    res = await _tools.bid_collect_daily()
                    if res.get("rows") is not None:
                        await asyncio.to_thread(_snap.save, "bid_history_last", today)
                except Exception as e:
                    logging.getLogger("bid_history").warning("daily failed: %s", e)
        await asyncio.sleep(1800)


async def _trends_weekly():
    """Радар трендов: раз в неделю (вторник 12 МСК) снимаем поисковые запросы
    своих товаров из Ozon Seller API. Дедуп по неделе через kv_cache."""
    import snapshot as _snap
    from routers import tools as _tools
    await asyncio.sleep(900)
    while True:
        now = datetime.utcnow() + timedelta(hours=3)
        if now.weekday() == 1 and now.hour >= 12:
            wk = now.strftime("%G-W%V")
            last = await asyncio.to_thread(_snap.load, "trends_last_week", "")
            if last != wk:
                try:
                    res = await _tools.trends_collect_ozon()
                    if res.get("rows"):
                        await asyncio.to_thread(_snap.save, "trends_last_week", wk)
                except Exception as e:
                    logging.getLogger("trends").warning("weekly failed: %s", e)
        await asyncio.sleep(3600)


async def _strategist_loop():
    """Стратег: полная сессия по понедельникам в 10 МСК; ежедневно в 10 МСК —
    проверка задач с подошедшей датой (есть due → сфокусированная сессия).
    Дедуп по дню через kv_cache."""
    import agent_strategist as st
    import agent_review
    if not agent_review.configured():
        return
    import snapshot as _snap
    await asyncio.sleep(1200)   # даём прогреться юнитке после рестарта
    while True:
        now = datetime.utcnow() + timedelta(hours=3)
        today = now.strftime("%Y-%m-%d")
        if now.hour >= 10:
            last = await asyncio.to_thread(_snap.load, "strategist_last_day", "")
            if last != today:
                try:
                    if now.weekday() == 0:
                        res = await st.run_session(trigger="еженедельная сессия (понедельник)")
                    else:
                        due = await asyncio.to_thread(st.due_tasks)
                        res = {"ok": True, "skipped": "нет задач к проверке"}
                        if due:
                            titles = "; ".join(t["title"] for t in due[:5])
                            res = await st.run_session(
                                trigger="ежедневная проверка задач",
                                focus=f"Подошла дата проверки задач: {titles}. "
                                      f"Сверь план/факт по ним и закрой; полный "
                                      f"разбор кабинета не нужен.")
                    if res.get("ok"):
                        await asyncio.to_thread(_snap.save, "strategist_last_day", today)
                    else:
                        logging.getLogger("strategist").warning("session: %s", res)
                except Exception as e:
                    logging.getLogger("strategist").warning("loop failed: %s", e)
        await asyncio.sleep(1800)


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
    import agent_review as _agent
    task5 = asyncio.create_task(_agent.bot_loop())
    task6 = asyncio.create_task(_competitors_daily())
    task7 = asyncio.create_task(_trends_weekly())
    task8 = asyncio.create_task(_strategist_loop())
    task9 = asyncio.create_task(_bid_history_daily())
    task10 = asyncio.create_task(_slot_watcher())
    yield
    for t in (task, task2, task3, task4, task5, task6, task7, task8, task9, task10):
        t.cancel()


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
    from config import USE_MOCK
    out = {"id": CABINET, "name": CABINET_NAME, "marketplaces": CABINET_MARKETPLACES,
           "demo": USE_MOCK}
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
