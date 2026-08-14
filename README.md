<p align="center">
  <h1 align="center">🌦️ AEMET Avisos Bot</h1>
  <p align="center">
    A Telegram bot that delivers <a href="https://www.aemet.es">AEMET</a> weather alerts
    for Spain's autonomous communities straight to your chat.
  </p>
  <p align="center">
    <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg"></a>
    <a href="https://www.python.org/downloads/"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-blue.svg"></a>
    <a href="https://t.me/BotFather"><img alt="Built for Telegram" src="https://img.shields.io/badge/Platform-Telegram-26A5E4.svg"></a>
    <img alt="3 lightweight dependencies" src="https://img.shields.io/badge/dependencies-3-brightgreen.svg">
  </p>
</p>

---

## ✨ What is this?

AEMET Avisos Bot polls the official [AEMET RSS feeds](https://www.aemet.es/es/rss_info/avisos/), watches for new weather warnings (🟡 yellow, 🟠 orange, 🔴 red), and pushes them to every subscriber in real time. Subscribe once, get alerted forever — no apps, no websites, no noise.

## 🚀 Features

- 📡 **Live AEMET alerts** for all 19 autonomous communities and cities
- 🔔 **Push notifications** to Telegram the moment a new warning is published
- 🎚️ **Severity levels** — 🟡 yellow / 🟠 orange / 🔴 red with one-tap links to details
- 👥 **Per-user subscriptions** — follow as many regions as you want
- 🧠 **Smart deduplication** — stable alert IDs survive AEMET's GUID churn, so you never get duplicate notifications
- 🗄️ **Zero-config SQLite storage** — no database server needed
- ⚡ **Lightweight** — 3 dependencies, runs happily on a Raspberry Pi

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
| `/ayuda` | Show help |

## 🔧 Configuration

All settings live in `.env`:

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Your bot token from @BotFather | — (required) |
| `POLL_INTERVAL_SECONDS` | How often the RSS feeds are polled | `300` |
| `DATABASE_PATH` | Path to the SQLite database | `subscriptions.db` |

## 🏗️ How it works

```
AEMET RSS ──► rss_parser.py ──► dedupe ──► database.py ──► handlers.py ──► Telegram
                (poll every       (stable     (SQLite         (commands &    (subscribers
                 N seconds)        alert IDs)  subscriptions)   keyboards)      notified)
```

1. `bot.py` schedules a polling job every `POLL_INTERVAL_SECONDS`.
2. `rss_parser.py` discovers each region's per-zone feeds and parses new alerts, normalizing AEMET's ever-changing GUIDs into stable IDs.
3. `database.py` tracks subscriptions and which alerts have already been seen.
4. New alerts are formatted with severity emoji, region, description, and a link — then sent to every subscriber.

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
