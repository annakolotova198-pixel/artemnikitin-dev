"""Telegram Bot API fallback for group construction leads.

This mode does not need api_id/api_hash. It reads new messages from groups
where the bot is a member. BotFather privacy mode must be disabled.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from telegram_lead_parser import LeadStore, csv_set, parse_message, row_to_text


LOG = logging.getLogger("telegram-bot-api-leads")


class BotApiLeadService:
    def __init__(self):
        self.bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
        self.claim_token = os.getenv("TG_OWNER_CLAIM_TOKEN", "").strip()
        self.owner_ids = {int(v) for v in csv_set("TG_OWNER_IDS") if v.lstrip("-").isdigit()}
        self.allowlist = csv_set("TG_CHAT_ALLOWLIST")
        self.blocklist = csv_set("TG_CHAT_BLOCKLIST")
        self.store = LeadStore(os.getenv("TG_DB_PATH", "telegram_leads.db"))
        stored = self.store.get_config("owner_ids")
        self.owner_ids.update(int(v) for v in stored.split(",") if v.strip().lstrip("-").isdigit())
        self.offset = 0

    def validate(self):
        missing = []
        if not self.bot_token:
            missing.append("TG_BOT_TOKEN")
        if missing:
            raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))

    def api(self, method: str, payload: dict | None = None, timeout: int = 35) -> dict:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/{method}",
            data=urllib.parse.urlencode(payload or {}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode())
        if not result.get("ok"):
            raise RuntimeError(result.get("description", f"Bot API error: {method}"))
        return result

    def send(self, chat_id: int, text: str):
        self.api(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def chat_allowed(self, chat_id: int, username: str = "") -> bool:
        keys = {str(chat_id).lower(), username.lower().lstrip("@")}
        if keys & self.blocklist:
            return False
        return not self.allowlist or bool(keys & self.allowlist)

    @staticmethod
    def display_name(user: dict) -> str:
        return " ".join(
            part for part in (user.get("first_name", ""), user.get("last_name", "")) if part
        ).strip()

    @staticmethod
    def source_link(message: dict, username: str) -> str:
        if username:
            return f"https://t.me/{username}/{message['message_id']}"
        chat_id = str(abs(int(message["chat"]["id"])))
        if chat_id.startswith("100"):
            return f"https://t.me/c/{chat_id[3:]}/{message['message_id']}"
        return ""

    def command(self, message: dict) -> bool:
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return False
        command, _, argument = text.partition(" ")
        command = command.lower().split("@")[0]
        sender_id = int(message.get("from", {}).get("id", 0))
        chat_id = int(message["chat"]["id"])

        if command == "/id":
            self.send(chat_id, f"Ваш Telegram ID: <code>{sender_id}</code>")
            return True

        if sender_id not in self.owner_ids:
            valid_claim = (
                command == "/claim"
                and self.claim_token
                and secrets.compare_digest(argument.strip(), self.claim_token)
            )
            if not valid_claim:
                return True
            self.owner_ids.add(sender_id)
            self.store.set_config("owner_ids", ",".join(str(v) for v in sorted(self.owner_ids)))
            self.send(chat_id, "✅ Аккаунт владельца привязан. Удалите сообщение с кодом.")
            return True

        if command in {"/start", "/help"}:
            response = (
                "Парсер строительных заявок работает.\n\n"
                "/status — состояние базы\n"
                "/last 10 — последние заявки\n"
                "/lead 123 — полная заявка\n"
                "/search песок — поиск по базе"
            )
        elif command == "/status":
            response = f"✅ Парсер работает\nЗаявок в базе: <b>{self.store.count()}</b>"
        elif command == "/last":
            limit = int(argument) if argument.isdigit() else 10
            rows = self.store.recent(limit=limit)
            response = "\n\n".join(row_to_text(row, compact=True) for row in rows) or "Заявок пока нет."
        elif command == "/lead" and argument.isdigit():
            row = self.store.by_id(int(argument))
            response = row_to_text(row) if row else "Заявка не найдена."
        elif command == "/search" and argument.strip():
            rows = self.store.recent(limit=15, query=argument.strip())
            response = "\n\n".join(row_to_text(row, compact=True) for row in rows) or "Совпадений нет."
        else:
            response = "Неизвестная команда. Используйте /help."
        self.send(chat_id, response)
        return True

    def message(self, message: dict):
        if self.command(message):
            return
        chat = message.get("chat", {})
        if chat.get("type") not in {"group", "supergroup", "channel"}:
            return
        chat_id = int(chat.get("id", 0))
        username = chat.get("username", "") or ""
        if not self.chat_allowed(chat_id, username):
            return
        sender = message.get("from", {}) or {}
        text = message.get("text") or message.get("caption") or ""
        lead = parse_message(
            text=text,
            chat_id=chat_id,
            message_id=int(message.get("message_id", 0)),
            chat_title=chat.get("title") or username or str(chat_id),
            sender_id=sender.get("id"),
            sender_name=self.display_name(sender),
            sender_username=sender.get("username", "") or "",
            source_link=self.source_link(message, username),
            message_date=datetime.fromtimestamp(message.get("date", time.time()), tz=timezone.utc),
        )
        if not lead:
            return
        lead_id = self.store.add(lead)
        if not lead_id:
            return
        row = self.store.by_id(lead_id)
        for owner_id in self.owner_ids:
            try:
                self.send(owner_id, row_to_text(row))
            except Exception:
                LOG.exception("Не удалось отправить заявку владельцу %s", owner_id)

    def run(self):
        self.validate()
        self.api("deleteWebhook", {"drop_pending_updates": "false"})
        LOG.info("Telegram Bot API parser started")
        while True:
            try:
                updates = self.api(
                    "getUpdates",
                    {
                        "offset": str(self.offset),
                        "timeout": "25",
                        "allowed_updates": json.dumps(["message", "channel_post"]),
                    },
                ).get("result", [])
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    message = update.get("message") or update.get("channel_post")
                    if message:
                        self.message(message)
            except (urllib.error.URLError, TimeoutError):
                LOG.warning("Telegram Bot API unavailable; retrying")
                time.sleep(3)
            except Exception:
                LOG.exception("Bot API parser loop failed")
                time.sleep(3)


_thread: threading.Thread | None = None


def start_background_parser() -> threading.Thread | None:
    global _thread
    enabled = os.getenv("TG_PARSER_ENABLED", "0").lower() in {"1", "true", "yes"}
    if not enabled:
        return None
    if _thread and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=BotApiLeadService().run, name="telegram-bot-api", daemon=True)
    _thread.start()
    return _thread

