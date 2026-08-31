FROM python:3.12-slim

RUN useradd -m -u 10001 bot

WORKDIR /app

# Copy dependency manifest first so the pip install layer is cached across
# rebuilds that only touch the source.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
ENV DATABASE_PATH=/data/subscriptions.db

# Create the data directory and hand it to the non-root user before dropping
# privileges, so the bot can write subscriptions.db (and its WAL files) at
# runtime.
RUN mkdir -p /data && chown bot:bot /data
VOLUME ["/data"]

USER bot

CMD ["python", "bot.py"]
