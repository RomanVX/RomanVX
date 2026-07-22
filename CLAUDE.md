# Market Partners — мультимаркетплейс-дашборд

Аналитика для селлеров WB / Ozon / Яндекс.Маркет. FastAPI + vanilla JS (без
сборки). Владелец — Роман (Biomed Nutrition + агентство Market Partners).

## Прод (Render, регион Virginia)

| Сервис | URL | Что это |
|---|---|---|
| wb-dashboard | https://wb-dashboard-6wxf.onrender.com | кабинет Biomed (Starter, 512 МБ) |
| fk-dashboard | https://fk-dashboard.onrender.com | кабинет «Фабрика красоты»/Миагра (free) |
| romanvx | https://romanvx.onrender.com | wb-fetch Docker-сервис |
| mp-postgres | Basic-256mb, $6/мес | ОДНА база на оба кабинета |

- Деплой: пуш в `main` → Render автодеплоит оба дашборда.
- **База**: Render Postgres, кабинеты разведены по схемам через env
  `DB_SCHEMA` = `biomed` / `fk` (см. `backend/db.py`, search_path).
  До 2026-07-15 жили на Neon — там кончилась квота data transfer
  (5 ГБ/мес), из-за этого переехали. Neon-проекты можно удалить после
  2026-08-01. Перенос: `scripts/migrate_db.py`.
- Память 512 МБ — ГЛАВНОЕ ограничение. Все решения ниже растут из него.

## Архитектура выживания на 512 МБ (не ломать!)

- `backend/heavy.py` — semaphore(1): тяжёлые сборки строго по одной
  (`heavy.guard`). rss_mb() — замер памяти, пишется в логи.
- `backend/snapshot.py` — снапшоты кешей в kv_cache (zlib):
  переживают рестарт/сон. `save_parts`/`save_rows` — потоковое сжатие
  кусками (НЕ собирать гигантские JSON-строки в памяти!). Дедуп по md5,
  ключи >256 КБ пишутся не чаще раза в 6 ч (экономия трафика БД).
- Детальный отчёт WB (~45 тыс. строк) качается страницами по 10 тыс.
  (`wb_client._REPORT_KEEP` — проекция полей + sys.intern), снапшотится
  в `wb_detail`, после рестарта поднимается из БД мгновенно.
- Реклама по nmId (`_build_adv_nm_bg` в finance.py) ждёт, пока
  `_detail_fetching` не отпустит память; хранится помесячно в БД.
- `advert_client.get_spend_by_month` — lock+кеш: параллельные сборки P&L
  не дублируют запросы (иначе 429 от лимитера WB).
- `main.py`: `_keep_awake` (самопинг 7–24 МСК), `_warm_finance` (прогрев
  по очереди со стартовой задержкой 180с), `_prefetch_weekly`.
  `/api/health` → rss_mb, wb_detail_rows.

## Данные и клиенты

- `wb_client.py` — statistics/analytics/content API WB. Также:
  `get_commission_tariffs()` (официальные тарифы комиссии; ВНИМАНИЕ:
  поле `paidStorageKgvp` = «Склад WB (FBW)», `kgvpSupplier` = DBS!),
  `get_card_subjects()` (nmID → категория).
- `wb_finance_client.py` — finance-api (лимит 1 req/мин, паузы до 60с).
- `ozon_client.py`, `ozon_perf_client.py` (реклама Ozon, OAuth
  client_credentials), `ym_client.py`.
- `sales_history.py` — таблица sales_daily (дата × платформа × SKU):
  вечная история продаж (WB API отдаёт только 90 дней — НЕ терять БД).
- `cost_store.py` — себестоимости; `catalog.py`/`catalog_fk.py` — SKU-карты
  кабинетов, `articleGroup` — группировка по категориям.
- `reviews_client.py` — отзывы всех площадок, инкрементальная догрузка.
- ИИ: `claude-opus-4-8` (продуктолог, вердикты ниши, Vision-разбор
  визуалов через base64, советы по рекламе). Ключ ANTHROPIC_API_KEY.

## Анти-бот WB (выдача поиска)

Серверные/прокси IP забанены WB (403/429/498). Работает ТОЛЬКО браузер с
домашнего IP: `agent/wb_browser_agent.py` (Playwright) крутится на втором
ПК владельца (МГТС), опрашивает `/api/tools/niche/pending?token=...`
(WB_AGENT_TOKEN, env) и отдаёт результаты в `/niche/ingest`. Джем-подписка
даёт официальный search-report API, но только по СВОИМ товарам.

## Фронтенд

`frontend/app.js` (один файл, ~4000 строк) + `index.html`. При каждом
изменении app.js — поднять `?v=` в index.html (cache-bust). Паттерн
вкладок: `loadX(refresh)` → `renderX()`; при building-ответе — setTimeout
повторного опроса. Группы артикулов: GROUP_ORDER + articleGroup().

## Калькулятор маржи (routers/tools.py: get_margin)

Самый насыщенный инструмент: затраты на штуку из юнитки, окна усреднения
(свежие статьи — 2 мес, хранение/штрафы — 4 мес, авторасширение при <10
продаж), комиссия из официальных тарифов по категории, редактируемые
цена/себес/ДРР, 3 колонки целевых марж, цена покупателя (после СПП),
прогноз месяца (модель Холта с затуханием по 12 нед sales_daily —
`_sales_forecast`), алерт при изменении тарифов WB (`_tariff_watch`),
экспорт в Excel (POST /margin/export, учитывает правки с фронта).

## Env-переменные (Render)

DATABASE_URL (+DB_SCHEMA!), WB_API_TOKEN, OZON_CLIENT_ID/API_KEY,
OZON_PERF_CLIENT_ID/SECRET, YM_TOKEN/BUSINESS_ID, ANTHROPIC_API_KEY,
WB_AGENT_MODE=1 + WB_AGENT_TOKEN, KEEP_AWAKE, SQLITE_PATH (локально).

## Документация WB API

`docs/wb_api/` — официальные OpenAPI-спеки всех разделов WB
(13 файлов + INDEX.md со списком методов). При любой работе с WB API
СНАЧАЛА смотреть метод/поля/лимиты там, потом писать код — WB часто
выключает старые методы (напр., POST /adv/v1/promotion/adverts умер
07.2026), а dev.wildberries.ru с сервера недоступен.
Owner-only прокси для живой проверки методов (бой и песочница):
POST /api/tools/adv/sandbox {path, method, params?, json?, sandbox: bool}.

## Известные грабли

- Юнитка/P&L WB готовы только когда `pnl.source == "detail"` — до этого
  эндпоинты отдают building-message, фронт поллит.
- WB формирует отчёт реализации раз в неделю; хвост месяца добирается
  оперативными продажами (`tail_days`).
- Числа Ozon Performance приходят строками с запятой (`_num`).
- Комиссия WB выросла 07.07.2026 (+6–9 пп почти по всем категориям).
- Пароли/токены светились в переписке — при работе не публиковать их в
  коммитах; дефолтные admin/admin в кабинетах надо менять.

## Хвосты (на 2026-07-16)

- Rotate password у mp-postgres + обновить DATABASE_URL обоих сервисов.
- Сменить admin/admin в обоих кабинетах.
- Удалить Neon-проекты после 01.08.2026.
- Ozon «Запросы»: группировка по товарам доделана, следить за жалобами.
- Отложено: черновики ответов на отзывы без текста (draft-batch их
  пропускает).
