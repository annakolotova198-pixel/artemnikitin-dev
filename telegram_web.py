"""Gunicorn entrypoint: existing Flask site plus optional Telegram parser."""

import os

from app import app
from dzen_content_service import install_routes, start_dzen_content_worker

if os.getenv("TG_MODE", "user").strip().lower() == "bot":
    from telegram_bot_api import start_background_parser
else:
    from telegram_lead_parser import start_background_parser


start_background_parser()
install_routes(app)
start_dzen_content_worker()
