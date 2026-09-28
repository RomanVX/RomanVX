# Аналитика МПФИТ — отдельный сервис Render

Лёгкий FastAPI-сервис: свой процесс и память, общая база `mp-postgres`
со своей схемой `mpfit`. Спека API: `docs/mpfit_api/openapi.yaml`.

## Создание сервиса в Render (один раз)
New → Web Service → этот репозиторий, ветка `main`:
- Root Directory: `mpfit_dash`
- Runtime: Python 3 · Region: Virginia (как mp-postgres)
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/health`
- Build Filters → Included Paths: `mpfit_dash/**`, `backend/db.py`, `docs/mpfit_api/**`

Env: `DATABASE_URL` (Internal URL mp-postgres), `DB_SCHEMA=mpfit`,
`DASH_USER`, `DASH_PASSWORD`, позже `MPFIT_TOKEN`.

Проверка: `/api/status` — база, схема, токен и живой вызов API МПФИТ.
