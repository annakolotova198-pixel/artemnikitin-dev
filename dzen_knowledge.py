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

FALLBACK_TOPICS = (
    ("technical", "ГОСТ 27751-2014", "Надёжность строительных конструкций и оснований",
     "Разбираем, как требования к надёжности связывают проектные решения, расчётные ситуации, ответственность участников и контроль на стройке."),
    ("technical", "СП 20.13330.2016", "Нагрузки и воздействия",
     "Практический разбор нагрузок, сочетаний и исходных данных, которые нельзя переносить из похожего проекта без проверки."),
    ("technical", "СП 63.13330.2018", "Бетонные и железобетонные конструкции",
     "Что проверять в расчёте, армировании, защитном слое и документации на железобетонные конструкции."),
    ("technical", "СП 70.13330.2012", "Несущие и ограждающие конструкции",
     "Контроль монтажа, бетонирования, сварных соединений и исполнительной документации на площадке."),
    ("technical", "СП 28.13330.2017", "Защита строительных конструкций от коррозии",
     "Как условия эксплуатации влияют на защиту бетона и металла и почему покрытие нельзя выбирать только по цене."),
    ("technical", "СП 22.13330.2016", "Основания зданий и сооружений",
     "Связь инженерных изысканий, расчёта основания, осадок и решений по фундаментам."),
    ("technical", "СП 45.13330.2017", "Земляные сооружения, основания и фундаменты",
     "Что контролировать при разработке грунта, подготовке основания, обратной засыпке и приёмке работ."),
    ("technical", "СП 48.13330.2019", "Организация строительства",
     "Как связаны подготовка производства, стройгенплан, контроль, исполнительная документация и безопасность работ."),
    ("technical", "СП 16.13330.2017", "Стальные конструкции",
     "Проверяем расчётную схему, устойчивость, соединения, изготовление и монтаж металлических конструкций."),
    ("technical", "ГОСТ 34028-2016", "Прокат арматурный для железобетонных конструкций",
     "Как читать обозначение арматуры и сверять класс, геометрию, документы качества и фактическую поставку."),
    ("technical", "ГОСТ 13015-2012", "Изделия бетонные и железобетонные для строительства",
     "Маркировка, приёмка, хранение, транспортирование и документы качества сборного железобетона."),
    ("technical", "ГОСТ 7473-2010", "Смеси бетонные",
     "Что должно быть в документе о качестве бетонной смеси и какие параметры проверять при поставке."),
    ("technical", "ГОСТ 26633-2015", "Бетоны тяжёлые и мелкозернистые",
     "Классы, марки и показатели бетона: как не перепутать требование проекта с коммерческим названием поставщика."),
    ("technical", "ГОСТ Р 21.101-2020", "Основные требования к проектной и рабочей документации",
     "Как читать комплект, изменения, ссылки и спецификации так, чтобы закупка не работала по устаревшему листу."),
    ("technical", "СП 71.13330.2017", "Изоляционные и отделочные покрытия",
     "Требования к основанию, условиям производства и контролю качества изоляционных и отделочных работ."),
    ("major_project", "Кампус МГСУ", "Кампус мирового уровня МГСУ",
     "Разбор крупного университетского строительного проекта: функции кампуса, инженерная инфраструктура, этапность и организация площадки."),
    ("major_project", "Кампус Екатеринбурга", "Межвузовский кампус мирового уровня в Екатеринбурге",
     "Смотрим на кампус как на комплекс зданий и городской инфраструктуры, а не как на один учебный корпус."),
    ("major_project", "Кампус Перми", "Межвузовский кампус мирового уровня в Перми",
     "Какие задачи ставит строительство кампуса перед проектировщиками, генподрядчиком, снабжением и городской сетью."),
    ("major_project", "НКЦ", "Национальный космический центр в Москве",
     "Технический обзор большого общественно-делового комплекса: конструктив, инженерные системы, логистика и интеграция в город."),
    ("major_project", "ВСМ Москва — Санкт-Петербург", "Высокоскоростная магистраль Москва — Санкт-Петербург",
     "Разбираем мегапроект через земляное полотно, искусственные сооружения, станции, энергетику и управление сроками."),
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
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(dzen_knowledge)").fetchall()
    }
    if "summary" not in columns:
        connection.execute(
            "ALTER TABLE dzen_knowledge ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
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
        if added == 0 and errors == len(SOURCES):
            for category, code, title, summary in FALLBACK_TOPICS:
                anchor = re.sub(r"[^a-zа-я0-9]+", "-", code.lower()).strip("-")
                url = f"https://www.minstroyrf.gov.ru/docs/#{anchor}"
                source_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO dzen_knowledge
                    (category, publisher, title, source_url, source_hash,
                     document_code, revision_hint, summary, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        category,
                        "Нормативная библиотека «АР-ФАРВАТЕР»",
                        title,
                        url,
                        source_hash,
                        code,
                        summary,
                        now,
                    ),
                )
                added += int(bool(cursor.rowcount))
        connection.commit()
    finally:
        connection.close()
    return {"added": added, "source_errors": errors}


def _page_facts(url: str, limit: int = 10) -> list[str]:
    """Extract short factual paragraphs without copying a whole publication."""
    response = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    for node in soup.select("script, style, nav, footer, form, aside"):
        node.decompose()
    container = soup.select_one("article") or soup.select_one("main") or soup.body
    facts: list[str] = []
    if container:
        for node in container.select("p, li"):
            text = _clean_text(node.get_text(" ", strip=True))
            if 70 <= len(text) <= 600 and text not in facts:
                facts.append(text)
            if len(facts) >= limit:
                break
    return facts


def _knowledge_article(row) -> tuple[str, str]:
    try:
        facts = _page_facts(row["source_url"])
    except Exception:
        facts = []
    if row["summary"] and row["summary"] not in facts:
        facts.insert(0, row["summary"])
    if len(facts) == 1:
        facts.append(
            "Материал построен как рабочий чек-лист: без подмены полного текста "
            "норматива и без выдуманных пунктов или числовых требований."
        )
    if len(facts) < 2:
        raise ValueError("Недостаточно фактов для технического разбора")

    code = row["document_code"] or row["title"]
    if row["category"] in {"technical", "regulation"}:
        title = f"{code}: что проверить на стройке до начала работ"
        sections = [
            title, "", facts[0], "",
            "Что регулирует документ", " ".join(facts[1:3]), "",
            "Где чаще всего возникает ошибка",
            (
                "Проблема обычно начинается не на площадке, а раньше: в проекте, "
                "ведомости объёмов или закупке. Поэтому сверять нужно не только "
                "исполнение, но и исходное задание, узел, спецификацию и актуальную редакцию."
            ),
            "", "Чек-лист для проекта и снабжения",
            "1. Проверить обозначение и действующую редакцию документа.",
            "2. Найти требования, относящиеся именно к вашему узлу и условиям эксплуатации.",
            "3. Сверить проект, спецификацию, сертификаты и исполнительную документацию.",
            "4. Зафиксировать расхождения до закупки и монтажа.", "",
            (
                "Важно: это практический обзор, а не замена нормативного документа. "
                "Перед решением проверьте полный текст и статус редакции в официальном источнике."
            ),
        ]
    else:
        title = f"{row['title']}: как устроен крупный строительный проект"
        sections = [
            title, "", facts[0], "", "Что строят", " ".join(facts[1:3]), "",
            "На что смотреть профессионалу",
            " ".join(facts[3:5]) if len(facts) > 3 else (
                "Для оценки проекта важны не только площадь и стоимость: смотрим "
                "на этапность, инженерную инфраструктуру, логистику площадки, "
                "сроки и связь объекта с городской средой."
            ),
            "", "Практический вывод",
            (
                "Большой объект — это всегда соревнование не одного подрядчика, а всей "
                "цепочки решений. Чем раньше проектировщик, снабжение и производство "
                "синхронизируют данные, тем меньше дорогих сюрпризов появляется на монтаже."
            ),
        ]

    sections.extend([
        "", f"Источник: {row['publisher']}", row["source_url"], "",
        "Автор: Артём Никитин, генеральный директор «АР-ФАРВАТЕР».",
    ])
    text = "\n".join(sections)
    if len(text) > 4050:
        text = text[:3900].rsplit(" ", 1)[0] + (
            f"\n\nИсточник: {row['publisher']}\n{row['source_url']}"
        )
    return title[:140], text


def prepare_knowledge_articles(limit: int = 30) -> dict:
    """Promote indexed official sources into the common publishing queue."""
    connection = _connect()
    _ensure_table(connection)
    prepared = errors = 0
    now = datetime.now(MOSCOW).isoformat()
    try:
        rows = connection.execute(
            """
            SELECT id, category, publisher, title, source_url, source_hash,
                   document_code, revision_hint, summary
            FROM dzen_knowledge
            WHERE status='indexed'
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
        for row in rows:
            try:
                title, text = _knowledge_article(row)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO dzen_articles
                    (source, source_url, source_hash, source_title, article_title,
                     article_text, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                    """,
                    (
                        row["category"], row["source_url"], row["source_hash"],
                        row["title"], title, text, now, now,
                    ),
                )
                prepared += int(bool(cursor.rowcount))
                connection.execute(
                    "UPDATE dzen_knowledge SET status='prepared', error='' WHERE id=?",
                    (row["id"],),
                )
            except Exception as exc:
                errors += 1
                connection.execute(
                    "UPDATE dzen_knowledge SET status='error', error=? WHERE id=?",
                    (str(exc)[:500], row["id"]),
                )
        connection.commit()
    finally:
        connection.close()
    return {"prepared": prepared, "errors": errors}


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
