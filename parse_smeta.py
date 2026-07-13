"""Respectful smeta-n.ru -> Google Sheets synchronizer.

The source owner has granted the project permission to collect these public
catalog data for internal construction-company automation.  The crawler uses
one request per second, retries with backoff and a 72-hour run guard.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from google.auth import default as google_auth_default
from typing import Iterable
from urllib.parse import urljoin, urlparse

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://smeta-n.ru"
CATALOG_URL = f"{BASE_URL}/materials_and_suppliers/list_of_quarries"
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1Zb-38mYR63KCnI7JjTZGoedm9LGFZ3snfhoaYBMwuwo")
REQUEST_DELAY = max(float(os.getenv("SMETA_REQUEST_DELAY", "1.0")), 0.8)
MAX_PAGES = int(os.getenv("SMETA_MAX_PAGES", "50"))
FORCE_SYNC = os.getenv("FORCE_SYNC", "").lower() in {"1", "true", "yes"}
DRY_RUN = os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}

OBJECT_HEADERS = [
    "source_id", "Название", "Тип объекта", "Юр. лицо", "Регион",
    "Полный адрес", "Широта", "Долгота", "Режим работы",
    "Поддерживаемый транспорт", "Основной телефон", "Email",
    "Контактное лицо", "Доп. контакты", "Источник", "URL источника",
    "Проверено системой", "Активен",
]
MATERIAL_HEADERS = [
    "source_id", "object_source_id", "Название объекта", "Материал исходный",
    "Категория", "Подкатегория", "Фракция", "Класс материала", "Единица",
    "Цена за м³", "Цена за тонну", "Насыпная плотность",
    "Дата цены в источнике", "Проверено системой", "Цена известна", "Активен",
    "URL источника",
]
HISTORY_HEADERS = [
    "recorded_at", "material_source_id", "object_source_id", "Название объекта",
    "Материал", "Цена за м³", "Цена за тонну", "Дата цены в источнике",
    "URL источника", "Хэш записи",
]
COMPAT_HEADERS = [
    "Название", "Юр лицо", "Вид товара", "Цена м3", "Широта", "Долгота",
    "Телефон", "Стоимость доставки руб_км_м3",
]
LOG_HEADERS = [
    "started_at", "finished_at", "Статус", "Источник", "Страниц обработано",
    "Объектов найдено", "Материалов найдено", "Добавлено", "Обновлено",
    "Без изменений", "Ошибок", "Сообщение",
]


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_number(value: object) -> float | None:
    text = clean(value).lower().replace("\xa0", " ")
    if not text or any(x in text for x in ("по запросу", "уточн", "договор")):
        return None
    match = re.search(r"-?\d[\d\s]*(?:[,.]\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def parse_coordinates(value: str) -> tuple[float | None, float | None]:
    numbers = re.findall(r"\d{2}(?:[.,]\d+)", clean(value))
    if len(numbers) < 2:
        return None, None
    lat, lon = (float(x.replace(",", ".")) for x in numbers[:2])
    if not (40 <= lat <= 82 and 15 <= lon <= 190):
        return None, None
    return lat, lon


def emails(value: str) -> str:
    found = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}", value or "")
    return ", ".join(dict.fromkeys(found))


def object_type(name: str, material_names: Iterable[str]) -> str:
    text = clean(" ".join([name, *material_names])).lower()
    if any(x in text for x in ("полигон", "утилиз", "отход")):
        return "Полигон/утилизация"
    if any(x in text for x in ("перевал", "склад", "база")):
        return "Перевалка/склад"
    return "Карьер"


def classify_material(name: str) -> tuple[str, str, str, str]:
    text = clean(name).lower().replace("ё", "е")
    fraction_match = re.search(r"\b\d{1,3}\s*[-/]\s*\d{1,3}\b", text)
    fraction = fraction_match.group(0).replace(" ", "") if fraction_match else ""
    class_match = re.search(r"(?:^|\s)([1-5])\s*(?:класс|кл\.)", text)
    material_class = class_match.group(1) if class_match else ""
    if "пескогрунт" in text:
        return "Песок", "Пескогрунт", fraction, material_class
    if "песок" in text:
        grade = next((label for key, label in (
            ("очень мелк", "Очень мелкий"), ("тонк", "Тонкий"),
            ("мелк", "Мелкий"), ("средн", "Средний"),
            ("крупн", "Крупный")) if key in text), "Без уточнения")
        treatment = next((label for key, label in (
            ("мытый", "Мытый"), ("сеян", "Сеяный"),
            ("обогащ", "Обогащённый"), ("карьерн", "Карьерный")) if key in text), "")
        return "Песок", clean(f"{treatment} {grade}"), fraction, material_class
    if "щеб" in text:
        rock = next((label for key, label in (
            ("гранит", "Гранитный"), ("извест", "Известняковый"),
            ("гравийн", "Гравийный"), ("вторич", "Вторичный")) if key in text), "Без уточнения")
        return "Щебень", rock, fraction, material_class
    if "грав" in text and "смес" not in text:
        return "Гравий", "", fraction, material_class
    if any(x in text for x in ("пгс", "гпс", "щпс", "смесь", "с-")):
        return "Смеси", "ПГС/ЩПС", fraction, material_class
    if "отсев" in text:
        return "Отсев", "", fraction, material_class
    if any(x in text for x in ("грунт", "земл")):
        return "Грунт", "", fraction, material_class
    if any(x in text for x in ("асфальт", "бетон", "кирпич", "рецикл")):
        return "Вторичные материалы", "", fraction, material_class
    return "Прочее", "", fraction, material_class


@dataclass
class Quarry:
    source_id: str
    name: str
    legal_entity: str = ""
    region: str = ""
    address: str = ""
    latitude: float | None = None
    longitude: float | None = None
    work_hours: str = ""
    transport: str = ""
    main_phone: str = ""
    email: str = ""
    contact_person: str = ""
    contacts: str = ""
    source_url: str = ""


@dataclass
class Material:
    source_id: str
    object_source_id: str
    object_name: str
    raw_name: str
    category: str
    subcategory: str
    fraction: str
    material_class: str
    unit: str
    price_m3: float | None
    price_ton: float | None
    density: float | None
    source_updated_at: str
    source_url: str


class SmetaClient:
    def __init__(self) -> None:
        retry = Retry(
            total=4, connect=4, read=4, backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}), respect_retry_after_header=True,
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "User-Agent": "ConstructionCompanyCatalogSync/2.0 (+internal automation; respectful rate limit)",
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        self.last_request_at = 0.0

    def get(self, url: str) -> BeautifulSoup:
        wait = REQUEST_DELAY - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        response = self.session.get(url, timeout=(10, 30))
        self.last_request_at = time.monotonic()
        response.raise_for_status()
        if "text/html" not in response.headers.get("Content-Type", "text/html"):
            raise RuntimeError(f"Unexpected content type for {url}")
        return BeautifulSoup(response.text, "lxml")

    def discover(self) -> tuple[dict[str, Quarry], int]:
        objects: dict[str, Quarry] = {}
        pages_processed = 0
        for page in range(1, MAX_PAGES + 1):
            url = CATALOG_URL if page == 1 else f"{CATALOG_URL}?page={page}"
            soup = self.get(url)
            rows = soup.select("table tbody tr")
            new_on_page = 0
            for row in rows:
                cells = row.find_all("td", recursive=False)
                link = row.select_one('a[href*="/page_of_quarry-"]')
                if not link or len(cells) < 4:
                    continue
                href = urljoin(BASE_URL, link.get("href", ""))
                match = re.search(r"page_of_quarry-(\d+)", href)
                if not match:
                    continue
                source_id = match.group(1)
                values = [clean(cell.get_text(" ", strip=True)) for cell in cells]
                if source_id not in objects:
                    new_on_page += 1
                objects[source_id] = Quarry(
                    source_id=source_id,
                    name=values[0],
                    legal_entity=values[1] if len(values) > 1 and values[1] != "—" else "",
                    region=values[2] if len(values) > 2 else "",
                    address=values[3] if len(values) > 3 else "",
                    work_hours=values[4] if len(values) > 4 else "",
                    contacts=values[5] if len(values) > 5 else "",
                    email=emails(values[5] if len(values) > 5 else ""),
                    source_url=href,
                )
            pages_processed += 1
            next_link = soup.select_one(f'a.page-link[href*="page={page + 1}"]')
            if not rows or (new_on_page == 0 and not next_link) or not next_link:
                break
        return objects, pages_processed

    def details(self, quarry: Quarry) -> list[Material]:
        soup = self.get(quarry.source_url)
        selectors = {
            "address": ".container_for_address .value",
            "coordinates": ".container_for_coordinates .value",
            "transport": ".container_for_types_of_transport .value",
            "work_hours": ".container_for_work_hours .value",
            "legal_entity": ".container_for_legal_entity .value",
            "main_phone": ".container_for_main_phone .value",
            "contacts": ".container_for_contacts .value",
        }
        values = {key: clean(node.get_text(" ", strip=True)) if (node := soup.select_one(selector)) else ""
                  for key, selector in selectors.items()}
        for field in ("address", "transport", "work_hours", "legal_entity", "main_phone", "contacts"):
            if values[field]:
                setattr(quarry, field, values[field])
        quarry.email = emails(quarry.contacts)
        quarry.latitude, quarry.longitude = parse_coordinates(values["coordinates"])
        h1 = soup.select_one("h1")
        if h1:
            quarry.name = re.sub(r"^Карьер\s+", "", clean(h1.get_text(" ", strip=True)), flags=re.I)

        materials: list[Material] = []
        for table in soup.select("table"):
            headers = [clean(x.get_text(" ", strip=True)).lower() for x in table.select("th")]
            if not headers or not any("материал" in x for x in headers):
                continue
            for row_index, row in enumerate(table.select("tbody tr, tr")):
                cells = [clean(x.get_text(" ", strip=True)) for x in row.find_all("td", recursive=False)]
                if len(cells) < 2:
                    continue
                raw_name = cells[0]
                category, subcategory, fraction, material_class = classify_material(raw_name)
                fingerprint = hashlib.sha1(f"{quarry.source_id}|{raw_name}|{row_index}".encode("utf-8")).hexdigest()[:16]
                materials.append(Material(
                    source_id=fingerprint,
                    object_source_id=quarry.source_id,
                    object_name=quarry.name,
                    raw_name=raw_name,
                    category=category,
                    subcategory=subcategory,
                    fraction=fraction,
                    material_class=material_class,
                    price_m3=normalize_number(cells[1] if len(cells) > 1 else ""),
                    price_ton=normalize_number(cells[2] if len(cells) > 2 else ""),
                    unit=cells[3] if len(cells) > 3 else "",
                    density=normalize_number(cells[4] if len(cells) > 4 else ""),
                    source_updated_at=cells[5] if len(cells) > 5 else "",
                    source_url=quarry.source_url,
                ))
        return materials


def sheet_client():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if raw:
        info = json.loads(raw)
        credentials = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        credentials, _ = google_auth_default(scopes=scopes)
    return gspread.authorize(credentials).open_by_key(SHEET_ID)


def worksheet(book, title: str, rows: int = 1000, cols: int = 20):
    try:
        return book.worksheet(title)
    except gspread.WorksheetNotFound:
        return book.add_worksheet(title=title, rows=rows, cols=cols)


def replace_sheet(ws, headers: list[str], rows: list[list[object]]) -> None:
    ws.clear()
    ws.update([headers, *rows], "A1", value_input_option="RAW")
    ws.freeze(rows=1)
    ws.set_basic_filter(f"A1:{gspread.utils.rowcol_to_a1(max(1, len(rows) + 1), len(headers))}")


def last_success_too_recent(log_ws) -> bool:
    if FORCE_SYNC:
        return False
    records = log_ws.get_all_records(expected_headers=LOG_HEADERS)
    for row in reversed(records):
        if clean(row.get("Статус")).lower() != "успешно":
            continue
        try:
            last = datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00"))
            return datetime.now(timezone.utc) - last < timedelta(hours=72)
        except (KeyError, ValueError, TypeError):
            return False
    return False


def price_hash(material: Material) -> str:
    value = "|".join(map(str, (
        material.source_id, material.price_m3, material.price_ton,
        material.source_updated_at,
    )))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def export_dry_run(quarries: list[Quarry], materials: list[Material]) -> None:
    output = Path(os.getenv("OUTPUT_DIR", "."))
    output.mkdir(parents=True, exist_ok=True)
    with (output / "objects.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(quarries[0]).keys()) if quarries else ["source_id"])
        writer.writeheader(); writer.writerows(asdict(x) for x in quarries)
    with (output / "materials.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(materials[0]).keys()) if materials else ["source_id"])
        writer.writeheader(); writer.writerows(asdict(x) for x in materials)


def run() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    started = datetime.now(timezone.utc)
    book = None if DRY_RUN else sheet_client()
    log_ws = None if DRY_RUN else worksheet(book, "Журнал_обновлений", cols=len(LOG_HEADERS))
    if log_ws and last_success_too_recent(log_ws):
        logging.info("A successful sync ran less than 72 hours ago; skipping")
        return 0

    errors: list[str] = []
    pages = 0
    try:
        client = SmetaClient()
        discovered, pages = client.discover()
        quarries: list[Quarry] = []
        materials: list[Material] = []
        for index, quarry in enumerate(discovered.values(), 1):
            try:
                items = client.details(quarry)
                quarries.append(quarry)
                materials.extend(items)
                logging.info("%s/%s %s: %s materials", index, len(discovered), quarry.name, len(items))
            except Exception as exc:  # keep the rest of the catalog useful
                errors.append(f"{quarry.source_id}: {exc}")
                logging.exception("Failed quarry %s", quarry.source_id)

        checked_at = datetime.now(timezone.utc).isoformat()
        if DRY_RUN:
            export_dry_run(quarries, materials)
            return 0

        object_rows = [[
            q.source_id, q.name, object_type(q.name, [m.raw_name for m in materials if m.object_source_id == q.source_id]),
            q.legal_entity, q.region, q.address, q.latitude or "", q.longitude or "",
            q.work_hours, q.transport, q.main_phone, q.email, q.contact_person, q.contacts,
            "smeta-n.ru", q.source_url, checked_at, "Да",
        ] for q in quarries]
        material_rows = [[
            m.source_id, m.object_source_id, m.object_name, m.raw_name, m.category,
            m.subcategory, m.fraction, m.material_class, m.unit,
            "" if m.price_m3 is None else m.price_m3,
            "" if m.price_ton is None else m.price_ton,
            "" if m.density is None else m.density,
            m.source_updated_at, checked_at, "Да" if (m.price_m3 is not None or m.price_ton is not None) else "Нет",
            "Да", m.source_url,
        ] for m in materials]
        compat_rows = [[
            m.object_name,
            next((q.legal_entity for q in quarries if q.source_id == m.object_source_id), ""),
            m.raw_name,
            "" if m.price_m3 is None else m.price_m3,
            next((q.latitude for q in quarries if q.source_id == m.object_source_id), "") or "",
            next((q.longitude for q in quarries if q.source_id == m.object_source_id), "") or "",
            next((q.main_phone or q.contacts for q in quarries if q.source_id == m.object_source_id), ""),
            15,
        ] for m in materials]

        replace_sheet(worksheet(book, "Объекты", cols=len(OBJECT_HEADERS)), OBJECT_HEADERS, object_rows)
        replace_sheet(worksheet(book, "Материалы_и_цены", cols=len(MATERIAL_HEADERS)), MATERIAL_HEADERS, material_rows)
        replace_sheet(worksheet(book, "Карьеры", cols=len(COMPAT_HEADERS)), COMPAT_HEADERS, compat_rows)

        history_ws = worksheet(book, "История_цен", cols=len(HISTORY_HEADERS))
        existing_hashes = set(history_ws.col_values(len(HISTORY_HEADERS))[1:])
        new_history = []
        for m in materials:
            digest = price_hash(m)
            if digest in existing_hashes:
                continue
            new_history.append([
                checked_at, m.source_id, m.object_source_id, m.object_name, m.raw_name,
                "" if m.price_m3 is None else m.price_m3,
                "" if m.price_ton is None else m.price_ton,
                m.source_updated_at, m.source_url, digest,
            ])
        if not history_ws.get_all_values():
            history_ws.append_row(HISTORY_HEADERS)
            history_ws.freeze(rows=1)
        if new_history:
            history_ws.append_rows(new_history, value_input_option="USER_ENTERED")

        finished = datetime.now(timezone.utc).isoformat()
        log_ws.append_row([
            started.isoformat(), finished, "Успешно", "smeta-n.ru", pages,
            len(quarries), len(materials), len(new_history), len(material_rows), 0,
            len(errors), "; ".join(errors[:10]),
        ], value_input_option="USER_ENTERED")
        return 0
    except Exception as exc:
        logging.exception("Sync failed")
        if log_ws:
            log_ws.append_row([
                started.isoformat(), datetime.now(timezone.utc).isoformat(), "Ошибка",
                "smeta-n.ru", pages, 0, 0, 0, 0, 0, len(errors) + 1, clean(exc),
            ], value_input_option="USER_ENTERED")
        return 1


if __name__ == "__main__":
    sys.exit(run())
