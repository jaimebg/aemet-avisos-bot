from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

DATABASE_PATH = os.getenv("DATABASE_PATH", "subscriptions.db")

REGIONS: dict[str, str] = {
    "and": "Andalucía",
    "ara": "Aragón",
    "ast": "Asturias",
    "bal": "Illes Balears",
    "can": "Cantabria",
    "cat": "Cataluña",
    "ceu": "Ceuta",
    "clm": "Castilla-La Mancha",
    "coo": "Canarias",
    "cyl": "Castilla y León",
    "ext": "Extremadura",
    "gal": "Galicia",
    "mad": "Madrid",
    "mel": "Melilla",
    "mur": "Murcia",
    "nav": "Navarra",
    "pva": "País Vasco",
    "rio": "La Rioja",
    "val": "C. Valenciana",
}

RSS_URL_TEMPLATE = "https://www.aemet.es/es/rss_info/avisos/{code}"
