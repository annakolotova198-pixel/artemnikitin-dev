"""Durable Google Sheets mirror for the Dzen queue and knowledge index."""

from __future__ import annotations

import json
import os
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from dzen_content_service import MOSCOW, _connect
from dzen_knowledge import _ensure_table


DEFAULT_SHEET_ID = "1Zb-38mYR63KCnI7JjTZGoedm9LGFZ3snfhoaYBMwuwo"
QUEUE_SHEET = "Дзен_Очередь"
KNOWLEDGE_SHEET = "Дзен_Библиотека"
QUEUE_HEADERS = [
    "ID",
    "Источник",
    "URL источника",
    "Исходный заголовок",
    "Заголовок статьи",
    "Текст статьи",
    "Статус",
    "Запланировано",
    "Опубликовано",
    "ID сообщения Telegram",
    "Ошибка",
    "Обновлено",
]
KNOWLEDGE_HEADERS = [
    "ID",
    "Раздел",
    "Владелец источника",
    "Название",
    "URL источника",
    "Обозначение документа",
    "Редакция / изменение",
    "Статус",
    "Обнаружено",
]


def _book():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON не настроен")
    credentials = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    sheet_id = (
        os.getenv("DZEN_SHEET_ID", "").strip()
        or os.getenv("GOOGLE_SHEET_ID", "").strip()
        or DEFAULT_SHEET_ID
    )
    return gspread.authorize(credentials).open_by_key(sheet_id)


def _worksheet(book, title: str, headers: list[str]):
    try:
        worksheet = book.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = book.add_worksheet(title=title, rows=2000, cols=len(headers))
    if worksheet.row_values(1) != headers:
        worksheet.update(range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(headers))}", values=[headers])
        worksheet.freeze(rows=1)
    return worksheet


def restore_queue() -> int:
    """Restore dedupe keys and publication state after a Render redeploy."""
    book = _book()
    worksheet = _worksheet(book, QUEUE_SHEET, QUEUE_HEADERS)
    records = worksheet.get_all_records(expected_headers=QUEUE_HEADERS)
    connection = _connect()
    restored = 0
    now = datetime.now(MOSCOW).isoformat()
    try:
        for record in records:
            source_url = str(record.get("URL источника", "")).strip()
            if not source_url:
                continue
            import hashlib

            source_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO dzen_articles
                (source, source_url, source_hash, source_title, article_title,
                 article_text, status, scheduled_at, published_at,
                 telegram_message_id, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.get("Источник", "")),
                    source_url,
                    source_hash,
                    str(record.get("Исходный заголовок", "")),
                    str(record.get("Заголовок статьи", "")),
                    str(record.get("Текст статьи", "")),
                    str(record.get("Статус", "prepared")) or "prepared",
                    str(record.get("Запланировано", "")) or None,
                    str(record.get("Опубликовано", "")) or None,
                    int(record["ID сообщения Telegram"])
                    if str(record.get("ID сообщения Telegram", "")).isdigit()
                    else None,
                    str(record.get("Ошибка", "")),
                    now,
                    str(record.get("Обновлено", "")) or now,
                ),
            )
            restored += int(bool(cursor.rowcount))
        connection.commit()
    finally:
        connection.close()
    return restored


def sync_all() -> dict:
    book = _book()
    queue_ws = _worksheet(book, QUEUE_SHEET, QUEUE_HEADERS)
    knowledge_ws = _worksheet(book, KNOWLEDGE_SHEET, KNOWLEDGE_HEADERS)
    connection = _connect()
    _ensure_table(connection)
    try:
        queue_rows = [
            [
                row["id"],
                row["source"],
                row["source_url"],
                row["source_title"],
                row["article_title"],
                row["article_text"],
                row["status"],
                row["scheduled_at"] or "",
                row["published_at"] or "",
                row["telegram_message_id"] or "",
                row["error"],
                row["updated_at"],
            ]
            for row in connection.execute(
                "SELECT * FROM dzen_articles ORDER BY id DESC LIMIT 1000"
            ).fetchall()
        ]
        knowledge_rows = [
            [
                row["id"],
                row["category"],
                row["publisher"],
                row["title"],
                row["source_url"],
                row["document_code"],
                row["revision_hint"],
                row["status"],
                row["discovered_at"],
            ]
            for row in connection.execute(
                "SELECT * FROM dzen_knowledge ORDER BY id DESC LIMIT 1500"
            ).fetchall()
        ]
    finally:
        connection.close()
    queue_ws.clear()
    queue_ws.update(range_name="A1", values=[QUEUE_HEADERS, *queue_rows])
    queue_ws.freeze(rows=1)
    knowledge_ws.clear()
    knowledge_ws.update(range_name="A1", values=[KNOWLEDGE_HEADERS, *knowledge_rows])
    knowledge_ws.freeze(rows=1)
    return {"queue": len(queue_rows), "knowledge": len(knowledge_rows)}


def enabled() -> bool:
    return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())

