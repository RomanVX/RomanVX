# Дашборд маркетплейсов (`backend/` + `frontend/`)

Один код — несколько деплоев. Каждый деплой обслуживает **один кабинет**
(`CABINET`): `biomed` (WB+Ozon+YM), `fk` (WB+Ozon), `tl` (Технолюкс, WB+Ozon,
клиентский), `avr` (Аврора, WB, клиентский). Данные разделены схемой Postgres
(`DB_SCHEMA`, `search_path={schema},public`, создаётся автоматически).
Клиентские кабинеты по умолчанию `READ_ONLY=1` и `UI_PROFILE=analytics`.

## Модули backend

| Модуль | Назначение |
|---|---|
| `main.py` | приложение, фоновые циклы, middleware авторизации и READ_ONLY (`_RO_DENY`), GZip/CORS; `/api/health`, `/api/cabinet`, `/api/keys/check`; отдаёт `/`, `/wms`, `/fulfilment/`, `/landing/site.tar.gz` |
| `config.py` | env: ключи площадок, `CABINET`, `READ_ONLY`, `UI_PROFILE`, мок-режим, `AI_ONLY_REVIEWS` + `ai_gate()` (при 1 — ИИ только для отзывов) |
| `auth.py` | пользователи, роли (owner / director / manager), cookie `mp_session` 30 дней |
| `db.py` | Postgres пул (или SQLite), плейсхолдер `?` |
| `snapshot.py` | kv_cache со сжатием: `save`, `save_parts`, `save_rows`, `load`; дедуп md5; большие ключи ≤ раз в 6 ч |
| `heavy.py` | `guard()` — тяжёлые сборки по одной; `rss_mb()` |
| `cache.py` | кеш сырых данных WB (sales/orders/stocks 90 дней) |
| `analytics.py` | агрегаты без pandas: финансы, KPI, ABC, склады, юнитка, сводки |
| `wb_client.py` | WB statistics/analytics/content/prices/calendar/common; детальный отчёт постранично |
| `wb_finance_client.py` | finance-api (1 запрос/мин) |
| `wb_fbs.py` | FBS (marketplace v3): задания, стикеры, остатки, мультисклад |
| `wb_funnel.py` | воронка nm-report → таблица `wb_funnel` |
| `advert_client.py` | реклама WB: кампании, расход по месяцам (lock+кеш), статистика, ставки |
| `ozon_client.py` | Ozon Seller: остатки (кластеры), отправления, начисления, отчёты, сертификаты, аналитика, цены, поставки/слоты/грузоместа, акции, КИЗ |
| `ozon_perf_client.py` | Ozon Performance (OAuth), `_num` для чисел-строк |
| `ym_client.py` | Яндекс Маркет: заказы, остатки, отчёт услуг, габариты |
| `reviews_client.py` | отзывы WB/Ozon/YM, черновики, рейтинги, `post_answer` |
| `review_ai.py` | Claude для ответов на отзывы, правила стиля |
| `sales_history.py` | вечная история `sales_daily`, `stocks_daily` (WB API отдаёт только 90 дней — **не терять БД**) |
| `cost_store.py` | себестоимости (`cogs`), сиды `data/cogs_seed.csv` / `cogs_seed_fk.csv` |
| `catalog.py` / `catalog_fk.py` / `catalog_auto.py` | SKU-карты: Biomed / Миагра / автокаталог для клиентских кабинетов (группа = бренд); самообучение nmId → артикул (`sku_map`) |
| `mock_data.py` | демо-кабинет «ФармаЛайн» (если нет WB-ключа) |
| `agent_review.py` | Telegram-бот и еженедельный разбор, история цен |
| `agent_strategist.py`, `agent_actions.py`, `agent_api.py`, `agent_digest.py`, `agent_watch.py` | ИИ-агент: стратег с инструментами, очередь действий с подтверждением, сводка кабинета, ежечасные сторожа (баланс рекламы, скрытые товары, сроки документов, автоакции, out-of-stock) |
| `gist_bridge.py` | запасной мост с домашним агентом через GitHub Gist |
| `start_tunnel.py` | локальный запуск + ngrok |

## Роутеры (`backend/routers/`)

Пометки: **[O]** — только owner, **[RO]** — запрещено при `READ_ONLY` (не-GET).

### `dashboard` — `/api/dashboard`
Продажи и склад: `sales_history`, `filters`, `finance`, `sales-dynamics`,
`products`, `warehouses`, `stocks_table` (+ `stocks_export` xlsx),
`supply-recommendations` (ИИ), `sales_analysis` (ИИ), `supplies`, `kpi`,
`top-skus`, `abc-revenue`, `abc-turnover`, `reorder`, `monthly-pivot`,
`weekly_orders` (заказы по неделям × SKU, все площадки), `orders_matrix`.

### `finance` — `/api/finance` (manager видит только `/unit`)
`wb/pnl`, `wb/unit`, `wb/unit_days`, `wb/reports`, `wb/detail/refetch`,
`ozon/pnl`, `ozon/unit`, `ozon/reports`, `ym/pnl`, `ym/unit`, `ym/reports`,
`payouts` (балансы и выплаты), `manual_costs` (CRUD ручных статей). Отладочные
`*_debug` скрыты. **Юнитка/P&L WB готовы, когда `source=="detail"`** — до этого
building-сообщение, фронт поллит.

### `reviews` — `/api/reviews`
`data` (отзывы, рейтинги, динамика, черновики, статус автоответов), `refresh`,
`export`, `answer-stats`, `rules` (CRUD правил стиля), `analyze-style`,
`draft`, `draft-batch`, `approve` (публикация), `decline`, `auto-pass`.

### `tools` — `/api/tools` (~95 эндпоинтов)
| Область | Эндпоинты |
|---|---|
| Маржа | `margin` (калькулятор: затраты из юнитки, окна усреднения, комиссия из тарифов, прогноз Холта), `margin/export`, `tariff_alert` |
| WB аналитика | `productolog` (ИИ по отзывам), `clusters`, `geo`, `whstocks`, `wb_funnel`, `adv` (реклама WB с советами), `demand` (Джем), `logistics_compare`, `fbs_compare` |
| Ниши и конкуренты | `niche` (+ status/get/history/export), `niche/pending` и `niche/ingest` (домашний агент, токен), `visuals` (+prompt), `competitors` (+queries/collect), `client_prices` |
| Ozon | `ozon/clusters`, `ozads2` (реклама), `ozads/export`, `ozphrases`, `funnel` (Premium), `ozon/marks/*` (КИЗ) [O] |
| Поставки Ozon | `supply/orders`, `supply/watch` (охота за таймслотами), `supply/boxes/upload|match|apply` (грузоместа), `supply/labels`, `supply/export` [O] |
| FBS склад | `fbs/overview`, `fbs/stickers`, `fbs/stocks` (+export/import), `fbs/multi` (+sync/check), `fbs/warehouse` |
| Прочее | `dimensions` (+actual), `sales_history/cleanup`, `img/wb/...jpg` (публичные JPG фото WB для Ozon) |
| Owner-only | `mp/proxy` — прокси к API площадок (см. `10_runbooks.md`), `adv/sandbox`, `ozon/certificate/upload`, `agent/*` |

### Остальные
| Роутер | Эндпоинты |
|---|---|
| `growth` `/api/growth` | `state` (план роста Ozon), `baseline` [O][RO], `journal` [O][RO] |
| `pricelock` `/api/pricelock` | `GET`/`POST` (список фиксаций), `POST /check?fix=` — всё [O][RO] |
| `cardsync` `/api/cardsync` | `compare`, `tasks`, `job`, `apply` [O][RO], `auto` [O][RO] |
| `promo` `/api/promo` | `calendar`, `action` (товары акции с экономикой и рекомендацией), `export.xlsx` |
| `docs` `/api/docs` | `summary`, `matrix` (SKU × документ), `manual` (реестр документов) |
| `upload` `/api/upload` | `costs` (себестоимости xlsx/csv), `costs/status`, `costs/list` |
| `advert` `/api/advert` | `campaigns` (старое) |
| `landing` `/api/landing` | `lead` (публичный) |
| `wms` `/api/wms` | своя WMS — см. `07_mpfit_warehouse.md` |
| auth | `/api/auth/login|logout|me`, `/api/users` [O] |

`_RO_DENY` (main.py): `/api/reviews/approve|auto-pass|draft|analyze-style|decline|rules`,
`/api/tools/fbs/|supply/|ozon/certificate/|adv/sandbox|agent|competitors/|niche|visuals/|dimensions/`,
`/api/wms/`, `/api/cardsync/apply|auto`, `/api/growth/baseline|journal`, `/api/pricelock`.

## Фоновые циклы (main.py, `lifespan`)

| Цикл | Интервал | Что делает |
|---|---|---|
| `_prefetch_weekly` | 30 мин | weekly_orders, stocks_table, отзывы, накопление продаж и цен |
| `_warm_finance` | старт через 180 с, 30 мин | прогрев P&L WB → Ozon → YM |
| `_keep_awake` | 10 мин | самопинг 7–24 МСК (+ второй кабинет, wbfetch) |
| `_funnel_daily` | 3 ч | воронка WB |
| `catalog_auto.loop` | 6 ч | автокаталог (клиентские кабинеты) |
| *не в READ_ONLY:* `_auto_reviews_loop` | 20 мин | обновить отзывы + автоответы (нужен Anthropic) |
| `pricelock.loop` | 30 мин | сторож цен |
| `cardsync.loop` | 6 ч | автоперенос карточек WB → Ozon (если включён) |
| `_fbs_multi_loop` | 15 мин | мультисклад FBS |
| `_client_prices_loop` | 30 мин | клиентские цены через агента (10–22 МСК) |
| `_competitors_daily` | 30 мин | срез выдачи конкурентов (11–21 МСК) |
| `_agent_watch_loop` | 1 ч | снимок остатков, сводка, сторожа агента |
| `_agent_weekly`, `bot_loop` | — | Telegram-разбор по понедельникам, бот |
| `_slot_watcher` | 7 мин | охота за таймслотами Ozon |
| `_wms_storage_loop` | 1 ч | начисление хранения WMS |
| `gist_bridge.loop` | 6 с | если задан Gist-мост |

## Фронтенд (`frontend/index.html`, `app.js`)

Навигация: `goNav(tab)` / `goNavTool(tab, tool)`; паттерн вкладки —
`loadX(refresh)` → `renderX()`, при building-ответе повторный опрос.
**При изменении `app.js` поднять `?v=` в `index.html`.** `fetchJSON` сам
добавляет период и фильтры бренда/категории.

| Меню | Пункты (вкладка → эндпоинты) |
|---|---|
| Аналитика | Калькулятор маржи (`/tools/margin`), Календарь акций (`/promo/*`), Заказы (`/dashboard/weekly_orders`, `orders_matrix`, `sales_analysis`), Финансы (`/finance/*/pnl`, `payouts`, `manual_costs`), Юнитка (`/finance/*/unit`) |
| Склад | Остатки (`stocks_table`), FBS — склад Чехов (`/tools/fbs/*`), WB: кластеры / склады / география, Ozon: кластеры, Ozon: поставки — слоты и короба (`/tools/supply/*`), План поставки Excel |
| Операционка | Отзывы (`/reviews/*`), Документы (`/docs/*`), Габариты |
| Wildberries | Продуктолог, Воронка, Реклама, Спрос (Джем), Калькулятор ниши, Визуалы топ-20, Конкуренты |
| Ozon | Воронка, Реклама (Performance), Запросы, **План роста Ozon** (`/growth/state`), **Карточки WB → Ozon** (`/cardsync/*`) |
| Аккаунт | Обновить данные, Сменить кабинет, Доступы (owner), Выйти |

Роли в UI: manager не видит «Финансы»; «Доступы» — только owner. Профиль
`analytics` (клиентские кабинеты) скрывает FBS, поставки, ИИ-инструменты,
карточки, план роста, операционку.

Мёртвый код в `app.js` (не вызывается): `loadSalesChart`, `loadProducts`,
`loadSupplies`, `loadAdvert`, `loadHistory`, `loadOrdersDaily`, `uploadCosts`,
`wbDetailRefetch`, `renderOzAdsLegacy`, `loadUnitEc`, `loadMonthlyPivot`.

## Хранение

**Таблицы:** `kv_cache`, `users`, `sessions`, `cogs`, `cogs_meta`, `sku_map`,
`sales_daily`, `stocks_daily`, `wb_funnel`, `reviews`, `drafts`,
`rating_snapshots`, `review_rules`, `adv_nm_spend`, `fin_history`,
`oz_pnl_groups`, `oz_unit_sku`, `ym_srv_groups`, `ym_unit_sku`, `manual_costs`,
`docs_manual`, `productolog`, `adv_tool`, `niche_snapshots`, `niche_history`,
`visuals_history`, `client_price_history`, `competitor_daily`, `price_history`,
`agent_actions`, `strategist_tasks` (+ таблицы `wms_*`).

**Ключи kv_cache (главные):** кеши данных — `wb_raw`, `wb_report`, `wb_detail`,
`wb_detail_m_YYYY-MM`, `wb_reports`, `oz_pnl_payload`, `payouts`, `stocks_table`,
`weekly_orders`, `ozads2_*`, `ozphrases_v2`, `promo_calendar`, `demand_jam`,
`margin_snap_*`, `client_prices`, `wb_tariff_alert`, `auto_catalog`; настройки и
состояния — `fbs_multi`, `fbs_warehouse_id`, `dims_actual`, `slot_watch`,
`supply_boxes_plan`, `price_lock`, `cardsync_*`, `growth_baseline`,
`growth_journal`, `growth_state`; агент — `agent_*`, `strategist_*`,
`owner_rules`, `tg_offset`.

## Переменные окружения (только имена)

| Переменная | Назначение |
|---|---|
| `DATABASE_URL`, `DB_SCHEMA`, `SQLITE_PATH` | база |
| `WB_API_KEY` (+ `WB_ADVERT_KEY` для рекламы) | WB. Пустой ключ → мок-режим |
| `OZON_CLIENT_ID`, `OZON_API_KEY` | Ozon Seller |
| `OZON_PERF_CLIENT_ID`, `OZON_PERF_CLIENT_SECRET` | Ozon Performance |
| `YM_API_KEY`, `YM_CAMPAIGN_ID`, `YM_BUSINESS_ID` | Яндекс Маркет |
| `ANTHROPIC_API_KEY`, `AI_ONLY_REVIEWS` | Claude; по умолчанию ИИ только для отзывов |
| `CABINET`, `CABINET_NAME`, `CABINET_MONO`, `OTHER_CABINET_URL/NAME/MONO` | кабинет и ссылка на второй |
| `READ_ONLY`, `UI_PROFILE` | клиентский режим |
| `ADMIN_LOGIN`, `ADMIN_PASSWORD` | стартовый owner (по умолчанию небезопасный — **сменить**) |
| `KEEP_AWAKE`, `KEEP_AWAKE_HOURS`, `KEEP_AWAKE_URL`, `RENDER_EXTERNAL_URL` | самопинг |
| `WB_FETCH_URL`, `WB_FETCH_TOKEN` | сервис wbfetch |
| `WB_AGENT_MODE`, `WB_AGENT_TOKEN` | домашний агент |
| `WB_SEARCH_PROXY`, `WB_SEARCH_VER`, `WB_SEARCH_APPTYPE`, `WB_SEARCH_INTERVAL` | поиск WB |
| `WB_FBS_WAREHOUSE_ID`, `CARDSYNC_IMG_BASE`, `AUTO_MIN_RATING` | FBS, фото для Ozon, порог автоответов |
| `TG_BOT_TOKEN`, `TG_CHAT_ID`, `LEADS_TG_CHAT_ID` | Telegram |
| `GIST_BRIDGE_ID`, `GIST_BRIDGE_TOKEN` | Gist-мост |
| `WMS_ADMIN_LOGIN`, `WMS_ADMIN_PASSWORD` | первый админ WMS |

## wbfetch (сервис `romanvx`)

Docker (`mcr.microsoft.com/playwright/python`), `GET /healthz`, `/diag`,
`/search?query=&token=` — headless Chromium + прокси, перехват выдачи WB.
Сейчас основной путь к выдаче — домашний агент.

## ИИ в дашборде

Модели: `claude-opus-4-8` (ответы на отзывы, продуктолог, ниши, визуалы,
агент), `claude-haiku-4-5` (рекомендации поставок, анализ продаж). При
`AI_ONLY_REVIEWS=1` всё, кроме отзывов, выключено (экономия). **Баланс
Anthropic закончился 11.11.2026** — автоответы стоят.
