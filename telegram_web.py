"""Gunicorn entrypoint: existing Flask site plus optional Telegram parser."""

import os

from app import app

if os.getenv("TG_MODE", "user").strip().lower() == "bot":
    from telegram_bot_api import start_background_parser
else:
    from telegram_lead_parser import start_background_parser


start_background_parser()

