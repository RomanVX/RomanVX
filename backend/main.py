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


async def _report_interrupted():
    """Сессия агента, оборванная деплоем, оставляет статус «взялся за вопрос»
    навсегда. При старте честно закрываем такие висяки."""
    import snapshot as _snap
    import agent_review as _agent
    await asyncio.sleep(25)
    try:
        st = await asyncio.to_thread(_snap.load, "agent_inflight", None)
        if not st:
            return
        await asyncio.to_thread(_snap.save, "agent_inflight", None)
        await _agent.tg_edit(st.get("msg_id"),
                             "Сессия прервалась (перезапуск сервера). "
                             "Повтори вопрос — отвечу.",
                             chat_id=st.get("chat") or "")
    except Exception as e:
        logging.getLogger("agent").warning("interrupted report: %s", e)


async def _damage_autofill():
    """Пустые склады ущерба заполняются сами из отчётов платного хранения WB.

    Лимит метода — 1 запрос в минуту, поэтому по очереди с паузами.
    Заполненные склады повторно не трогаем; если отчёт не собрался с первого
    раза (у WB это бывает) — повторяем каждые полчаса, пока не соберём."""
    import damage
    import agent_review as _agent
    await asyncio.sleep(240)          # дать серверу прогреться
    log = logging.getLogger("damage")
    results = []
    for attempt in range(12):         # ~6 часов попыток максимум
        if attempt:
            await asyncio.sleep(1800)
        try:
            pend = [w for w in
                    (await asyncio.to_thread(damage.summary))["pending"]
                    if w in damage.FIRE_DATES]
        except Exception as e:
            log.warning("autofill list: %s", e)
            continue
        if not pend:
            break
        for wh in pend:
            try:
                res = await damage.fetch_from_storage(wh)
            except Exception as e:
                res = {"error": str(e)[:200]}
            if res.get("error"):
                log.warning("autofill %s: %s", wh, res["error"])
            else:
                log.info("autofill %s: %s SKU, %s шт",
                         wh, res["skus"], res["qty"])
                results.append(f"{wh}: {res['skus']} SKU, {res['qty']} шт "
                               f"(срез {res['snap_date']})")
            await asyncio.sleep(75)   # лимит отчёта хранения: 1 запрос/мин
    if not results:
        return
    try:
        d = await asyncio.to_thread(damage.summary)
        t = d["total"]
        await _agent.tg_send(
            "<b>Ущерб от пожаров — заполнил сам из отчётов хранения WB</b>\n\n"
            + "\n".join(results)
            + f"\n\nИтого по всем складам: {t['qty']} шт, "
              f"{t['cost_total']:,} ₽ по себестоимости, "
              f"{t['retail_total']:,} ₽ по рознице.".replace(",", " ")
            + "\nДетали и Excel — Склад → Ущерб от пожаров.")
    except Exception as e:
        log.warning("autofill notify: %s", e)


async def _fbs_multi_loop():
    """Мультисклад FBS: синк виртуального остатка на привязанные склады WB
    каждые 15 минут (как FBS-хабы, но своими руками, ключи не уходят наружу)."""
    import wb_fbs
    await asyncio.sleep(600)
    log = logging.getLogger("fbs_multi")
    while True:
        try:
            res = await wb_fbs.multi_sync()
            if not res.get("skipped"):
                log.info("синк: заказов списано %s, склады: %s",
                         res.get("consumed_orders"), res.get("pushed"))
        except Exception as e:
            log.warning("синк: %s", str(e)[:150])
        await asyncio.sleep(900)


async def _client_prices_loop():
    """Клиентские цены (СПП) своих карточек через домашний агент: раз в
    4 часа в окно 10-22 МСК (ПК владельца включён). Молча пропускает,
    если агент офлайн."""
    from routers import tools as _tools
    import snapshot as _snap
    await asyncio.sleep(900)
    log = logging.getLogger("client_prices")
    while True:
        now = datetime.utcnow() + timedelta(hours=3)
        if 10 <= now.hour < 22:
            stamp = now.strftime("%Y-%m-%d %H")
            last = await asyncio.to_thread(_snap.load, "client_prices_last", "")
            if not last or (now - datetime.strptime(last, "%Y-%m-%d %H")
                            ).total_seconds() >= 4 * 3600 - 60:
                try:
                    res = await _tools.refresh_client_prices()
                    if res.get("error"):
                        log.info("skip: %s", res["error"][:120])
                    else:
                        log.info("обновлено цен: %s", res.get("updated"))
                        await asyncio.to_thread(_snap.save,
                                                "client_prices_last", stamp)
                except Exception as e:
                    log.warning("loop: %s", str(e)[:150])
        await asyncio.sleep(1800)


async def _funnel_daily():
    """Воронка WB (nm-report): раз в сутки дособираем последние 7 дней в
    вечную таблицу. Первый прогон — глубже (28 дн), чтобы сразу было с чем
    сравнивать."""
    import config
    import wb_funnel
    if config.USE_MOCK:
        return
    await asyncio.sleep(420)
    log = logging.getLogger("wb_funnel")
    first = True
    while True:
        try:
            have = await asyncio.to_thread(
                lambda: (wb_funnel.summary(14) or {}).get("history"))
            res = await wb_funnel.fetch(28 if (first and not have) else 7)
            if res.get("error"):
                log.warning("daily: %s", res["error"])
            else:
                log.info("daily: %s", res)
        except Exception as e:
            log.warning("daily: %s", str(e)[:200])
        first = False
        await asyncio.sleep(24 * 3600)



async def _agent_is_quiet() -> bool:
    """Тихий режим (/quiet в TG): фоновые LLM-циклы и алерты выключены."""
    import snapshot as _snap
    try:
        return bool(await asyncio.to_thread(_snap.load, "agent_quiet", False))
    except Exception:
        return False


async def _news_loop():
    """Новости площадок: сбор и разбор раз в 3 часа, сводка в 10:00 МСК."""
    import snapshot as _snap
    import news as _news
    import agent_review as _agent
    await asyncio.sleep(900)
    log = logging.getLogger("news")
    while True:
        if await _agent_is_quiet():
            await asyncio.sleep(3 * 3600)
            continue
        try:
            res = await _news.refresh_all()
            if res.get("added"):
                log.info("новостей добавлено %s, разобрано %s",
                         res["added"], res.get("analyzed"))
        except Exception as e:
            log.warning("refresh: %s", e)
        try:
            now = datetime.utcnow() + timedelta(hours=3)      # МСК
            today = now.strftime("%Y-%m-%d")
            last = await asyncio.to_thread(_snap.load, "news_digest_last", "")
            if now.hour == 10 and last != today:
                text = await _news.morning_digest()
                if text:
                    await _agent.tg_send(text)
                await asyncio.to_thread(_snap.save, "news_digest_last", today)
        except Exception as e:
            log.warning("digest: %s", e)
        await asyncio.sleep(1800)


async def _agent_watch_loop():
    """Сторожа агента: раз в час проверяет и пишет сам, если что-то горит."""
    import agent_watch
    await asyncio.sleep(600)          # дать серверу прогреться после старта
    while True:
        try:
            try:      # история остатков: копится вечно, как продажи
                import sales_history as _sh
                await _sh.snapshot_stocks()
            except Exception as e:
                logging.getLogger("stocks_hist").warning("snapshot: %s", e)
            try:      # снимок кабинета держим свежим для агента
                import agent_digest
                await agent_digest.refresh()
            except Exception as e:
                logging.getLogger("agent_digest").warning("refresh: %s", e)
            if await _agent_is_quiet():
                await asyncio.sleep(3600)
                continue
            res = await agent_watch.tick()
            if res.get("sent"):
                logging.getLogger("agent_watch").info("отправлено тревог: %s",
                                                      res["sent"])
        except Exception as e:
            logging.getLogger("agent_watch").warning("tick: %s", e)
        await asyncio.sleep(3600)


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
        if await _agent_is_quiet():
            await asyncio.sleep(3600)
            continue
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
        if await _agent_is_quiet():
            await asyncio.sleep(3600)
            continue
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
    task_watch = asyncio.create_task(_agent_watch_loop())
    task_news = asyncio.create_task(_news_loop())
    asyncio.create_task(_damage_autofill())
    asyncio.create_task(_report_interrupted())
    task7 = asyncio.create_task(_trends_weekly())
    task8 = asyncio.create_task(_strategist_loop())
    task9 = asyncio.create_task(_bid_history_daily())
    asyncio.create_task(_funnel_daily())
    asyncio.create_task(_client_prices_loop())
    asyncio.create_task(_fbs_multi_loop())
    import gist_bridge
    asyncio.create_task(gist_bridge.loop())
    task10 = asyncio.create_task(_slot_watcher())
    yield
    for t in (task, task2, task3, task4, task5, task6, task7, task8, task9,
              task10, task_watch, task_news):
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
