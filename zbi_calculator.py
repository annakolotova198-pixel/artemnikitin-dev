import csv
import json
import math
import os
from pathlib import Path

import requests
from flask import Blueprint, current_app, render_template_string, request


zbi_bp = Blueprint("zbi", __name__, url_prefix="/zbi")
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "zbi_products.csv"
DELIVERY_FILE = BASE_DIR / "zbi_delivery.csv"


def _float(value, default=0.0):
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def load_catalog():
    with PRODUCTS_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        products = list(csv.DictReader(handle))
    for index, item in enumerate(products):
        item["id"] = str(index)
        item["price_rub"] = _float(item.get("price_rub"))
        item["weight_kg"] = _float(item.get("weight_kg"))
    with DELIVERY_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        delivery = list(csv.DictReader(handle))
    for item in delivery:
        for key in ("capacity_t", "rate_rub_km", "lat", "lon"):
            item[key] = _float(item.get(key))
    return products, delivery


def geocode(address):
    text = str(address or "").strip()
    if not text:
        return None, None
    # A coordinate pair is useful on sites where the geocoding key is temporarily unavailable.
    if "," in text:
        parts = [part.strip() for part in text.split(",")]
        if len(parts) == 2:
            lat, lon = _float(parts[0], None), _float(parts[1], None)
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    api_key = current_app.config.get("YANDEX_API_KEY") or os.getenv("YANDEX_API_KEY", "")
    if not api_key:
        return None, None
    try:
        response = requests.get(
            "https://geocode-maps.yandex.ru/1.x/",
            params={"apikey": api_key, "geocode": text, "format": "json", "lang": "ru_RU"},
            timeout=15,
        )
        response.raise_for_status()
        member = response.json()["response"]["GeoObjectCollection"]["featureMember"][0]
        lon, lat = member["GeoObject"]["Point"]["pos"].split()
        return float(lat), float(lon)
    except Exception:
        return None, None


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    values = [math.radians(value) for value in (lat1, lon1, lat2, lon2)]
    lat1, lon1, lat2, lon2 = values
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.asin(math.sqrt(value))


def road_distance_km(start_lat, start_lon, end_lat, end_lon):
    coordinates = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    try:
        response = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{coordinates}",
            params={"overview": "false", "alternatives": "false", "steps": "false"},
            timeout=15,
        )
        data = response.json()
        if data.get("code") == "Ok":
            return data["routes"][0]["distance"] / 1000, "по автодороге"
    except Exception:
        pass
    # More realistic than a straight line when the public router is unavailable.
    return haversine_km(start_lat, start_lon, end_lat, end_lon) * 1.25, "оценка по прямой × 1,25"


def delivery_calculation(product, quantity, distance, delivery_options):
    weight = product["weight_kg"]
    if weight <= 0:
        return None
    alternatives = []
    for option in delivery_options:
        capacity_kg = option["capacity_t"] * 1000
        units_full = math.floor(capacity_kg / weight)
        if units_full < 1:
            continue
        trips = math.ceil(quantity / units_full)
        trip_price = distance * option["rate_rub_km"]
        request_delivery = trip_price * trips
        alternatives.append({
            "vehicle": option["vehicle"],
            "capacity_t": option["capacity_t"],
            "rate": option["rate_rub_km"],
            "units_full": units_full,
            "trips": trips,
            "request_delivery": request_delivery,
            "request_delivery_unit": request_delivery / quantity,
            "full_delivery": trip_price,
            "full_delivery_unit": trip_price / units_full,
            "loading_address": option["loading_address"],
        })
    if not alternatives:
        return None
    request_best = min(alternatives, key=lambda item: (item["request_delivery"], item["request_delivery_unit"]))
    full_best = min(alternatives, key=lambda item: (item["full_delivery_unit"], item["full_delivery"]))
    return {
        "request_vehicle": request_best["vehicle"],
        "request_capacity_t": request_best["capacity_t"],
        "request_rate": request_best["rate"],
        "request_units_per_trip": request_best["units_full"],
        "request_trips": request_best["trips"],
        "request_delivery": request_best["request_delivery"],
        "request_delivery_unit": request_best["request_delivery_unit"],
        "full_vehicle": full_best["vehicle"],
        "full_capacity_t": full_best["capacity_t"],
        "full_rate": full_best["rate"],
        "units_full": full_best["units_full"],
        "full_delivery": full_best["full_delivery"],
        "full_delivery_unit": full_best["full_delivery_unit"],
        "loading_address": request_best["loading_address"],
    }


PAGE = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Калькулятор ЖБИ</title>
  <style>
    :root{--ink:#18202a;--muted:#647184;--blue:#185bd8;--line:#dce3ec;--bg:#f3f6fa;--ok:#e9f8ef}
    *{box-sizing:border-box} body{font-family:Arial,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:24px}
    .wrap{max-width:1280px;margin:auto}.card{background:white;border-radius:16px;padding:22px;margin-bottom:18px;box-shadow:0 5px 18px #20305012}
    h1,h2,h3{margin-top:0}.nav a{margin-right:18px;color:var(--blue);font-weight:700;text-decoration:none}
    .grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.wide{grid-column:span 2}
    label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}input,select,button{width:100%;min-height:44px;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:15px;background:#fff}
    button{background:var(--blue);border-color:var(--blue);color:white;font-weight:700;cursor:pointer}.hint,.muted{color:var(--muted);font-size:13px}
    .search-wrap{position:relative}.suggestions{position:absolute;z-index:5;left:0;right:0;top:74px;max-height:320px;overflow:auto;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 28px #10203026;display:none}
    .suggestion{padding:10px 12px;border-bottom:1px solid #edf0f4;cursor:pointer}.suggestion:hover{background:#eef4ff}.suggestion small{display:block;color:var(--muted);margin-top:3px}
    .summary{background:var(--ok);border:1px solid #bfe5cc}.result{border:1px solid var(--line);border-radius:14px;padding:18px;margin-top:14px}.result.best{border:2px solid #2a9d5b}
    .badges{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0}.badge{background:#eef3ff;border-radius:999px;padding:6px 9px;font-size:12px;font-weight:700}
    table{width:100%;border-collapse:collapse;margin-top:12px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e6ebf1}th{font-size:12px;color:var(--muted)}
    .money{font-weight:800;white-space:nowrap}.warning{background:#fff6df;border:1px solid #f0d58a;padding:13px;border-radius:10px}
    @media(max-width:850px){body{padding:12px}.grid{grid-template-columns:1fr}.wide{grid-column:span 1}.table-wrap{overflow:auto}}
  </style>
</head>
<body><div class="wrap">
  <div class="card nav"><a href="/">Нерудные материалы</a><a href="/zbi">Калькулятор ЖБИ</a><a href="/carriers">Перевозчики</a></div>
  <div class="card">
    <h1>Калькулятор ЖБИ</h1>
    <p class="muted">В каталоге {{ product_count }} позиций от {{ supplier_count }} производителей. Тариф и грузоподъёмность берутся только из листа «Доставка».</p>
    <form method="post" id="zbiForm">
      <div class="grid">
        <div class="wide"><label>Адрес доставки</label><input name="address" value="{{ form.address }}" placeholder="Москва, улица и дом" required><div class="hint">Можно также ввести координаты: 55.75, 37.62</div></div>
        <div><label>Производитель</label><select name="supplier" id="supplier"><option value="">Любой — найти ближайшего</option>{% for item in suppliers %}<option value="{{ item }}" {% if form.supplier==item %}selected{% endif %}>{{ item }}</option>{% endfor %}</select></div>
        <div><label>Группа изделий</label><select name="group" id="group"><option value="">Все ЖБИ / любое изделие</option>{% for item in groups %}<option value="{{ item }}" {% if form.group==item %}selected{% endif %}>{{ item }} — любые</option>{% endfor %}</select></div>
        <div class="wide search-wrap"><label>Поиск и выбор изделия</label><input id="productSearch" autocomplete="off" value="{{ form.search }}" placeholder="Например: кольцо КС 15-9 или бордюр"><input type="hidden" name="product_id" id="productId" value="{{ form.product_id }}"><input type="hidden" name="search" id="searchValue" value="{{ form.search }}"><div class="suggestions" id="suggestions"></div><div class="hint">Можно выбрать точную позицию или написать часть названия и оставить без выбора.</div></div>
        <div><label>Количество, шт.</label><input type="number" min="1" step="1" name="quantity" value="{{ form.quantity }}" required></div>
        <div><label>Наценка менеджера, %</label><input type="number" min="0" step="0.1" name="markup" value="{{ form.markup }}" required></div>
        <div><label>&nbsp;</label><button type="submit">Рассчитать стоимость</button></div>
      </div>
    </form>
  </div>
  {% if error %}<div class="card warning"><b>Не удалось выполнить расчёт.</b> {{ error }}</div>{% endif %}
  {% if results %}
  <div class="card summary"><h2>Рекомендация</h2><p>Ближайший подходящий производитель: <b>{{ results[0].supplier }}</b>, расстояние {{ results[0].distance|round(1) }} км. Ниже показана закупка, доставка и продажа с наценкой {{ markup }}%.</p></div>
  <div class="card"><h2>Подходящие варианты</h2><p class="muted">Сначала ближайшие производители, внутри одного производителя — меньшая стоимость единицы с доставкой. Показано до 50 вариантов.</p>
  {% for item in results %}<article class="result {% if loop.first %}best{% endif %}">
    <h3>{{ item.name }}</h3><div class="badges"><span class="badge">{{ item.supplier }}</span><span class="badge">{{ item.group }}</span><span class="badge">{{ item.weight_kg|round(1) }} кг/шт.</span><span class="badge">{{ item.distance|round(1) }} км · {{ item.distance_kind }}</span></div>
    <div class="muted">{{ item.size_mm }}{% if item.note %} · {{ item.note }}{% endif %}</div>
    <div class="table-wrap"><table><thead><tr><th>Сценарий</th><th>Количество</th><th>Машина</th><th>Рейсов</th><th>Доставка</th><th>Закупка с доставкой, 1 шт.</th><th>Продажа, 1 шт.</th><th>Продажа всего</th></tr></thead><tbody>
      <tr><td>Заявка менеджера</td><td>{{ quantity }}</td><td>{{ item.delivery.request_vehicle }} · {{ item.delivery.request_capacity_t|round(0)|int }} т</td><td>{{ item.delivery.request_trips }}</td><td class="money">{{ item.delivery.request_delivery|money }} ₽</td><td class="money">{{ item.request_delivered_unit|money }} ₽</td><td class="money">{{ item.request_sale_unit|money }} ₽</td><td class="money">{{ item.request_sale_total|money }} ₽</td></tr>
      <tr><td>Полная загрузка</td><td>{{ item.delivery.units_full }}</td><td>{{ item.delivery.full_vehicle }} · {{ item.delivery.full_capacity_t|round(0)|int }} т</td><td>1</td><td class="money">{{ item.delivery.full_delivery|money }} ₽</td><td class="money">{{ item.full_delivered_unit|money }} ₽</td><td class="money">{{ item.full_sale_unit|money }} ₽</td><td class="money">{{ item.full_sale_total|money }} ₽</td></tr>
    </tbody></table></div>
    <p class="muted">Цена завода: {{ item.price_rub|money }} ₽/шт. · Тариф заявки: {{ item.delivery.request_rate|money }} ₽/км · тариф полной машины: {{ item.delivery.full_rate|money }} ₽/км · Загрузка: {{ item.delivery.loading_address }}</p>
  </article>{% endfor %}</div>
  {% endif %}
</div>
<script>
const catalog={{ catalog_json|safe }}; const supplier=document.getElementById('supplier'); const group=document.getElementById('group'); const input=document.getElementById('productSearch'); const box=document.getElementById('suggestions'); const idField=document.getElementById('productId'); const searchField=document.getElementById('searchValue');
function visibleProducts(){const q=input.value.trim().toLowerCase();return catalog.filter(p=>(!supplier.value||p.supplier===supplier.value)&&(!group.value||p.group===group.value)&&(!q||p.name.toLowerCase().includes(q)||p.size_mm.toLowerCase().includes(q))).slice(0,60)}
function show(){const rows=visibleProducts();box.innerHTML=rows.map(p=>`<div class="suggestion" data-id="${p.id}"><b>${p.name}</b><small>${p.supplier} · ${p.group} · ${p.weight_kg||'масса не указана'} кг · ${p.price_rub} ₽</small></div>`).join('')||'<div class="suggestion">Совпадений нет</div>';box.style.display='block';box.querySelectorAll('[data-id]').forEach(el=>el.onclick=()=>{const p=catalog.find(x=>x.id===el.dataset.id);input.value=p.name;idField.value=p.id;searchField.value=p.name;box.style.display='none'})}
input.addEventListener('input',()=>{idField.value='';searchField.value=input.value;show()});input.addEventListener('focus',show);supplier.addEventListener('change',()=>{idField.value='';show()});group.addEventListener('change',()=>{idField.value='';show()});document.addEventListener('click',e=>{if(!e.target.closest('.search-wrap'))box.style.display='none'});document.getElementById('zbiForm').addEventListener('submit',()=>searchField.value=input.value);
</script></body></html>
"""


@zbi_bp.app_template_filter("money")
def money(value):
    return f"{float(value):,.0f}".replace(",", " ")


@zbi_bp.route("", methods=["GET", "POST"])
@zbi_bp.route("/", methods=["GET", "POST"])
def calculator():
    products, delivery_rows = load_catalog()
    suppliers = sorted({item["supplier"] for item in products})
    groups = sorted({item["group"] for item in products})
    form = {
        "address": request.form.get("address", ""),
        "supplier": request.form.get("supplier", ""),
        "group": request.form.get("group", ""),
        "product_id": request.form.get("product_id", ""),
        "search": request.form.get("search", ""),
        "quantity": request.form.get("quantity", "1"),
        "markup": request.form.get("markup", "10"),
    }
    error = ""
    results = []
    quantity = max(1, int(_float(form["quantity"], 1)))
    markup = max(0, _float(form["markup"], 0))

    if request.method == "POST":
        lat, lon = geocode(form["address"])
        if lat is None:
            error = "Адрес не найден. Проверьте адрес или введите координаты через запятую."
        else:
            candidates = products
            if form["product_id"]:
                candidates = [item for item in candidates if item["id"] == form["product_id"]]
            else:
                if form["supplier"]:
                    candidates = [item for item in candidates if item["supplier"] == form["supplier"]]
                if form["group"]:
                    candidates = [item for item in candidates if item["group"] == form["group"]]
                query = form["search"].strip().casefold()
                if query:
                    candidates = [item for item in candidates if query in item["name"].casefold() or query in item["size_mm"].casefold()]
            if not candidates:
                error = "По выбранным параметрам изделий не найдено."
            else:
                supplier_distance = {}
                for supplier_name in {item["supplier"] for item in candidates}:
                    rows = [item for item in delivery_rows if item["supplier"] == supplier_name]
                    if not rows:
                        continue
                    distance, kind = road_distance_km(rows[0]["lat"], rows[0]["lon"], lat, lon)
                    supplier_distance[supplier_name] = (distance, kind, rows)
                for product in candidates:
                    info = supplier_distance.get(product["supplier"])
                    if not info:
                        continue
                    distance, kind, options = info
                    delivery = delivery_calculation(product, quantity, distance, options)
                    if not delivery:
                        continue
                    request_delivered_unit = product["price_rub"] + delivery["request_delivery_unit"]
                    full_delivered_unit = product["price_rub"] + delivery["full_delivery_unit"]
                    item = dict(product)
                    item.update({
                        "distance": distance,
                        "distance_kind": kind,
                        "delivery": delivery,
                        "request_delivered_unit": request_delivered_unit,
                        "request_sale_unit": request_delivered_unit * (1 + markup / 100),
                        "request_sale_total": request_delivered_unit * (1 + markup / 100) * quantity,
                        "full_delivered_unit": full_delivered_unit,
                        "full_sale_unit": full_delivered_unit * (1 + markup / 100),
                        "full_sale_total": full_delivered_unit * (1 + markup / 100) * delivery["units_full"],
                    })
                    results.append(item)
                results.sort(key=lambda item: (item["distance"], item["request_delivered_unit"]))
                results = results[:50]
                if not results and not error:
                    error = "У подходящих изделий не указана масса либо изделие тяжелее доступной машины."

    catalog_for_js = [{key: item[key] for key in ("id", "supplier", "group", "name", "size_mm", "weight_kg", "price_rub")} for item in products]
    return render_template_string(
        PAGE,
        products=products,
        suppliers=suppliers,
        groups=groups,
        form=form,
        error=error,
        results=results,
        quantity=quantity,
        markup=markup,
        product_count=len(products),
        supplier_count=len(suppliers),
        catalog_json=json.dumps(catalog_for_js, ensure_ascii=False).replace("</", "<\\/"),
    )
import csv
import json
import math
import os
from pathlib import Path

import requests
from flask import Blueprint, current_app, render_template_string, request


zbi_bp = Blueprint("zbi", __name__, url_prefix="/zbi")
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "zbi_products.csv"
DELIVERY_FILE = BASE_DIR / "zbi_delivery.csv"


def _float(value, default=0.0):
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def load_catalog():
    with PRODUCTS_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        products = list(csv.DictReader(handle))
    for index, item in enumerate(products):
        item["id"] = str(index)
        item["price_rub"] = _float(item.get("price_rub"))
        item["weight_kg"] = _float(item.get("weight_kg"))
    with DELIVERY_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        delivery = list(csv.DictReader(handle))
    for item in delivery:
        for key in ("capacity_t", "rate_rub_km", "lat", "lon"):
            item[key] = _float(item.get(key))
    return products, delivery


def geocode(address):
    text = str(address or "").strip()
    if not text:
        return None, None
    # A coordinate pair is useful on sites where the geocoding key is temporarily unavailable.
    if "," in text:
        parts = [part.strip() for part in text.split(",")]
        if len(parts) == 2:
            lat, lon = _float(parts[0], None), _float(parts[1], None)
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    api_key = current_app.config.get("YANDEX_API_KEY") or os.getenv("YANDEX_API_KEY", "")
    if not api_key:
        return None, None
    try:
        response = requests.get(
            "https://geocode-maps.yandex.ru/1.x/",
            params={"apikey": api_key, "geocode": text, "format": "json", "lang": "ru_RU"},
            timeout=15,
        )
        response.raise_for_status()
        member = response.json()["response"]["GeoObjectCollection"]["featureMember"][0]
        lon, lat = member["GeoObject"]["Point"]["pos"].split()
        return float(lat), float(lon)
    except Exception:
        return None, None


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    values = [math.radians(value) for value in (lat1, lon1, lat2, lon2)]
    lat1, lon1, lat2, lon2 = values
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.asin(math.sqrt(value))


def road_distance_km(start_lat, start_lon, end_lat, end_lon):
    coordinates = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    try:
        response = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{coordinates}",
            params={"overview": "false", "alternatives": "false", "steps": "false"},
            timeout=15,
        )
        data = response.json()
        if data.get("code") == "Ok":
            return data["routes"][0]["distance"] / 1000, "по автодороге"
    except Exception:
        pass
    # More realistic than a straight line when the public router is unavailable.
    return haversine_km(start_lat, start_lon, end_lat, end_lon) * 1.25, "оценка по прямой × 1,25"


def delivery_calculation(product, quantity, distance, delivery_options):
    weight = product["weight_kg"]
    if weight <= 0:
        return None
    alternatives = []
    for option in delivery_options:
        capacity_kg = option["capacity_t"] * 1000
        units_full = math.floor(capacity_kg / weight)
        if units_full < 1:
            continue
        trips = math.ceil(quantity / units_full)
        trip_price = distance * option["rate_rub_km"]
        request_delivery = trip_price * trips
        alternatives.append({
            "vehicle": option["vehicle"],
            "capacity_t": option["capacity_t"],
            "rate": option["rate_rub_km"],
            "units_full": units_full,
            "trips": trips,
            "request_delivery": request_delivery,
            "request_delivery_unit": request_delivery / quantity,
            "full_delivery": trip_price,
            "full_delivery_unit": trip_price / units_full,
            "loading_address": option["loading_address"],
        })
    if not alternatives:
        return None
    return min(alternatives, key=lambda item: (item["request_delivery"], item["full_delivery_unit"]))


PAGE = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Калькулятор ЖБИ</title>
  <style>
    :root{--ink:#18202a;--muted:#647184;--blue:#185bd8;--line:#dce3ec;--bg:#f3f6fa;--ok:#e9f8ef}
    *{box-sizing:border-box} body{font-family:Arial,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:24px}
    .wrap{max-width:1280px;margin:auto}.card{background:white;border-radius:16px;padding:22px;margin-bottom:18px;box-shadow:0 5px 18px #20305012}
    h1,h2,h3{margin-top:0}.nav a{margin-right:18px;color:var(--blue);font-weight:700;text-decoration:none}
    .grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.wide{grid-column:span 2}
    label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}input,select,button{width:100%;min-height:44px;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:15px;background:#fff}
    button{background:var(--blue);border-color:var(--blue);color:white;font-weight:700;cursor:pointer}.hint,.muted{color:var(--muted);font-size:13px}
    .search-wrap{position:relative}.suggestions{position:absolute;z-index:5;left:0;right:0;top:74px;max-height:320px;overflow:auto;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 28px #10203026;display:none}
    .suggestion{padding:10px 12px;border-bottom:1px solid #edf0f4;cursor:pointer}.suggestion:hover{background:#eef4ff}.suggestion small{display:block;color:var(--muted);margin-top:3px}
    .summary{background:var(--ok);border:1px solid #bfe5cc}.result{border:1px solid var(--line);border-radius:14px;padding:18px;margin-top:14px}.result.best{border:2px solid #2a9d5b}
    .badges{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0}.badge{background:#eef3ff;border-radius:999px;padding:6px 9px;font-size:12px;font-weight:700}
    table{width:100%;border-collapse:collapse;margin-top:12px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e6ebf1}th{font-size:12px;color:var(--muted)}
    .money{font-weight:800;white-space:nowrap}.warning{background:#fff6df;border:1px solid #f0d58a;padding:13px;border-radius:10px}
    @media(max-width:850px){body{padding:12px}.grid{grid-template-columns:1fr}.wide{grid-column:span 1}.table-wrap{overflow:auto}}
  </style>
</head>
<body><div class="wrap">
  <div class="card nav"><a href="/">Нерудные материалы</a><a href="/zbi">Калькулятор ЖБИ</a><a href="/carriers">Перевозчики</a></div>
  <div class="card">
    <h1>Калькулятор ЖБИ</h1>
    <p class="muted">В каталоге {{ product_count }} позиций от {{ supplier_count }} производителей. Тариф и грузоподъёмность берутся только из листа «Доставка».</p>
    <form method="post" id="zbiForm">
      <div class="grid">
        <div class="wide"><label>Адрес доставки</label><input name="address" value="{{ form.address }}" placeholder="Москва, улица и дом" required><div class="hint">Можно также ввести координаты: 55.75, 37.62</div></div>
        <div><label>Производитель</label><select name="supplier" id="supplier"><option value="">Любой — найти ближайшего</option>{% for item in suppliers %}<option value="{{ item }}" {% if form.supplier==item %}selected{% endif %}>{{ item }}</option>{% endfor %}</select></div>
        <div><label>Группа изделий</label><select name="group" id="group"><option value="">Все ЖБИ / любое изделие</option>{% for item in groups %}<option value="{{ item }}" {% if form.group==item %}selected{% endif %}>{{ item }} — любые</option>{% endfor %}</select></div>
        <div class="wide search-wrap"><label>Поиск и выбор изделия</label><input id="productSearch" autocomplete="off" value="{{ form.search }}" placeholder="Например: кольцо КС 15-9 или бордюр"><input type="hidden" name="product_id" id="productId" value="{{ form.product_id }}"><input type="hidden" name="search" id="searchValue" value="{{ form.search }}"><div class="suggestions" id="suggestions"></div><div class="hint">Можно выбрать точную позицию или написать часть названия и оставить без выбора.</div></div>
        <div><label>Количество, шт.</label><input type="number" min="1" step="1" name="quantity" value="{{ form.quantity }}" required></div>
        <div><label>Наценка менеджера, %</label><input type="number" min="0" step="0.1" name="markup" value="{{ form.markup }}" required></div>
        <div><label>&nbsp;</label><button type="submit">Рассчитать стоимость</button></div>
      </div>
    </form>
  </div>
  {% if error %}<div class="card warning"><b>Не удалось выполнить расчёт.</b> {{ error }}</div>{% endif %}
  {% if results %}
  <div class="card summary"><h2>Рекомендация</h2><p>Ближайший подходящий производитель: <b>{{ results[0].supplier }}</b>, расстояние {{ results[0].distance|round(1) }} км. Ниже показана закупка, доставка и продажа с наценкой {{ markup }}%.</p></div>
  <div class="card"><h2>Подходящие варианты</h2><p class="muted">Сначала ближайшие производители, внутри одного производителя — меньшая стоимость единицы с доставкой. Показано до 50 вариантов.</p>
  {% for item in results %}<article class="result {% if loop.first %}best{% endif %}">
    <h3>{{ item.name }}</h3><div class="badges"><span class="badge">{{ item.supplier }}</span><span class="badge">{{ item.group }}</span><span class="badge">{{ item.weight_kg|round(1) }} кг/шт.</span><span class="badge">{{ item.distance|round(1) }} км · {{ item.distance_kind }}</span></div>
    <div class="muted">{{ item.size_mm }}{% if item.note %} · {{ item.note }}{% endif %}</div>
    <div class="table-wrap"><table><thead><tr><th>Сценарий</th><th>Количество</th><th>Машина</th><th>Рейсов</th><th>Доставка</th><th>Закупка с доставкой, 1 шт.</th><th>Продажа, 1 шт.</th><th>Продажа всего</th></tr></thead><tbody>
      <tr><td>Заявка менеджера</td><td>{{ quantity }}</td><td>{{ item.delivery.vehicle }} · {{ item.delivery.capacity_t|round(0)|int }} т</td><td>{{ item.delivery.trips }}</td><td class="money">{{ item.delivery.request_delivery|money }} ₽</td><td class="money">{{ item.request_delivered_unit|money }} ₽</td><td class="money">{{ item.request_sale_unit|money }} ₽</td><td class="money">{{ item.request_sale_total|money }} ₽</td></tr>
      <tr><td>Полная загрузка</td><td>{{ item.delivery.units_full }}</td><td>{{ item.delivery.vehicle }} · {{ item.delivery.capacity_t|round(0)|int }} т</td><td>1</td><td class="money">{{ item.delivery.full_delivery|money }} ₽</td><td class="money">{{ item.full_delivered_unit|money }} ₽</td><td class="money">{{ item.full_sale_unit|money }} ₽</td><td class="money">{{ item.full_sale_total|money }} ₽</td></tr>
    </tbody></table></div>
    <p class="muted">Цена завода: {{ item.price_rub|money }} ₽/шт. · Тариф: {{ item.delivery.rate|money }} ₽/км · Загрузка: {{ item.delivery.loading_address }}</p>
  </article>{% endfor %}</div>
  {% endif %}
</div>
<script>
const catalog={{ catalog_json|safe }}; const supplier=document.getElementById('supplier'); const group=document.getElementById('group'); const input=document.getElementById('productSearch'); const box=document.getElementById('suggestions'); const idField=document.getElementById('productId'); const searchField=document.getElementById('searchValue');
function visibleProducts(){const q=input.value.trim().toLowerCase();return catalog.filter(p=>(!supplier.value||p.supplier===supplier.value)&&(!group.value||p.group===group.value)&&(!q||p.name.toLowerCase().includes(q)||p.size_mm.toLowerCase().includes(q))).slice(0,60)}
function show(){const rows=visibleProducts();box.innerHTML=rows.map(p=>`<div class="suggestion" data-id="${p.id}"><b>${p.name}</b><small>${p.supplier} · ${p.group} · ${p.weight_kg||'масса не указана'} кг · ${p.price_rub} ₽</small></div>`).join('')||'<div class="suggestion">Совпадений нет</div>';box.style.display='block';box.querySelectorAll('[data-id]').forEach(el=>el.onclick=()=>{const p=catalog.find(x=>x.id===el.dataset.id);input.value=p.name;idField.value=p.id;searchField.value=p.name;box.style.display='none'})}
input.addEventListener('input',()=>{idField.value='';searchField.value=input.value;show()});input.addEventListener('focus',show);supplier.addEventListener('change',()=>{idField.value='';show()});group.addEventListener('change',()=>{idField.value='';show()});document.addEventListener('click',e=>{if(!e.target.closest('.search-wrap'))box.style.display='none'});document.getElementById('zbiForm').addEventListener('submit',()=>searchField.value=input.value);
</script></body></html>
"""


@zbi_bp.app_template_filter("money")
def money(value):
    return f"{float(value):,.0f}".replace(",", " ")


@zbi_bp.route("", methods=["GET", "POST"])
@zbi_bp.route("/", methods=["GET", "POST"])
def calculator():
    products, delivery_rows = load_catalog()
    suppliers = sorted({item["supplier"] for item in products})
    groups = sorted({item["group"] for item in products})
    form = {
        "address": request.form.get("address", ""),
        "supplier": request.form.get("supplier", ""),
        "group": request.form.get("group", ""),
        "product_id": request.form.get("product_id", ""),
        "search": request.form.get("search", ""),
        "quantity": request.form.get("quantity", "1"),
        "markup": request.form.get("markup", "10"),
    }
    error = ""
    results = []
    quantity = max(1, int(_float(form["quantity"], 1)))
    markup = max(0, _float(form["markup"], 0))

    if request.method == "POST":
        lat, lon = geocode(form["address"])
        if lat is None:
            error = "Адрес не найден. Проверьте адрес или введите координаты через запятую."
        else:
            candidates = products
            if form["product_id"]:
                candidates = [item for item in candidates if item["id"] == form["product_id"]]
            else:
                if form["supplier"]:
                    candidates = [item for item in candidates if item["supplier"] == form["supplier"]]
                if form["group"]:
                    candidates = [item for item in candidates if item["group"] == form["group"]]
                query = form["search"].strip().casefold()
                if query:
                    candidates = [item for item in candidates if query in item["name"].casefold() or query in item["size_mm"].casefold()]
            if not candidates:
                error = "По выбранным параметрам изделий не найдено."
            else:
                supplier_distance = {}
                for supplier_name in {item["supplier"] for item in candidates}:
                    rows = [item for item in delivery_rows if item["supplier"] == supplier_name]
                    if not rows:
                        continue
                    distance, kind = road_distance_km(rows[0]["lat"], rows[0]["lon"], lat, lon)
                    supplier_distance[supplier_name] = (distance, kind, rows)
                for product in candidates:
                    info = supplier_distance.get(product["supplier"])
                    if not info:
                        continue
                    distance, kind, options = info
                    delivery = delivery_calculation(product, quantity, distance, options)
                    if not delivery:
                        continue
                    request_delivered_unit = product["price_rub"] + delivery["request_delivery_unit"]
                    full_delivered_unit = product["price_rub"] + delivery["full_delivery_unit"]
                    item = dict(product)
                    item.update({
                        "distance": distance,
                        "distance_kind": kind,
                        "delivery": delivery,
                        "request_delivered_unit": request_delivered_unit,
                        "request_sale_unit": request_delivered_unit * (1 + markup / 100),
                        "request_sale_total": request_delivered_unit * (1 + markup / 100) * quantity,
                        "full_delivered_unit": full_delivered_unit,
                        "full_sale_unit": full_delivered_unit * (1 + markup / 100),
                        "full_sale_total": full_delivered_unit * (1 + markup / 100) * delivery["units_full"],
                    })
                    results.append(item)
                results.sort(key=lambda item: (item["distance"], item["request_delivered_unit"]))
                results = results[:50]
                if not results and not error:
                    error = "У подходящих изделий не указана масса либо изделие тяжелее доступной машины."

    catalog_for_js = [{key: item[key] for key in ("id", "supplier", "group", "name", "size_mm", "weight_kg", "price_rub")} for item in products]
    return render_template_string(
        PAGE,
        products=products,
        suppliers=suppliers,
        groups=groups,
        form=form,
        error=error,
        results=results,
        quantity=quantity,
        markup=markup,
        product_count=len(products),
        supplier_count=len(suppliers),
        catalog_json=json.dumps(catalog_for_js, ensure_ascii=False).replace("</", "<\\/"),
    )
