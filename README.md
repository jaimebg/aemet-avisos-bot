<p align="center">
  <h1 align="center">🌦️ AEMET Avisos Bot</h1>
  <p align="center">
    A Telegram bot that delivers <a href="https://www.aemet.es">AEMET</a> weather alerts
    for Spain's autonomous communities straight to your chat.
  </p>
  <p align="center">
    <a href="https://github.com/jaimebg/aemet-avisos-bot/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/jaimebg/aemet-avisos-bot/actions/workflows/ci.yml/badge.svg"></a>
    <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg"></a>
    <a href="https://www.python.org/downloads/"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-blue.svg"></a>
    <a href="https://t.me/BotFather"><img alt="Built for Telegram" src="https://img.shields.io/badge/Platform-Telegram-26A5E4.svg"></a>
    <img alt="4 lightweight dependencies" src="https://img.shields.io/badge/dependencies-4-brightgreen.svg">
  </p>
</p>

---

## ✨ What is this?

AEMET Avisos Bot polls the official [AEMET RSS feeds](https://www.aemet.es/es/rss_info/avisos/), watches for new weather warnings (🟡 yellow, 🟠 orange, 🔴 red), and pushes them to every subscriber in real time. Subscribe once, get alerted forever — no apps, no websites, no noise.

## 🚀 Features

- 📡 **Live AEMET alerts** for all 19 autonomous communities and cities, fetched concurrently over async HTTP with bounded concurrency and automatic retries
- 🔔 **Push notifications** to Telegram the moment a new warning is published
- 🎚️ **Severity levels** — 🟡 yellow / 🟠 orange / 🔴 red with one-tap links to details
- 🔕 **Per-user severity filtering** — set a minimum level with `/nivel` to stop yellow (or yellow+orange) warnings from reaching you
- 🔺 **Escalation notifications** — if AEMET upgrades an alert you already received (e.g. yellow → red), you're notified again
- 🕒 **Validity windows** — an alert message shows exactly when it starts and ends, when AEMET publishes one
- 📢 **On-demand lookup** — `/avisos` shows what's active right now in your regions, without waiting for the next push
- 👥 **Per-user subscriptions** — follow as many regions as you want
- 🧠 **Smart deduplication** — stable alert IDs survive AEMET's GUID churn, so you never get duplicate notifications
- 🧹 **Self-cleaning** — subscribers who block the bot are pruned automatically, and old alert records age out on a schedule
- 🗄️ **Zero-config SQLite storage** — no database server needed, with in-place schema migrations
- ⚡ **Lightweight** — 4 dependencies, runs happily on a Raspberry Pi

## 📋 Requirements

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## ⚙️ Installation

```bash
git clone https://github.com/jaimebg/aemet-avisos-bot.git
cd aemet-avisos-bot
pip install -r requirements.txt
cp .env.example .env
# edit .env and drop in your TELEGRAM_TOKEN
```

## ▶️ Usage

```bash
python bot.py
```

Open Telegram, find your bot, and hit `/start`. Done — pick your regions with `/suscribir` and the alerts start rolling in.

## 🤖 Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and setup instructions |
| `/suscribir` | Subscribe to an autonomous community |
| `/desuscribir` | Remove a subscription |
| `/mis_avisos` | List your active subscriptions |
| `/avisos` | Show alerts currently active in your subscribed regions |
| `/nivel` | Choose your minimum severity level (mute yellow or yellow+orange warnings) |
| `/ayuda` | Show help |

## 🔧 Configuration

All settings live in `.env`:

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Your bot token from @BotFather | — (required) |
| `POLL_INTERVAL_SECONDS` | How often the RSS feeds are polled (must be at least `60`) | `300` |
| `DATABASE_PATH` | Path to the SQLite database | `subscriptions.db` |
| `SEEN_RETENTION_DAYS` | Days a delivered alert is remembered before it's pruned | `7` |
| `CLEANUP_INTERVAL_SECONDS` | Minimum seconds between two prunings of old seen alerts | `3600` |
| `HTTP_TIMEOUT_SECONDS` | Timeout in seconds for each HTTP request to AEMET | `20` |
| `HTTP_MAX_CONCURRENCY` | Maximum concurrent HTTP requests to AEMET across all regions | `8` |
| `HTTP_MAX_RETRIES` | Retries after a failed request (network error or 5xx response) | `2` |

## 🧪 Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the test suite and the linter before opening a pull request:

```bash
pytest
ruff check .
ruff format --check .
```

CI (`.github/workflows/ci.yml`) runs the same two checks on Python 3.10 through 3.13 on every push and pull request, plus a `docker build` of the image.

## 📦 Deployment

### Docker

```bash
docker build -t aemet-avisos-bot .
docker run -d --name aemet-avisos-bot \
  --env-file .env \
  -e DATABASE_PATH=/data/subscriptions.db \
  -v aemet-data:/data \
  --restart unless-stopped \
  aemet-avisos-bot
```

The image writes its database to `/data/subscriptions.db` and declares `/data` as a volume, so mount a named volume (or bind mount) there to persist it across container restarts and rebuilds. The explicit `-e DATABASE_PATH=...` is there on purpose: `.env.example` defaults `DATABASE_PATH` to the bare-metal-friendly `subscriptions.db`, and any `DATABASE_PATH` supplied via `--env-file` would otherwise override the image's default and point the bot outside the mounted volume.

### Docker Compose

```bash
cp .env.example .env   # add your TELEGRAM_TOKEN
docker compose up -d
```

`docker-compose.yml` builds the image, loads `.env` for `TELEGRAM_TOKEN` and the rest, pins `DATABASE_PATH` to `/data/subscriptions.db` (for the same reason as above — so a `DATABASE_PATH` line in `.env` can't redirect it outside the volume), persists `/data` in the `aemet-data` named volume, restarts the container unless it's explicitly stopped, and caps container logs at 10 MB × 3 files.

### systemd (Raspberry Pi / bare metal)

```bash
git clone https://github.com/jaimebg/aemet-avisos-bot.git /opt/aemet-avisos-bot
cd /opt/aemet-avisos-bot
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env and drop in your TELEGRAM_TOKEN

sudo useradd -r -s /usr/sbin/nologin aemetbot
sudo chown -R aemetbot:aemetbot /opt/aemet-avisos-bot

sudo cp deploy/aemet-avisos-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aemet-avisos-bot
```

The unit runs as the unprivileged `aemetbot` user, restarts automatically on failure, and locks down the filesystem (`ProtectSystem=strict`) with a single writable exception for `/opt/aemet-avisos-bot` — where `subscriptions.db` (the default `DATABASE_PATH`, resolved relative to `WorkingDirectory`) lives.

## 🏗️ How it works

```
AEMET RSS ──► rss_parser.py ──► dedupe ──► database.py ──► handlers.py ──► Telegram
 (per-region      (async httpx,     (stable     (SQLite:         (severity      (eligible
  RSS feeds,       bounded           alert IDs)  subscriptions,    filter,        subscribers
  poll every N     concurrency,                  seen alerts +     escalation     notified)
  seconds)         retries)                      levels, prefs)    handling)
```

1. `bot.py` schedules a polling job every `POLL_INTERVAL_SECONDS` (plus a periodic sweep that prunes old seen-alert records).
2. `rss_parser.py` discovers each region's per-zone feeds and fetches them concurrently over async HTTP — bounded concurrency, automatic retries with backoff — normalizing AEMET's ever-changing GUIDs into stable IDs and extracting each alert's severity, zone and validity window.
3. `database.py` tracks subscriptions, which alerts have already been seen (and at what severity), and each user's minimum-level preference.
4. New alerts — and alerts AEMET has escalated to a higher severity since they were first seen — are formatted (HTML-escaped, with severity emoji, region, validity window and a link) and sent only to subscribers whose `/nivel` preference allows that severity; subscribers who have blocked the bot are pruned automatically. `/avisos` runs the same fetch-and-filter pipeline on demand for a single user.

## 🤝 Contributing

Found a bug? Want a new feature? All contributions are welcome:

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/awesome-thing`)
3. Commit your changes
4. Push and open a Pull Request

Spanish speakers, English speakers, weather nerds — everyone is welcome. 🌍

## 📄 License

Released under the [MIT License](LICENSE). Free to use, modify, and share.

---

<p align="center">
  Made with ☀️🌧️⚡ for everyone who checks the sky before leaving home.
</p>
