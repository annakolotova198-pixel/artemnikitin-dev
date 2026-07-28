"""Read-only Telegram construction lead parser.

The user client reads groups/channels available to the owner's Telegram
account. A separate bot client delivers results and accepts commands.
Secrets are supplied only through environment variables.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    from telethon.utils import get_display_name
except ImportError:  # Allows the extraction logic to be tested before dependencies are installed.
    TelegramClient = None
    events = None
    StringSession = None
    get_display_name = None


LOG = logging.getLogger("telegram-leads")

PRODUCT_PATTERNS = {
    "Песок": (r"\bпес(?:ок|ка|ком)\b", r"\bпескогрунт\b"),
    "Щебень": (r"\bщеб(?:ень|ня|нем)\b", r"\bщеб[её]нк"),
    "Вторичный щебень": (
        r"\bвторичн\w*\s+щеб",
        r"\bрецикл\w*\s+щеб",
        r"\bщеб\w*\s+вторичн",
        r"\bщеб\w*\s+рецикл",
    ),
    "Керамзит": (r"\bкерамзит\w*\b", r"\bкермазит\w*\b"),
    "ПГС / ГПС": (r"\b[пг]г?с\b", r"песчано[- ]гравийн", r"гравийно[- ]песчан"),
    "ЩПС": (r"\bщпс\b", r"щеб[её]но[- ]песчан"),
    "Отсев": (r"\bотсев\w*\b",),
    "Грунт": (r"\bгрунт\w*\b", r"\bпочвогрунт\w*\b"),
    "Чернозём": (r"\bчерноз[её]м\w*\b",),
    "Торф": (r"\bторф\w*\b",),
    "Глина": (r"\bглин(?:а|ы|у|ой)\b",),
    "Асфальтовая крошка": (r"\bасфальт\w*\s+крошк",),
    "Бетон": (r"\bбетон\w*\b", r"\bбст\b"),
    "Раствор": (r"\bраствор\w*\b",),
    "Цемент": (r"\bцемент\w*\b",),
    "ЖБИ": (r"\bжби\b", r"железобетон"),
    "ФБС": (r"\bфбс\b", r"фундаментн\w*\s+блок"),
    "Дорожные плиты": (r"\b(?:плита|плиты)\s+(?:дорожн|2п|1п|пдн)", r"\bпдн\b"),
    "Кольца ЖБИ": (r"\bкольц\w*\s+(?:жби|кс)\b", r"\bкс[- ]?\d"),
    "Лотки": (r"\bлот(?:ок|ки|ков)\b",),
    "Бордюр": (r"\bбордюр\w*\b", r"\bбортов\w*\s+кам"),
    "Кирпич": (r"\bкирпич\w*\b",),
    "Газоблок": (r"\bгазоблок\w*\b", r"газобетонн\w*\s+блок"),
    "Арматура": (r"\bарматур\w*\b",),
    "Металлопрокат": (r"\bметаллопрокат\w*\b", r"\bшвеллер\w*\b", r"\bдвутавр\w*\b"),
    "Грузоперевозки": (
        r"\bгрузоперевоз\w*\b",
        r"\bшаланд\w*\b",
        r"\bсамосвал\w*\b",
        r"\bманипулятор\w*\b",
    ),
    "Спецтехника": (
        r"\bэкскаватор\w*\b",
        r"\bпогрузчик\w*\b",
        r"\bавтокран\w*\b",
        r"\bбульдозер\w*\b",
    ),
}

REQUEST_PATTERNS = (
    r"\bнуж(?:ен|на|но|ны)\b",
    r"\bтребу(?:ется|ются)\b",
    r"\bищ(?:у|ем|ут)\b",
    r"\bкуп(?:лю|им|аем)\b",
    r"\bзаявк\w*\b",
    r"\bкто\s+(?:может|возит|поставит|продаст)\b",
    r"\bподскажите\b",
    r"\bрассчитайте\b",
    r"\bдайте\s+(?:цену|сч[её]т|кп)\b",
)

OFFER_PATTERNS = (
    r"\bпрода(?:м|ем|ётся|ется)\b",
    r"\bпредлага(?:ю|ем)\b",
    r"\bв\s+наличии\b",
    r"\bсобственное\s+производство\b",
    r"\bоказываем\s+услуги\b",
)

QUANTITY_RE = re.compile(
    r"(?P<number>\d+(?:[.,]\d+)?(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?)\s*"
    r"(?P<unit>м\s*[³3]|куб(?:а|ов)?|тонн?(?:а|ы)?|тн?\.?|кг|шт\.?|штук(?:а|и)?|ед\.?|"
    r"рейс(?:а|ов)?|машин(?:а|ы)?|мешк(?:а|ов)?|палл(?:ет|еты|ет)|поддон(?:а|ов)?|"
    r"комплект(?:а|ов)?|пог\.?\s*м|л(?:итр(?:а|ов)?)?)",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}\b", re.IGNORECASE)
USERNAME_RE = re.compile(r"(?<![\w@])@[a-zA-Z][a-zA-Z0-9_]{4,31}\b")
ADDRESS_HINT_RE = re.compile(
    r"(?:адрес|доставк\w*(?:\s+(?:на|в|до))?|объект|обьект|точка|выгрузк\w*)\s*[:\-]?\s*(.+)",
    re.IGNORECASE,
)
LOCATION_WORDS_RE = re.compile(
    r"\b(?:москва|московск\w*\s+обл|мо\b|район|округ|шоссе|улица|ул\.|"
    r"проспект|пр-т|проезд|деревня|д\.|пос[её]лок|п\.|город|г\.)\b",
    re.IGNORECASE,
)


@dataclass
class ParsedLead:
    fingerprint: str
    chat_id: int
    message_id: int
    chat_title: str
    sender_id: int | None
    sender_name: str
    sender_username: str
    products: list[str]
    quantities: list[dict[str, str]]
    phones: list[str]
    emails: list[str]
    telegram_contacts: list[str]
    address: str
    source_link: str
    media_type: str
    media_name: str
    media_mime: str
    media_size: int
    forwarded_from: str
    reply_to_message_id: int | None
    message_text: str
    message_date: str
    created_at: str


def env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def csv_set(name: str) -> set[str]:
    return {part.strip().lower() for part in os.getenv(name, "").split(",") if part.strip()}


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits if len(digits) == 11 and digits.startswith("7") else value.strip()


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def extract_products(text: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    lowered = text.lower()
    for product, patterns in PRODUCT_PATTERNS.items():
        positions = [match.start() for pattern in patterns if (match := re.search(pattern, lowered, re.IGNORECASE))]
        if positions:
            matches.append((min(positions), product))
    matches.sort()
    products = [name for _, name in matches]
    if "Вторичный щебень" in products and "Щебень" in products:
        products.remove("Щебень")
    return products


def extract_quantities(text: str) -> list[dict[str, str]]:
    result = []
    for match in QUANTITY_RE.finditer(text):
        # Do not confuse price abbreviations such as "23 т. руб."
        # with tonnes requested for delivery.
        unit_raw = re.sub(r"\s+", "", match.group("unit").lower())
        tail = text[match.end() : match.end() + 12]
        if unit_raw in {"т", "т.", "тн", "тн."} and re.match(
            r"\s*(?:руб(?:\.|лей|ля)?|р\b)", tail, re.IGNORECASE
        ):
            continue
        number = match.group("number").replace(",", ".")
        unit = unit_raw
        unit_map = {
            "м3": "м³",
            "м³": "м³",
            "куб": "м³",
            "куба": "м³",
            "кубов": "м³",
            "т": "т",
        }
        result.append({"value": number, "unit": unit_map.get(unit, unit)})
    return result


def extract_address(text: str) -> str:
    for line in (part.strip(" \t—-:;,.") for part in text.splitlines()):
        if not line:
            continue
        hinted = ADDRESS_HINT_RE.search(line)
        if hinted:
            address = re.split(
                r"\s*(?:[.;,]\s*)?(?:тел(?:ефон)?\.?|контакт|звонить)\s*[:\-]?",
                hinted.group(1),
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            return address.strip(" \t—-:;,.")[:300]
    for line in (part.strip() for part in text.splitlines()):
        if LOCATION_WORDS_RE.search(line):
            return line[:300]
    return ""


def is_probable_request(text: str, products: list[str], quantities: list[dict[str, str]]) -> bool:
    if not products:
        return False
    request_score = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in REQUEST_PATTERNS)
    offer_score = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in OFFER_PATTERNS)
    if offer_score and not request_score:
        return False
    return request_score > 0 or bool(quantities)


def parse_message(
    *,
    text: str,
    chat_id: int,
    message_id: int,
    chat_title: str,
    sender_id: int | None = None,
    sender_name: str = "",
    sender_username: str = "",
    source_link: str = "",
    media_type: str = "",
    media_name: str = "",
    media_mime: str = "",
    media_size: int = 0,
    forwarded_from: str = "",
    reply_to_message_id: int | None = None,
    message_date: datetime | None = None,
) -> ParsedLead | None:
    clean_text = re.sub(r"\r\n?", "\n", text or "").strip()
    products = extract_products(clean_text)
    quantities = extract_quantities(clean_text)
    if not is_probable_request(clean_text, products, quantities):
        return None

    phones = unique(normalize_phone(match.group(0)) for match in PHONE_RE.finditer(clean_text))
    emails = unique(match.group(0).lower() for match in EMAIL_RE.finditer(clean_text))
    telegram_contacts = unique(match.group(0) for match in USERNAME_RE.finditer(clean_text))

    fingerprint = hashlib.sha256(f"{chat_id}:{message_id}".encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    date = (message_date or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return ParsedLead(
        fingerprint=fingerprint,
        chat_id=chat_id,
        message_id=message_id,
        chat_title=chat_title,
        sender_id=sender_id,
        sender_name=sender_name,
        sender_username=sender_username,
        products=products,
        quantities=quantities,
        phones=phones,
        emails=emails,
        telegram_contacts=telegram_contacts,
        address=extract_address(clean_text),
        source_link=source_link,
        media_type=media_type,
        media_name=media_name,
        media_mime=media_mime,
        media_size=media_size,
        forwarded_from=forwarded_from,
        reply_to_message_id=reply_to_message_id,
        message_text=clean_text[:6000],
        message_date=date,
        created_at=now,
    )


class LeadStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_title TEXT NOT NULL,
                    sender_id INTEGER,
                    sender_name TEXT,
                    sender_username TEXT,
                    products_json TEXT NOT NULL,
                    quantities_json TEXT NOT NULL,
                    phones_json TEXT NOT NULL,
                    emails_json TEXT NOT NULL,
                    telegram_contacts_json TEXT NOT NULL,
                    address TEXT,
                    source_link TEXT,
                    media_type TEXT,
                    media_name TEXT,
                    media_mime TEXT,
                    media_size INTEGER NOT NULL DEFAULT 0,
                    forwarded_from TEXT,
                    reply_to_message_id INTEGER,
                    message_text TEXT NOT NULL,
                    message_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(telegram_leads)").fetchall()
            }
            migrations = {
                "media_type": "TEXT",
                "media_name": "TEXT",
                "media_mime": "TEXT",
                "media_size": "INTEGER NOT NULL DEFAULT 0",
                "forwarded_from": "TEXT",
                "reply_to_message_id": "INTEGER",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE telegram_leads ADD COLUMN {column} {definition}"
                    )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tg_leads_date ON telegram_leads(message_date DESC)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_bot_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def add(self, lead: ParsedLead) -> int | None:
        data = asdict(lead)
        for key in ("products", "quantities", "phones", "emails", "telegram_contacts"):
            data[f"{key}_json"] = json.dumps(data.pop(key), ensure_ascii=False)
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    f"INSERT INTO telegram_leads ({columns}) VALUES ({placeholders})",
                    tuple(data.values()),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM telegram_leads").fetchone()[0])

    def delete_by_sender(self, sender_id: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM telegram_leads
                WHERE sender_id=?
                   OR message_text LIKE '🧱 Заявка #%'
                   OR message_text LIKE 'Заявка #%'
                """,
                (sender_id,),
            )
            return int(cursor.rowcount)

    def recent(self, limit: int = 10, query: str = "") -> list[sqlite3.Row]:
        limit = max(1, min(limit, 30))
        with self.connect() as connection:
            if query:
                return list(
                    connection.execute(
                        """
                        SELECT * FROM telegram_leads
                        WHERE message_text LIKE ? OR products_json LIKE ? OR address LIKE ?
                        ORDER BY message_date DESC LIMIT ?
                        """,
                        (f"%{query}%", f"%{query}%", f"%{query}%", limit),
                    )
                )
            return list(connection.execute("SELECT * FROM telegram_leads ORDER BY message_date DESC LIMIT ?", (limit,)))

    def by_id(self, lead_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM telegram_leads WHERE id=?", (lead_id,)).fetchone()

    def get_config(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM telegram_bot_config WHERE key=?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else default

    def set_config(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_bot_config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )


def row_to_text(row: sqlite3.Row, compact: bool = False) -> str:
    products = ", ".join(json.loads(row["products_json"])) or "не определено"
    quantities = ", ".join(
        f'{item["value"]} {item["unit"]}' for item in json.loads(row["quantities_json"])
    ) or "не указано"
    contacts_from_text = unique(
        json.loads(row["phones_json"])
        + json.loads(row["emails_json"])
        + json.loads(row["telegram_contacts_json"])
    )
    message_date = datetime.fromisoformat(row["message_date"]).astimezone(
        ZoneInfo("Europe/Moscow")
    ).strftime("%d.%m.%Y %H:%M")
    sender_name = row["sender_name"] or "Имя не указано"
    sender_username = (row["sender_username"] or "").lstrip("@")
    sender_id = row["sender_id"]
    if sender_username:
        sender = (
            f'<a href="https://t.me/{html.escape(sender_username, quote=True)}">'
            f'{html.escape(sender_name)}</a> (@{html.escape(sender_username)})'
        )
        sender_contact = (
            f'<a href="https://t.me/{html.escape(sender_username, quote=True)}">'
            f'Написать @{html.escape(sender_username)}</a>'
        )
    elif sender_id:
        sender = (
            f'<a href="tg://user?id={int(sender_id)}">{html.escape(sender_name)}</a> '
            f"(ID {int(sender_id)})"
        )
        sender_contact = (
            f'<a href="tg://user?id={int(sender_id)}">Открыть профиль автора</a> '
            "(публичный @username не установлен)"
        )
    else:
        sender = html.escape(sender_name)
        sender_contact = "профиль автора недоступен для этого типа публикации"
    lines = [
        f'🧱 <b>Заявка #{row["id"]}</b>',
        f"<b>Дата заявки:</b> {message_date} МСК",
        f"<b>Товар:</b> {html.escape(products)}",
        f"<b>Объём:</b> {html.escape(quantities)}",
    ]
    if not compact:
        lines.extend(
            [
                f'<b>Адрес:</b> {html.escape(row["address"] or "не указан")}',
                f"<b>Автор:</b> {sender}",
                f"<b>Контакт автора:</b> {sender_contact}",
                f'<b>ID автора:</b> {html.escape(str(sender_id or "не указан"))}',
                f'<b>Контакты из текста:</b> {html.escape(", ".join(contacts_from_text) or "не указаны")}',
                f'<b>Чат:</b> {html.escape(row["chat_title"])}',
                f'<b>ID чата / сообщения:</b> {row["chat_id"]} / {row["message_id"]}',
                f'<b>Текст:</b> {html.escape(row["message_text"][:1200])}',
            ]
        )
        if row["forwarded_from"]:
            lines.append(f'<b>Переслано от:</b> {html.escape(row["forwarded_from"])}')
        if row["reply_to_message_id"]:
            lines.append(f'<b>Ответ на сообщение:</b> {row["reply_to_message_id"]}')
        if row["media_type"]:
            media = row["media_type"]
            if row["media_name"]:
                media += f' — {row["media_name"]}'
            if row["media_mime"]:
                media += f' ({row["media_mime"]})'
            if row["media_size"]:
                media += f', {row["media_size"] / 1024 / 1024:.1f} МБ'
            lines.append(f'<b>Вложение:</b> {html.escape(media)}')
        if row["source_link"]:
            lines.append(
                f'<a href="{html.escape(row["source_link"], quote=True)}">'
                "Открыть исходное сообщение</a>"
            )
    return "\n".join(lines)


class TelegramLeadService:
    def __init__(self):
        if TelegramClient is None:
            raise RuntimeError("Telethon не установлен. Выполните установку зависимостей из requirements.txt")
        self.api_id = env_int("TG_API_ID")
        self.api_hash = os.getenv("TG_API_HASH", "").strip()
        self.user_session = os.getenv("TG_USER_SESSION", "").strip()
        self.bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
        self.owner_ids = {int(value) for value in csv_set("TG_OWNER_IDS") if value.lstrip("-").isdigit()}
        self.claim_token = os.getenv("TG_OWNER_CLAIM_TOKEN", "").strip()
        self.allowlist = csv_set("TG_CHAT_ALLOWLIST")
        self.blocklist = csv_set("TG_CHAT_BLOCKLIST")
        self.history_limit = env_int("TG_HISTORY_LIMIT", 100)
        self.backfill = os.getenv("TG_BACKFILL", "1").strip().lower() not in {"0", "false", "no"}
        self.store = LeadStore(os.getenv("TG_DB_PATH", "telegram_leads.db"))
        stored_owner_ids = self.store.get_config("owner_ids")
        self.owner_ids.update(
            int(value) for value in stored_owner_ids.split(",") if value.strip().lstrip("-").isdigit()
        )
        self.user = TelegramClient(StringSession(self.user_session), self.api_id, self.api_hash)
        self.bot = TelegramClient("telegram_lead_bot", self.api_id, self.api_hash)

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("TG_API_ID", self.api_id),
                ("TG_API_HASH", self.api_hash),
                ("TG_USER_SESSION", self.user_session),
                ("TG_BOT_TOKEN", self.bot_token),
            )
            if not value
        ]
        if not self.owner_ids and not self.claim_token:
            missing.append("TG_OWNER_IDS или TG_OWNER_CLAIM_TOKEN")
        if missing:
            raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))

    def chat_allowed(self, chat_id: int, username: str = "") -> bool:
        keys = {str(chat_id).lower(), username.lower().lstrip("@")}
        if keys & self.blocklist:
            return False
        return not self.allowlist or bool(keys & self.allowlist)

    async def source_link(self, event, username: str) -> str:
        if username:
            return f"https://t.me/{username}/{event.id}"
        chat_id = str(abs(event.chat_id or 0))
        if chat_id.startswith("100"):
            chat_id = chat_id[3:]
            return f"https://t.me/c/{chat_id}/{event.id}"
        return f"tg://openmessage?chat_id={chat_id}&message_id={event.id}"

    @staticmethod
    def media_metadata(event) -> tuple[str, str, str, int]:
        file = getattr(event, "file", None)
        media_type = ""
        if getattr(event, "photo", None):
            media_type = "Фото"
        elif getattr(event, "video", None):
            media_type = "Видео"
        elif getattr(event, "voice", None):
            media_type = "Голосовое сообщение"
        elif getattr(event, "document", None):
            media_type = "Документ"
        elif getattr(event, "media", None):
            media_type = type(event.media).__name__
        return (
            media_type,
            getattr(file, "name", "") or "",
            getattr(file, "mime_type", "") or "",
            int(getattr(file, "size", 0) or 0),
        )

    @staticmethod
    def forwarded_from(event) -> str:
        forwarded = getattr(event, "fwd_from", None)
        if not forwarded:
            return ""
        if getattr(forwarded, "from_name", None):
            return str(forwarded.from_name)
        if getattr(forwarded, "from_id", None):
            return str(forwarded.from_id)
        return ""

    async def send_media(self, owner_id: int, event, lead_id: int) -> None:
        media_type, media_name, media_mime, media_size = self.media_metadata(event)
        if not media_type or media_size > 15 * 1024 * 1024:
            return
        buffer = io.BytesIO()
        buffer.name = media_name or (
            "photo.jpg" if media_type == "Фото" else f"attachment-{lead_id}"
        )
        target = getattr(event, "message", event)
        await self.user.download_media(target, file=buffer)
        buffer.seek(0)
        await self.bot.send_file(
            owner_id,
            buffer,
            caption=f"Вложение к заявке #{lead_id}: {media_type}",
        )

    async def send_stored_media(self, owner_id: int, row: sqlite3.Row) -> None:
        """Fetch a historical attachment from its source message and send it."""
        if not row["media_type"] or int(row["media_size"] or 0) > 15 * 1024 * 1024:
            return
        try:
            message = await self.user.get_messages(
                int(row["chat_id"]), ids=int(row["message_id"])
            )
            if message:
                await self.send_media(owner_id, message, int(row["id"]))
        except Exception:
            LOG.exception("Не удалось получить вложение для заявки %s", row["id"])

    async def send_full_rows(self, event, rows: Iterable[sqlite3.Row]) -> None:
        """Send each lead as a complete card so Telegram does not truncate it."""
        sent = False
        for row in rows:
            sent = True
            await event.respond(
                row_to_text(row),
                parse_mode="html",
                link_preview=False,
            )
            await self.send_stored_media(int(event.sender_id), row)
        if not sent:
            await event.respond("Заявок пока нет.")

    async def process_event(self, event, notify: bool = True) -> int | None:
        if getattr(event, "is_private", False) or not event.raw_text:
            return None
        chat = await event.get_chat()
        username = getattr(chat, "username", "") or ""
        if not self.chat_allowed(event.chat_id, username):
            return None
        sender = await event.get_sender()
        sender_id = getattr(sender, "id", None)
        if getattr(sender, "bot", False) or sender_id in self.owner_ids:
            return None
        media_type, media_name, media_mime, media_size = self.media_metadata(event)
        lead = parse_message(
            text=event.raw_text,
            chat_id=event.chat_id,
            message_id=event.id,
            chat_title=get_display_name(chat) or username or str(event.chat_id),
            sender_id=sender_id,
            sender_name=get_display_name(sender) if sender else "",
            sender_username=getattr(sender, "username", "") or "",
            source_link=await self.source_link(event, username),
            media_type=media_type,
            media_name=media_name,
            media_mime=media_mime,
            media_size=media_size,
            forwarded_from=self.forwarded_from(event),
            reply_to_message_id=getattr(event, "reply_to_msg_id", None),
            message_date=event.date,
        )
        if not lead:
            return None
        lead_id = self.store.add(lead)
        if lead_id and notify:
            row = self.store.by_id(lead_id)
            for owner_id in self.owner_ids:
                try:
                    await self.bot.send_message(
                        owner_id,
                        row_to_text(row),
                        parse_mode="html",
                        link_preview=False,
                    )
                    await self.send_media(owner_id, event, lead_id)
                except Exception:
                    LOG.exception("Не удалось отправить заявку владельцу %s", owner_id)
        return lead_id

    async def scan_history(self) -> None:
        if not self.backfill or self.history_limit <= 0:
            return
        scanned_chats = 0
        async for dialog in self.user.iter_dialogs():
            entity = dialog.entity
            if not (dialog.is_group or dialog.is_channel):
                continue
            username = getattr(entity, "username", "") or ""
            if not self.chat_allowed(dialog.id, username):
                continue
            scanned_chats += 1
            LOG.info("История: %s", dialog.name)
            async for message in self.user.iter_messages(entity, limit=self.history_limit, reverse=True):
                if message.message:
                    await self.process_event(message, notify=False)
        LOG.info("История обработана: %s чатов, всего заявок %s", scanned_chats, self.store.count())

    async def bot_command(self, event) -> None:
        text = (event.raw_text or "").strip()
        command, _, argument = text.partition(" ")
        command = command.lower().split("@")[0]
        if event.sender_id not in self.owner_ids:
            valid_claim = (
                command == "/claim"
                and self.claim_token
                and secrets.compare_digest(argument.strip(), self.claim_token)
            )
            if not valid_claim:
                return
            self.owner_ids.add(int(event.sender_id))
            self.store.set_config("owner_ids", ",".join(str(value) for value in sorted(self.owner_ids)))
            await event.respond(
                "✅ Аккаунт владельца привязан. Код больше не показывайте и удалите сообщение с ним.",
                parse_mode="html",
            )
            return
        if command in {"/start", "/help"}:
            response = (
                "Парсер строительных заявок работает.\n\n"
                "/status — состояние базы\n"
                "/last 10 — последние заявки\n"
                "/lead 123 — полная заявка\n"
                "/search песок — поиск по товару, адресу или тексту"
            )
        elif command == "/status":
            response = f"✅ Парсер работает\nЗаявок в базе: <b>{self.store.count()}</b>"
        elif command == "/last":
            limit = min(max(int(argument), 1), 20) if argument.isdigit() else 10
            rows = self.store.recent(limit=limit)
            await self.send_full_rows(event, rows)
            return
        elif command == "/lead" and argument.isdigit():
            row = self.store.by_id(int(argument))
            if not row:
                response = "Заявка не найдена."
            else:
                await self.send_full_rows(event, [row])
                return
        elif command == "/search" and argument.strip():
            rows = self.store.recent(limit=15, query=argument.strip())
            if not rows:
                response = "Совпадений нет."
            else:
                await self.send_full_rows(event, rows)
                return
        else:
            response = "Неизвестная команда. Используйте /help."
        await event.respond(response[:4000], parse_mode="html", link_preview=False)

    async def run(self) -> None:
        self.validate()
        await self.user.start()
        await self.bot.start(bot_token=self.bot_token)
        bot_me = await self.bot.get_me()
        removed = self.store.delete_by_sender(int(bot_me.id))
        for owner_id in self.owner_ids:
            removed += self.store.delete_by_sender(int(owner_id))
        if removed:
            LOG.info("Удалено ошибочных заявок от бота и владельцев: %s", removed)
        self.user.add_event_handler(self.process_event, events.NewMessage(incoming=True))
        self.bot.add_event_handler(self.bot_command, events.NewMessage(incoming=True))
        LOG.info("Telegram-парсер запущен")
        history_task = asyncio.create_task(self.scan_history(), name="telegram-history-scan")
        try:
            await asyncio.gather(
                self.user.run_until_disconnected(),
                self.bot.run_until_disconnected(),
            )
        finally:
            history_task.cancel()


class BotApiLeadService:
    """Bot-only fallback that works without a Telegram API application.

    It receives new messages from groups where the bot is a member. Telegram
    privacy mode must be disabled in BotFather for ordinary group messages.
    """

    def __init__(self):
        self.bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
        self.owner_ids = {int(value) for value in csv_set("TG_OWNER_IDS") if value.lstrip("-").isdigit()}
        self.claim_token = os.getenv("TG_OWNER_CLAIM_TOKEN", "").strip()
        self.allowlist = csv_set("TG_CHAT_ALLOWLIST")
        self.blocklist = csv_set("TG_CHAT_BLOCKLIST")
        self.store = LeadStore(os.getenv("TG_DB_PATH", "telegram_leads.db"))
        stored_owner_ids = self.store.get_config("owner_ids")
        self.owner_ids.update(
            int(value) for value in stored_owner_ids.split(",") if value.strip().lstrip("-").isdigit()
        )
        self.offset = 0

    def validate(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("TG_BOT_TOKEN")
        if not self.owner_ids and not self.claim_token:
            missing.append("TG_OWNER_IDS или TG_OWNER_CLAIM_TOKEN")
        if missing:
            raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))

    def api(self, method: str, payload: dict | None = None, timeout: int = 35) -> dict:
        data = urllib.parse.urlencode(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/{method}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Bot API {method}: {result.get('description', 'unknown error')}")
        return result

    def send(self, chat_id: int, text: str) -> None:
        self.api(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def send_full_rows(self, chat_id: int, rows: Iterable[sqlite3.Row]) -> None:
        sent = False
        for row in rows:
            sent = True
            self.send(chat_id, row_to_text(row))
        if not sent:
            self.send(chat_id, "Заявок пока нет.")

    def chat_allowed(self, chat_id: int, username: str = "") -> bool:
        keys = {str(chat_id).lower(), username.lower().lstrip("@")}
        if keys & self.blocklist:
            return False
        return not self.allowlist or bool(keys & self.allowlist)

    @staticmethod
    def display_name(user: dict) -> str:
        return " ".join(part for part in (user.get("first_name", ""), user.get("last_name", "")) if part).strip()

    def source_link(self, message: dict, username: str) -> str:
        if username:
            return f"https://t.me/{username}/{message['message_id']}"
        chat_id = str(abs(int(message["chat"]["id"])))
        if chat_id.startswith("100"):
            return f"https://t.me/c/{chat_id[3:]}/{message['message_id']}"
        return ""

    def handle_command(self, message: dict) -> bool:
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return False
        command, _, argument = text.partition(" ")
        command = command.lower().split("@")[0]
        sender_id = int(message.get("from", {}).get("id", 0))
        chat_id = int(message["chat"]["id"])
        if sender_id not in self.owner_ids:
            valid_claim = (
                command == "/claim"
                and self.claim_token
                and secrets.compare_digest(argument.strip(), self.claim_token)
            )
            if not valid_claim:
                return True
            self.owner_ids.add(sender_id)
            self.store.set_config("owner_ids", ",".join(str(value) for value in sorted(self.owner_ids)))
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
            limit = min(max(int(argument), 1), 20) if argument.isdigit() else 10
            rows = self.store.recent(limit=limit)
            self.send_full_rows(chat_id, rows)
            return True
        elif command == "/lead" and argument.isdigit():
            row = self.store.by_id(int(argument))
            response = row_to_text(row) if row else "Заявка не найдена."
        elif command == "/search" and argument.strip():
            rows = self.store.recent(limit=15, query=argument.strip())
            if not rows:
                response = "Совпадений нет."
            else:
                self.send_full_rows(chat_id, rows)
                return True
        else:
            response = "Неизвестная команда. Используйте /help."
        self.send(chat_id, response)
        return True

    def handle_message(self, message: dict) -> None:
        if self.handle_command(message):
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

    def run(self) -> None:
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
                    timeout=35,
                ).get("result", [])
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    message = update.get("message") or update.get("channel_post")
                    if message:
                        self.handle_message(message)
            except (urllib.error.URLError, TimeoutError):
                LOG.warning("Telegram Bot API unavailable; retrying")
                time.sleep(3)
            except Exception:
                LOG.exception("Bot API parser loop failed")
                time.sleep(3)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mode = os.getenv("TG_MODE", "user").strip().lower()
    if mode == "bot":
        BotApiLeadService().run()
    else:
        asyncio.run(TelegramLeadService().run())


_background_thread: threading.Thread | None = None


def start_background_parser() -> threading.Thread | None:
    """Starts one parser thread when TG_PARSER_ENABLED is explicitly enabled."""
    global _background_thread
    enabled = os.getenv("TG_PARSER_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        return None
    if _background_thread and _background_thread.is_alive():
        return _background_thread
    _background_thread = threading.Thread(
        target=main,
        name="telegram-lead-parser",
        daemon=True,
    )
    _background_thread.start()
    return _background_thread


if __name__ == "__main__":
    main()
