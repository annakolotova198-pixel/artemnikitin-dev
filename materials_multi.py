import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape

from flask import Blueprint, request


materials_multi_bp = Blueprint("materials_multi", __name__)


def _number(value, default=0):
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def _route_key(row):
    return (
        str(row["Название"]),
        round(float(row["Широта"]), 6),
        round(float(row["Долгота"]), 6),
    )


def _parse_cart(raw_value):
    try:
        raw_items = json.loads(raw_value or "[]")
    except (TypeError, ValueError):
        raw_items = []
    result = []
    for item in raw_items:
        selection = str(item.get("selection") or "").strip()
        label = str(item.get("label") or selection).strip()
        volume = _number(item.get("volume"))
        if selection and volume > 0:
            result.append({
                "selection": selection,
                "label": label,
                "volume": volume,
            })
    return result[:25]


def _calculate_options(df, cart, client_lat, client_lon, carrier_rate, sale_rate):
    from app import filter_material_selection, get_route, haversine_distance_km

    prepared = []
    route_candidates = {}
    for line_index, line in enumerate(cart):
        filtered = filter_material_selection(df, line["selection"]).copy()
        if filtered.empty:
            prepared.append((line_index, line, filtered))
            continue
        filtered["approx_distance"] = filtered.apply(
            lambda row: haversine_distance_km(
                row["Широта"], row["Долгота"], client_lat, client_lon
            ),
            axis=1,
        )
        nearest = (
            filtered.sort_values("approx_distance")
            .drop_duplicates(subset=["Название", "Широта", "Долгота"])
            .head(35)
        )
        allowed_keys = {_route_key(row) for _, row in nearest.iterrows()}
        filtered = filtered[
            filtered.apply(lambda row: _route_key(row) in allowed_keys, axis=1)
        ]
        prepared.append((line_index, line, filtered))
        for _, row in nearest.iterrows():
            route_candidates[_route_key(row)] = row

    route_cache = {}

    def fetch(row):
        key = _route_key(row)
        distance, duration = get_route(
            row["Широта"], row["Долгота"], client_lat, client_lon
        )
        return key, distance, duration

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(fetch, row) for row in route_candidates.values()]
        for future in as_completed(futures):
            try:
                key, distance, duration = future.result()
                if distance is not None:
                    route_cache[key] = (distance, duration)
            except Exception:
                continue

    options_by_line = {}
    for line_index, line, filtered in prepared:
        options = []
        for _, row in filtered.iterrows():
            key = _route_key(row)
            if key not in route_cache:
                continue
            distance, duration = route_cache[key]
            material_price = _number(row["Цена м3"])
            purchase_unit = material_price + distance * carrier_rate
            client_unit = material_price + distance * sale_rate
            options.append({
                "requested_label": line["label"],
                "volume": line["volume"],
                "career": str(row["Название"]),
                "legal": str(row.get("Юр лицо", "") or "Не указано"),
                "material": str(row["Вид товара"]),
                "address": str(row.get("Адрес", "") or "Адрес не указан"),
                "phone": str(row.get("Телефон", "") or "Не указан"),
                "distance": round(distance, 1),
                "duration": round(duration or 0),
                "material_price": round(material_price, 2),
                "purchase_unit": round(purchase_unit, 2),
                "client_unit": round(client_unit, 2),
                "purchase_total": round(purchase_unit * line["volume"], 2),
                "client_total": round(client_unit * line["volume"], 2),
                "profit_total": round((client_unit - purchase_unit) * line["volume"], 2),
                "lat": float(row["Широта"]),
                "lon": float(row["Долгота"]),
            })
        options_by_line[line_index] = options
    return options_by_line


def _make_plan(cart, options_by_line, mode):
    rows = []
    missing = []
    for index, line in enumerate(cart):
        options = options_by_line.get(index, [])
        if not options:
            missing.append(line["label"])
            continue
        if mode == "shortest":
            selected = min(
                options,
                key=lambda item: (item["distance"], item["client_unit"], item["career"]),
            )
        else:
            selected = min(
                options,
                key=lambda item: (item["client_unit"], item["distance"], item["career"]),
            )
        rows.append(selected)
    return {
        "rows": rows,
        "missing": missing,
        "purchase_total": sum(row["purchase_total"] for row in rows),
        "client_total": sum(row["client_total"] for row in rows),
        "profit_total": sum(row["profit_total"] for row in rows),
    }


def _render_plan(plan, title):
    rows = []
    for item in plan["rows"]:
        map_url = (
            "https://yandex.ru/maps/?pt="
            + str(item["lon"])
            + ","
            + str(item["lat"])
            + "&z=13&l=map"
        )
        rows.append(
            "<tr>"
            f"<td><b>{escape(item['requested_label'])}</b><br>"
            f"<small>Подобрано: {escape(item['material'])}</small></td>"
            f"<td>{item['volume']:g} м³</td>"
            f"<td><b>{escape(item['career'])}</b><br>"
            f"<small>{escape(item['address'])}<br>{escape(item['phone'])}</small></td>"
            f"<td>{item['distance']:g} км<br><small>{item['duration']} мин.</small></td>"
            f"<td>{item['material_price']:,.0f} ₽</td>"
            f"<td>{item['purchase_unit']:,.0f} ₽</td>"
            f"<td>{item['client_unit']:,.0f} ₽</td>"
            f"<td>{item['client_total']:,.0f} ₽</td>"
            f"<td><a href=\"{escape(map_url, quote=True)}\" target=\"_blank\">Карта</a></td>"
            "</tr>"
        )
    missing = ""
    if plan["missing"]:
        missing = (
            "<div class='alert'>Не найдены варианты: "
            + ", ".join(escape(item) for item in plan["missing"])
            + "</div>"
        )
    return (
        f"<h2>{escape(title)}</h2>{missing}"
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Запрошено / подобрано</th><th>Объём</th><th>Карьер</th>"
        "<th>Маршрут</th><th>Материал, ₽/м³</th><th>Закупка с доставкой, ₽/м³</th>"
        "<th>Цена клиенту, ₽/м³</th><th>Сумма клиенту</th><th></th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        "<div class='totals'>"
        f"<span>Закупка: <b>{plan['purchase_total']:,.0f} ₽</b></span>"
        f"<span>Продажа: <b>{plan['client_total']:,.0f} ₽</b></span>"
        f"<span>Маржа: <b>{plan['profit_total']:,.0f} ₽</b></span>"
        "</div>"
    ).replace(",", " ")


@materials_multi_bp.route("/materials-request", methods=["GET", "POST"])
def materials_request():
    from app import build_material_suggestions, geocode_address, load_data

    df = load_data()
    suggestions = build_material_suggestions(df)
    suggestions_json = json.dumps(suggestions, ensure_ascii=False).replace("</", "<\\/")
    result_html = ""
    preserved_cart = []
    active_mode = request.form.get("mode", "cheapest")
    if request.method == "POST":
        preserved_cart = _parse_cart(request.form.get("materials_json"))
        address = request.form.get("address", "").strip()
        carrier_rate = _number(request.form.get("carrier_rate"))
        sale_rate = _number(request.form.get("sale_rate"))
        if not preserved_cart:
            result_html = "<div class='alert'>Добавьте хотя бы один материал в заявку.</div>"
        elif carrier_rate < 0 or sale_rate < 0:
            result_html = "<div class='alert'>Тарифы доставки не могут быть отрицательными.</div>"
        else:
            client_lat, client_lon = geocode_address(address)
            if client_lat is None or client_lon is None:
                result_html = "<div class='alert'>Адрес не найден. Укажите город, улицу и дом.</div>"
            else:
                options = _calculate_options(
                    df,
                    preserved_cart,
                    client_lat,
                    client_lon,
                    carrier_rate,
                    sale_rate,
                )
                cheapest = _make_plan(preserved_cart, options, "cheapest")
                shortest = _make_plan(preserved_cart, options, "shortest")
                result_html = (
                    "<section class='results'>"
                    "<div class='result-tabs'>"
                    "<button type='button' data-result-tab='cheapest'>Самый дешёвый</button>"
                    "<button type='button' data-result-tab='shortest'>Самый короткий</button>"
                    "</div>"
                    "<div class='result-panel' data-result-panel='cheapest'>"
                    + _render_plan(cheapest, "Самый дешёвый вариант по каждой позиции")
                    + "</div>"
                    "<div class='result-panel' data-result-panel='shortest'>"
                    + _render_plan(shortest, "Самый короткий маршрут по каждой позиции")
                    + "</div></section>"
                )

    cart_json = json.dumps(preserved_cart, ensure_ascii=False).replace("</", "<\\/")
    address_value = escape(request.form.get("address", ""), quote=True)
    carrier_value = escape(request.form.get("carrier_rate", "15"), quote=True)
    sale_value = escape(request.form.get("sale_rate", "25"), quote=True)
    page = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Многопозиционная заявка — нерудные материалы</title>
<style>
:root{--blue:#0b5cab;--ink:#172033;--line:#dce3ec;--bg:#f4f7fb;--gold:#f4a300}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font:15px Arial,sans-serif;color:var(--ink)}
.wrap{max-width:1500px;margin:auto;padding:24px}.top{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
a{color:var(--blue)}h1{margin:8px 0}.card,.results{background:#fff;border-radius:16px;padding:22px;margin-top:18px;box-shadow:0 5px 24px #1b365d13}
.grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px}.field{position:relative}label{display:block;font-weight:700;margin:0 0 7px}
input{width:100%;padding:12px;border:1px solid #b9c5d3;border-radius:10px;font-size:16px}
.search-row{display:grid;grid-template-columns:minmax(260px,2fr) minmax(130px,.7fr) auto;gap:10px;align-items:end;margin-top:18px}
button{border:0;border-radius:10px;padding:12px 18px;font-weight:700;cursor:pointer;background:var(--blue);color:#fff}
.dropdown{position:absolute;z-index:20;left:0;right:0;top:72px;max-height:350px;overflow:auto;background:#fff;border:1px solid var(--line);border-radius:10px;display:none;box-shadow:0 12px 28px #18294230}
.dropdown.open{display:block}.option{padding:11px;border-bottom:1px solid #edf1f6;cursor:pointer}.option:first-child{background:#fff7dc;font-weight:700}.option:hover{background:#edf5ff}
.cart{margin-top:18px}.cart-item{display:grid;grid-template-columns:1fr 150px auto;gap:10px;align-items:center;padding:10px;border:1px solid var(--line);border-radius:10px;margin-top:8px}
.cart-item input{padding:8px}.remove{background:#f1f3f6;color:#9e2834;padding:9px 12px}.submit{margin-top:18px;background:#0c7a44;font-size:17px}
.result-tabs{display:flex;gap:10px}.result-tabs button{background:#e8eef6;color:#1c314d}.result-tabs button.active{background:var(--blue);color:#fff}
.result-panel{display:none}.result-panel.active{display:block}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:1100px}th,td{padding:11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:#edf3f9}
.totals{display:flex;gap:24px;flex-wrap:wrap;background:#eef8f2;padding:15px;border-radius:10px;margin-top:15px}.alert{background:#fff0f1;color:#8f2430;padding:14px;border-radius:10px;margin:14px 0}
.hint{color:#657084;font-size:13px;margin-top:6px}
@media(max-width:800px){.wrap{padding:12px}.grid,.search-row{grid-template-columns:1fr}.cart-item{grid-template-columns:1fr 100px auto}.card{padding:15px}}
</style></head><body><main class="wrap">
<div class="top"><a href="/">← Обычный расчёт</a><a href="/zbi">Калькулятор ЖБИ</a><a href="/careers">Все карьеры</a></div>
<h1>Заявка из нескольких нерудных материалов</h1>
<p>Каждая позиция может быть подобрана в своём карьере. После расчёта переключайтесь между минимальной ценой и минимальным расстоянием.</p>
<form method="post" class="card" id="request-form">
<div class="grid">
<div><label>Адрес доставки</label><input name="address" value="__ADDRESS__" placeholder="Москва, Беговая улица, 22с41" required></div>
<div><label>Тариф перевозчика, ₽/км/м³</label><input name="carrier_rate" value="__CARRIER__" inputmode="decimal" required></div>
<div><label>Тариф клиенту, ₽/км/м³</label><input name="sale_rate" value="__SALE__" inputmode="decimal" required></div>
</div>
<div class="search-row">
<div class="field"><label>Материал</label><input id="material-input" autocomplete="off" placeholder="Например: известняковый щебень"><div id="dropdown" class="dropdown"></div><div class="hint">Сокращения в каталоге расшифрованы. Вариант «любой» показывается первым.</div></div>
<div><label>Объём, м³</label><input id="volume-input" inputmode="decimal" value="20"></div>
<button type="button" id="add-item">Добавить позицию</button>
</div>
<div id="cart" class="cart"></div>
<input type="hidden" name="materials_json" id="materials-json">
<input type="hidden" name="mode" value="__MODE__">
<button class="submit" type="submit">Рассчитать всю заявку</button>
</form>
__RESULT__
</main><script>
const suggestions=__SUGGESTIONS__;let cart=__CART__;let selected=null;
const input=document.getElementById("material-input"),dropdown=document.getElementById("dropdown"),volume=document.getElementById("volume-input");
function norm(v){return String(v||"").toLowerCase().replaceAll("ё","е").replace(/[^a-zа-я0-9]+/g," ").trim()}
function matches(item,q){const tokens=norm(q).split(" ").filter(Boolean);return tokens.every(t=>item.search.includes(t))}
function show(){const q=input.value;let items=suggestions.filter(x=>matches(x,q));if(q) {const families=new Set(items.map(x=>x.family));items=[...suggestions.filter(x=>x.kind==="any"&&families.has(x.family)),...items.filter(x=>x.kind==="product")]} else items=items.filter(x=>x.kind==="any");const seen=new Set();items=items.filter(x=>!seen.has(x.value)&&seen.add(x.value)).slice(0,100);dropdown.replaceChildren();items.forEach((item,i)=>{const d=document.createElement("div");d.className="option";d.textContent=item.label;d.onmousedown=e=>{e.preventDefault();selected=item;input.value=item.label;dropdown.classList.remove("open")};dropdown.appendChild(d)});dropdown.classList.toggle("open",items.length>0)}
input.onfocus=show;input.oninput=()=>{selected=null;show()};input.onblur=()=>setTimeout(()=>dropdown.classList.remove("open"),120);
function renderCart(){const root=document.getElementById("cart");root.replaceChildren();cart.forEach((item,index)=>{const row=document.createElement("div");row.className="cart-item";const title=document.createElement("b");title.textContent=item.label;const qty=document.createElement("input");qty.value=item.volume;qty.inputMode="decimal";qty.oninput=()=>item.volume=qty.value;const del=document.createElement("button");del.type="button";del.className="remove";del.textContent="Удалить";del.onclick=()=>{cart.splice(index,1);renderCart()};row.append(title,qty,del);root.appendChild(row)});document.getElementById("materials-json").value=JSON.stringify(cart)}
document.getElementById("add-item").onclick=()=>{if(!selected){show();input.focus();return}const amount=parseFloat(volume.value.replace(",","."));if(!(amount>0)){volume.focus();return}cart.push({selection:selected.value,label:selected.label,volume:amount});selected=null;input.value="";renderCart()};
document.getElementById("request-form").onsubmit=e=>{renderCart();if(!cart.length){e.preventDefault();alert("Добавьте хотя бы один материал")}};
renderCart();
const activeMode="__MODE__";document.querySelectorAll("[data-result-tab]").forEach(btn=>{btn.onclick=()=>{document.querySelectorAll("[data-result-tab]").forEach(x=>x.classList.toggle("active",x.dataset.resultTab===btn.dataset.resultTab));document.querySelectorAll("[data-result-panel]").forEach(x=>x.classList.toggle("active",x.dataset.resultPanel===btn.dataset.resultTab))}});
const initial=document.querySelector(`[data-result-tab="${activeMode}"]`)||document.querySelector("[data-result-tab]");if(initial)initial.click();
</script></body></html>"""
    return (
        page.replace("__ADDRESS__", address_value)
        .replace("__CARRIER__", carrier_value)
        .replace("__SALE__", sale_value)
        .replace("__MODE__", "shortest" if active_mode == "shortest" else "cheapest")
        .replace("__RESULT__", result_html)
        .replace("__SUGGESTIONS__", suggestions_json)
        .replace("__CART__", cart_json)
    )
