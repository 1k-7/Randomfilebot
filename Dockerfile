FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/bot.sqlite3 \
    SESSION_WORKDIR=/data

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /data \
    && chown -R botuser:botuser /data /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY random_file_bot ./random_file_bot
COPY README.md .

USER botuser

CMD ["python", "-m", "random_file_bot"]
