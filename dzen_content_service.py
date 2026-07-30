"""Autonomous construction-news queue for the Dzen channel.

Only two news sources are accepted:
* https://ancb.ru/
* https://www.nopriz.ru/

The module is deliberately isolated from the calculator and Telegram lead
parser.  It can ingest and prepare drafts without a publishing channel.  When
``DZEN_TG_CHANNEL`` is configured, due articles are sent to that public
Telegram channel and Dzen's official ``zen_sync_bot`` can cross-post them.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


MOSCOW = ZoneInfo("Europe/Moscow")
ALLOWED_HOSTS = {"ancb.ru", "www.ancb.ru", "nopriz.ru", "www.nopriz.ru"}
SOURCE_PAGES = (
    ("АНСБ", "https://ancb.ru/"),
    ("НОПРИЗ", "https://www.nopriz.ru/"),
)
SLOTS = ((8, 30), (13, 0), (19, 0))
USER_AGENT = (
    "Mozilla/5.0 (compatible; AR-Farvater-Dzen/1.0; "
    "+https://artemnikitin-dev.onrender.com/)"
)
TELEGRAM_CAPTION_LIMIT = 1024
PUBLICATION_TEXT_LIMIT = 1000
LONGFORM_MIN_CHARS = 6000
CONTACT_FOOTER = (
    "АР-ФАРВАТЕР\n"
    "Сайт: https://ar-farvater.ru/\n"
    "Почта: nzzk@mail.ru\n"
    "Телефон: +7 916 727-36-87"
)
DEFAULT_ARTICLE_IMAGES = (
    "https://ar-farvater.ru/media/image-cache/slideshow/resize1758111504038.60d3e98b.jpg",
    "https://ar-farvater.ru/media/image-cache/site/resize1758117936909.57e3d5ab.jpg",
)


@dataclass(frozen=True)
class SourceItem:
    source: str
    title: str
    url: str
    published_at: str = ""
    image_url: str = ""


def _database_path() -> str:
    configured = os.getenv("DZEN_DB_PATH", "").strip()
    if configured:
        return configured
    if os.path.isdir("/var/data"):
        return "/var/data/dzen_content.db"
    return os.path.join(os.path.dirname(__file__), "dzen_content.db")


def _connect() -> sqlite3.Connection:
    path = _database_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS dzen_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL UNIQUE,
            source_hash TEXT NOT NULL UNIQUE,
            source_title TEXT NOT NULL,
            source_date TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '',
            article_title TEXT NOT NULL DEFAULT '',
            article_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            scheduled_at TEXT,
            published_at TEXT,
            telegram_message_id INTEGER,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _clean_text(value: str) -> str:
    value = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in ALLOWED_HOSTS


def _request(url: str) -> requests.Response:
    if not _allowed_url(url):
        raise ValueError("Источник не входит в разрешённый список")
    response = requests.get(
        url,
        timeout=25,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    )
    response.raise_for_status()
    return response


def _looks_like_news(title: str, url: str) -> bool:
    text = f"{title} {url}".lower()
    if len(title) < 18:
        return False
    blocked = (
        "javascript:",
        "mailto:",
        "личный кабинет",
        "контакты",
        "политика конфиденциальности",
        "карта сайта",
        "об ассоциации",
        "членство",
    )
    return not any(part in text for part in blocked)


def discover_source_items(limit_per_source: int = 30) -> list[SourceItem]:
    """Collect likely news links in page order, with strict domain filtering."""
    discovered: list[SourceItem] = []
    seen: set[str] = set()
    for source_name, page_url in SOURCE_PAGES:
        try:
            soup = BeautifulSoup(_request(page_url).text, "lxml")
        except Exception:
            continue
        for anchor in soup.select("a[href]"):
            title = _clean_text(anchor.get_text(" ", strip=True))
            url = urljoin(page_url, anchor.get("href", "")).split("#", 1)[0]
            if url in seen or not _allowed_url(url) or not _looks_like_news(title, url):
                continue
            path = urlparse(url).path.lower()
            if source_name == "АНСБ":
                likely = any(marker in path for marker in ("/publication/", "/news/", "/read/"))
            else:
                likely = any(marker in path for marker in ("/news/", "/press/", "/publication/"))
            if not likely:
                continue
            seen.add(url)
            discovered.append(SourceItem(source=source_name, title=title, url=url))
            if sum(1 for item in discovered if item.source == source_name) >= limit_per_source:
                break
    return discovered


def _extract_article(item: SourceItem) -> tuple[str, list[str], str, str]:
    soup = BeautifulSoup(_request(item.url).text, "lxml")
    for node in soup.select("script, style, nav, footer, form, aside"):
        node.decompose()
    title_node = soup.select_one("h1")
    title = _clean_text(title_node.get_text(" ", strip=True) if title_node else item.title)
    image = ""
    image_node = soup.select_one('meta[property="og:image"]')
    if image_node:
        image = urljoin(item.url, image_node.get("content", ""))
    date = ""
    date_node = soup.select_one("time[datetime], meta[property='article:published_time']")
    if date_node:
        date = _clean_text(date_node.get("datetime") or date_node.get("content") or "")
    container = soup.select_one("article") or soup.select_one("main") or soup.body
    paragraphs: list[str] = []
    if container:
        for node in container.select("p, li"):
            text = _clean_text(node.get_text(" ", strip=True))
            if 45 <= len(text) <= 1200 and text not in paragraphs:
                paragraphs.append(text)
            if len(paragraphs) >= 18:
                break
    return title, paragraphs, date, image


def _sentences(paragraphs: Iterable[str]) -> list[str]:
    result: list[str] = []
    for paragraph in paragraphs:
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            sentence = _clean_text(sentence)
            if 55 <= len(sentence) <= 360 and sentence not in result:
                result.append(sentence)
    return result


def _seo_title(source_title: str) -> str:
    title = re.sub(r"\s*[|—-]\s*(АНСБ|НОПРИЗ).*$", "", source_title, flags=re.I)
    title = _clean_text(title).rstrip(".")
    if len(title) > 118:
        title = title[:115].rsplit(" ", 1)[0] + "…"
    return title


def build_article(item: SourceItem) -> tuple[str, str, str, str]:
    """Create a factual, compact article without inventing figures or quotes."""
    source_title, paragraphs, source_date, image = _extract_article(item)
    sentences = _sentences(paragraphs)
    if len(sentences) < 3:
        raise ValueError("В источнике недостаточно связного текста")
    title = _seo_title(source_title)
    lead = sentences[0]
    facts = sentences[1:5]
    implications = sentences[5:8] or sentences[2:4]
    body = [
        title,
        "",
        lead,
        "",
        "Что произошло",
        " ".join(facts[:2]),
        "",
        "Почему это важно для стройки",
        " ".join(implications[:2]),
        "",
        "Практический вывод",
        (
            "Участникам проекта стоит проверить, меняет ли эта новость сроки, "
            "состав документации, требования к подрядчику или бюджет. "
            "Здесь полезнее один своевременный вопрос, чем три поздних допсоглашения."
        ),
        "",
        (
            "Наблюдение Артёма Никитина: стройка редко проигрывает из-за одного "
            "громкого решения. Чаще она теряет деньги на мелочах, которые заметили "
            "слишком поздно. Поэтому смотрим не только на заголовок, но и на последствия."
        ),
        "",
        f"Источник: {item.source}",
        item.url,
        "",
        "Автор: Артём Никитин, генеральный директор «АР-ФАРВАТЕР».",
    ]
    text = "\n".join(body)
    if len(text) > 4050:
        text = text[:3980].rsplit(" ", 1)[0] + f"\n\nИсточник: {item.source}\n{item.url}"
    return title, text, source_date, image


def ingest() -> dict:
    added = prepared = errors = 0
    connection = _connect()
    now = datetime.now(MOSCOW).isoformat()
    try:
        for item in discover_source_items():
            source_hash = hashlib.sha256(item.url.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO dzen_articles
                (source, source_url, source_hash, source_title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item.source, item.url, source_hash, item.title, now, now),
            )
            if not cursor.rowcount:
                continue
            added += 1
            article_id = cursor.lastrowid
            try:
                title, text, source_date, image = build_article(item)
                connection.execute(
                    """
                    UPDATE dzen_articles
                    SET article_title=?, article_text=?, source_date=?, image_url=?,
                        status='prepared', updated_at=?
                    WHERE id=?
                    """,
                    (title, text, source_date, image, now, article_id),
                )
                prepared += 1
            except Exception as exc:
                connection.execute(
                    "UPDATE dzen_articles SET status='error', error=?, updated_at=? WHERE id=?",
                    (str(exc)[:500], now, article_id),
                )
                errors += 1
        connection.commit()
    finally:
        connection.close()
    schedule_queue()
    return {"added": added, "prepared": prepared, "errors": errors}


def _future_slots(count: int) -> list[datetime]:
    now = datetime.now(MOSCOW)
    result: list[datetime] = []
    day = now.date()
    while len(result) < count:
        for hour, minute in SLOTS:
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=MOSCOW)
            if candidate > now + timedelta(minutes=2):
                result.append(candidate)
                if len(result) >= count:
                    break
        day += timedelta(days=1)
    return result


def schedule_queue(target_size: int = 21) -> int:
    connection = _connect()
    try:
        # Rebalance an already-filled news queue after technical/project material
        # becomes available. This also repairs queues restored from Sheets.
        scheduled_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, source, scheduled_at
                FROM dzen_articles
                WHERE status='scheduled' AND scheduled_at IS NOT NULL
                ORDER BY scheduled_at
                """
            ).fetchall()
        ]
        prepared_pool = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, source
                FROM dzen_articles
                WHERE status='prepared'
                ORDER BY COALESCE(source_date, '') DESC, id DESC
                """
            ).fetchall()
        ]
        for current_row in scheduled_rows:
            slot = datetime.fromisoformat(current_row["scheduled_at"])
            if slot.hour < 11:
                preferred = {"АНСБ", "НОПРИЗ"}
            elif slot.hour < 17:
                preferred = {"technical", "regulation"}
            else:
                preferred = {"major_project"}
            if current_row["source"] in preferred:
                continue
            replacement_index = next(
                (
                    index
                    for index, row in enumerate(prepared_pool)
                    if row["source"] in preferred
                ),
                None,
            )
            if replacement_index is None:
                continue
            replacement = prepared_pool.pop(replacement_index)
            connection.execute(
                """
                UPDATE dzen_articles
                SET status='prepared', scheduled_at=NULL, updated_at=?
                WHERE id=?
                """,
                (datetime.now(MOSCOW).isoformat(), current_row["id"]),
            )
            connection.execute(
                """
                UPDATE dzen_articles
                SET status='scheduled', scheduled_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    current_row["scheduled_at"],
                    datetime.now(MOSCOW).isoformat(),
                    replacement["id"],
                ),
            )
            prepared_pool.append(
                {"id": current_row["id"], "source": current_row["source"]}
            )
        connection.commit()

        scheduled_count = connection.execute(
            "SELECT COUNT(*) FROM dzen_articles WHERE status='scheduled'"
        ).fetchone()[0]
        needed = max(0, target_size - scheduled_count)
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, source FROM dzen_articles
                WHERE status='prepared'
                ORDER BY COALESCE(source_date, '') DESC, id DESC
                """
            ).fetchall()
        ]
        if not rows:
            return 0
        occupied = {
            row[0]
            for row in connection.execute(
                "SELECT scheduled_at FROM dzen_articles WHERE status='scheduled'"
            ).fetchall()
        }
        slots = [
            slot
            for slot in _future_slots(target_size + len(occupied))
            if slot.isoformat() not in occupied
        ]
        now = datetime.now(MOSCOW).isoformat()
        selected: list[tuple[dict, datetime]] = []
        remaining = list(rows)
        for slot in slots:
            if len(selected) >= needed or not remaining:
                break
            if slot.hour < 11:
                preferred = {"АНСБ", "НОПРИЗ"}
            elif slot.hour < 17:
                preferred = {"technical", "regulation"}
            else:
                preferred = {"major_project"}
            index = next(
                (i for i, row in enumerate(remaining) if row["source"] in preferred),
                0,
            )
            selected.append((remaining.pop(index), slot))
        for row, slot in selected:
            connection.execute(
                "UPDATE dzen_articles SET status='scheduled', scheduled_at=?, updated_at=? WHERE id=?",
                (slot.isoformat(), now, row["id"]),
            )
        connection.commit()
        return len(selected)
    finally:
        connection.close()


def _telegram_token() -> str:
    return (
        os.getenv("DZEN_TG_BOT_TOKEN", "").strip()
        or os.getenv("TG_BOT_TOKEN", "").strip()
    )


def _configured_default_images() -> list[str]:
    configured = [
        value.strip()
        for value in os.getenv("DZEN_DEFAULT_IMAGE_URLS", "").split(",")
        if value.strip()
    ]
    return configured or list(DEFAULT_ARTICLE_IMAGES)


def _valid_public_image_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _article_images(source_url: str, primary_image: str = "") -> list[str]:
    """Return two images: a source photo plus a branded construction fallback."""
    images: list[str] = []

    def add(value: str) -> None:
        value = str(value or "").strip()
        lowered = value.lower()
        if (
            _valid_public_image_url(value)
            and value not in images
            and not lowered.endswith(".svg")
            and not any(marker in lowered for marker in ("favicon", "avatar", "icon-"))
        ):
            images.append(value)

    add(primary_image)
    if len(images) < 2 and _allowed_url(source_url):
        try:
            soup = BeautifulSoup(_request(source_url).text, "lxml")
            for node in soup.select(
                'meta[property="og:image"], article img[src], main img[src]'
            ):
                add(urljoin(source_url, node.get("content") or node.get("src") or ""))
                if len(images) >= 2:
                    break
        except Exception:
            pass
    for value in _configured_default_images():
        add(value)
        if len(images) >= 2:
            break
    return images[:2]


def _publication_text(
    title: str,
    text: str,
    source: str,
    source_url: str,
) -> str:
    """Keep a heading, source and full company contacts within caption limits."""
    title = _clean_text(title)
    body = str(text or "").strip()
    if title and not body.startswith(title):
        body = f"{title}\n\n{body}"

    # Replace the old source/author tail with one predictable footer.
    if source_url and source_url in body:
        source_position = body.rfind(source_url)
        previous_line = body.rfind("\n", 0, source_position)
        source_label_line = body.rfind("\n", 0, max(previous_line, 0))
        if source_label_line >= 0:
            body = body[:source_label_line].rstrip()

    footer = f"\n\nИсточник: {source}\n{source_url}\n\n{CONTACT_FOOTER}"
    available = PUBLICATION_TEXT_LIMIT - len(footer)
    if len(body) > available:
        body = body[: max(0, available - 1)].rsplit(" ", 1)[0].rstrip() + "…"
    result = body + footer
    if len(result) > TELEGRAM_CAPTION_LIMIT:
        raise ValueError("Telegram caption exceeds the photo-post limit")
    return result


def _send_telegram(text: str, image_urls: list[str]) -> int:
    token = _telegram_token()
    channel = os.getenv("DZEN_TG_CHANNEL", "").strip()
    if not token or not channel:
        raise RuntimeError("DZEN_TG_CHANNEL или токен Telegram не настроен")
    if len(image_urls) < 2:
        raise RuntimeError("Для статьи не удалось подобрать две фотографии")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMediaGroup",
        json={
            "chat_id": channel,
            "media": [
                {
                    "type": "photo",
                    "media": image_urls[0],
                    "caption": text,
                },
                {
                    "type": "photo",
                    "media": image_urls[1],
                },
            ],
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    result = payload.get("result") or []
    if not result:
        raise RuntimeError("Telegram API returned an empty media group")
    return int(result[0]["message_id"])


def publish_due() -> dict:
    if os.getenv("DZEN_AUTOPUBLISH", "false").lower() not in {"1", "true", "yes"}:
        return {"published": 0, "disabled": True}
    connection = _connect()
    published = failed = 0
    try:
        now = datetime.now(MOSCOW)
        rows = connection.execute(
            """
            SELECT id, source, source_url, article_title, article_text, image_url
            FROM dzen_articles
            WHERE status='scheduled' AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            LIMIT 3
            """,
            (now.isoformat(),),
        ).fetchall()
        for row in rows:
            try:
                if len(str(row["article_text"] or "").strip()) < LONGFORM_MIN_CHARS:
                    connection.execute(
                        """
                        UPDATE dzen_articles
                        SET status='needs_rewrite', scheduled_at=NULL,
                            error='Материал короче стандарта лонгрида', updated_at=?
                        WHERE id=?
                        """,
                        (now.isoformat(), row["id"]),
                    )
                    failed += 1
                    continue
                publication_text = _publication_text(
                    row["article_title"],
                    row["article_text"],
                    row["source"],
                    row["source_url"],
                )
                image_urls = _article_images(row["source_url"], row["image_url"])
                message_id = _send_telegram(publication_text, image_urls)
                connection.execute(
                    """
                    UPDATE dzen_articles
                    SET status='published', published_at=?, telegram_message_id=?,
                        error='', updated_at=?
                    WHERE id=?
                    """,
                    (now.isoformat(), message_id, now.isoformat(), row["id"]),
                )
                published += 1
            except Exception as exc:
                connection.execute(
                    "UPDATE dzen_articles SET error=?, updated_at=? WHERE id=?",
                    (str(exc)[:500], now.isoformat(), row["id"]),
                )
                failed += 1
        connection.commit()
    finally:
        connection.close()
    return {"published": published, "failed": failed}


def status(limit: int = 20) -> dict:
    connection = _connect()
    try:
        totals = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM dzen_articles GROUP BY status"
            ).fetchall()
        }
        rows = connection.execute(
            """
            SELECT id, source, source_url, article_title, status, scheduled_at,
                   published_at, error
            FROM dzen_articles ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
        return {"totals": totals, "items": [dict(row) for row in rows]}
    finally:
        connection.close()


def _worker() -> None:
    next_ingest = 0.0
    next_knowledge_ingest = 0.0
    next_sheet_sync = 0.0
    sheets_restored = False
    while True:
        try:
            current = time.time()
            if not sheets_restored:
                try:
                    from dzen_google_store import enabled, restore_queue

                    if enabled():
                        restore_queue()
                except Exception as exc:
                    print("Dzen queue restore:", str(exc)[:500], flush=True)
                finally:
                    # A temporary Google quota error must never stop ingestion.
                    sheets_restored = True
            if current >= next_ingest:
                ingest()
                next_ingest = current + 30 * 60
            if current >= next_knowledge_ingest:
                from dzen_knowledge import ingest_knowledge, prepare_knowledge_articles

                ingest_knowledge()
                prepare_knowledge_articles()
                schedule_queue()
                next_knowledge_ingest = current + 6 * 60 * 60
            publish_due()
            if current >= next_sheet_sync:
                try:
                    from dzen_google_store import enabled, sync_all

                    if enabled():
                        sync_all()
                except Exception as exc:
                    print("Dzen sheet sync:", str(exc)[:500], flush=True)
                finally:
                    next_sheet_sync = current + 60 * 60
        except Exception as exc:
            print("Dzen content worker:", str(exc)[:500], flush=True)
        time.sleep(45)


_worker_started = False
_worker_lock = threading.Lock()


def start_dzen_content_worker() -> bool:
    global _worker_started
    if os.getenv("DZEN_CONTENT_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return False
    with _worker_lock:
        if _worker_started:
            return True
        thread = threading.Thread(target=_worker, name="dzen-content-worker", daemon=True)
        thread.start()
        _worker_started = True
    return True


def install_routes(app) -> None:
    @app.get("/dzen-content/status")
    def dzen_content_status():
        from flask import jsonify, request

        limit = request.args.get("limit", "20")
        try:
            limit_value = int(limit)
        except ValueError:
            limit_value = 20
        return jsonify(status(limit_value))

    @app.get("/dzen-content/knowledge")
    def dzen_knowledge_status():
        from flask import jsonify, request
        from dzen_knowledge import knowledge_status

        try:
            limit_value = int(request.args.get("limit", "30"))
        except ValueError:
            limit_value = 30
        return jsonify(knowledge_status(limit_value))

    @app.post("/dzen-content/run")
    def dzen_content_run():
        from flask import jsonify, request

        expected = os.getenv("DZEN_ADMIN_TOKEN", "").strip()
        supplied = request.headers.get("X-Dzen-Admin-Token", "")
        if not expected or supplied != expected:
            return jsonify({"error": "forbidden"}), 403
        from dzen_knowledge import ingest_knowledge, prepare_knowledge_articles
        from dzen_google_store import enabled, sync_all

        result = {
            "ingest": ingest(),
            "knowledge": ingest_knowledge(),
            "technical_articles": prepare_knowledge_articles(),
            "publish": publish_due(),
        }
        if enabled():
            result["sheets"] = sync_all()
        return jsonify(result)
