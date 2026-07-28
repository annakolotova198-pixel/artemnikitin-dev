"""Read-only Telegram construction lead parser.

The user client reads groups/channels available to the owner's Telegram
account. A separate bot client delivers results and accepts commands.
Secrets are supplied only through environment variables.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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
    r"(?P<number>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>м\s*[³3]|куб(?:а|ов)?|тонн?(?:а|ы)?|т\b|шт\.?|штук(?:а|и)?|"
    r"рейс(?:а|ов)?|машин(?:а|ы)?|мешк(?:а|ов)?|палл(?:ет|еты|ет)|пог\.?\s*м)",
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
        number = match.group("number").replace(",", ".")
        unit = re.sub(r"\s+", "", match.group("unit").lower())
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
    if sender_username:
        telegram_contacts = unique([f"@{sender_username.lstrip('@')}"] + telegram_contacts)

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
                    message_text TEXT NOT NULL,
                    message_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tg_leads_date ON telegram_leads(message_date DESC)")

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


def row_to_text(row: sqlite3.Row, compact: bool = False) -> str:
    products = ", ".join(json.loads(row["products_json"])) or "не определено"
    quantities = ", ".join(
        f'{item["value"]} {item["unit"]}' for item in json.loads(row["quantities_json"])
    ) or "не указано"
    contacts = unique(
        json.loads(row["phones_json"])
        + json.loads(row["emails_json"])
        + json.loads(row["telegram_contacts_json"])
    )
    lines = [
        f'🧱 <b>Заявка #{row["id"]}</b>',
        f"<b>Товар:</b> {html.escape(products)}",
        f"<b>Объём:</b> {html.escape(quantities)}",
    ]
    if not compact:
        lines.extend(
            [
                f'<b>Адрес:</b> {html.escape(row["address"] or "не указан")}',
                f'<b>Контакт:</b> {html.escape(", ".join(contacts) or row["sender_name"] or "не указан")}',
                f'<b>Чат:</b> {html.escape(row["chat_title"])}',
                f'<b>Текст:</b> {html.escape(row["message_text"][:1200])}',
            ]
        )
        if row["source_link"]:
            lines.append(f'<a href="{html.escape(row["source_link"], quote=True)}">Открыть сообщение</a>')
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
        self.allowlist = csv_set("TG_CHAT_ALLOWLIST")
        self.blocklist = csv_set("TG_CHAT_BLOCKLIST")
        self.history_limit = env_int("TG_HISTORY_LIMIT", 100)
        self.backfill = os.getenv("TG_BACKFILL", "1").strip().lower() not in {"0", "false", "no"}
        self.store = LeadStore(os.getenv("TG_DB_PATH", "telegram_leads.db"))
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
                ("TG_OWNER_IDS", self.owner_ids),
            )
            if not value
        ]
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
        return ""

    async def process_event(self, event, notify: bool = True) -> int | None:
        if not event.raw_text:
            return None
        chat = await event.get_chat()
        username = getattr(chat, "username", "") or ""
        if not self.chat_allowed(event.chat_id, username):
            return None
        sender = await event.get_sender()
        lead = parse_message(
            text=event.raw_text,
            chat_id=event.chat_id,
            message_id=event.id,
            chat_title=get_display_name(chat) or username or str(event.chat_id),
            sender_id=getattr(sender, "id", None),
            sender_name=get_display_name(sender) if sender else "",
            sender_username=getattr(sender, "username", "") or "",
            source_link=await self.source_link(event, username),
            message_date=event.date,
        )
        if not lead:
            return None
        lead_id = self.store.add(lead)
        if lead_id and notify:
            row = self.store.by_id(lead_id)
            for owner_id in self.owner_ids:
                try:
                    await self.bot.send_message(owner_id, row_to_text(row), parse_mode="html", link_preview=False)
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
        if event.sender_id not in self.owner_ids:
            return
        text = (event.raw_text or "").strip()
        command, _, argument = text.partition(" ")
        command = command.lower().split("@")[0]
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
        await event.respond(response[:4000], parse_mode="html", link_preview=False)

    async def run(self) -> None:
        self.validate()
        await self.user.start()
        await self.bot.start(bot_token=self.bot_token)
        self.user.add_event_handler(self.process_event, events.NewMessage(incoming=True))
        self.bot.add_event_handler(self.bot_command, events.NewMessage(incoming=True))
        LOG.info("Telegram-парсер запущен")
        await self.scan_history()
        await asyncio.gather(self.user.run_until_disconnected(), self.bot.run_until_disconnected())


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(TelegramLeadService().run())


if __name__ == "__main__":
    main()
