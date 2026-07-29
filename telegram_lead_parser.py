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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None
    Credentials = None

try:
    from telethon import TelegramClient, events, functions, types
    from telethon.sessions import StringSession
    from telethon.utils import get_display_name, get_peer_id
except ImportError:  # Allows the extraction logic to be tested before dependencies are installed.
    TelegramClient = None
    events = None
    functions = None
    types = None
    StringSession = None
    get_display_name = None
    get_peer_id = None


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
    "Асфальт": (r"\bасфальт(?:а|ом|ный|ная|ное)?\b",),
    "Сухие смеси": (r"\bсух\w*\s+смес", r"\bштукатурк\w*\b", r"\bшпакл[её]вк\w*\b"),
    "Пиломатериалы": (r"\bпиломатериал\w*\b", r"\bдоск(?:а|и|у)\b", r"\bбрус\w*\b"),
    "Кровельные материалы": (r"\bкровельн\w*\s+материал", r"\bпрофнастил\w*\b", r"\bрубероид\w*\b"),
    "Утеплитель": (r"\bутеплител\w*\b", r"\bминват\w*\b", r"\bпеноплекс\w*\b"),
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
    "Вывоз грунта и мусора": (
        r"\bвывоз\w*\s+(?:грунт|мусор|снег)",
        r"\bутилизац\w*\s+(?:грунт|мусор)",
    ),
    "Земляные работы": (r"\bземлян\w*\s+работ", r"\bразработк\w*\s+котлован"),
    "Демонтаж": (r"\bдемонтаж\w*\b", r"\bснос\w*\b"),
    "Благоустройство": (r"\bблагоустройств\w*\b", r"\bукладк\w*\s+(?:асфальт|плитк)"),
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
    r"\bпрода(?:ю|ём|ем|м|ётся|ется)\b",
    r"\bпредлага(?:ю|ем)\b",
    r"\bв\s+наличии\b",
    r"\bсобственное\s+производство\b",
    r"\bоказываем\s+услуги\b",
    r"\bпредоставля(?:ю|ем)\b",
    r"\bдостав(?:им|ляем|ка\s+от)\b",
    r"\bработаем\s+(?:по|с)\b",
    r"\bцена\s+(?:от|за)\b",
    r"\bпрайс\b",
    r"\bобращайтесь\b",
    r"\bзвоните\b",
    r"\bсамовывоз\b",
    r"\bотгрузк\w*\b",
    r"\bаренд\w*\b",
)

SERVICE_PRODUCTS = {
    "Грузоперевозки",
    "Спецтехника",
    "Вывоз грунта и мусора",
    "Земляные работы",
    "Демонтаж",
    "Благоустройство",
}
PRICE_RE = re.compile(
    r"(?<!\d)(?:от\s*)?\d[\d\s]*(?:[.,]\d+)?\s*"
    r"(?:₽|руб(?:\.|лей|ля)?|р\.)"
    r"(?:\s*(?:/|за)\s*(?:м\s*[³3]|куб|т(?:онн\w*)?|кг|шт\.?|рейс|час|смен\w*))?",
    re.IGNORECASE,
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
    r"(?:адрес|доставк\w*(?:\s+(?:на|в|до))?|объект|обьект|точка|выгрузк\w*|"
    r"база|склад|карьер|производство)\s*[:\-]?\s*(.+)",
    re.IGNORECASE,
)
LOCATION_WORDS_RE = re.compile(
    r"\b(?:москва|московск\w*\s+обл|мо\b|район|округ|шоссе|улица|ул\.|"
    r"проспект|пр-т|проезд|деревня|д\.|пос[её]лок|п\.|город|г\.)\b",
    re.IGNORECASE,
)
TELEGRAM_CHAT_LINK_RE = re.compile(
    r"(?:(?:https?://)?(?:t\.me|telegram\.me)/[^\s<>\[\]()\"']+)",
    re.IGNORECASE,
)
NON_CHAT_TELEGRAM_PATHS = {
    "addlist",
    "addstickers",
    "blog",
    "faq",
    "iv",
    "login",
    "proxy",
    "share",
    "socks",
}


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


@dataclass
class ParsedProviderOffer:
    fingerprint: str
    dedupe_key: str
    category: str
    chat_id: int
    message_id: int
    chat_title: str
    sender_id: int | None
    sender_name: str
    sender_username: str
    items: list[str]
    prices: list[str]
    phones: list[str]
    emails: list[str]
    telegram_contacts: list[str]
    address: str
    source_link: str
    message_text: str
    message_date: str
    first_seen: str
    last_seen: str


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


def extract_telegram_chat_targets(
    text: str, extra_urls: Iterable[str] = ()
) -> tuple[set[str], set[str]]:
    """Return public usernames and private invite hashes from Telegram links."""
    usernames: set[str] = set()
    invite_hashes: set[str] = set()
    raw_links = list(TELEGRAM_CHAT_LINK_RE.findall(text or "")) + list(extra_urls)
    for raw_link in raw_links:
        link = str(raw_link or "").strip().rstrip(".,;:!?)]}>")
        if not link:
            continue
        if not re.match(r"^https?://", link, re.IGNORECASE):
            link = "https://" + link
        parsed = urllib.parse.urlparse(link)
        if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            continue
        path = urllib.parse.unquote(parsed.path).strip("/")
        if not path:
            continue
        if path.startswith("+"):
            invite_hashes.add(path[1:].split("/", 1)[0])
            continue
        if path.lower().startswith("joinchat/"):
            invite_hashes.add(path.split("/", 1)[1].split("/", 1)[0])
            continue
        username = path.split("/", 1)[0].lstrip("@").lower()
        if (
            username not in NON_CHAT_TELEGRAM_PATHS
            and re.fullmatch(r"[a-z0-9_]{5,32}", username, re.IGNORECASE)
        ):
            usernames.add(username)
    return usernames, invite_hashes


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


def extract_prices(text: str) -> list[str]:
    return unique(re.sub(r"\s+", " ", match.group(0)).strip() for match in PRICE_RE.finditer(text))


def is_probable_request(
    text: str,
    products: list[str],
    quantities: list[dict[str, str]],
    address: str = "",
) -> bool:
    goods = [product for product in products if product not in SERVICE_PRODUCTS]
    if not goods or not quantities or not address:
        return False
    request_score = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in REQUEST_PATTERNS)
    offer_score = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in OFFER_PATTERNS)
    if request_score < 1 or offer_score:
        return False
    if (
        len(text) > 1000
        and not quantities
        and not PHONE_RE.search(text)
        and not EMAIL_RE.search(text)
        and not USERNAME_RE.search(text)
        and not re.search(
            r"\b(?:адрес|доставк\w*|объект|обьект|цена|стоимость|смета|закупк\w*)\b",
            text,
            re.IGNORECASE,
        )
    ):
        return False
    return True


def is_probable_provider_offer(text: str, products: list[str], prices: list[str]) -> bool:
    if not products:
        return False
    request_score = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in REQUEST_PATTERNS)
    offer_score = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in OFFER_PATTERNS)
    if request_score and not offer_score:
        return False
    return offer_score > 0 or bool(prices)


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
    address = extract_address(clean_text)
    if not is_probable_request(clean_text, products, quantities, address):
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
        products=[product for product in products if product not in SERVICE_PRODUCTS],
        quantities=quantities,
        phones=phones,
        emails=emails,
        telegram_contacts=telegram_contacts,
        address=address,
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


def parse_provider_offers(
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
) -> list[ParsedProviderOffer]:
    clean_text = re.sub(r"\r\n?", "\n", text or "").strip()
    products = extract_products(clean_text)
    prices = extract_prices(clean_text)
    if not is_probable_provider_offer(clean_text, products, prices):
        return []

    phones = unique(normalize_phone(match.group(0)) for match in PHONE_RE.finditer(clean_text))
    emails = unique(match.group(0).lower() for match in EMAIL_RE.finditer(clean_text))
    telegram_contacts = unique(match.group(0) for match in USERNAME_RE.finditer(clean_text))
    identity = (
        "|".join(sorted(phones))
        or "|".join(sorted(value.lower() for value in telegram_contacts))
        or sender_username.lower().lstrip("@")
        or str(sender_id or "")
        or sender_name.casefold()
    )
    date = (message_date or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    groups = (
        ("Товары", [product for product in products if product not in SERVICE_PRODUCTS]),
        ("Услуги", [product for product in products if product in SERVICE_PRODUCTS]),
    )
    offers: list[ParsedProviderOffer] = []
    for category, items in groups:
        if not items:
            continue
        # One supplier has one row per category. Repeated advertisements update
        # that row and enrich its assortment instead of creating duplicates.
        dedupe_source = f"{category}|{identity}"
        dedupe_key = hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest()
        fingerprint = hashlib.sha256(
            f"{chat_id}:{message_id}:{category}".encode("utf-8")
        ).hexdigest()
        offers.append(
            ParsedProviderOffer(
                fingerprint=fingerprint,
                dedupe_key=dedupe_key,
                category=category,
                chat_id=chat_id,
                message_id=message_id,
                chat_title=chat_title,
                sender_id=sender_id,
                sender_name=sender_name,
                sender_username=sender_username,
                items=items,
                prices=prices,
                phones=phones,
                emails=emails,
                telegram_contacts=telegram_contacts,
                address=extract_address(clean_text),
                source_link=source_link,
                message_text=clean_text[:6000],
                message_date=date,
                first_seen=now,
                last_seen=now,
            )
        )
    return offers


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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_provider_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_title TEXT NOT NULL,
                    sender_id INTEGER,
                    sender_name TEXT,
                    sender_username TEXT,
                    items_json TEXT NOT NULL,
                    prices_json TEXT NOT NULL,
                    phones_json TEXT NOT NULL,
                    emails_json TEXT NOT NULL,
                    telegram_contacts_json TEXT NOT NULL,
                    address TEXT,
                    source_link TEXT,
                    message_text TEXT NOT NULL,
                    message_date TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_provider_messages (
                    fingerprint TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_provider_category "
                "ON telegram_provider_offers(category, last_seen DESC)"
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

    def add_or_update_offer(self, offer: ParsedProviderOffer) -> tuple[sqlite3.Row | None, bool]:
        data = asdict(offer)
        fingerprint = data.pop("fingerprint")
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO telegram_provider_messages (fingerprint, dedupe_key) VALUES (?, ?)",
                    (fingerprint, offer.dedupe_key),
                )
            except sqlite3.IntegrityError:
                return None, False
            existing = connection.execute(
                "SELECT * FROM telegram_provider_offers WHERE dedupe_key=?",
                (offer.dedupe_key,),
            ).fetchone()
            for key in ("items", "prices", "phones", "emails", "telegram_contacts"):
                values = data.pop(key)
                if existing:
                    values = unique(json.loads(existing[f"{key}_json"]) + values)
                data[f"{key}_json"] = json.dumps(values, ensure_ascii=False)
            if existing:
                data["first_seen"] = existing["first_seen"]
                if not data["address"]:
                    data["address"] = existing["address"]
            columns = ", ".join(data)
            placeholders = ", ".join("?" for _ in data)
            update_columns = ", ".join(
                f"{column}=excluded.{column}"
                for column in data
                if column not in {"dedupe_key", "first_seen"}
            )
            connection.execute(
                f"""
                INSERT INTO telegram_provider_offers ({columns})
                VALUES ({placeholders})
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    {update_columns},
                    seen_count=telegram_provider_offers.seen_count + 1
                """,
                tuple(data.values()),
            )
            row = connection.execute(
                "SELECT * FROM telegram_provider_offers WHERE dedupe_key=?",
                (offer.dedupe_key,),
            ).fetchone()
            return row, True

    def offer_count(self, category: str = "") -> int:
        with self.connect() as connection:
            if category:
                return int(
                    connection.execute(
                        "SELECT COUNT(*) FROM telegram_provider_offers WHERE category=?",
                        (category,),
                    ).fetchone()[0]
                )
            return int(
                connection.execute("SELECT COUNT(*) FROM telegram_provider_offers").fetchone()[0]
            )

    def offers_by_keys(self, keys: Iterable[str]) -> list[sqlite3.Row]:
        values = list(dict.fromkeys(keys))
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self.connect() as connection:
            return list(
                connection.execute(
                    f"SELECT * FROM telegram_provider_offers WHERE dedupe_key IN ({placeholders})",
                    values,
                )
            )

    def all_offer_keys(self) -> set[str]:
        with self.connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT dedupe_key FROM telegram_provider_offers"
                )
            }

    def prune_invalid_leads(self) -> int:
        invalid_ids: list[int] = []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, message_text, products_json, quantities_json, address FROM telegram_leads"
            ).fetchall()
            for row in rows:
                if not is_probable_request(
                    row["message_text"],
                    json.loads(row["products_json"]),
                    json.loads(row["quantities_json"]),
                    row["address"] or "",
                ):
                    invalid_ids.append(int(row["id"]))
            if invalid_ids:
                placeholders = ",".join("?" for _ in invalid_ids)
                connection.execute(
                    f"DELETE FROM telegram_leads WHERE id IN ({placeholders})",
                    invalid_ids,
                )
        return len(invalid_ids)

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

    def retain_chat_ids(self, chat_ids: set[int]) -> int:
        """Remove rows from chats that are not in the current approved index."""
        with self.connect() as connection:
            if not chat_ids:
                cursor = connection.execute("DELETE FROM telegram_leads")
            else:
                placeholders = ",".join("?" for _ in chat_ids)
                cursor = connection.execute(
                    f"DELETE FROM telegram_leads WHERE chat_id NOT IN ({placeholders})",
                    tuple(sorted(chat_ids)),
                )
            return int(cursor.rowcount)

    def retain_offer_chat_ids(self, chat_ids: set[int]) -> int:
        with self.connect() as connection:
            if not chat_ids:
                cursor = connection.execute("DELETE FROM telegram_provider_offers")
                connection.execute("DELETE FROM telegram_provider_messages")
                return int(cursor.rowcount)
            placeholders = ",".join("?" for _ in chat_ids)
            keys = [
                row[0]
                for row in connection.execute(
                    f"SELECT dedupe_key FROM telegram_provider_offers "
                    f"WHERE chat_id NOT IN ({placeholders})",
                    tuple(sorted(chat_ids)),
                )
            ]
            cursor = connection.execute(
                f"DELETE FROM telegram_provider_offers WHERE chat_id NOT IN ({placeholders})",
                tuple(sorted(chat_ids)),
            )
            if keys:
                key_placeholders = ",".join("?" for _ in keys)
                connection.execute(
                    f"DELETE FROM telegram_provider_messages "
                    f"WHERE dedupe_key IN ({key_placeholders})",
                    keys,
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


class ProviderSheetExporter:
    HEADERS = [
        "Ключ",
        "Тип",
        "Товары / услуги",
        "Цены",
        "Компания / автор",
        "Телефон",
        "Telegram",
        "Email",
        "Адрес / регион",
        "Чат",
        "Ссылка на объявление",
        "Первое обнаружение",
        "Последнее обновление",
        "Повторов найдено",
        "Текст объявления",
    ]

    def __init__(self):
        self.sheet_id = (
            os.getenv("TG_SUPPLIERS_SHEET_ID", "").strip()
            or os.getenv("GOOGLE_SHEET_ID", "").strip()
            or "1Zb-38mYR63KCnI7JjTZGoedm9LGFZ3snfhoaYBMwuwo"
        )
        self.credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        self.worksheet_names = {
            "Товары": os.getenv("TG_GOODS_SHEET_NAME", "Поставщики_Товары").strip(),
            "Услуги": os.getenv("TG_SERVICES_SHEET_NAME", "Поставщики_Услуги").strip(),
        }
        self._book = None

    @property
    def enabled(self) -> bool:
        return bool(
            gspread
            and Credentials
            and self.sheet_id
            and self.credentials_json
        )

    def _open(self):
        if self._book is not None:
            return self._book
        if not self.enabled:
            raise RuntimeError(
                "Google Sheets export disabled: GOOGLE_SERVICE_ACCOUNT_JSON is not configured"
            )
        info = json.loads(self.credentials_json)
        credentials = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        self._book = gspread.authorize(credentials).open_by_key(self.sheet_id)
        return self._book

    def _worksheet(self, category: str):
        book = self._open()
        title = self.worksheet_names[category]
        try:
            worksheet = book.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = book.add_worksheet(title=title, rows=1000, cols=len(self.HEADERS))
        values = worksheet.row_values(1)
        if values != self.HEADERS:
            worksheet.update(range_name="A1:O1", values=[self.HEADERS])
            worksheet.freeze(rows=1)
        return worksheet

    @staticmethod
    def _row_values(row: sqlite3.Row) -> list[object]:
        contacts = unique(
            json.loads(row["telegram_contacts_json"])
            + ([f'@{row["sender_username"].lstrip("@")}'] if row["sender_username"] else [])
        )
        return [
            row["dedupe_key"],
            row["category"],
            ", ".join(json.loads(row["items_json"])),
            ", ".join(json.loads(row["prices_json"])),
            row["sender_name"] or row["sender_username"] or str(row["sender_id"] or ""),
            ", ".join(json.loads(row["phones_json"])),
            ", ".join(contacts),
            ", ".join(json.loads(row["emails_json"])),
            row["address"] or "",
            row["chat_title"],
            row["source_link"] or "",
            row["first_seen"],
            row["last_seen"],
            int(row["seen_count"]),
            row["message_text"],
        ]

    def sync(self, rows: Iterable[sqlite3.Row]) -> int:
        grouped: dict[str, list[sqlite3.Row]] = {"Товары": [], "Услуги": []}
        for row in rows:
            grouped[row["category"]].append(row)
        synced = 0
        for category, category_rows in grouped.items():
            if not category_rows:
                continue
            worksheet = self._worksheet(category)
            existing_values = worksheet.get_all_values()
            row_by_key = {
                values[0]: index
                for index, values in enumerate(existing_values[1:], start=2)
                if values
            }
            append_values: list[list[object]] = []
            for row in category_rows:
                values = self._row_values(row)
                existing_row = row_by_key.get(row["dedupe_key"])
                if existing_row:
                    worksheet.update(
                        range_name=f"A{existing_row}:O{existing_row}",
                        values=[values],
                    )
                else:
                    append_values.append(values)
                synced += 1
            if append_values:
                worksheet.append_rows(append_values, value_input_option="USER_ENTERED")
        return synced


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
        self.index_chat_name = os.getenv("TG_INDEX_CHAT_NAME", "Бизнес Гид").strip()
        self.index_history_limit = env_int("TG_INDEX_HISTORY_LIMIT", 3000)
        self.indexed_chat_ids: set[int] = set()
        self.indexed_entities: dict[int, object] = {}
        self.index_public_usernames: set[str] = set()
        self.index_invite_hashes: set[str] = set()
        self.joined_dialog_ids: set[int] = set()
        self.managed_chat_ids: set[int] = set()
        self.index_loaded = False
        self.index_poll_seconds = max(env_int("TG_INDEX_POLL_SECONDS", 300), 60)
        self.join_interval_seconds = max(env_int("TG_JOIN_INTERVAL_SECONDS", 60), 60)
        self.organize_interval_seconds = max(env_int("TG_ORGANIZE_INTERVAL_SECONDS", 2), 1)
        self.index_folder_name = os.getenv("TG_INDEX_FOLDER_NAME", "Бизнес Гид").strip()
        self.auto_join = os.getenv("TG_AUTO_JOIN_INDEXED_CHATS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        self.history_limit = env_int("TG_HISTORY_LIMIT", 100)
        self.backfill = os.getenv("TG_BACKFILL", "1").strip().lower() not in {"0", "false", "no"}
        self.store = LeadStore(os.getenv("TG_DB_PATH", "telegram_leads.db"))
        self.sheet_exporter = ProviderSheetExporter()
        self.pending_offer_keys: set[str] = self.store.all_offer_keys()
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
        if self.allowlist and not (keys & self.allowlist):
            return False
        if self.index_chat_name:
            return self.index_loaded and int(chat_id) in self.indexed_chat_ids
        return True

    @staticmethod
    def message_urls(message) -> list[str]:
        urls: list[str] = []
        for entity in getattr(message, "entities", None) or []:
            url = getattr(entity, "url", None)
            if url:
                urls.append(str(url))
        webpage = getattr(getattr(message, "media", None), "webpage", None)
        if getattr(webpage, "url", None):
            urls.append(str(webpage.url))
        for row in getattr(message, "buttons", None) or []:
            for button in row:
                if getattr(button, "url", None):
                    urls.append(str(button.url))
        return urls

    async def load_indexed_chats(self) -> None:
        """Build a fail-closed chat allowlist from links sent by «Бизнес Гид»."""
        self.indexed_chat_ids.clear()
        self.indexed_entities.clear()
        self.index_loaded = False
        removed_initial = self.store.retain_chat_ids(set())
        if removed_initial:
            LOG.info(
                "Перед индексацией удалено старых заявок: %s", removed_initial
            )
        if not self.index_chat_name:
            self.index_loaded = True
            return

        dialogs = [dialog async for dialog in self.user.iter_dialogs()]
        self.joined_dialog_ids = {
            int(dialog.id) for dialog in dialogs if dialog.is_group or dialog.is_channel
        }
        wanted = re.sub(r"\s+", " ", self.index_chat_name.casefold()).strip()
        exact = [
            dialog
            for dialog in dialogs
            if re.sub(r"\s+", " ", (dialog.name or "").casefold()).strip() == wanted
        ]
        candidates = exact or [
            dialog
            for dialog in dialogs
            if wanted in re.sub(r"\s+", " ", (dialog.name or "").casefold()).strip()
        ]
        if not candidates:
            self.index_loaded = True
            removed = self.store.retain_chat_ids(set())
            removed_offers = self.store.retain_offer_chat_ids(set())
            LOG.error(
                "Переписка-индекс «%s» не найдена; все чаты заблокированы, "
                "удалено заявок %s и предложений %s",
                self.index_chat_name,
                removed,
                removed_offers,
            )
            return

        index_dialog = candidates[0]
        public_usernames: set[str] = set()
        invite_hashes: set[str] = set()
        async for message in self.user.iter_messages(
            index_dialog.entity, limit=self.index_history_limit
        ):
            usernames, invites = extract_telegram_chat_targets(
                message.message or "", self.message_urls(message)
            )
            public_usernames.update(usernames)
            invite_hashes.update(invites)
        self.index_public_usernames = public_usernames
        self.index_invite_hashes = invite_hashes

        group_dialogs = [
            dialog for dialog in dialogs if dialog.is_group or dialog.is_channel
        ]
        for dialog in group_dialogs:
            username = (getattr(dialog.entity, "username", "") or "").lower()
            if username and username in public_usernames:
                self.indexed_chat_ids.add(int(dialog.id))
                self.indexed_entities[int(dialog.id)] = dialog.entity

        resolved_public = 0
        for username in sorted(public_usernames):
            if any(
                (getattr(entity, "username", "") or "").lower() == username
                for entity in self.indexed_entities.values()
            ):
                continue
            try:
                entity = await self.user.get_entity(username)
                peer_id = int(get_peer_id(entity))
                if peer_id < 0:
                    self.indexed_chat_ids.add(peer_id)
                    self.indexed_entities[peer_id] = entity
                    resolved_public += 1
            except Exception as exc:
                LOG.warning(
                    "Не удалось открыть публичную ссылку @%s: %s",
                    username,
                    type(exc).__name__,
                )

        self.index_loaded = True
        removed = self.store.retain_chat_ids(self.indexed_chat_ids)
        removed_offers = self.store.retain_offer_chat_ids(self.indexed_chat_ids)
        LOG.info(
            "Индекс «%s»: ссылок %s, разрешено публичных по ссылке %s, "
            "приватных приглашений %s, разрешено чатов %s, "
            "удалено посторонних заявок %s",
            self.index_chat_name,
            len(public_usernames),
            resolved_public,
            len(invite_hashes),
            len(self.indexed_chat_ids),
            removed,
        )
        if removed_offers:
            LOG.info("Удалено предложений из посторонних чатов: %s", removed_offers)

    async def add_to_index_folder(self, entity) -> None:
        """Add one managed chat to a dedicated Telegram chat folder."""
        if not self.index_folder_name:
            return
        input_peer = await self.user.get_input_entity(entity)
        peer_id = int(get_peer_id(input_peer))
        filters_result = await self.user(functions.messages.GetDialogFiltersRequest())
        filters = list(getattr(filters_result, "filters", filters_result) or [])

        selected = None
        used_ids: set[int] = set()
        wanted = self.index_folder_name.casefold()
        for dialog_filter in filters:
            filter_id = getattr(dialog_filter, "id", None)
            if isinstance(filter_id, int):
                used_ids.add(filter_id)
            title = getattr(dialog_filter, "title", "")
            title = getattr(title, "text", title)
            if str(title).casefold() == wanted and hasattr(dialog_filter, "include_peers"):
                selected = dialog_filter
                break

        if selected is None:
            filter_id = next((value for value in range(2, 256) if value not in used_ids), None)
            if filter_id is None:
                raise RuntimeError("Нет свободного ID для папки Telegram")
            selected = types.DialogFilter(
                id=filter_id,
                title=types.TextWithEntities(text=self.index_folder_name, entities=[]),
                pinned_peers=[],
                include_peers=[input_peer],
                exclude_peers=[],
                contacts=False,
                non_contacts=False,
                groups=False,
                broadcasts=False,
                bots=False,
                exclude_muted=False,
                exclude_read=False,
                exclude_archived=False,
                title_noanimate=False,
                emoticon="🏗",
            )
        else:
            include_peers = list(getattr(selected, "include_peers", None) or [])
            if peer_id not in {int(get_peer_id(peer)) for peer in include_peers}:
                include_peers.append(input_peer)
                selected.include_peers = include_peers

        await self.user(
            functions.messages.UpdateDialogFilterRequest(
                id=int(selected.id),
                filter=selected,
            )
        )

    async def mute_archive_and_folder(self, entity) -> int:
        """Silence a chat, archive it, and add it to the dedicated folder."""
        input_peer = await self.user.get_input_entity(entity)
        peer_id = int(get_peer_id(input_peer))
        await self.user(
            functions.account.UpdateNotifySettingsRequest(
                peer=types.InputNotifyPeer(peer=input_peer),
                settings=types.InputPeerNotifySettings(
                    show_previews=False,
                    silent=True,
                    mute_until=int(
                        (datetime.now(timezone.utc) + timedelta(days=3650)).timestamp()
                    ),
                ),
            )
        )
        await self.user.edit_folder(entity, 1)
        await self.add_to_index_folder(entity)
        self.indexed_chat_ids.add(peer_id)
        self.indexed_entities[peer_id] = entity
        self.managed_chat_ids.add(peer_id)
        return peer_id

    async def archive_entities_batch(self, entities: list[object]) -> set[int]:
        """Move already joined chats to the archive in one Telegram request."""
        folder_peers = []
        peer_ids: set[int] = set()
        for entity in entities:
            input_peer = await self.user.get_input_entity(entity)
            peer_id = int(get_peer_id(input_peer))
            folder_peers.append(
                types.InputFolderPeer(peer=input_peer, folder_id=1)
            )
            peer_ids.add(peer_id)
        if folder_peers:
            await self.user(
                functions.folders.EditPeerFoldersRequest(
                    folder_peers=folder_peers
                )
            )
        return peer_ids

    async def manage_public_target(self, username: str) -> int:
        entity = next(
            (
                item
                for item in self.indexed_entities.values()
                if (getattr(item, "username", "") or "").casefold() == username.casefold()
            ),
            None,
        )
        if entity is None:
            entity = await self.user.get_entity(username)
        peer_id = int(get_peer_id(entity))
        if peer_id in self.managed_chat_ids:
            return peer_id
        if peer_id not in self.joined_dialog_ids:
            await self.user(functions.channels.JoinChannelRequest(channel=entity))
            self.joined_dialog_ids.add(peer_id)
        return await self.mute_archive_and_folder(entity)

    async def manage_private_target(self, invite_hash: str) -> int:
        try:
            updates = await self.user(
                functions.messages.ImportChatInviteRequest(hash=invite_hash)
            )
            chats = list(getattr(updates, "chats", None) or [])
            if not chats:
                raise RuntimeError("Telegram не вернул чат после вступления")
            entity = chats[0]
        except Exception as exc:
            if type(exc).__name__ != "UserAlreadyParticipantError":
                raise
            invite = await self.user(
                functions.messages.CheckChatInviteRequest(hash=invite_hash)
            )
            entity = getattr(invite, "chat", None)
            if entity is None:
                raise
        peer_id = int(get_peer_id(entity))
        if peer_id in self.managed_chat_ids:
            return peer_id
        self.joined_dialog_ids.add(peer_id)
        return await self.mute_archive_and_folder(entity)

    async def organize_existing_indexed_chats(self) -> None:
        """Immediately mute/archive chats that are already joined and indexed.

        Joining new chats remains rate-limited separately. Moving already joined
        chats into the archive is a local account organization operation and
        should not wait behind the multi-hour join queue.
        """
        while not self.index_loaded:
            await asyncio.sleep(2)

        existing_entities = [
            entity
            for peer_id, entity in self.indexed_entities.items()
            if peer_id in self.joined_dialog_ids
        ]
        archived_ids: set[int] = set()
        try:
            archived_ids = await self.archive_entities_batch(existing_entities)
            self.managed_chat_ids.update(archived_ids)
            LOG.info(
                "Существующие чаты из «%s» пакетно перемещены в архив: %s",
                self.index_chat_name,
                len(archived_ids),
            )
        except Exception:
            LOG.exception(
                "Не удалось пакетно переместить существующие чаты из «%s» в архив",
                self.index_chat_name,
            )

        muted = 0
        for entity in existing_entities:
            try:
                input_peer = await self.user.get_input_entity(entity)
                await self.user(
                    functions.account.UpdateNotifySettingsRequest(
                        peer=types.InputNotifyPeer(peer=input_peer),
                        settings=types.InputPeerNotifySettings(
                            show_previews=False,
                            silent=True,
                            mute_until=int(
                                (
                                    datetime.now(timezone.utc)
                                    + timedelta(days=3650)
                                ).timestamp()
                            ),
                        ),
                    )
                )
                muted += 1
            except Exception as exc:
                flood_seconds = int(getattr(exc, "seconds", 0) or 0)
                if flood_seconds:
                    LOG.warning(
                        "Telegram временно ограничил отключение уведомлений на %s сек.; "
                        "чаты уже перенесены в архив, уведомления будут отключаться "
                        "для новых чатов по очереди",
                        flood_seconds,
                    )
                    break
                LOG.exception("Не удалось отключить уведомления для чата")
            await asyncio.sleep(self.organize_interval_seconds)
        LOG.info(
            "Существующие чаты из «%s» обработаны: архивировано %s, "
            "уведомления отключены у %s",
            self.index_chat_name,
            len(archived_ids),
            muted,
        )

    async def manage_indexed_memberships(self) -> None:
        """Join at most one indexed chat per minute and organize it."""
        while not self.index_loaded:
            await asyncio.sleep(2)
        if not self.auto_join:
            return

        targets = [
            *(("public", username) for username in sorted(self.index_public_usernames)),
            *(("private", invite_hash) for invite_hash in sorted(self.index_invite_hashes)),
        ]
        LOG.info(
            "Очередь вступления из «%s»: %s целей, интервал не менее %s сек.",
            self.index_chat_name,
            len(targets),
            self.join_interval_seconds,
        )
        for kind, value in targets:
            while True:
                try:
                    if kind == "public":
                        peer_id = await self.manage_public_target(value)
                        label = f"@{value}"
                    else:
                        peer_id = await self.manage_private_target(value)
                        label = "приватное приглашение"
                    LOG.info(
                        "Чат из «%s» подключён, отключены уведомления, добавлен в архив "
                        "и папку «%s»: %s (%s)",
                        self.index_chat_name,
                        self.index_folder_name,
                        label,
                        peer_id,
                    )
                    break
                except Exception as exc:
                    flood_seconds = int(getattr(exc, "seconds", 0) or 0)
                    if flood_seconds:
                        LOG.warning(
                            "Telegram установил FloodWait на %s сек.; очередь вступления "
                            "приостановлена без обхода ограничения",
                            flood_seconds,
                        )
                        await asyncio.sleep(flood_seconds + 1)
                        continue
                    LOG.warning(
                        "Цель из «%s» пропущена (%s): %s",
                        self.index_chat_name,
                        kind,
                        type(exc).__name__,
                    )
                    break
            await asyncio.sleep(self.join_interval_seconds)

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
        # NewMessage.Event.message is a Telegram Message, while
        # Message.message is only its text. Historical cards pass a Message
        # directly, so keep that object instead of trying to download a string.
        if isinstance(target, str) or not getattr(target, "media", None):
            target = event
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
        common = dict(
            text=event.raw_text,
            chat_id=event.chat_id,
            message_id=event.id,
            chat_title=get_display_name(chat) or username or str(event.chat_id),
            sender_id=sender_id,
            sender_name=get_display_name(sender) if sender else "",
            sender_username=getattr(sender, "username", "") or "",
            source_link=await self.source_link(event, username),
            message_date=event.date,
        )
        lead = parse_message(
            **common,
            media_type=media_type,
            media_name=media_name,
            media_mime=media_mime,
            media_size=media_size,
            forwarded_from=self.forwarded_from(event),
            reply_to_message_id=getattr(event, "reply_to_msg_id", None),
        )
        for offer in parse_provider_offers(**common):
            offer_row, changed = self.store.add_or_update_offer(offer)
            if changed and offer_row:
                self.pending_offer_keys.add(str(offer_row["dedupe_key"]))
        lead_id = self.store.add(lead) if lead else None
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

    async def sync_provider_sheets(self) -> None:
        while True:
            await asyncio.sleep(15)
            if not self.pending_offer_keys:
                continue
            keys = set(self.pending_offer_keys)
            self.pending_offer_keys.difference_update(keys)
            rows = self.store.offers_by_keys(keys)
            if not rows:
                continue
            if not self.sheet_exporter.enabled:
                LOG.warning(
                    "Google Sheets для поставщиков не настроен; %s записей сохранены в SQLite",
                    len(rows),
                )
                continue
            try:
                synced = await asyncio.to_thread(self.sheet_exporter.sync, rows)
                LOG.info("В Google Таблицу выгружено предложений: %s", synced)
            except Exception:
                self.pending_offer_keys.update(keys)
                LOG.exception("Не удалось синхронизировать базу поставщиков с Google Таблицей")

    async def scan_history(self) -> None:
        if not self.backfill or self.history_limit <= 0:
            return
        scanned_chats = 0
        for chat_id, entity in list(self.indexed_entities.items()):
            username = getattr(entity, "username", "") or ""
            if not self.chat_allowed(chat_id, username):
                continue
            scanned_chats += 1
            LOG.info("История разрешённого чата: %s", get_display_name(entity))
            async for message in self.user.iter_messages(entity, limit=self.history_limit, reverse=True):
                if message.message:
                    await self.process_event(message, notify=False)
        LOG.info("История обработана: %s чатов, всего заявок %s", scanned_chats, self.store.count())

    async def poll_indexed_history(self) -> None:
        while not self.index_loaded:
            await asyncio.sleep(2)
        while True:
            await self.scan_history()
            await asyncio.sleep(self.index_poll_seconds)

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
            if self.index_chat_name and not self.index_loaded:
                index_status = (
                    f'⏳ Индексация ссылок из «{html.escape(self.index_chat_name)}»; '
                    f"уже найдено чатов: <b>{len(self.indexed_chat_ids)}</b>"
                )
            elif self.index_chat_name:
                index_status = (
                    f'Чатов из «{html.escape(self.index_chat_name)}»: '
                    f"<b>{len(self.indexed_chat_ids)}</b>"
                )
            else:
                index_status = "Индекс чатов отключён"
            response = (
                f"✅ Парсер работает\n{index_status}\n"
                f"Организовано в папку и архив: <b>{len(self.managed_chat_ids)}</b>\n"
                f"Заявок покупателей: <b>{self.store.count()}</b>\n"
                f"Поставщиков товаров: <b>{self.store.offer_count('Товары')}</b>\n"
                f"Поставщиков услуг: <b>{self.store.offer_count('Услуги')}</b>\n"
                f"Google Таблица: <b>{'подключена' if self.sheet_exporter.enabled else 'не настроена'}</b>"
            )
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
        pruned = self.store.prune_invalid_leads()
        if pruned:
            LOG.info("Удалено записей, не отвечающих строгому фильтру: %s", pruned)
        removed = self.store.delete_by_sender(int(bot_me.id))
        for owner_id in self.owner_ids:
            removed += self.store.delete_by_sender(int(owner_id))
        if removed:
            LOG.info("Удалено ошибочных заявок от бота и владельцев: %s", removed)
        self.user.add_event_handler(self.process_event, events.NewMessage(incoming=True))
        self.bot.add_event_handler(self.bot_command, events.NewMessage(incoming=True))
        LOG.info("Telegram-парсер запущен")
        index_task = asyncio.create_task(
            self.load_indexed_chats(), name="telegram-business-guide-index"
        )
        history_task = asyncio.create_task(
            self.poll_indexed_history(), name="telegram-indexed-history-scan"
        )
        membership_task = asyncio.create_task(
            self.manage_indexed_memberships(), name="telegram-indexed-membership-manager"
        )
        organize_task = asyncio.create_task(
            self.organize_existing_indexed_chats(), name="telegram-indexed-chat-organizer"
        )
        sheet_task = asyncio.create_task(
            self.sync_provider_sheets(), name="telegram-provider-sheet-sync"
        )
        try:
            await asyncio.gather(
                self.user.run_until_disconnected(),
                self.bot.run_until_disconnected(),
            )
        finally:
            index_task.cancel()
            history_task.cancel()
            membership_task.cancel()
            organize_task.cancel()
            sheet_task.cancel()


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
