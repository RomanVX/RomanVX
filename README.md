# WB Analytics Dashboard

Дашборд аналитики продаж Wildberries: ABC-анализ, прогноз дозаказа, динамика выручки.

## Стек

- **Backend:** Python 3.11+ / FastAPI / pandas
- **Frontend:** HTML + Bootstrap 5 + Chart.js (CDN, без сборки)

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
