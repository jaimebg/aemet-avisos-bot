# AEMET Avisos Bot

Bot de Telegram que notifica avisos meteorológicos de la AEMET por comunidad autónoma.

## Requisitos

- Python 3.10+
- Token de bot de Telegram (obtenerlo con [@BotFather](https://t.me/BotFather))

## Instalación

```bash
pip install -r requirements.txt
cp .env.example .env
# Editar .env con tu token de Telegram
```

## Uso

```bash
python bot.py
```

### Comandos del bot

| Comando | Descripción |
|---|---|
| `/start` | Bienvenida e instrucciones |
| `/suscribir` | Elegir comunidad autónoma |
| `/desuscribir` | Eliminar suscripción |
| `/mis_avisos` | Ver suscripciones activas |
| `/ayuda` | Ayuda |

## Configuración

Variables de entorno (`.env`):

- `TELEGRAM_TOKEN` — Token del bot (obligatorio)
- `POLL_INTERVAL_SECONDS` — Intervalo de consulta RSS en segundos (default: 300)
- `DATABASE_PATH` — Ruta de la base de datos SQLite (default: `subscriptions.db`)
