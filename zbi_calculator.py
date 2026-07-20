import csv
import json
import math
import os
import re
from collections import defaultdict
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


def parse_dimensions(size_text, weight_kg=0):
    """Return transport envelope dimensions in metres and its volume.

    The catalogue contains dimensions written with х, x, × and *.  For products
    without three dimensions we use a conservative volume estimate based on the
    concrete weight and mark it as estimated.
    """
    normalized = str(size_text or "").lower()
    for marker in ("×", "х", "x", "*", "õ"):
        normalized = normalized.replace(marker, "x")
    numbers = [_float(value) for value in re.findall(r"\d+(?:[.,]\d+)?", normalized)]
    numbers = [value for value in numbers if value > 0]
    if len(numbers) >= 3:
        metres = [value / 1000 for value in numbers[:3]]
        length, width, height = sorted(metres, reverse=True)
        return {
            "length_m": length,
            "width_m": width,
            "height_m": height,
            "volume_m3": length * width * height,
            "estimated": False,
        }
    estimated_volume = max(_float(weight_kg) / 2400 * 1.35, 0.03)
    return {
        "length_m": 0,
        "width_m": 0,
        "height_m": 0,
        "volume_m3": estimated_volume,
        "estimated": True,
    }


def load_catalog():
    with PRODUCTS_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        products = list(csv.DictReader(handle))
    for index, item in enumerate(products):
        if item.get("supplier") == "ИП Михайлов" and str(item.get("name") or "").strip().upper().startswith("ПД "):
            item["group"] = "Лотки и водоотвод"
        item["id"] = str(index)
        item["price_rub"] = _float(item.get("price_rub"))
        item["weight_kg"] = _float(item.get("weight_kg"))
        item["dimensions"] = parse_dimensions(item.get("size_mm"), item["weight_kg"])
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
    lat1, lon1, lat2, lon2 = [math.radians(value) for value in (lat1, lon1, lat2, lon2)]
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
    return haversine_km(start_lat, start_lon, end_lat, end_lon) * 1.25, "оценка по прямой × 1,25"


def vehicle_profile(option):
    name = str(option.get("vehicle") or "").casefold()
    if "манипулятор" in name:
        length, width, height = 6.5, 2.45, 2.5
    else:
        length, width, height = 13.6, 2.45, 2.5
    return {
        "length_m": length,
        "width_m": width,
        "height_m": height,
        "usable_volume_m3": length * width * height * 0.78,
    }


def mixed_delivery_calculation(lines, distance, delivery_options):
    total_weight_kg = sum(line["weight_total_kg"] for line in lines)
    total_volume_m3 = sum(line["volume_total_m3"] for line in lines)
    alternatives = []
    for option in delivery_options:
        capacity_kg = option["capacity_t"] * 1000
        if capacity_kg <= 0:
            continue
        profile = vehicle_profile(option)
        oversize = []
        for line in lines:
            dims = line["dimensions"]
            if dims["estimated"]:
                continue
            if (
                dims["length_m"] > profile["length_m"]
                or dims["width_m"] > profile["width_m"]
                or dims["height_m"] > profile["height_m"]
            ):
                oversize.append(line["name"])
        if oversize:
            continue
        weight_trips = max(1, math.ceil(total_weight_kg / capacity_kg))
        volume_trips = max(1, math.ceil(total_volume_m3 / profile["usable_volume_m3"]))
        trips = max(weight_trips, volume_trips)
        trip_price = distance * option["rate_rub_km"]
        alternatives.append({
            "vehicle": option["vehicle"],
            "capacity_t": option["capacity_t"],
            "rate_rub_km": option["rate_rub_km"],
            "trips": trips,
            "weight_trips": weight_trips,
            "volume_trips": volume_trips,
            "delivery_total": trip_price * trips,
            "trip_price": trip_price,
            "loading_address": option["loading_address"],
            "profile": profile,
            "weight_load_pct": total_weight_kg / (capacity_kg * trips) * 100,
            "volume_load_pct": total_volume_m3 / (profile["usable_volume_m3"] * trips) * 100,
            "limiting_factor": "вес" if weight_trips >= volume_trips else "габаритный объём",
        })
    if not alternatives:
        return None
    return min(alternatives, key=lambda item: (item["delivery_total"], item["trips"], -item["capacity_t"]))


def parse_cart(raw_value, product_by_id):
    try:
        raw_items = json.loads(raw_value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_items = []
    merged = defaultdict(int)
    for item in raw_items if isinstance(raw_items, list) else []:
        product_id = str(item.get("id", ""))
        quantity = max(1, int(_float(item.get("quantity"), 1)))
        if product_id in product_by_id:
            merged[product_id] += quantity
    return [{"id": product_id, "quantity": quantity} for product_id, quantity in merged.items()]


def build_quote(cart, product_by_id, delivery_rows, lat, lon, markup):
    supplier_lines = defaultdict(list)
    all_lines = []
    for cart_item in cart:
        product = product_by_id[cart_item["id"]]
        quantity = cart_item["quantity"]
        dimensions = product["dimensions"]
        line = dict(product)
        line.update({
            "quantity": quantity,
            "dimensions": dimensions,
            "weight_total_kg": product["weight_kg"] * quantity,
            "volume_total_m3": dimensions["volume_m3"] * quantity,
            "purchase_total": product["price_rub"] * quantity,
            "sale_unit": product["price_rub"] * (1 + markup / 100),
            "sale_total": product["price_rub"] * quantity * (1 + markup / 100),
        })
        supplier_lines[product["supplier"]].append(line)
        all_lines.append(line)

    deliveries = []
    errors = []
    for supplier, lines in supplier_lines.items():
        options = [row for row in delivery_rows if row["supplier"] == supplier]
        if not options:
            errors.append(f"Для производителя «{supplier}» нет тарифа доставки.")
            continue
        distance, distance_kind = road_distance_km(options[0]["lat"], options[0]["lon"], lat, lon)
        calculation = mixed_delivery_calculation(lines, distance, options)
        if not calculation:
            errors.append(f"Заявка производителя «{supplier}» не помещается в доступный транспорт по весу или габаритам.")
            continue
        deliveries.append({
            "supplier": supplier,
            "lines": lines,
            "distance": distance,
            "distance_kind": distance_kind,
            "weight_total_kg": sum(line["weight_total_kg"] for line in lines),
            "volume_total_m3": sum(line["volume_total_m3"] for line in lines),
            **calculation,
        })

    purchase_total = sum(line["purchase_total"] for line in all_lines)
    sale_total = sum(line["sale_total"] for line in all_lines)
    delivery_total = sum(item["delivery_total"] for item in deliveries)
    return {
        "lines": all_lines,
        "deliveries": deliveries,
        "errors": errors,
        "purchase_total": purchase_total,
        "sale_total": sale_total,
        "delivery_total": delivery_total,
        "client_total": sale_total + delivery_total,
        "weight_total_kg": sum(line["weight_total_kg"] for line in all_lines),
        "volume_total_m3": sum(line["volume_total_m3"] for line in all_lines),
    }


PAGE = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Калькулятор заявки ЖБИ</title>
<style>
:root{--ink:#18202a;--muted:#647184;--blue:#185bd8;--line:#dce3ec;--bg:#f3f6fa;--ok:#e9f8ef;--danger:#a62d2d}
*{box-sizing:border-box}body{font-family:Arial,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:24px}.wrap{max-width:1380px;margin:auto}.card{background:#fff;border-radius:16px;padding:22px;margin-bottom:18px;box-shadow:0 5px 18px #20305012}h1,h2,h3{margin-top:0}.nav a{margin-right:18px;color:var(--blue);font-weight:700;text-decoration:none}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.span2{grid-column:span 2}.span4{grid-column:span 4}label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}input,select,button{width:100%;min-height:44px;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:15px;background:#fff}button{background:var(--blue);border-color:var(--blue);color:#fff;font-weight:700;cursor:pointer}.secondary{background:#fff;color:var(--blue)}.remove{background:#fff;color:var(--danger);border-color:#e6bcbc;padding:7px;min-height:34px}.hint,.muted{color:var(--muted);font-size:13px}.search-wrap{position:relative}.suggestions{position:absolute;z-index:20;left:0;right:0;top:74px;max-height:320px;overflow:auto;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 28px #10203026;display:none}.suggestion{display:block;width:100%;min-height:0;padding:10px 12px;background:#fff;color:var(--ink);text-align:left;border:0;border-bottom:1px solid #edf0f4;border-radius:0;cursor:pointer}.suggestion:hover{background:#eef4ff}.suggestion small{display:block;color:var(--muted);margin-top:3px;font-weight:400}.summary{background:var(--ok);border:1px solid #bfe5cc}.warning{background:#fff6df;border:1px solid #f0d58a}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.kpi{background:#f5f8fc;border-radius:12px;padding:14px}.kpi b{display:block;font-size:22px;margin-top:4px}.delivery{border:1px solid var(--line);border-radius:14px;padding:16px;margin-top:12px}.badges{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0}.badge{background:#eef3ff;border-radius:999px;padding:6px 9px;font-size:12px;font-weight:700}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e6ebf1;vertical-align:top}th{font-size:12px;color:var(--muted)}.money{font-weight:800;white-space:nowrap}.empty{text-align:center;color:var(--muted);padding:24px}.actions{display:flex;gap:10px;align-items:end}.actions>*{flex:1}
@media(max-width:900px){body{padding:12px}.grid{grid-template-columns:1fr}.span2,.span4{grid-column:span 1}.kpis{grid-template-columns:1fr 1fr}.actions{display:block}.actions>*{margin-top:8px}}
</style></head><body><div class="wrap">
<div class="card nav"><a href="/">Нерудные материалы</a><a href="/zbi">Калькулятор ЖБИ</a><a href="/carriers">Перевозчики</a></div>
<div class="card"><h1>Расчёт заявки ЖБИ</h1><p class="muted">Добавьте несколько изделий. Машина и количество рейсов определяются по суммарному весу и транспортным габаритам. Товары разных производителей рассчитываются отдельными доставками.</p>
<form method="post" id="quoteForm"><input type="hidden" name="items_json" id="itemsJson">
<div class="grid"><div class="span2"><label>Адрес доставки</label><input name="address" value="{{ form.address }}" placeholder="Москва, улица и дом" required></div><div><label>Наценка на товары, %</label><input type="number" min="0" step="0.1" name="markup" value="{{ form.markup }}" required></div><div><label>&nbsp;</label><button type="submit">Рассчитать всю заявку</button></div></div>
</form></div>
<div class="card"><h2>Добавить изделие</h2><div class="grid">
<div><label>Производитель</label><select id="supplier"><option value="">Любой производитель</option>{% for item in suppliers %}<option value="{{ item }}">{{ item }}</option>{% endfor %}</select></div>
<div><label>Раздел</label><select id="group"><option value="">Все изделия / любые</option>{% for item in groups %}<option value="{{ item }}">{{ item }} — любые</option>{% endfor %}</select></div>
<div class="span2 search-wrap"><label>Поиск изделия</label><input id="productSearch" autocomplete="off" placeholder="Например: ФБС 24.4.6, 2П 30.18 или лоток"><div id="suggestions" class="suggestions"></div></div>
<div><label>Количество, шт.</label><input id="addQuantity" type="number" min="1" step="1" value="1"></div><div class="span2"><label>Выбрано</label><input id="selectedName" readonly placeholder="Сначала выберите позицию из подсказки"></div><div><label>&nbsp;</label><button type="button" id="addItem">Добавить в заявку</button></div>
</div></div>
<div class="card"><h2>Состав заявки</h2><div class="table-wrap"><table><thead><tr><th>Изделие</th><th>Производитель</th><th>Габариты</th><th>Масса 1 шт.</th><th>Количество</th><th>Общая масса</th><th>Цена завода без доставки</th><th></th></tr></thead><tbody id="cartBody"></tbody></table></div><div id="emptyCart" class="empty">В заявке пока нет изделий</div></div>
{% if error %}<div class="card warning"><b>Не удалось выполнить расчёт.</b> {{ error }}</div>{% endif %}
{% if quote %}
<div class="card summary"><h2>Итог заявки</h2><div class="kpis"><div class="kpi">Товары по закупке<b>{{ quote.purchase_total|money }} ₽</b><span class="muted">без доставки</span></div><div class="kpi">Продажа товаров<b>{{ quote.sale_total|money }} ₽</b><span class="muted">с наценкой {{ markup }}%, без доставки</span></div><div class="kpi">Доставка отдельно<b>{{ quote.delivery_total|money }} ₽</b><span class="muted">до объекта</span></div><div class="kpi">Итого клиенту<b>{{ quote.client_total|money }} ₽</b><span class="muted">товары + доставка</span></div></div><p><b>Общая масса:</b> {{ quote.weight_total_kg|round(0)|int }} кг · <b>транспортный габаритный объём:</b> {{ quote.volume_total_m3|round(2) }} м³</p></div>
<div class="card"><h2>Цена изделий без доставки</h2><div class="table-wrap"><table><thead><tr><th>Изделие</th><th>Кол-во</th><th>Габариты</th><th>Масса</th><th>Цена завода за 1 шт.</th><th>Цена продажи за 1 шт.</th><th>Продажа всего</th></tr></thead><tbody>{% for line in quote.lines %}<tr><td><b>{{ line.name }}</b><div class="muted">{{ line.supplier }} · {{ line.group }}</div></td><td>{{ line.quantity }}</td><td>{{ line.size_mm }}{% if line.dimensions.estimated %}<div class="muted">объём оценён по массе</div>{% endif %}</td><td>{{ line.weight_kg|round(1) }} кг/шт.<br><b>{{ line.weight_total_kg|round(0)|int }} кг всего</b></td><td class="money">{{ line.price_rub|money }} ₽</td><td class="money">{{ line.sale_unit|money }} ₽</td><td class="money">{{ line.sale_total|money }} ₽</td></tr>{% endfor %}</tbody></table></div></div>
<div class="card"><h2>Доставка до объекта — отдельно</h2>{% for item in quote.deliveries %}<div class="delivery"><h3>{{ item.supplier }}</h3><div class="badges"><span class="badge">{{ item.vehicle }} · {{ item.capacity_t|round(0)|int }} т</span><span class="badge">{{ item.trips }} рейс(а)</span><span class="badge">{{ item.distance|round(1) }} км · {{ item.distance_kind }}</span><span class="badge">ограничение: {{ item.limiting_factor }}</span></div><p><b>{{ item.delivery_total|money }} ₽ за доставку</b> · {{ item.rate_rub_km|money }} ₽/км · масса {{ item.weight_total_kg|round(0)|int }} кг · габаритный объём {{ item.volume_total_m3|round(2) }} м³</p><p class="muted">Средняя загрузка одного рейса: по массе {{ item.weight_load_pct|round(0)|int }}%, по объёму {{ item.volume_load_pct|round(0)|int }}%. Погрузка: {{ item.loading_address }}</p></div>{% endfor %}{% for message in quote.errors %}<div class="warning">{{ message }}</div>{% endfor %}</div>
{% endif %}
</div><script>
const catalog={{ catalog_json|safe }};let cart={{ cart_json|safe }};let selectedId='';
const supplier=document.getElementById('supplier'),group=document.getElementById('group'),search=document.getElementById('productSearch'),box=document.getElementById('suggestions'),selectedName=document.getElementById('selectedName'),qty=document.getElementById('addQuantity'),body=document.getElementById('cartBody'),empty=document.getElementById('emptyCart'),itemsJson=document.getElementById('itemsJson');
const esc=s=>String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
function filtered(){const q=search.value.trim().toLowerCase();if(q.length<2)return[];return catalog.filter(p=>(!supplier.value||p.supplier===supplier.value)&&(!group.value||p.group===group.value)&&(p.name.toLowerCase().includes(q)||p.size_mm.toLowerCase().includes(q))).slice(0,50)}
function show(){const rows=filtered();if(search.value.trim().length<2){box.style.display='none';return}box.innerHTML=rows.map(p=>`<button type="button" class="suggestion" data-id="${p.id}"><b>${esc(p.name)}</b><small>${esc(p.supplier)} · ${esc(p.group)} · ${esc(p.size_mm)} · ${p.weight_kg||'масса не указана'} кг · ${p.price_rub} ₽</small></button>`).join('')||'<div class="suggestion">Совпадений нет</div>';box.style.display='block';box.querySelectorAll('[data-id]').forEach(el=>el.onclick=()=>{const p=catalog.find(x=>x.id===el.dataset.id);selectedId=p.id;selectedName.value=p.name;search.value=p.name;box.style.display='none'})}
function resetSearch(){selectedId='';selectedName.value='';search.value='';box.style.display='none'}supplier.addEventListener('change',resetSearch);group.addEventListener('change',resetSearch);search.addEventListener('input',()=>{selectedId='';selectedName.value='';show()});search.addEventListener('focus',show);document.addEventListener('click',e=>{if(!e.target.closest('.search-wrap'))box.style.display='none'});
function render(){body.innerHTML=cart.map((item,i)=>{const p=catalog.find(x=>x.id===item.id);const total=p.weight_kg*item.quantity;return`<tr><td><b>${esc(p.name)}</b><div class="muted">${esc(p.group)}</div></td><td>${esc(p.supplier)}</td><td>${esc(p.size_mm)}</td><td>${p.weight_kg} кг</td><td><input class="cartQty" data-index="${i}" type="number" min="1" step="1" value="${item.quantity}"></td><td><b>${Math.round(total)} кг</b></td><td class="money">${Math.round(p.price_rub).toLocaleString('ru-RU')} ₽/шт.</td><td><button type="button" class="remove" data-index="${i}">Удалить</button></td></tr>`}).join('');empty.style.display=cart.length?'none':'block';itemsJson.value=JSON.stringify(cart);document.querySelectorAll('.cartQty').forEach(el=>el.onchange=()=>{cart[+el.dataset.index].quantity=Math.max(1,parseInt(el.value)||1);render()});document.querySelectorAll('.remove').forEach(el=>el.onclick=()=>{cart.splice(+el.dataset.index,1);render()})}
document.getElementById('addItem').onclick=()=>{if(!selectedId){alert('Выберите точное изделие из подсказки');return}const amount=Math.max(1,parseInt(qty.value)||1);const old=cart.find(x=>x.id===selectedId);if(old)old.quantity+=amount;else cart.push({id:selectedId,quantity:amount});qty.value=1;resetSearch();render()};document.getElementById('quoteForm').onsubmit=e=>{if(!cart.length){e.preventDefault();alert('Добавьте хотя бы одно изделие в заявку');return}itemsJson.value=JSON.stringify(cart)};render();
</script></body></html>
"""


@zbi_bp.app_template_filter("money")
def money(value):
    return f"{float(value):,.0f}".replace(",", " ")


@zbi_bp.route("", methods=["GET", "POST"])
@zbi_bp.route("/", methods=["GET", "POST"])
def calculator():
    products, delivery_rows = load_catalog()
    product_by_id = {item["id"]: item for item in products}
    form = {
        "address": request.form.get("address", ""),
        "markup": request.form.get("markup", "10"),
    }
    markup = max(0, _float(form["markup"], 0))
    cart = parse_cart(request.form.get("items_json", "[]"), product_by_id)
    quote = None
    error = ""
    if request.method == "POST":
        if not cart:
            error = "Добавьте хотя бы одно изделие в заявку."
        else:
            lat, lon = geocode(form["address"])
            if lat is None:
                error = "Адрес не найден. Проверьте адрес или введите координаты через запятую."
            else:
                quote = build_quote(cart, product_by_id, delivery_rows, lat, lon, markup)
                if not quote["deliveries"]:
                    error = "Не удалось подобрать транспорт ни для одного производителя."
    catalog_for_js = [{
        key: item[key] for key in ("id", "supplier", "group", "name", "size_mm", "weight_kg", "price_rub")
    } for item in products]
    return render_template_string(
        PAGE,
        suppliers=sorted({item["supplier"] for item in products}),
        groups=sorted({item["group"] for item in products}),
        form=form,
        markup=markup,
        quote=quote,
        error=error,
        catalog_json=json.dumps(catalog_for_js, ensure_ascii=False).replace("</", "<\\/"),
        cart_json=json.dumps(cart, ensure_ascii=False).replace("</", "<\\/"),
    )
