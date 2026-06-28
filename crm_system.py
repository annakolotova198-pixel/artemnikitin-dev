import os
import re
import html
import sqlite3
from functools import wraps
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, session, redirect, send_from_directory

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from docx import Document
except Exception:
    Document = None


crm_bp = Blueprint("office_crm", __name__)

UPLOAD_FOLDER = "crm_uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CRM_USERS = {
    "artem": {"password": "1234", "role": "admin"},
    "manager1": {"password": "1234", "role": "manager"},
    "manager2": {"password": "1234", "role": "manager"},
}


def db():
    return sqlite3.connect("office_crm.db")


def money(v):
    try:
        return float(str(v).replace(" ", "").replace(",", ".").replace("₽", ""))
    except Exception:
        return 0


def esc(v):
    return html.escape(str(v or ""))


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager TEXT,
            client TEXT,
            material TEXT,
            supplier TEXT,
            purchase_sum REAL DEFAULT 0,
            logistics_sum REAL DEFAULT 0,
            other_expenses REAL DEFAULT 0,
            client_sum REAL DEFAULT 0,
            production_date TEXT,
            shipment_date TEXT,
            contract_notes TEXT,
            status TEXT DEFAULT 'В работе',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS manager_settings (
            manager TEXT PRIMARY KEY,
            scheme TEXT DEFAULT 'progressive'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER,
            doc_type TEXT,
            filename TEXT,
            original_filename TEXT,
            extracted_text TEXT,
            analysis TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def commission_rate(margin, scheme):
    if margin <= 0:
        return 0
    if scheme == "simple":
        return 0.10 if margin < 1_000_000 else 0.20
    if margin < 100_000:
        return 0
    if margin < 200_000:
        return 0.10
    if margin < 400_000:
        return 0.12
    if margin < 600_000:
        return 0.14
    if margin < 800_000:
        return 0.16
    if margin < 1_000_000:
        return 0.18
    return 0.20


def get_scheme(manager):
    init_db()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT scheme FROM manager_settings WHERE manager=?", (manager,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "progressive"


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "office_user" not in session:
            return redirect("/office/login")
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("office_role") != "admin":
            return "<h1>Доступ запрещён</h1><p><a href='/office'>Назад</a></p>"
        return fn(*args, **kwargs)
    return wrapper


def extract_text_from_file(path):
    lower = path.lower()

    try:
        if lower.endswith(".pdf"):
            if PdfReader is None:
                return "PDF загружен, но pypdf не установлен"
            reader = PdfReader(path)
            result = ""
            for page in reader.pages:
                result += page.extract_text() or ""
            return result.strip()

        if lower.endswith(".docx"):
            if Document is None:
                return "DOCX загружен, но python-docx не установлен"
            doc = Document(path)
            return "\n".join([p.text for p in doc.paragraphs]).strip()

        if lower.endswith(".txt"):
            return Path(path).read_text(errors="ignore").strip()

    except Exception as e:
        return f"Ошибка чтения файла: {e}"

    return "Формат пока не поддерживается"


def find_amounts(text):
    amounts = []
    patterns = [
        r"(\d[\d\s]{2,}(?:[,.]\d{1,2})?)\s*(?:руб|₽)",
        r"итого[^0-9]{0,40}(\d[\d\s]{2,}(?:[,.]\d{1,2})?)",
        r"всего[^0-9]{0,40}(\d[\d\s]{2,}(?:[,.]\d{1,2})?)",
        r"сумма[^0-9]{0,40}(\d[\d\s]{2,}(?:[,.]\d{1,2})?)",
        r"к оплате[^0-9]{0,40}(\d[\d\s]{2,}(?:[,.]\d{1,2})?)",
    ]

    for pattern in patterns:
        for m in re.findall(pattern, text, flags=re.I):
            value = money(m)
            if value > 0:
                amounts.append(value)

    return sorted(set(amounts), reverse=True)


def find_company(text):
    patterns = [
        r'ООО\s+["«][^"»]+["»]',
        r'АО\s+["«][^"»]+["»]',
        r'ЗАО\s+["«][^"»]+["»]',
        r'ПАО\s+["«][^"»]+["»]',
        r'ИП\s+[А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z][а-яёa-z]+){0,2}',
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0)

    return ""


def analyze_document_text(doc_type, text):
    amounts = find_amounts(text)
    company = find_company(text)
    amount = amounts[0] if amounts else 0
    lower = text.lower()

    notes = []

    if company:
        notes.append(f"Компания: {company}")
    if amount:
        notes.append(f"Основная сумма: {amount:,.0f} ₽")

    if "ндс" in lower:
        notes.append("Есть упоминание НДС")
    if "без ндс" in lower:
        notes.append("Есть условие без НДС")
    if "предоплат" in lower or "аванс" in lower:
        notes.append("Есть предоплата/аванс")
    if "отсроч" in lower:
        notes.append("Есть отсрочка платежа")
    if "штраф" in lower or "пен" in lower or "неустой" in lower:
        notes.append("Есть штрафы/пени/неустойка")
    if "самовывоз" in lower:
        notes.append("Есть условие самовывоза")
    if "доставка" in lower:
        notes.append("Есть условие доставки")
    if "срок" in lower:
        notes.append("Есть условия по срокам")
    if "расторж" in lower:
        notes.append("Есть условия расторжения")
    if "качест" in lower:
        notes.append("Есть условия по качеству товара")

    label = {
        "supplier_invoice": "Счет поставщика / закупка",
        "logistics_invoice": "Счет логистики",
        "client_invoice": "Счет клиенту / продажа",
        "contract": "Договор / условия",
        "other": "Прочий документ",
    }.get(doc_type, doc_type)

    summary = f"Тип документа: {label}\n" + ("\n".join(notes) if notes else "Ключевые данные не найдены")

    return {
        "company": company,
        "amount": amount,
        "analysis": summary
    }


def save_document_for_deal(cur, deal_id, file, doc_type):
    if not file or not file.filename:
        return None

    safe_name = f"deal_{deal_id}_{int(datetime.now().timestamp())}_{file.filename}"
    path = os.path.join(UPLOAD_FOLDER, safe_name)
    file.save(path)

    extracted = extract_text_from_file(path)
    parsed = analyze_document_text(doc_type, extracted)
    analysis = parsed["analysis"]

    cur.execute("""
        INSERT INTO documents (deal_id, doc_type, filename, original_filename, extracted_text, analysis)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (deal_id, doc_type, safe_name, file.filename, extracted[:30000], analysis))

    return parsed


def apply_parsed_to_deal(cur, deal_id, doc_type, parsed):
    amount = parsed.get("amount", 0)
    company = parsed.get("company", "")
    analysis = parsed.get("analysis", "")

    if doc_type == "supplier_invoice":
        if amount:
            cur.execute("UPDATE deals SET purchase_sum=? WHERE id=?", (amount, deal_id))
        if company:
            cur.execute("UPDATE deals SET supplier=? WHERE id=?", (company, deal_id))

    elif doc_type == "logistics_invoice":
        if amount:
            cur.execute("UPDATE deals SET logistics_sum=? WHERE id=?", (amount, deal_id))

    elif doc_type == "client_invoice":
        if amount:
            cur.execute("UPDATE deals SET client_sum=? WHERE id=?", (amount, deal_id))
        if company:
            cur.execute("UPDATE deals SET client=? WHERE id=?", (company, deal_id))

    elif doc_type == "contract":
        cur.execute("SELECT contract_notes FROM deals WHERE id=?", (deal_id,))
        old = cur.fetchone()[0] or ""
        new_notes = (old + "\n\nАвтоанализ договора:\n" + analysis).strip()
        cur.execute("UPDATE deals SET contract_notes=? WHERE id=?", (new_notes, deal_id))


@crm_bp.route("/office/login", methods=["GET", "POST"])
def login():
    init_db()
    error = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = CRM_USERS.get(username)

        if user and user["password"] == password:
            session["office_user"] = username
            session["office_role"] = user["role"]
            return redirect("/office")

        error = "Неверный логин или пароль"

    return f"""
    <html><head><meta charset="UTF-8"><title>CRM вход</title>
    <style>
    body{{font-family:Arial;background:#f4f5f7;padding:40px}}
    .card{{max-width:420px;margin:auto;background:white;padding:30px;border-radius:14px;box-shadow:0 4px 14px #0002}}
    input,button{{width:100%;padding:14px;margin:10px 0;font-size:16px;box-sizing:border-box}}
    button{{background:#111;color:white;border:0;border-radius:10px;cursor:pointer}}
    </style></head><body>
    <div class="card">
    <h1>Вход в CRM</h1>
    <form method="POST">
    <input name="username" placeholder="Логин">
    <input name="password" type="password" placeholder="Пароль">
    <button>Войти</button>
    </form>
    <p style="color:red">{error}</p>
    <p><b>Админ:</b> artem / 1234</p>
    </div></body></html>
    """


@crm_bp.route("/office/logout")
def logout():
    session.pop("office_user", None)
    session.pop("office_role", None)
    return redirect("/office/login")


@crm_bp.route("/office")
@login_required
def dashboard():
    init_db()
    user = session["office_user"]
    role = session.get("office_role")
    month = request.args.get("month", datetime.now().strftime("%Y-%m"))

    conn = db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if role == "admin":
        cur.execute("SELECT * FROM deals WHERE substr(created_at,1,7)=? ORDER BY id DESC", (month,))
    else:
        cur.execute("SELECT * FROM deals WHERE manager=? AND substr(created_at,1,7)=? ORDER BY id DESC", (user, month))

    deals = cur.fetchall()
    conn.close()

    # Считаем маржу по каждому менеджеру за выбранный месяц
    manager_margins = {}
    deal_margins = {}

    for d in deals:
        margin = d["client_sum"] - d["purchase_sum"] - d["logistics_sum"] - d["other_expenses"]
        deal_margins[d["id"]] = margin
        manager_margins[d["manager"]] = manager_margins.get(d["manager"], 0) + margin

    # Определяем месячную ставку каждого менеджера от его общей маржи за месяц
    manager_rates = {}
    manager_commissions = {}

    for manager, month_margin in manager_margins.items():
        scheme = get_scheme(manager)
        rate = commission_rate(month_margin, scheme)
        manager_rates[manager] = rate
        manager_commissions[manager] = month_margin * rate

    total_margin = sum(manager_margins.values())
    total_commission = sum(manager_commissions.values())

    managers_summary_html = ""
    for manager, month_margin in manager_margins.items():
        rate = manager_rates.get(manager, 0)
        commission = manager_commissions.get(manager, 0)
        managers_summary_html += f"""
        <div style="padding:12px;border:1px solid #ddd;border-radius:10px;margin:8px 0;background:#fafafa;">
            <b>{esc(manager)}</b><br>
            Маржа: {month_margin:,.0f} ₽<br>
            Процент: <b>{rate*100:.0f}%</b><br>
            Зарплата: <b>{commission:,.0f} ₽</b>
        </div>
        """

    rows = ""

    for d in deals:
        margin = deal_margins[d["id"]]
        rate = manager_rates.get(d["manager"], 0)

        # В таблице показываем вклад сделки в месячный заработок менеджера
        commission = margin * rate

        rows += f"""
        <tr>
            <td>{d['id']}</td>
            <td>{esc(d['manager'])}</td>
            <td><a href="/office/deal/{d['id']}">{esc(d['client'])}</a></td>
            <td>{esc(d['material'])}</td>
            <td>{esc(d['supplier'])}</td>
            <td>{d['client_sum']:,.0f} ₽</td>
            <td>{margin:,.0f} ₽</td>
            <td>{rate*100:.0f}%</td>
            <td>{commission:,.0f} ₽</td>
            <td>{esc(d['status'])}</td>
        </tr>
        """

    admin_link = '<a class="btn light" href="/office/admin">Настройки менеджеров</a>' if role == "admin" else ""

    return f"""
    <html><head><meta charset="UTF-8"><title>CRM</title>
    <style>
    body{{font-family:Arial;background:#f4f5f7;padding:30px}}
    .card{{background:white;padding:25px;border-radius:14px;margin-bottom:20px;box-shadow:0 4px 14px #0002}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}}
    .btn{{display:inline-block;background:#111;color:white;padding:12px 16px;border-radius:10px;text-decoration:none;margin-right:10px}}
    .light{{background:white;color:#111;border:1px solid #111}}
    input{{padding:10px;font-size:16px}}
    </style></head><body>
    <div class="card">
        <h1>CRM сделок</h1>
        <p>Пользователь: <b>{esc(user)}</b> / роль: <b>{esc(role)}</b></p>
        <form method="GET">
            <label>Месяц:</label>
            <input name="month" value="{esc(month)}" type="month">
            <button class="btn">Показать</button>
        </form>
        <p><b>Маржа за месяц:</b> {total_margin:,.0f} ₽</p>
        <p><b>Заработок менеджеров:</b> {total_commission:,.0f} ₽</p>
        <h3>Начисления по менеджерам</h3>
        {managers_summary_html}
        <a class="btn" href="/office/deal/new">+ Новая сделка</a>
        {admin_link}
        <a href="/office/logout">Выйти</a>
    </div>
    <div class="card">
    <table>
    <tr>
    <th>ID</th><th>Менеджер</th><th>Клиент</th><th>Материал</th><th>Поставщик</th>
    <th>Сумма клиента</th><th>Маржа</th><th>%</th><th>Заработок</th><th>Статус</th>
    </tr>
    {rows}
    </table>
    </div>
    </body></html>
    """


@crm_bp.route("/office/deal/new", methods=["GET", "POST"])
@login_required
def new_deal():
    init_db()
    user = session["office_user"]

    if request.method == "POST":
        conn = db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO deals (
                manager, client, material, supplier, purchase_sum, logistics_sum,
                other_expenses, client_sum, production_date, shipment_date, contract_notes, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user,
            request.form.get("client", ""),
            request.form.get("material", ""),
            request.form.get("supplier", ""),
            money(request.form.get("purchase_sum", 0)),
            money(request.form.get("logistics_sum", 0)),
            money(request.form.get("other_expenses", 0)),
            money(request.form.get("client_sum", 0)),
            request.form.get("production_date", ""),
            request.form.get("shipment_date", ""),
            request.form.get("contract_notes", ""),
            request.form.get("status", "В работе"),
        ))

        deal_id = cur.lastrowid

        upload_map = [
            ("supplier_invoice_file", "supplier_invoice"),
            ("logistics_invoice_file", "logistics_invoice"),
            ("client_invoice_file", "client_invoice"),
            ("contract_file", "contract"),
        ]

        for field_name, doc_type in upload_map:
            parsed = save_document_for_deal(cur, deal_id, request.files.get(field_name), doc_type)
            if parsed:
                apply_parsed_to_deal(cur, deal_id, doc_type, parsed)

        conn.commit()
        conn.close()

        return redirect(f"/office/deal/{deal_id}")

    return """
    <html><head><meta charset="UTF-8"><title>Новая сделка</title>
    <style>
    body{font-family:Arial;background:#f4f5f7;padding:30px}
    .card{max-width:900px;margin:auto;background:white;padding:25px;border-radius:14px;box-shadow:0 4px 14px #0002}
    input,textarea,select,button{width:100%;padding:13px;margin:8px 0 16px;font-size:16px;box-sizing:border-box}
    textarea{height:130px}
    button{background:#111;color:white;border:0;border-radius:10px;cursor:pointer}
    </style></head><body>
    <div class="card">
    <p><a href="/office">← Назад</a></p>
    <h1>Новая сделка</h1>
    <form method="POST" enctype="multipart/form-data">

    <label>Клиент</label><input name="client">
    <label>Материал</label><input name="material">
    <label>Поставщик / карьер</label><input name="supplier">

    <label>Счет поставщика / закупка</label>
    <input name="purchase_sum">
    <input type="file" name="supplier_invoice_file" accept=".pdf,.docx,.txt">

    <label>Логистика / расходы</label>
    <input name="logistics_sum">
    <input type="file" name="logistics_invoice_file" accept=".pdf,.docx,.txt">

    <label>Прочие расходы</label><input name="other_expenses">

    <label>Счет клиенту / продажа</label>
    <input name="client_sum">
    <input type="file" name="client_invoice_file" accept=".pdf,.docx,.txt">

    <label>Дата производства</label><input name="production_date" type="date">
    <label>Дата отгрузки</label><input name="shipment_date" type="date">

    <label>Оценка договора / ключевые условия</label>
    <textarea name="contract_notes" placeholder="НДС, предоплата, сроки, штрафы, риски..."></textarea>

    <label>Договор / условия</label>
    <input type="file" name="contract_file" accept=".pdf,.docx,.txt">

    <label>Статус</label>
    <select name="status">
    <option>В работе</option><option>Ожидаем оплату</option><option>Оплачено клиентом</option>
    <option>Производство</option><option>Отгрузка</option><option>Закрыта</option>
    </select>

    <button>Создать сделку и распознать документы</button>
    </form>
    </div></body></html>
    """


@crm_bp.route("/office/deal/<int:deal_id>")
@login_required
def deal_page(deal_id):
    init_db()
    user = session["office_user"]
    role = session.get("office_role")

    conn = db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if role == "admin":
        cur.execute("SELECT * FROM deals WHERE id=?", (deal_id,))
    else:
        cur.execute("SELECT * FROM deals WHERE id=? AND manager=?", (deal_id, user))

    d = cur.fetchone()

    if not d:
        conn.close()
        return "<h1>Сделка не найдена</h1><p><a href='/office'>Назад</a></p>"

    cur.execute("SELECT * FROM documents WHERE deal_id=? ORDER BY id DESC", (deal_id,))
    docs = cur.fetchall()
    conn.close()

    margin = d["client_sum"] - d["purchase_sum"] - d["logistics_sum"] - d["other_expenses"]
    scheme = get_scheme(d["manager"])
    rate = commission_rate(margin, scheme)
    commission = margin * rate

    docs_html = ""
    for doc in docs:
        docs_html += f"""
        <tr>
            <td>{esc(doc['doc_type'])}</td>
            <td><a href="/office/uploads/{esc(doc['filename'])}" target="_blank">{esc(doc['original_filename'] or doc['filename'])}</a></td>
            <td><pre>{esc(doc['analysis'])}</pre></td>
            <td>{esc(doc['created_at'])}</td>
        </tr>
        """

    return f"""
    <html><head><meta charset="UTF-8"><title>Сделка #{d['id']}</title>
    <style>
    body{{font-family:Arial;background:#f4f5f7;padding:30px}}
    .card{{max-width:1100px;margin:auto;background:white;padding:25px;border-radius:14px;box-shadow:0 4px 14px #0002;margin-bottom:20px}}
    p{{font-size:17px}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}}
    pre{{white-space:pre-wrap;font-family:Arial}}
    .btn{{display:inline-block;background:#111;color:white;padding:12px 16px;border-radius:10px;text-decoration:none;margin-right:10px}}
    .light{{background:white;color:#111;border:1px solid #111}}
    </style></head><body>
    <div class="card">
    <p><a href="/office">← Назад</a></p>
    <h1>Сделка #{d['id']}</h1>

    <p>
        <a class="btn" href="/office/deal/{d['id']}/edit">Редактировать сделку</a>
        <a class="btn light" href="/office/deal/{d['id']}/docs">Добавить документы</a>
    </p>

    <p><b>Менеджер:</b> {esc(d['manager'])}</p>
    <p><b>Клиент:</b> {esc(d['client'])}</p>
    <p><b>Материал:</b> {esc(d['material'])}</p>
    <p><b>Поставщик:</b> {esc(d['supplier'])}</p>
    <hr>
    <p><b>Закупка:</b> {d['purchase_sum']:,.0f} ₽</p>
    <p><b>Логистика:</b> {d['logistics_sum']:,.0f} ₽</p>
    <p><b>Прочие расходы:</b> {d['other_expenses']:,.0f} ₽</p>
    <p><b>Продажа клиенту:</b> {d['client_sum']:,.0f} ₽</p>
    <p><b>Маржа:</b> {margin:,.0f} ₽</p>
    <p><b>Ставка:</b> {rate*100:.0f}%</p>
    <p><b>Заработок менеджера:</b> {commission:,.0f} ₽</p>
    <hr>
    <p><b>Дата производства:</b> {esc(d['production_date'])}</p>
    <p><b>Дата отгрузки:</b> {esc(d['shipment_date'])}</p>
    <p><b>Статус:</b> {esc(d['status'])}</p>

    <h2>Оценка договора / условия</h2>
    <p>{esc(d['contract_notes'])}</p>
    </div>

    <div class="card">
    <h2>Документы сделки</h2>
    <table>
    <tr><th>Тип</th><th>Файл</th><th>Что найдено</th><th>Дата</th></tr>
    {docs_html}
    </table>
    </div>
    </body></html>
    """


@crm_bp.route("/office/deal/<int:deal_id>/edit", methods=["GET", "POST"])
@login_required
def edit_deal(deal_id):
    init_db()
    user = session["office_user"]
    role = session.get("office_role")

    conn = db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if role == "admin":
        cur.execute("SELECT * FROM deals WHERE id=?", (deal_id,))
    else:
        cur.execute("SELECT * FROM deals WHERE id=? AND manager=?", (deal_id, user))

    d = cur.fetchone()

    if not d:
        conn.close()
        return "<h1>Сделка не найдена</h1><p><a href='/office'>Назад</a></p>"

    if request.method == "POST":
        cur.execute("""
            UPDATE deals SET
                client=?, material=?, supplier=?, purchase_sum=?, logistics_sum=?,
                other_expenses=?, client_sum=?, production_date=?, shipment_date=?,
                contract_notes=?, status=?
            WHERE id=?
        """, (
            request.form.get("client", ""),
            request.form.get("material", ""),
            request.form.get("supplier", ""),
            money(request.form.get("purchase_sum", 0)),
            money(request.form.get("logistics_sum", 0)),
            money(request.form.get("other_expenses", 0)),
            money(request.form.get("client_sum", 0)),
            request.form.get("production_date", ""),
            request.form.get("shipment_date", ""),
            request.form.get("contract_notes", ""),
            request.form.get("status", "В работе"),
            deal_id,
        ))

        conn.commit()
        conn.close()
        return redirect(f"/office/deal/{deal_id}")

    conn.close()

    return f"""
    <html><head><meta charset="UTF-8"><title>Редактировать сделку</title>
    <style>
    body{{font-family:Arial;background:#f4f5f7;padding:30px}}
    .card{{max-width:900px;margin:auto;background:white;padding:25px;border-radius:14px;box-shadow:0 4px 14px #0002}}
    input,textarea,select,button{{width:100%;padding:13px;margin:8px 0 16px;font-size:16px;box-sizing:border-box}}
    textarea{{height:160px}}
    button{{background:#111;color:white;border:0;border-radius:10px;cursor:pointer}}
    </style></head><body>
    <div class="card">
    <p><a href="/office/deal/{deal_id}">← Назад в сделку</a></p>
    <h1>Редактировать сделку #{deal_id}</h1>
    <form method="POST">
    <label>Клиент</label><input name="client" value="{esc(d['client'])}">
    <label>Материал</label><input name="material" value="{esc(d['material'])}">
    <label>Поставщик / карьер</label><input name="supplier" value="{esc(d['supplier'])}">
    <label>Счет поставщика / закупка</label><input name="purchase_sum" value="{d['purchase_sum']}">
    <label>Логистика / расходы</label><input name="logistics_sum" value="{d['logistics_sum']}">
    <label>Прочие расходы</label><input name="other_expenses" value="{d['other_expenses']}">
    <label>Счет клиенту / продажа</label><input name="client_sum" value="{d['client_sum']}">
    <label>Дата производства</label><input name="production_date" type="date" value="{esc(d['production_date'])}">
    <label>Дата отгрузки</label><input name="shipment_date" type="date" value="{esc(d['shipment_date'])}">
    <label>Оценка договора / ключевые условия</label>
    <textarea name="contract_notes">{esc(d['contract_notes'])}</textarea>
    <label>Статус</label>
    <select name="status">
    <option {'selected' if d['status']=='В работе' else ''}>В работе</option>
    <option {'selected' if d['status']=='Ожидаем оплату' else ''}>Ожидаем оплату</option>
    <option {'selected' if d['status']=='Оплачено клиентом' else ''}>Оплачено клиентом</option>
    <option {'selected' if d['status']=='Производство' else ''}>Производство</option>
    <option {'selected' if d['status']=='Отгрузка' else ''}>Отгрузка</option>
    <option {'selected' if d['status']=='Закрыта' else ''}>Закрыта</option>
    </select>
    <button>Сохранить изменения</button>
    </form>
    </div></body></html>
    """


@crm_bp.route("/office/deal/<int:deal_id>/docs", methods=["GET", "POST"])
@login_required
def deal_documents(deal_id):
    init_db()
    user = session["office_user"]
    role = session.get("office_role")

    conn = db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if role == "admin":
        cur.execute("SELECT * FROM deals WHERE id=?", (deal_id,))
    else:
        cur.execute("SELECT * FROM deals WHERE id=? AND manager=?", (deal_id, user))

    deal = cur.fetchone()

    if not deal:
        conn.close()
        return "<h1>Сделка не найдена</h1><p><a href='/office'>Назад</a></p>"

    message = ""

    if request.method == "POST":
        doc_type = request.form.get("doc_type", "other")
        parsed = save_document_for_deal(cur, deal_id, request.files.get("file"), doc_type)
        if parsed:
            apply_parsed_to_deal(cur, deal_id, doc_type, parsed)
            conn.commit()
            message = "Документ загружен, прочитан и сделка обновлена"

    cur.execute("SELECT * FROM documents WHERE deal_id=? ORDER BY id DESC", (deal_id,))
    docs = cur.fetchall()
    conn.close()

    rows = ""
    for d in docs:
        rows += f"""
        <tr>
            <td>{esc(d['doc_type'])}</td>
            <td><a href="/office/uploads/{esc(d['filename'])}" target="_blank">{esc(d['original_filename'] or d['filename'])}</a></td>
            <td><pre>{esc(d['analysis'])}</pre></td>
            <td>{esc(d['created_at'])}</td>
        </tr>
        """

    return f"""
    <html><head><meta charset="UTF-8"><title>Документы сделки</title>
    <style>
    body{{font-family:Arial;background:#f4f5f7;padding:30px}}
    .card{{max-width:1100px;margin:auto;background:white;padding:25px;border-radius:14px;box-shadow:0 4px 14px #0002;margin-bottom:20px}}
    input,select,button{{width:100%;padding:13px;margin:8px 0 16px;font-size:16px;box-sizing:border-box}}
    button{{background:#111;color:white;border:0;border-radius:10px;cursor:pointer}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{padding:12px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}}
    pre{{white-space:pre-wrap;font-family:Arial}}
    </style></head><body>
    <div class="card">
    <p><a href="/office/deal/{deal_id}">← Назад в сделку</a></p>
    <h1>Документы сделки #{deal_id}</h1>
    <p style="color:green">{esc(message)}</p>
    <form method="POST" enctype="multipart/form-data">
    <label>Тип документа</label>
    <select name="doc_type">
    <option value="supplier_invoice">Счет поставщика / закупка</option>
    <option value="logistics_invoice">Счет логистики</option>
    <option value="client_invoice">Счет клиенту / продажа</option>
    <option value="contract">Договор / условия</option>
    <option value="other">Прочий документ</option>
    </select>
    <label>Файл PDF / DOCX / TXT</label>
    <input type="file" name="file" accept=".pdf,.docx,.txt">
    <button>Загрузить и распознать</button>
    </form>
    </div>

    <div class="card">
    <h2>Загруженные документы</h2>
    <table><tr><th>Тип</th><th>Файл</th><th>Что найдено</th><th>Дата</th></tr>{rows}</table>
    </div>
    </body></html>
    """


@crm_bp.route("/office/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=False)


@crm_bp.route("/office/admin", methods=["GET", "POST"])
@login_required
@admin_required
def admin():
    init_db()

    if request.method == "POST":
        manager = request.form.get("manager")
        scheme = request.form.get("scheme")
        conn = db()
        cur = conn.cursor()
        cur.execute("DELETE FROM manager_settings WHERE manager=?", (manager,))
        cur.execute("""
            INSERT INTO manager_settings(manager, scheme)
            VALUES(?, ?)
        """, (manager, scheme))
        conn.commit()
        conn.close()

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT manager, scheme FROM manager_settings ORDER BY manager")
    rows_data = cur.fetchall()
    conn.close()

    rows = "".join([f"<tr><td>{esc(m)}</td><td>{esc(s)}</td></tr>" for m, s in rows_data])
    options = "".join([f"<option>{esc(m)}</option>" for m in CRM_USERS.keys()])

    return f"""
    <html><head><meta charset="UTF-8"><title>Настройки CRM</title>
    <style>
    body{{font-family:Arial;background:#f4f5f7;padding:30px}}
    .card{{max-width:900px;margin:auto;background:white;padding:25px;border-radius:14px;box-shadow:0 4px 14px #0002}}
    select,button{{width:100%;padding:13px;margin:8px 0 16px;font-size:16px}}
    button{{background:#111;color:white;border:0;border-radius:10px;cursor:pointer}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{padding:12px;border-bottom:1px solid #ddd;text-align:left}}
    </style></head><body>
    <div class="card">
    <p><a href="/office">← Назад</a></p>
    <h1>Настройки мотивации</h1>
    <p>Схему можно менять сколько угодно раз. Расчет применяется к выбранному месяцу.</p>
    <form method="POST">
    <label>Менеджер</label>
    <select name="manager">{options}</select>
    <label>Схема</label>
    <select name="scheme">
    <option value="progressive">Прогрессивная: 0%, 10%, 12%, 14%, 16%, 18%, 20%</option>
    <option value="simple">Простая: до 1 млн — 10%, от 1 млн — 20%</option>
    </select>
    <button>Сохранить</button>
    </form>
    <h2>Текущие настройки</h2>
    <table><tr><th>Менеджер</th><th>Схема</th></tr>{rows}</table>
    </div></body></html>
    """
