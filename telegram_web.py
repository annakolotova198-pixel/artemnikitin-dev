"""Gunicorn entrypoint: existing Flask site plus optional Telegram parser."""

from app import app
from telegram_lead_parser import start_background_parser


start_background_parser()
