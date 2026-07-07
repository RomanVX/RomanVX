# Образ Playwright: chromium и все системные зависимости уже внутри
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Render подставляет $PORT
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
