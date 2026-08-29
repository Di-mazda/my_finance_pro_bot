FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# Xvfb и шрифты для headful-режима
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Браузеры уже есть в базовом образе, но на всякий случай досинхронизируем версию
RUN python -m playwright install chromium

COPY . .

CMD ["python", "main.py"]