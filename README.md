# WB Analytics Dashboard

Дашборд аналитики продаж Wildberries: ABC-анализ, прогноз дозаказа, динамика выручки.

## Стек

- **Backend:** Python 3.11+ / FastAPI / pandas
- **Frontend:** HTML + Bootstrap 5 + Chart.js (CDN, без сборки)

## Быстрый старт

```bash
git clone https://github.com/RomanVX/wb-dashboard
cd wb-dashboard/backend
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
# Открыть http://localhost:8000
```

> Без `.env` работает на mock-данных (50 SKU, 30 дней).

## Публичный URL через ngrok

```bash
cd backend
pip install -r requirements.txt
python start_tunnel.py                        # без аутентификации (5 запросов/мин)
python start_tunnel.py YOUR_NGROK_TOKEN      # с токеном без лимитов
```

Токен: https://dashboard.ngrok.com/get-started/your-authtoken

## API ключ Wildberries

1. [WB Seller](https://seller.wildberries.ru/) → Настройки → Доступ к API
2. Создать токен с правами **Статистика**
3. Создать `.env`:

```env
WB_API_KEY=eyJhbGci...
```

## API эндпоинты

| GET | `/api/dashboard/kpi` | KPI сводка |
| GET | `/api/dashboard/sales-dynamics` | Динамика по дням |
| GET | `/api/dashboard/abc-revenue` | ABC по выручке |
| GET | `/api/dashboard/abc-turnover` | ABC по оборачиваемости |
| GET | `/api/dashboard/reorder` | Прогноз дозаказа |
| GET | `/api/dashboard/top-skus` | Топ SKU |

Swagger UI: **http://localhost:8000/docs**

## Структура

```
backend/
  main.py, config.py, wb_client.py, analytics.py, mock_data.py
  start_tunnel.py
  requirements.txt
  routers/dashboard.py
frontend/
  index.html, app.js, style.css
.env.example
```

## Деплой (systemd)

```ini
[Service]
WorkingDirectory=/opt/wb-analytics/backend
ExecStart=/opt/wb-analytics/backend/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
```
