# Маркетплейсы: API, лимиты, грабли

Спецификации лежат в репозитории — **сначала смотреть их**, потом писать код:

- `docs/wb_api/` — 13 OpenAPI-файлов WB + `INDEX.md` (список всех методов).
- `docs/ozon_api/seller.json` — полная спека Ozon Seller API (458 методов).
- `docs/ozon_api/performance.txt` — документация Ozon Performance (реклама).
- `docs/mpfit_api/` — API МПФИТ.

Правило: брать из спеки **полную схему запроса и `example`**, enum-значения и
обязательные поля — только оттуда.

## Wildberries

| Тема | Что знать |
|---|---|
| Токены | Один токен на кабинет, категории доступа выбираются при выпуске. Срок жизни ограничен — у Миагры истёк ~27.10.2026. «Сервисный» токен нужен с нужными категориями (контент, цены, статистика, аналитика, вопросы/отзывы, продвижение, финансы). |
| Хосты | content / discounts-prices / statistics / analytics / advert / feedbacks / common / finance-api / dp-calendar (акции). Базовые URL — в `backend/wb_client.py`. |
| Цены | `POST /api/v2/upload/task` {nmID, price, discount}. Цена после скидки продавца = price×(1−discount/100); покупатель видит ещё минус СПП. Падение цены в 3+ раза → карантин. Лимит 10 запросов / 6 с. |
| Отчёт реализации | finance-api `sales-reports/detailed` — лимит 1 запрос/мин. Формируется раз в неделю, хвост месяца добирается оперативными продажами. Детальный отчёт ~45 тыс. строк — качаем страницами. |
| Statistics API | заказы/продажи только за ~90 дней — поэтому своя вечная история `sales_daily`. |
| Комиссия | Выросла 07.07.2026 (+6–9 п.п. почти везде). Официальные тарифы: `get_commission_tariffs()`; поле `paidStorageKgvp` = FBW, `kgvpSupplier` = DBS (не перепутать). |
| Реклама | `POST /adv/v1/promotion/adverts` умер в 07.2026 — использовать новые методы из спеки. Удержания «WB Продвижение» в отчёте реализации считаем расходом месяца удержания (решение владельца). |
| Поисковые запросы | normquery stats (≤30 дней, ≤100 пар); search-report (Джем) только по своим товарам. Выдачу поиска собирает только браузерный агент с домашнего IP. |
| Акции | dp-calendar-api: список, детали, товары (для автоакций список товаров API не отдаёт). |
| Вопросы/отзывы | feedbacks-api. Ответ на отзыв — PATCH. |

## Ozon Seller API

| Тема | Что знать |
|---|---|
| Декларации/сертификаты | Рабочий метод — **`/v2/product/certificate/create`** (v1 даёт 404): `{params:{certificate_type:"DECLARATION", certificate_country:"RU", accordance_type:"EAEU", name, number, issue_date, expired_date:{date:{year,month,day}} \| {infinite}, skus:[int], files:[{name, file_content(base64)}]}}`. Формат подобран зондированием — в спеке его нет. |
| Фото | `/v2/product/pictures/import` `{items:[{offer_id, primary_image, images}]}` → `task_id`, статус — `/v1/product/import/info`. v1 «obsolete». Грузить пачками по ~8, ждать задачу; повторную загрузку того же URL Ozon помечает skipped — добавлять `?v=`. |
| Атрибуты | `/v1/product/attributes/update`. ID: 4191 — аннотация, 4180 — название, 23171 — хештеги, 11254 — Rich JSON (`raTextBlock` + `raShowcase` roll/tileM с `imgLink`), 9048 — склейка (тот же тип+бренд, разные аспекты: вкус 4561 / объём 8163), 22390 — «Похожие», 85 — бренд. Rich-контент **заменяет** описание на витрине — описание для SEO держать в 4191, а rich делать с `raTextBlock`. |
| Цены | `/v1/product/import/prices`: `price`, `old_price`, `min_price` (акции ниже не опустят), `auto_action_enabled`, `price_strategy_enabled`. Не чаще 10 раз в час на товар. |
| Цена для покупателя | В API её **нет** (`/v1/product/prices/details` — только Premium Pro). Брали с витрины: `https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/product/<sku>/` с cookie `is_adult_confirmed=true; adult_user_birthdate=1990-01-01` → виджет `webPrice-*`: `cardPrice` (с Ozon Картой), `price`. Скидка Ozon нелинейна — подбор цены продавца в 1–2 итерации. |
| Акции | `/v1/actions/products/activate` заблокирован при «unified price logic»; товары под репрайсером вручную в акции не добавляются. Цена акции следует за `price`. |
| Аналитика | `/v1/analytics/data` без Premium отдаёт только ~3 последних месяца. |
| Вопросы | `/v1/question/list` — только Premium Plus. |
| Отзывы | Нельзя ответить на отзыв **без текста** (`cannot comment on empty review`) и на отзыв на модерации (`publish delayed`). |
| Лимиты | 429 «rate limit per second» на этикетках `cargoes-label` — нужны паузы. |
| Комиссия ~50% | У товаров Миагры/Biomed высокая комиссия при участии в программах софинансирования (баллы Ozon ~60% выручки). |

## Ozon Performance (реклама)

- OAuth client_credentials (`ozon_perf_client.py`). Числа приходят **строками с запятой** (`_num`).
- **«Оплата за заказ» (CPO):** ставка фиксированная (`get_cpo_min_bids`) — 28% от цены, **12%**, если товар одновременно в CPC-кампании. `v2/bids/set` устарел и ставку не меняет.
  Включить/выключить товары: `/api/client/campaign/search_promo/product/enable|disable {skus}`; список — `POST /api/client/campaign/search_promo/v2/products`.
- **CPC:** создать — `POST /api/client/campaign/cpc/v2/product {title, weeklyBudget (в 1e-6 ₽), placement, productAutopilotStrategy: TARGET_CIR}`; товары — `POST /api/client/campaign/{id}/products {bids:[{sku, targetCir}]}` (targetCir ≥ 10); бюджет — `PATCH /api/client/campaign/{id} {weeklyBudget}`; `/activate`, `/deactivate`.
- Остановка CPC поднимает CPO-ставку товара с 12% до 28% — учитывать.

## Репрайсер Ozon

- Без API, только xlsx-шаблон в `seller.ozon.ru/app/prices/repricer`.
- **Одна** «Целевая цена на сайте» (цена для покупателя) — сразу на Ozon и на связанный WB-артикул. Разные цены на площадках задать нельзя → такие товары снимаются с репрайсера (см. сторож цен в `04_biomed.md`).
- «Ограничение для стратегий на Ozon» обрезается до «предельной цены»; ограничение на WB — не ниже 50% цены.

## Яндекс Маркет

- `ym_client.py`, `YM_API_KEY` + `YM_CAMPAIGN_ID` (+ `YM_BUSINESS_ID`). Ответы на отзывы — `goods-feedback/comments/update`.
- На Маркете у Biomed скрыты массажные карточки (по просьбе владельца).

## «Честный знак»

- Срок годности по КИЗ — публичный мобильный сервис `mobile.api.crpt.ru/mobile/check?code=…&codeType=datamatrix` (как приложение ЧЗ, без регистрации). С зарубежных IP — 451, поэтому ходим через VPS (`/crpt/*`) из браузера склада.
- Маркировка лубрикантов/косметики — да, КИЗ на товаре; при переупаковке на складе дублируем КИЗ на этикетку («Упаковка КИЗ»).
