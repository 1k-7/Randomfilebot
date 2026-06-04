FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/bot.sqlite3 \
    SESSION_WORKDIR=/data \
    MAX_CONCURRENT_TRANSMISSIONS=4

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /data \
    && chown -R botuser:botuser /data /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY random_file_bot ./random_file_bot
COPY README.md .

USER botuser

CMD ["python", "-m", "random_file_bot"]
