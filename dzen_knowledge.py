"""Parsers for the technical and major-project knowledge library.

This is intentionally separate from the ANCB/NOPRIZ news parser.  It indexes
official sources, stores metadata and extracts only short factual fragments.
Full standards are not mirrored: edition, status and exact wording must always
be checked against the linked official document before publication.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from dzen_content_service import MOSCOW, USER_AGENT, _clean_text, _connect


@dataclass(frozen=True)
class KnowledgeSource:
    category: str
    publisher: str
    url: str
    allowed_hosts: tuple[str, ...]
    markers: tuple[str, ...]


SOURCES = (
    KnowledgeSource(
        category="technical",
        publisher="Минстрой России",
        url="https://minstroyrf.gov.ru/docs/?t%5B0%5D=60",
        allowed_hosts=("minstroyrf.gov.ru", "www.minstroyrf.gov.ru"),
        markers=(
            "гост",
            "сп ",
            "свод правил",
            "изменение №",
            "конструкц",
            "нагрузк",
            "бетон",
            "железобетон",
            "фундамент",
            "свар",
            "монтаж",
            "проектирован",
        ),
    ),
    KnowledgeSource(
        category="regulation",
        publisher="Официальное опубликование правовых актов",
        url="https://publication.pravo.gov.ru/documents/block/foiv274",
        allowed_hosts=("publication.pravo.gov.ru",),
        markers=(
            "строитель",
            "проектн",
            "инженерн",
            "смет",
            "подряд",
            "материал",
            "экспертиз",
            "градостро",
        ),
    ),
    KnowledgeSource(
        category="major_project",
        publisher="Минстрой России",
        url="https://minstroyrf.gov.ru/press/",
        allowed_hosts=("minstroyrf.gov.ru", "www.minstroyrf.gov.ru"),
        markers=(
            "кампус",
            "мост",
            "метро",
            "метрополитен",
            "аэропорт",
            "порт ",
            "тоннел",
            "промышлен",
            "инфраструктур",
            "научн",
            "университет",
            "транспортн",
        ),
    ),
    KnowledgeSource(
        category="major_project",
        publisher="Минобрнауки России",
        url="https://minobrnauki.gov.ru/press-center/news/",
        allowed_hosts=("minobrnauki.gov.ru", "www.minobrnauki.gov.ru"),
        markers=("кампус", "научн", "университет", "лаборатор", "строитель"),
    ),
)


def _ensure_table(connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS dzen_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            publisher TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT NOT NULL UNIQUE,
            source_hash TEXT NOT NULL UNIQUE,
            document_code TEXT NOT NULL DEFAULT '',
            revision_hint TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'indexed',
            discovered_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.commit()


def _allowed(url: str, hosts: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in hosts


def _document_code(title: str) -> str:
    patterns = (
        r"\bГОСТ(?:\s+Р)?\s+\d+(?:[.\-]\d+)*(?:-\d{2,4})?\b",
        r"\bСП\s+\d+(?:\.\d+)+(?:\.\d+)?\b",
        r"\bСНиП\s+[IVXLC\d.\-*]+\b",
        r"\b(?:приказ|постановление)\s+[^№]{0,30}№\s*[\d/-]+\b",
    )
    for pattern in patterns:
        match = re.search(pattern, title, flags=re.I)
        if match:
            return _clean_text(match.group(0))
    return ""


def _revision_hint(title: str) -> str:
    match = re.search(r"\b(?:изменени[ея]|редакци[яи])\s*№?\s*\d+\b", title, flags=re.I)
    return _clean_text(match.group(0)) if match else ""


def _discover(source: KnowledgeSource, limit: int = 40):
    response = requests.get(
        source.url,
        timeout=30,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    seen: set[str] = set()
    count = 0
    for anchor in soup.select("a[href]"):
        title = _clean_text(anchor.get_text(" ", strip=True))
        if len(title) < 24:
            continue
        lowered = title.lower().replace("ё", "е")
        if not any(marker in lowered for marker in source.markers):
            continue
        url = urljoin(source.url, anchor.get("href", "")).split("#", 1)[0]
        if url in seen or not _allowed(url, source.allowed_hosts):
            continue
        seen.add(url)
        yield {
            "category": source.category,
            "publisher": source.publisher,
            "title": title,
            "url": url,
            "document_code": _document_code(title),
            "revision_hint": _revision_hint(title),
        }
        count += 1
        if count >= limit:
            return


def ingest_knowledge() -> dict:
    connection = _connect()
    _ensure_table(connection)
    now = datetime.now(MOSCOW).isoformat()
    added = errors = 0
    try:
        for source in SOURCES:
            try:
                items = list(_discover(source))
            except Exception:
                errors += 1
                continue
            for item in items:
                source_hash = hashlib.sha256(item["url"].encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO dzen_knowledge
                    (category, publisher, title, source_url, source_hash,
                     document_code, revision_hint, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["category"],
                        item["publisher"],
                        item["title"],
                        item["url"],
                        source_hash,
                        item["document_code"],
                        item["revision_hint"],
                        now,
                    ),
                )
                added += int(bool(cursor.rowcount))
        connection.commit()
    finally:
        connection.close()
    return {"added": added, "source_errors": errors}


def knowledge_status(limit: int = 30) -> dict:
    connection = _connect()
    _ensure_table(connection)
    try:
        totals = {
            row["category"]: row["count"]
            for row in connection.execute(
                "SELECT category, COUNT(*) AS count FROM dzen_knowledge GROUP BY category"
            ).fetchall()
        }
        rows = connection.execute(
            """
            SELECT id, category, publisher, title, source_url, document_code,
                   revision_hint, status, discovered_at
            FROM dzen_knowledge ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
        return {"totals": totals, "items": [dict(row) for row in rows]}
    finally:
        connection.close()

