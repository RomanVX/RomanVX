# WB Analytics Dashboard

Дашборд аналитики продаж Wildberries: ABC-анализ, прогноз дозаказа, динамика выручки.

## Стек

- **Backend:** Python 3.11+ / FastAPI / pandas
- **Frontend:** HTML + Bootstrap 5 + Chart.js (CDN)

## Быстрый старт

```bash
git clone https://github.com/RomanVX/RomanVX.git
cd RomanVX
cp .env.example .env
# Вставьте WB_API_KEY в .env

cd backend
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Дашборд: **http://localhost:8000**

> Без ключа работает на mock-данных (50 SKU).

## Публичный URL через ngrok

```bash
python backend/start_tunnel.py [NGROK_AUTHTOKEN]
```

## API

| GET | Путь | Описание |
|-----|------|----------|
| | `/api/dashboard/kpi` | KPI сводка |
| | `/api/dashboard/sales-dynamics` | Динамика по дням |
| | `/api/dashboard/abc-revenue` | ABC по выручке |
| | `/api/dashboard/abc-turnover` | ABC по оборачиваемости |
| | `/api/dashboard/reorder` | Прогноз дозаказа |
| | `/api/dashboard/top-skus` | Топ SKU |

Swagger UI: **http://localhost:8000/docs**

## Структура

```
.
├── backend/
│   ├── main.py
│   ├── config.py
│   ├── wb_client.py
│   ├── analytics.py
│   ├── mock_data.py
│   ├── requirements.txt
│   ├── start_tunnel.py
│   └── routers/dashboard.py
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── .env.example
└── README.md
```
