# Market Partners — WB Analytics Dashboard

Профессиональный дашборд селлера Wildberries: финансовая аналитика (P&L),
структура выручки, ABC по выручке/прибыли, остатки по складам, планирование поставок.

## Возможности

- **Вход:** `admin` / `admin` (хардкод на фронте, без бэкенда)
- **Выбор кабинета:** Biomed Nutrition
- **Дашборд:** 16 финансовых метрик (чистая прибыль, маржа, ROI, комиссия,
  логистика, реклама/ДРР, хранение, налоги…) с дельтой к прошлому периоду
- **Структура выручки:** горизонтальный bar-chart + топ-5 SKU
- **Товары:** таблица с сортировкой, поиском, ABC-метками (выручка/прибыль)
- **Остатки:** карточки по складам со статусом и спарклайнами
- **Поставки:** план дозаказа на 30/60/90 дней с приоритетом
- **Фильтры:** период (от/до), бренд, категория

## Стек

- **Backend:** Python 3.11+ / FastAPI (чистый Python, без pandas)
- **Frontend:** HTML + кастомный CSS (тёмная тема, золото `#c9a84c`) + Chart.js (CDN)

> **Логотипы.** `frontend/static/lion_logo.svg` (Biomed) и `mp_logo.svg`
> (Market Partners) — векторные SVG-эмблемы. Чтобы поставить реальные PNG,
> положите `lion_logo.png` / `mp_logo.png` в `frontend/static/` и поправьте
> пути в `index.html`.

## Публичный лендинг (marketpartners.ru)

В папке `landing/` живёт одностраничный маркетинговый сайт агентства.
FastAPI отдаёт его с **корня** сервиса, дашборд переехал на **`/app`**:

| URL | Что открывается |
| --- | --- |
| `/` | лендинг Market Partners |
| `/app/` | дашборд аналитики (логин как раньше) |
| `POST /api/lead` | приём заявок с формы лендинга (публичный) |

Заявки всегда дописываются в `backend/data/leads.jsonl` (на Render диск
эфемерный — файл живёт до следующего деплоя, это только страховка).
Основные каналы уведомлений включаются переменными окружения — см.
`.env.example` (`LEAD_TG_BOT_TOKEN`/`LEAD_TG_CHAT_ID` для Telegram,
`LEAD_SMTP_*`/`LEAD_EMAIL_*` для почты). Настроить стоит хотя бы один.

Папка `landing/` самодостаточна: её можно выложить и на обычный
shared-хостинг с PHP (reg.ru) — форма шлётся в `send.php`, который
FastAPI на Render просто перехватывает совместимым роутом.

> На бесплатном тарифе Render сервис засыпает ночью (см. `KEEP_AWAKE_HOURS`),
> и лендинг будет открываться с задержкой в 30–60 секунд. Если сайт должен
> быть быстрым круглосуточно — расширьте окно до `KEEP_AWAKE_HOURS=0-24`
> или перенесите лендинг на Render Static Site.

## Быстрый старт

### 1. Клонировать и настроить окружение

```bash
git clone <repo>
cd <repo>

cp .env.example .env
# Откройте .env и вставьте свой WB_API_KEY
```

### 2. Установить зависимости и запустить

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Дашборд откроется по адресу: **http://localhost:8000**

> **Без ключа** приложение работает на mock-данных (50 SKU, 30 дней). Это удобно для разработки и демонстрации.

### 3. Получить API-ключ Wildberries

1. Войдите в [Личный кабинет WB Seller](https://seller.wildberries.ru/)
2. Перейдите: **Настройки → Доступ к API**
3. Создайте новый токен с правами **Статистика** (раздел Statistics)
4. Скопируйте токен в `.env`:

```env
WB_API_KEY=eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9...
```

## API эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/dashboard/kpi` | KPI сводка |
| GET | `/api/dashboard/sales-dynamics` | Динамика по дням |
| GET | `/api/dashboard/abc-revenue` | ABC по выручке |
| GET | `/api/dashboard/abc-turnover` | ABC по оборачиваемости |
| GET | `/api/dashboard/reorder` | Прогноз дозаказа |
| GET | `/api/dashboard/top-skus` | Топ SKU |

Все эндпоинты принимают query-параметр `?days=30` (период анализа).

Swagger UI: **http://localhost:8000/docs**

## Структура проекта

```
.
├── backend/
│   ├── main.py           # FastAPI app, раздача статики
│   ├── config.py         # Настройки, чтение .env
│   ├── wb_client.py      # HTTP-клиент WB Statistics API
│   ├── analytics.py      # ABC-анализ, KPI, прогноз
│   ├── mock_data.py      # Генератор тестовых данных
│   ├── requirements.txt
│   └── routers/
│       └── dashboard.py  # API роутер
├── frontend/
│   ├── index.html        # Дашборд (Bootstrap 5 dark)
│   ├── app.js            # Chart.js + fetch логика
│   └── style.css
├── .env.example
└── README.md
```

## Деплой на VPS (Ubuntu + systemd)

```bash
# Установить зависимости
sudo apt install python3.11 python3.11-venv -y
cd /opt && git clone <repo> wb-analytics
cd wb-analytics/backend
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env && nano ../.env  # вставьте ключ
```

Создайте `/etc/systemd/system/wb-analytics.service`:

```ini
[Unit]
Description=WB Analytics Dashboard
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/wb-analytics/backend
EnvironmentFile=/opt/wb-analytics/.env
ExecStart=/opt/wb-analytics/backend/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now wb-analytics
# Откройте http://<IP>:8000 или настройте nginx как reverse proxy
```

## Функциональность

- **KPI карточки:** выручка, заказы, выкупы, стоимость остатков
- **Динамика продаж:** график выручки + заказов по дням
- **Топ-10 SKU:** горизонтальная гистограмма
- **ABC по выручке:** A = 80% выручки, B = 15%, C = 5%
- **ABC по оборачиваемости:** категория по покрытию (остаток / ср.продажи в день)
- **Прогноз дозаказа:** потребность на 30/60/90 дней с учётом текущих остатков
- Все таблицы сортируются по любому столбцу
- Авто-обновление каждые 5 минут
