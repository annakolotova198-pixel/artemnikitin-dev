from crm_system import crm_bp
from flask import Flask, request, session, redirect, url_for
import pandas as pd
import requests
import json
import sqlite3
from functools import wraps
import math
from urllib.parse import quote, unquote, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from html import escape
import os
import time

app = Flask(__name__)
app.register_blueprint(crm_bp)
app.secret_key = "change-this-secret-key"

YANDEX_API_KEY = "aaaac1c5-442b-4970-9bd9-1f1929227a78"
CSV_FILE = "https://docs.google.com/spreadsheets/d/1Zb-38mYR63KCnI7JjTZGoedm9LGFZ3snfhoaYBMwuwo/export?format=csv&gid=0"
CARRIERS_FILE = os.path.join(os.path.dirname(__file__), "carriers.csv")

def geocode_address(address):
    url = "https://geocode-maps.yandex.ru/1.x/"
    params = {"apikey": YANDEX_API_KEY, "geocode": address, "format": "json", "lang": "ru_RU"}
    try:
        data = requests.get(url, params=params, timeout=15).json()
        obj = data["response"]["GeoObjectCollection"]["featureMember"][0]
        pos = obj["GeoObject"]["Point"]["pos"]
        lon, lat = pos.split(" ")
        return float(lat), float(lon)
    except Exception:
        return None, None

def reverse_geocode(lat, lon):
    url = "https://geocode-maps.yandex.ru/1.x/"
    params = {"apikey": YANDEX_API_KEY, "geocode": str(lon) + "," + str(lat), "format": "json", "lang": "ru_RU"}
    try:
        data = requests.get(url, params=params, timeout=15).json()
        obj = data["response"]["GeoObjectCollection"]["featureMember"][0]
        return obj["GeoObject"]["metaDataProperty"]["GeocoderMetaData"]["text"]
    except Exception:
        return "Адрес не найден"

def haversine_distance_km(lat1, lon1, lat2, lon2):
    r = 6371
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return r * c


def get_route(start_lat, start_lon, end_lat, end_lon):
    coords = str(start_lon) + "," + str(start_lat) + ";" + str(end_lon) + "," + str(end_lat)
    url = "https://router.project-osrm.org/route/v1/driving/" + coords
    params = {"overview": "false", "alternatives": "false", "steps": "false"}
    try:
        data = requests.get(url, params=params, timeout=20).json()
        if data.get("code") != "Ok":
            return None, None
        route = data["routes"][0]
        return route["distance"] / 1000, route["duration"] / 60
    except Exception:
        return None, None

def material_group(material_name):
    """Возвращает понятную пользователю группу материала."""
    text = str(material_name or "").lower().replace("ё", "е")

    if "песок" in text or "пескогрунт" in text:
        if "очень мелк" in text or "тонк" in text or "0,7 - 1,0" in text or "0.7 - 1.0" in text:
            return "Песок — тонкий"
        if "мелк" in text or "1,0 - 1,5" in text or "1.0 - 1.5" in text or "1,5 - 2,0" in text or "1.5 - 2.0" in text:
            return "Песок — мелкий"
        if "средн" in text or "2,0 - 2,5" in text or "2.0 - 2.5" in text:
            return "Песок — средний"
        if "крупн" in text or "2,5 - 3,0" in text or "2.5 - 3.0" in text:
            return "Песок — крупный"
        return "Песок — крупность не указана"

    if "вторич" in text or "рецикл" in text or "дроблен" in text:
        return "Вторичный / рецикл щебень"
    if "щебень" in text:
        return "Щебень"
    if "гпс" in text or "пгс" in text or "гравийно-песчан" in text or "песчано-гравий" in text:
        return "ГПС / ПГС"
    if "смесь" in text or "щпс" in text:
        return "Смеси / ЩПС"
    if "отсев" in text:
        return "Отсев"
    if "перевалка" in text:
        return "Перевалка"
    return "Другие материалы"


MATERIAL_SELECTIONS = {
    "Любой материал / ближайший карьер": None,
    "Песок — любой": (
        "Песок — тонкий",
        "Песок — мелкий",
        "Песок — средний",
        "Песок — крупный",
        "Песок — крупность не указана",
    ),
    "Песок — тонкий": ("Песок — тонкий",),
    "Песок — мелкий": ("Песок — мелкий",),
    "Песок — средний": ("Песок — средний",),
    "Песок — крупный": ("Песок — крупный",),
    "Песок — крупность не указана": ("Песок — крупность не указана",),
    "Щебень — любой": ("Щебень", "Вторичный / рецикл щебень"),
    "Щебень": ("Щебень",),
    "Вторичный / рецикл щебень": ("Вторичный / рецикл щебень",),
    "Смеси — любые": ("Смеси / ЩПС", "ГПС / ПГС"),
    "Смеси / ЩПС": ("Смеси / ЩПС",),
    "ГПС / ПГС": ("ГПС / ПГС",),
    "Отсев": ("Отсев",),
    "Перевалка": ("Перевалка",),
    "Другие материалы": ("Другие материалы",),
}


def filter_material_selection(df, selection):
    """Фильтрует предложения по точному виду или объединённой группе."""
    groups = MATERIAL_SELECTIONS.get(selection)
    if groups is None:
        return df
    return df[df["Группа материала"].isin(groups)]


def load_data():
    response = requests.get(CSV_FILE, timeout=20)
    response.encoding = "utf-8"
    csv_text = response.text
    df = pd.read_csv(StringIO(csv_text))
    df["Цена м3 текст"] = df["Цена м3"].where(df["Цена м3"].notna(), "По запросу").astype(str)

    df["Цена м3"] = (
        df["Цена м3"]
        .astype(str)
        .str.replace(",", ".", regex=False)
        .str.replace("по запросу", "0", case=False, regex=False)
        .str.replace("По запросу", "0", regex=False)
    )
    df["Цена м3"] = pd.to_numeric(df["Цена м3"], errors="coerce").fillna(0)
    for numeric_column in ["Широта", "Долгота", "Стоимость доставки руб_км_м3"]:
        df[numeric_column] = pd.to_numeric(
            df[numeric_column].astype(str).str.replace(",", ".", regex=False),
            errors="coerce",
        )
    if "Телефон" in df.columns:
        df["Телефон"] = df["Телефон"].fillna("").astype(str)
        df.loc[df["Телефон"].str.contains("#ERROR!", regex=False), "Телефон"] = "Не указан"

    df["Юр лицо"] = df["Юр лицо"].fillna("").astype(str)
    df.loc[df["Юр лицо"].str.strip() == "", "Юр лицо"] = "Не указано"

    df["Группа материала"] = df["Вид товара"].apply(material_group)
    return df.dropna(subset=["Название", "Вид товара", "Цена м3", "Широта", "Долгота"])

@app.route("/", methods=["GET", "POST"])
def home():
    df = load_data()
    available_groups = set(df["Группа материала"].dropna().unique())
    sand_types = []
    for label, groups in MATERIAL_SELECTIONS.items():
        if groups is None or any(group in available_groups for group in groups):
            sand_types.append(label)

    unique_careers = df.drop_duplicates(subset=["Название"])[["Название", "Широта", "Долгота", "Телефон"]]
    careers_for_map = []
    for _, row in unique_careers.iterrows():
        careers_for_map.append({
            "name": str(row["Название"]),
            "lat": float(row["Широта"]),
            "lon": float(row["Долгота"]),
            "phone": str(row.get("Телефон", ""))
        })

    result_html = ""
    route_data = None

    if request.method == "POST":
        address = request.form.get("address", "").strip()
        sand_type = request.form.get("sand_type", "").strip()
        volume_raw = request.form.get("volume", "").strip()
        carrier_rate_raw = request.form.get("carrier_rate", "").strip()
        sale_rate_raw = request.form.get("sale_rate", "").strip()

        try:
            volume = float(volume_raw.replace(",", "."))
            carrier_rate = float(carrier_rate_raw.replace(",", "."))
            sale_rate = float(sale_rate_raw.replace(",", "."))
        except Exception:
            return "<h1>Ошибка</h1><p>Объем, цена перевозчика и цена продажи должны быть числами.</p><a href='/'>Назад</a>"

        client_lat, client_lon = geocode_address(address)

        if client_lat is None or client_lon is None:
            return "<h1>Ошибка</h1><p>Адрес не найден через Яндекс.</p><a href='/'>Назад</a>"

        filtered = filter_material_selection(df, sand_type)

        routes = []

        # Сначала выбираем 50 ближайших уникальных карьеров по координатам.
        # Потом параллельно строим реальные маршруты только по ним.
        filtered = filtered.copy()
        filtered["approx_distance"] = filtered.apply(
            lambda r: haversine_distance_km(
                r["Широта"],
                r["Долгота"],
                client_lat,
                client_lon
            ),
            axis=1
        )

        nearest_careers = (
            filtered
            .sort_values("approx_distance")
            .drop_duplicates(subset=["Название", "Широта", "Долгота"])
            .head(50)
        )

        nearest_keys = set()
        for _, r in nearest_careers.iterrows():
            nearest_keys.add((
                str(r["Название"]),
                round(float(r["Широта"]), 6),
                round(float(r["Долгота"]), 6)
            ))

        filtered = filtered[
            filtered.apply(
                lambda r: (
                    str(r["Название"]),
                    round(float(r["Широта"]), 6),
                    round(float(r["Долгота"]), 6)
                ) in nearest_keys,
                axis=1
            )
        ]

        route_cache = {}

        def fetch_route(r):
            key = (
                str(r["Название"]),
                round(float(r["Широта"]), 6),
                round(float(r["Долгота"]), 6)
            )

            distance_km, duration_min = get_route(
                r["Широта"],
                r["Долгота"],
                client_lat,
                client_lon
            )

            return key, distance_km, duration_min

        candidates = nearest_careers.to_dict("records")

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(fetch_route, r) for r in candidates]

            for future in as_completed(futures):
                try:
                    key, distance_km, duration_min = future.result()
                    route_cache[key] = (distance_km, duration_min)
                except Exception:
                    pass

        for _, row in filtered.iterrows():
            route_key = (
                str(row["Название"]),
                round(float(row["Широта"]), 6),
                round(float(row["Долгота"]), 6)
            )

            if route_key not in route_cache:
                continue

            distance_km, duration_min = route_cache[route_key]

            if distance_km is None:
                continue

            carrier_delivery_m3 = distance_km * carrier_rate
            sale_delivery_m3 = distance_km * sale_rate

            purchase_price_m3 = row["Цена м3"] + carrier_delivery_m3
            sale_price_m3 = row["Цена м3"] + sale_delivery_m3
            profit_m3 = sale_price_m3 - purchase_price_m3

            total_purchase = purchase_price_m3 * volume
            total_sale = sale_price_m3 * volume
            total_profit = total_sale - total_purchase

            routes.append({
                "career": row["Название"],
                "legal": row["Юр лицо"],
                "sand_type": row["Вид товара"],
                "phone": row.get("Телефон", ""),
                "distance": round(distance_km, 1),
                "duration": round(duration_min),
                "sand_price": round(row["Цена м3"], 2),
                "sand_price_text": row.get("Цена м3 текст", row["Цена м3"]),
                "carrier_rate": round(carrier_rate, 2),
                "sale_rate": round(sale_rate, 2),
                "carrier_delivery_m3": round(carrier_delivery_m3, 2),
                "sale_delivery_m3": round(sale_delivery_m3, 2),
                "purchase_price_m3": round(purchase_price_m3, 2),
                "sale_price_m3": round(sale_price_m3, 2),
                "profit_m3": round(profit_m3, 2),
                "total_purchase": round(total_purchase, 2),
                "total_sale": round(total_sale, 2),
                "total_profit": round(total_profit, 2),
                "total_price_m3": round(sale_price_m3, 2),
                "total_sum": round(total_sale, 2),
                "career_lat": float(row["Широта"]),
                "career_lon": float(row["Долгота"]),
                "career_address": row.get("Адрес", "Адрес не указан")
            })

        if not routes:
            return "<h1>Ошибка</h1><p>Не удалось построить маршрут.</p><a href='/'>Назад</a>"

        # Одна группа соответствует одному карьеру. Все подходящие товары
        # остаются внутри группы, поэтому карьер больше не повторяется в списке.
        career_groups = {}
        for item in routes:
            group_key = (
                str(item["career"]),
                round(float(item["career_lat"]), 6),
                round(float(item["career_lon"]), 6),
            )
            if group_key not in career_groups:
                career_groups[group_key] = {
                    "career": item["career"],
                    "legal": item["legal"],
                    "phone": item["phone"],
                    "address": item["career_address"],
                    "lat": item["career_lat"],
                    "lon": item["career_lon"],
                    "distance": item["distance"],
                    "duration": item["duration"],
                    "products": [],
                }
            career_groups[group_key]["products"].append(item)

        grouped_careers = sorted(
            career_groups.values(),
            key=lambda group: (group["distance"], group["career"]),
        )

        nearest_group = grouped_careers[0]
        best = min(
            nearest_group["products"],
            key=lambda item: item["total_price_m3"],
        )

        route_data = {
            "client_lat": client_lat,
            "client_lon": client_lon,
            "career_lat": nearest_group["lat"],
            "career_lon": nearest_group["lon"],
            "client_address": address,
            "career_name": nearest_group["career"],
        }

        career_products = df[df["Название"] == nearest_group["career"]].copy()
        career_products = career_products.sort_values(["Группа материала", "Цена м3"])

        transport_url_best = "/transport_request?" + urlencode({
            "career": nearest_group["career"],
            "client_address": address,
            "client_lat": client_lat,
            "client_lon": client_lon,
            "distance": nearest_group["distance"],
            "material": best["sand_type"],
        })

        recommended_carriers, route_region = recommend_carriers(
            nearest_group["lat"],
            nearest_group["lon"],
            client_lat,
            client_lon,
            best["sand_type"],
            limit=3,
        )
        carriers_html = render_carrier_recommendations(
            recommended_carriers,
            route_region,
            compact=True,
        )

        products_html = "<table class='products-table'>"
        products_html += "<tr><th>Группа</th><th>Материал</th><th>Цена материала</th></tr>"
        for _, product in career_products.iterrows():
            products_html += (
                "<tr><td>" + str(product["Группа материала"]) + "</td><td>" +
                str(product["Вид товара"]) + "</td><td>" +
                str(product.get("Цена м3 текст", product["Цена м3"])) + " ₽/м³</td></tr>"
            )
        products_html += "</table>"

        result_html += "<div class='result nearest-summary'>"
        result_html += "<h2>Ближайший карьер</h2>"
        result_html += "<p><b>Карьер:</b> " + str(nearest_group["career"]) + "</p>"
        result_html += "<p><a class='transport-btn' href='" + transport_url_best + "'>Оформить заявку на перевозку</a></p>"
        result_html += "<p><b>Юр. лицо:</b> " + str(nearest_group["legal"]) + "</p>"
        result_html += "<p><b>Телефон:</b> " + str(nearest_group["phone"]) + "</p>"
        result_html += "<p><b>Адрес:</b> " + str(nearest_group["address"]) + "</p>"
        result_html += "<p><b>Расстояние:</b> " + str(nearest_group["distance"]) + " км</p>"
        result_html += "<p><b>Время в пути:</b> " + str(nearest_group["duration"]) + " мин</p>"
        result_html += "<p><b>Самое выгодное подходящее предложение:</b> " + str(best["sand_type"]) + " — " + str(best["total_price_m3"]) + " ₽/м³</p>"
        result_html += carriers_html
        result_html += "<h3>Все материалы этого карьера</h3>"
        result_html += products_html
        result_html += "</div>"

        total_offers = sum(len(group["products"]) for group in grouped_careers)
        result_html += "<div class='result'>"
        result_html += "<h2>Карьеры от ближайшего к дальнему</h2>"
        result_html += "<p>Найдено карьеров: <b>" + str(len(grouped_careers)) + "</b>. Подходящих предложений: <b>" + str(total_offers) + "</b>.</p>"
        result_html += "<p>Каждый карьер показан один раз. Внутри раскрыты все подходящие материалы.</p>"
        result_html += "</div>"

        for position, group in enumerate(grouped_careers, start=1):
            transport_url = "/transport_request?" + urlencode({
                "career": group["career"],
                "client_address": address,
                "client_lat": client_lat,
                "client_lon": client_lon,
                "distance": group["distance"],
                "material": min(group["products"], key=lambda item: item["total_price_m3"])["sand_type"],
            })
            sorted_products = sorted(
                group["products"],
                key=lambda item: (material_group(item["sand_type"]), item["total_price_m3"]),
            )
            minimum_price = min(item["total_price_m3"] for item in sorted_products)

            result_html += "<div class='result career-group'>"
            result_html += "<div class='career-heading'>"
            result_html += "<div><span class='position'>№" + str(position) + "</span> <a href=\"/career/" + quote(str(group["career"])) + "\"><b>" + str(group["career"]) + "</b></a></div>"
            result_html += "<div class='distance-badge'>" + str(group["distance"]) + " км · " + str(group["duration"]) + " мин</div>"
            result_html += "</div>"
            result_html += "<p>Юр. лицо: " + str(group["legal"]) + " · Телефон: " + str(group["phone"]) + "</p>"
            result_html += "<p>Минимальная итоговая цена: <b>" + str(minimum_price) + " ₽/м³</b> · <a class='small-transport-btn' href='" + transport_url + "'>Заявка на перевозку</a></p>"
            result_html += "<div class='table-scroll'><table>"
            result_html += "<tr><th>Группа</th><th>Материал</th><th>Материал ₽/м³</th><th>Доставка ₽/м³</th><th>Итого ₽/м³</th><th>Сумма за объем</th></tr>"
            for item in sorted_products:
                result_html += "<tr>"
                result_html += "<td>" + material_group(item["sand_type"]) + "</td>"
                result_html += "<td>" + str(item["sand_type"]) + "</td>"
                result_html += "<td>" + str(item.get("sand_price_text", item["sand_price"])) + "</td>"
                result_html += "<td>" + str(item["sale_delivery_m3"]) + "</td>"
                result_html += "<td><b>" + str(item["total_price_m3"]) + "</b></td>"
                result_html += "<td>" + str(item["total_sum"]) + " ₽</td>"
                result_html += "</tr>"
            result_html += "</table></div></div>"

    options = ""
    for sand in sand_types:
        options += '<option value="' + str(sand) + '">' + str(sand) + '</option>'

    careers_json = json.dumps(careers_for_map, ensure_ascii=False)
    route_json = json.dumps(route_data, ensure_ascii=False)

    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Калькулятор доставки песка</title>
        <script src="https://api-maps.yandex.ru/2.1/?apikey=""" + YANDEX_API_KEY + """&lang=ru_RU"></script>
        <style>
            body {font-family: Arial, sans-serif; background: #f4f5f7; margin: 0; padding: 30px;}
            .container {max-width: 1200px; margin: auto;}
            .card, .result {background: white; padding: 25px; border-radius: 14px; margin-bottom: 20px; box-shadow: 0 4px 14px rgba(0,0,0,0.08);}
            input, select, button {width: 100%; padding: 14px; margin-top: 8px; margin-bottom: 18px; font-size: 16px; box-sizing: border-box;}
            button {background: #111; color: white; border: none; border-radius: 10px; cursor: pointer; font-size: 18px;}
            .transport-btn {display:inline-block; background:#111; color:white; padding:12px 16px; border-radius:10px; text-decoration:none; font-weight:bold;}
            .small-transport-btn {display:inline-block; margin-top:8px; background:#111; color:white; padding:7px 10px; border-radius:8px; text-decoration:none; font-size:13px;}
            table {width: 100%; border-collapse: collapse;}
            th, td {border-bottom: 1px solid #ddd; padding: 10px; text-align: left; vertical-align: top;}
            li {margin-bottom: 8px;}
            #map {width: 100%; height: 520px; border-radius: 14px; overflow: hidden;}
            .career-heading {display:flex; justify-content:space-between; gap:16px; align-items:center; font-size:20px;}
            .position {display:inline-block; min-width:44px; color:#666;}
            .distance-badge {background:#eef3ff; padding:8px 12px; border-radius:999px; white-space:nowrap; font-weight:bold;}
            .table-scroll {overflow-x:auto;}
            .products-table {margin-top:12px;}
            .carrier-recommendations {margin:20px 0 8px; padding:18px; border:1px solid #dbe4f4; border-radius:13px; background:#f7faff;}
            .carrier-recommendations h3 {margin:0 0 6px;}
            .carrier-recommendations .route-region {color:#526176; margin:0 0 13px;}
            .recommended-carrier {background:#fff; border:1px solid #e1e5eb; border-radius:11px; padding:13px; margin-top:10px;}
            .recommended-carrier-head {display:flex; justify-content:space-between; gap:12px; align-items:flex-start;}
            .recommended-carrier .reason {color:#526176; font-size:13px; margin:7px 0;}
            .recommended-carrier .details {font-size:14px; line-height:1.5;}
            .carrier-score {white-space:nowrap; background:#e8f1ff; border-radius:999px; padding:5px 9px; font-size:12px; font-weight:bold;}
            @media (max-width: 700px) {
                body {padding:14px;}
                .card, .result {padding:16px;}
                .career-heading {display:block;}
                .distance-badge {display:inline-block; margin-top:10px;}
                #map {height:360px;}
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Калькулятор доставки материалов</h1>
            <p>
                <a href="/careers" style="font-size:18px; font-weight:bold; margin-right:20px;">Список всех карьеров</a>
                <a href="/carriers" style="font-size:18px; font-weight:bold; margin-right:20px;">Перевозчики</a>
                <a href="/office/login" style="font-size:18px; font-weight:bold;">CRM сделок</a>
            </p>

            <form method="POST" class="card">
                <label>Адрес клиента</label>
                <input name="address" placeholder="Например: Москва, ул. Ленина 10" required>
                <label>Материал</label>
                <select name="sand_type" required>""" + options + """</select>
                <label>Объем, м³</label>
                <input name="volume" placeholder="Например: 20" required>

                <label>Цена перевозчика, ₽/км/м³</label>
                <input name="carrier_rate" placeholder="Например: 15" required>

                <label>Цена продажи доставки клиенту, ₽/км/м³</label>
                <input name="sale_rate" placeholder="Например: 25" required>

                <button type="submit">Рассчитать</button>
            </form>

            """ + result_html + """

            <div class="card">
                <h2>Карта карьеров и маршрут доставки</h2>
                <div id="map"></div>
            </div>
        </div>

        <script>
            var careers = """ + careers_json + """;
            var routeData = """ + route_json + """;

            ymaps.ready(function () {
                var map = new ymaps.Map("map", {
                    center: [55.75, 37.62],
                    zoom: 8
                });

                careers.forEach(function(career) {
                    var placemark = new ymaps.Placemark(
                        [career.lat, career.lon],
                        {
                            balloonContent: "<b>" + career.name + "</b><br>Телефон: " + career.phone
                        }
                    );
                    map.geoObjects.add(placemark);
                });

                if (routeData) {
                    var clientMark = new ymaps.Placemark(
                        [routeData.client_lat, routeData.client_lon],
                        {balloonContent: "<b>Клиент</b><br>" + routeData.client_address},
                        {preset: "islands#redIcon"}
                    );

                    var bestCareerMark = new ymaps.Placemark(
                        [routeData.career_lat, routeData.career_lon],
                        {balloonContent: "<b>Лучший карьер</b><br>" + routeData.career_name},
                        {preset: "islands#greenIcon"}
                    );

                    map.geoObjects.add(clientMark);
                    map.geoObjects.add(bestCareerMark);

                    ymaps.route([
                        [routeData.career_lat, routeData.career_lon],
                        [routeData.client_lat, routeData.client_lon]
                    ]).then(function(route) {
                        map.geoObjects.add(route);
                        map.setBounds(route.getBounds(), {checkZoomRange: true, zoomMargin: 40});
                    });
                } else if (careers.length > 0) {
                    map.setBounds(map.geoObjects.getBounds(), {checkZoomRange: true, zoomMargin: 40});
                }
            });
        </script>
    </body>
    </html>
    """



def region_by_coords(lat, lon):
    if lat >= 55.85 and lon < 37.8:
        return "Север / Северо-Запад"
    if lat >= 55.85 and lon >= 37.8:
        return "Север / Северо-Восток"
    if lat < 55.55 and lon < 37.8:
        return "Юг / Юго-Запад"
    if lat < 55.55 and lon >= 37.8:
        return "Юг / Юго-Восток"
    if lon < 37.4:
        return "Запад"
    if lon > 38.0:
        return "Восток"
    return "Москва / Ближнее МО"



@app.route("/transport_request")
def transport_request():
    df = load_data()

    career_name = request.args.get("career", "").strip()
    client_address = request.args.get("client_address", "").strip()
    client_lat = request.args.get("client_lat", "").strip()
    client_lon = request.args.get("client_lon", "").strip()
    distance = request.args.get("distance", "").strip()
    material = request.args.get("material", "").strip()

    career_df = df[df["Название"].astype(str) == career_name]

    if career_df.empty:
        return "<h1>Карьер не найден</h1><p><a href='/'>Назад</a></p>"

    first = career_df.iloc[0]

    career_lat = float(first["Широта"])
    career_lon = float(first["Долгота"])

    loading_address = str(first.get("Адрес", "")).strip()
    if loading_address == "" or loading_address.lower() == "nan":
        loading_address = reverse_geocode(career_lat, career_lon)

    loading_coords = f"{career_lat}, {career_lon}"

    unloading_coords = ""
    if client_lat and client_lon:
        unloading_coords = f"{client_lat}, {client_lon}"

    recommended_carriers = []
    route_region = {"loading": "Не определено", "unloading": "Не определено", "label": "Не определено"}
    if client_lat and client_lon:
        try:
            recommended_carriers, route_region = recommend_carriers(
                career_lat,
                career_lon,
                float(client_lat),
                float(client_lon),
                material,
                limit=5,
            )
        except (TypeError, ValueError):
            pass
    carrier_recommendations_html = render_carrier_recommendations(
        recommended_carriers,
        route_region,
        select_id="recommended_carrier",
    )

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Заявка на перевозку</title>
        <style>
            body {{font-family: Arial, sans-serif; background:#f4f5f7; margin:0; padding:30px;}}
            .container {{max-width:1000px; margin:auto;}}
            .card {{background:white; padding:25px; border-radius:14px; margin-bottom:20px; box-shadow:0 4px 14px rgba(0,0,0,0.08);}}
            input, select, textarea, button {{width:100%; padding:13px; margin-top:7px; margin-bottom:15px; font-size:16px; box-sizing:border-box;}}
            .checkbox-group {{display:grid; grid-template-columns:1fr 1fr; gap:8px 18px; margin:10px 0 18px;}}
            .checkbox-group label {{background:#f4f5f7; padding:10px; border-radius:10px;}}
            .checkbox-group input {{width:auto; margin-right:8px;}}
            textarea {{height:220px;}}
            button {{background:#111; color:white; border:none; border-radius:10px; cursor:pointer; font-size:18px;}}
            .grid {{display:grid; grid-template-columns:1fr 1fr; gap:15px;}}
            a {{color:#111;}}
            .carrier-recommendations {{background:#f7faff; border:1px solid #dbe4f4; border-radius:14px; padding:20px; margin-bottom:20px;}}
            .carrier-recommendations h3 {{margin-top:0;}}
            .route-region,.reason {{color:#526176;}}
            .recommended-carrier {{background:#fff; border:1px solid #e1e5eb; border-radius:11px; padding:13px; margin-top:10px;}}
            .recommended-carrier-head {{display:flex; justify-content:space-between; gap:12px;}}
            .carrier-score {{white-space:nowrap; background:#e8f1ff; border-radius:999px; padding:5px 9px; font-size:12px; font-weight:bold;}}
            .details {{font-size:14px; line-height:1.5;}}
        </style>
    </head>
    <body>
        <div class="container">
            <p><a href="/">← Назад к калькулятору</a> · <a href="/carriers">Перевозчики</a></p>

            <div class="card">
                <h1>Заявка на перевозку</h1>
                <p><b>Карьер:</b> {career_name}</p>
                <p><b>Юр лицо:</b> {first.get("Юр лицо", "")}</p>
                <p><b>Телефон карьера:</b> {first.get("Телефон", "")}</p>
                <p><b>Материал:</b> {escape(material or "Не выбран")}</p>
            </div>

            {carrier_recommendations_html}

            <div class="card">
                <label>Адрес загрузки</label>
                <input id="loading_address" value="{loading_address}">

                <label>Координаты загрузки</label>
                <input id="loading_coords" value="{loading_coords}">

                <label>Адрес выгрузки</label>
                <input id="unloading_address" value="{client_address}">

                <label>Координаты выгрузки</label>
                <input id="unloading_coords" value="{unloading_coords}">

                <label>Плечо, км</label>
                <input id="distance" value="{distance}">

                <label>Объем перевозки, м³</label>
                <input id="volume" placeholder="Например: 100">

                <label>Транспорт</label>
                <div class="checkbox-group">
                    <label><input type="checkbox" name="transport" value="Транспорт любой"> Транспорт любой</label>
                    <label><input type="checkbox" name="transport" value="Двухосные 10 м³"> Двухосные 10 м³</label>
                    <label><input type="checkbox" name="transport" value="Двухосные 18 м³"> Двухосные 18 м³</label>
                    <label><input type="checkbox" name="transport" value="Двухосные 20 м³"> Двухосные 20 м³</label>
                    <label><input type="checkbox" name="transport" value="Трехосные 25 м³"> Трехосные 25 м³</label>
                    <label><input type="checkbox" name="transport" value="Трехосные 30 м³"> Трехосные 30 м³</label>
                    <label><input type="checkbox" name="transport" value="Трехосные 35 м³"> Трехосные 35 м³</label>
                    <label><input type="checkbox" name="transport" value="Четырехосные 30 м³"> Четырехосные 30 м³</label>
                    <label><input type="checkbox" name="transport" value="Четырехосные 35 м³"> Четырехосные 35 м³</label>
                    <label><input type="checkbox" name="transport" value="Тонары"> Тонары</label>
                </div>

                <div class="grid">
                    <div>
                        <label>Время загрузки с</label>
                        <input id="load_from" placeholder="Например: 09:00">
                    </div>
                    <div>
                        <label>Время загрузки до</label>
                        <input id="load_to" placeholder="Например: 12:00">
                    </div>
                </div>

                <div class="grid">
                    <div>
                        <label>Время выгрузки с</label>
                        <input id="unload_from" placeholder="Например: 10:00">
                    </div>
                    <div>
                        <label>Время выгрузки до</label>
                        <input id="unload_to" placeholder="Например: 14:00">
                    </div>
                </div>

                <label>Оплата</label>
                <select id="payment_type">
                    <option>Оплата любая</option>
                    <option>Наличными</option>
                    <option>Безналичный расчет</option>
                    <option>Безналичный расчет с НДС 22%</option>
                    <option>Оплата любая по безналичному расчету</option>
                    <option>Самозанятый</option>
                    <option>ИП</option>
                    <option>ООО</option>
                </select>

                <label>Вариант оплаты</label>
                <select id="payment_terms">
                    <option>Аванс</option>
                    <option>День в день</option>
                    <option>На следующий день</option>
                    <option>В течение 3 дней после доставки</option>
                    <option>В течение 5 дней после доставки</option>
                    <option>В течение 10 дней после доставки</option>
                </select>

                <h2>Контакт по заявке</h2>

                <label>Имя</label>
                <input id="contact_name" placeholder="Например: Артём">

                <label>Номер телефона</label>
                <input id="contact_phone" placeholder="+7...">

                <label>Почта</label>
                <input id="contact_email" placeholder="example@mail.ru">

                <button onclick="generateText()">Сформировать текст заявки</button>
            </div>

            <div class="card">
                <h2>Текст заявки</h2>
                <textarea id="result_text"></textarea>
                <button onclick="copyText()">Скопировать заявку</button>
            </div>
        </div>

        <script>
            function val(id) {{
                const element = document.getElementById(id);
                return element ? element.value : "Не выбран";
            }}

            function checkedValues(name) {{
                let items = Array.from(document.querySelectorAll('input[name="' + name + '"]:checked'));
                if (items.length === 0) {{
                    return "Не выбрано";
                }}
                return items.map(i => i.value).join(", ");
            }}

            function generateText() {{
                let text =
`ЗАЯВКА НА ПЕРЕВОЗКУ

Адрес загрузки:
${{val("loading_address")}}

Координаты загрузки:
${{val("loading_coords")}}

Адрес выгрузки:
${{val("unloading_address")}}

Координаты выгрузки:
${{val("unloading_coords")}}

Плечо:
${{val("distance")}} км

Направление маршрута:
{route_region["label"]}

Рекомендуемый перевозчик:
${{val("recommended_carrier")}}

Объем перевозки:
${{val("volume")}} м³

Транспорт:
${{checkedValues("transport")}}

Время загрузки:
с ${{val("load_from")}} до ${{val("load_to")}}

Время выгрузки:
с ${{val("unload_from")}} до ${{val("unload_to")}}

Оплата:
${{val("payment_type")}}

Вариант оплаты:
${{val("payment_terms")}}
Контактное лицо:
${{val("contact_name")}}
Телефон:
${{val("contact_phone")}}
Почта:
${{val("contact_email")}}`;

                document.getElementById("result_text").value = text;
            }}

            function copyText() {{
                let textArea = document.getElementById("result_text");
                textArea.select();
                document.execCommand("copy");
                alert("Заявка скопирована");
            }}

            generateText();
        </script>
    </body>
    </html>
    """


def load_carriers():
    """Читает очищенную локальную базу перевозчиков без превращения пустых полей в nan."""
    if not os.path.exists(CARRIERS_FILE):
        return pd.DataFrame()
    carriers = pd.read_csv(CARRIERS_FILE, sep=";", dtype=str, keep_default_na=False)
    carriers["profile_score"] = pd.to_numeric(carriers["profile_score"], errors="coerce").fillna(0)
    return carriers


def split_filter_values(series):
    values = set()
    for value in series.fillna("").astype(str):
        values.update(part.strip() for part in value.split(";") if part.strip())
    return sorted(values)


def chips(value, empty_text="Не указано"):
    parts = [part.strip() for part in str(value or "").split(";") if part.strip()]
    if not parts:
        return f'<span class="muted">{escape(empty_text)}</span>'
    return "".join(f'<span class="chip">{escape(part)}</span>' for part in parts)


DIRECTION_ALIASES = {
    "Север": ("север", "дмитров", "химки", "сходн", "зеленоград", "лобня", "долгопруд"),
    "Юг": ("юг", "домодед", "подольск", "видное", "ленинск", "чехов", "ступино"),
    "Запад": ("запад", "одинцов", "красногор", "истра", "можайск", "наро-фомин"),
    "Восток": ("восток", "балаших", "ногинск", "электросталь", "щелков", "щёлков"),
    "Юго-Восток": ("юго-вост", "люберц", "раменск", "жуковск", "воскресенск"),
    "Юго-Запад": ("юго-запад", "троицк", "новая москва"),
    "Северо-Восток": ("северо-вост", "мытищ", "королев", "королёв", "пушкино"),
    "Северо-Запад": ("северо-запад", "солнечногор", "клин"),
}

GLOBAL_AREA_MARKERS = (
    "москва и московская область",
    "москва и мо",
    "московская область",
    "вся москва",
    "вся область",
    "по всей москве",
)


def direction_by_coords(lat, lon):
    """Возвращает основной сектор относительно центра Москвы для подбора перевозчиков."""
    lat = float(lat)
    lon = float(lon)
    center_lat, center_lon = 55.7558, 37.6176
    north_south = "Север" if lat >= center_lat else "Юг"
    east_west = "Восток" if lon >= center_lon else "Запад"

    # Если отклонение по одной оси заметно больше, используем понятное основное направление.
    lat_delta = abs(lat - center_lat)
    lon_delta = abs(lon - center_lon) * 0.57
    if lat_delta > lon_delta * 1.8:
        return north_south
    if lon_delta > lat_delta * 1.8:
        return east_west
    diagonals = {
        ("Север", "Восток"): "Северо-Восток",
        ("Север", "Запад"): "Северо-Запад",
        ("Юг", "Восток"): "Юго-Восток",
        ("Юг", "Запад"): "Юго-Запад",
    }
    return diagonals[(north_south, east_west)]


def route_region_info(career_lat, career_lon, client_lat, client_lon):
    loading = direction_by_coords(career_lat, career_lon)
    unloading = direction_by_coords(client_lat, client_lon)
    label = loading if loading == unloading else f"{loading} → {unloading}"
    return {
        "loading": loading,
        "unloading": unloading,
        "label": label,
    }


def carrier_area_directions(areas):
    text = str(areas or "").lower().replace("ё", "е")
    directions = set()
    for direction, aliases in DIRECTION_ALIASES.items():
        normalized_aliases = tuple(alias.replace("ё", "е") for alias in aliases)
        if any(alias in text for alias in normalized_aliases):
            directions.add(direction)
            if direction.startswith("Северо-"):
                directions.add("Север")
            if direction.startswith("Юго-"):
                directions.add("Юг")
            if direction.endswith("Восток"):
                directions.add("Восток")
            if direction.endswith("Запад"):
                directions.add("Запад")
    global_area = any(marker in text for marker in GLOBAL_AREA_MARKERS)
    return directions, global_area


def material_matches_cargo(material, cargo):
    material_text = str(material or "").lower().replace("ё", "е")
    cargo_text = str(cargo or "").lower().replace("ё", "е")
    if not cargo_text:
        return False
    categories = {
        "песок": ("песок", "пескогрунт"),
        "щебень": ("щебень", "вторич", "рецикл"),
        "грунт": ("грунт", "почв"),
        "смесь": ("смесь", "щпс", "пгс", "гпс"),
        "отсев": ("отсев",),
    }
    for category, aliases in categories.items():
        if any(alias in material_text for alias in aliases):
            return category in cargo_text or any(alias in cargo_text for alias in aliases)
    return False


def recommend_carriers(career_lat, career_lon, client_lat, client_lon, material="", limit=5):
    """Ранжирует перевозчиков по маршруту, грузу и полноте доступной карточки."""
    route_region = route_region_info(career_lat, career_lon, client_lat, client_lon)
    carriers = load_carriers()
    if carriers.empty:
        return [], route_region

    results = []
    for _, row in carriers.iterrows():
        areas = str(row.get("areas", "")).strip()
        directions, global_area = carrier_area_directions(areas)
        loading_match = route_region["loading"] in directions
        unloading_match = route_region["unloading"] in directions
        regional_match = loading_match or unloading_match or global_area

        # Сначала предлагаем тех, кто явно работает хотя бы на одном конце маршрута.
        if not regional_match:
            continue

        profile_score = float(row.get("profile_score", 0) or 0)
        score = profile_score * 0.35
        reasons = []
        if loading_match and unloading_match:
            score += 42
            reasons.append("работает на всём направлении маршрута")
        elif unloading_match:
            score += 32
            reasons.append("работает в районе выгрузки")
        elif loading_match:
            score += 25
            reasons.append("работает в районе карьера")
        if global_area:
            score += 18
            reasons.append("заявлена работа по Москве и Московской области")
        if material_matches_cargo(material, row.get("cargo", "")):
            score += 16
            reasons.append("в списке грузов есть подходящий материал")
        if str(row.get("vehicles", "")).strip():
            score += 6
            reasons.append("указаны типы машин")
        if str(row.get("fleet_size", "")).strip():
            score += 6
            reasons.append("указан размер автопарка")
        if str(row.get("phone", "")).strip():
            score += 4
        if str(row.get("inn_valid", "")).lower() == "true":
            score += 3

        carrier = row.to_dict()
        carrier["recommendation_score"] = round(score, 1)
        carrier["match_reason"] = "; ".join(reasons)
        results.append(carrier)

    results.sort(key=lambda row: (-row["recommendation_score"], str(row.get("name", ""))))
    return results[:limit], route_region


def render_carrier_recommendations(carriers, route_region, compact=False, select_id=None):
    route_label = escape(route_region["label"])
    if not carriers:
        return (
            '<section class="carrier-recommendations">'
            '<h3>Перевозчики для маршрута</h3>'
            f'<p class="route-region">Направление: <b>{route_label}</b></p>'
            '<p>В базе нет перевозчика с подтверждённой работой на этом направлении. '
            '<a href="/carriers">Открыть всю базу и уточнить регион вручную</a>.</p>'
            '</section>'
        )

    cards = []
    options = []
    for index, row in enumerate(carriers, start=1):
        name = escape(str(row.get("name", "Не указано")))
        phone = escape(str(row.get("phone", "Не указан")) or "Не указан")
        areas = escape(str(row.get("areas", "Требует уточнения")) or "Требует уточнения")
        vehicles = escape(str(row.get("vehicles", "Требует уточнения")) or "Требует уточнения")
        fleet = escape(str(row.get("fleet_size", "")))
        cargo = escape(str(row.get("cargo", "Требует уточнения")) or "Требует уточнения")
        reason = escape(str(row.get("match_reason", "")))
        score = row.get("recommendation_score", 0)
        fleet_text = f" · автопарк: {fleet}" if fleet else ""
        cards.append(
            '<article class="recommended-carrier">'
            '<div class="recommended-carrier-head">'
            f'<b>№{index} {name}</b><span class="carrier-score">совпадение {score}</span>'
            '</div>'
            f'<div class="reason">Почему: {reason}</div>'
            f'<div class="details"><b>Регион:</b> {areas}<br><b>Телефон:</b> {phone}<br>'
            f'<b>Транспорт:</b> {vehicles}{fleet_text}<br><b>Грузы:</b> {cargo}</div>'
            '</article>'
        )
        options.append(f'<option value="{name}">{name} — {areas}</option>')

    select_html = ""
    if select_id:
        select_html = (
            f'<label for="{escape(select_id)}">Рекомендуемый перевозчик</label>'
            f'<select id="{escape(select_id)}"><option value="Не выбран">Не выбран</option>{"".join(options)}</select>'
        )

    extra_class = " compact" if compact else ""
    return (
        f'<section class="carrier-recommendations{extra_class}">'
        '<h3>Рекомендуемые перевозчики</h3>'
        f'<p class="route-region">Направление маршрута: <b>{route_label}</b>. '
        'Рейтинг — это совпадение с маршрутом и данными карточки, а не гарантия доступности машины.</p>'
        f'{select_html}{"".join(cards)}'
        '<p><a href="/carriers">Посмотреть всю базу перевозчиков →</a></p>'
        '</section>'
    )


@app.route("/carriers")
def carriers_list():
    carriers = load_carriers()
    if carriers.empty:
        return "<h1>База перевозчиков пока не загружена</h1><p><a href='/'>Назад</a></p>", 503

    q = request.args.get("q", "").strip()
    area = request.args.get("area", "").strip()
    vehicle = request.args.get("vehicle", "").strip()
    cargo = request.args.get("cargo", "").strip()
    service = request.args.get("service", "").strip()
    try:
        page = max(int(request.args.get("page", "1") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    page_size = 50

    all_areas = split_filter_values(carriers["areas"])
    all_vehicles = split_filter_values(carriers["vehicles"])
    all_cargo = split_filter_values(carriers["cargo"])
    all_services = split_filter_values(carriers["services"])

    filtered = carriers.copy()
    if q:
        search_text = filtered[["name", "inn", "phone", "email"]].fillna("").agg(" ".join, axis=1)
        filtered = filtered[search_text.str.contains(q, case=False, regex=False)]
    for column, value in [("areas", area), ("vehicles", vehicle), ("cargo", cargo), ("services", service)]:
        if value:
            filtered = filtered[filtered[column].str.contains(value, case=False, regex=False, na=False)]

    filtered = filtered.sort_values(["profile_score", "name"], ascending=[False, True])
    total = len(filtered)
    page_count = max(math.ceil(total / page_size), 1)
    page = min(page, page_count)
    page_rows = filtered.iloc[(page - 1) * page_size:page * page_size]
    check_timestamp = int(time.time() * 1000)

    cards_html = ""
    for _, row in page_rows.iterrows():
        inn = str(row.get("inn", "")).strip()
        phone = str(row.get("phone", "")).strip()
        email = str(row.get("email", "")).strip()
        fleet = str(row.get("fleet_size", "")).strip()
        score = int(row.get("profile_score", 0))
        inn_badge = '<span class="badge good">ИНН корректен</span>' if str(row.get("inn_valid", "")).lower() == "true" else '<span class="badge warn">ИНН не подтверждён</span>'
        fns_link = ""
        if inn:
            fns_url = f"https://pb.nalog.ru/search.html#t={check_timestamp}&mode=search-all&queryAll={quote(inn)}&page=1&pageSize=10"
            fns_link = f'<a href="{fns_url}" target="_blank" rel="noopener">Проверить статус в ФНС ↗</a>'
        phone_html = "<br>".join(
            f'<a href="tel:{escape(part.replace(" ", "").replace("-", ""))}">{escape(part)}</a>'
            for part in phone.split("; ") if part
        ) or '<span class="muted">Телефон не указан</span>'
        email_html = "<br>".join(
            f'<a href="mailto:{escape(part)}">{escape(part)}</a>'
            for part in email.split("; ") if part
        ) or '<span class="muted">E-mail не указан</span>'
        fleet_html = f'<span class="chip strong">Автопарк: {escape(fleet)} ед.</span>' if fleet else ""
        cards_html += f"""
        <article class="carrier-card">
            <div class="carrier-head">
                <div>
                    <h2>{escape(str(row.get("name", "Не указано")))}</h2>
                    <div class="inn">ИНН: <b>{escape(inn or "не указан")}</b> · {escape(str(row.get("entity_type", "")))}</div>
                </div>
                <div class="score" title="Это полнота карточки, а не финансовый рейтинг">Карточка {score}%</div>
            </div>
            <div class="checks">{inn_badge} {fns_link}</div>
            <div class="carrier-grid">
                <section><h3>Контакты</h3>{phone_html}<br>{email_html}</section>
                <section><h3>Где работает</h3>{chips(row.get("areas", ""), "Регион требует уточнения")}</section>
                <section><h3>Транспорт</h3>{chips(row.get("vehicles", ""), "Тип машин требует уточнения")}{fleet_html}</section>
                <section><h3>Грузы и услуги</h3>{chips(row.get("cargo", ""), "Грузы требуют уточнения")}{chips(row.get("services", ""), "")}</section>
            </div>
            <div class="status-line">Доступность: {escape(str(row.get("availability", "Не подтверждена")))} · исходная строка {escape(str(row.get("source_row", "")))}</div>
        </article>
        """

    if not cards_html:
        cards_html = '<div class="empty">По выбранным условиям перевозчики не найдены.</div>'

    def select_options(values, selected, placeholder):
        result = f'<option value="">{escape(placeholder)}</option>'
        for value in values:
            selected_attr = " selected" if value == selected else ""
            result += f'<option value="{escape(value)}"{selected_attr}>{escape(value)}</option>'
        return result

    pagination = ""
    base_params = {"q": q, "area": area, "vehicle": vehicle, "cargo": cargo, "service": service}
    if page > 1:
        pagination += f'<a href="/carriers?{urlencode({**base_params, "page": page - 1})}">← Предыдущая</a>'
    pagination += f'<span>Страница {page} из {page_count}</span>'
    if page < page_count:
        pagination += f'<a href="/carriers?{urlencode({**base_params, "page": page + 1})}">Следующая →</a>'

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Перевозчики строительных грузов</title>
        <style>
            * {{box-sizing:border-box;}}
            body {{font-family:Arial,sans-serif;background:#f4f5f7;margin:0;padding:28px;color:#151515;}}
            .container {{max-width:1280px;margin:auto;}}
            .panel,.carrier-card,.empty {{background:#fff;border-radius:16px;padding:22px;margin-bottom:18px;box-shadow:0 4px 16px rgba(0,0,0,.07);}}
            .toplinks a {{margin-right:18px;font-weight:bold;}}
            form {{display:grid;grid-template-columns:2fr repeat(4,1fr) auto;gap:10px;align-items:end;}}
            label {{font-size:13px;font-weight:bold;display:block;margin-bottom:6px;}}
            input,select,button {{width:100%;padding:11px;border:1px solid #ccd0d5;border-radius:9px;background:#fff;}}
            button {{background:#111;color:#fff;border-color:#111;font-weight:bold;cursor:pointer;}}
            a {{color:#151515;}}
            .carrier-head {{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;}}
            h1 {{margin-top:8px;}} h2 {{font-size:20px;margin:0 0 7px;}} h3 {{font-size:13px;text-transform:uppercase;color:#666;margin:0 0 9px;}}
            .inn,.muted,.status-line {{color:#666;font-size:13px;}}
            .score {{background:#eef3ff;padding:9px 12px;border-radius:999px;font-size:13px;font-weight:bold;white-space:nowrap;}}
            .checks {{display:flex;gap:12px;align-items:center;margin:14px 0;}}
            .badge,.chip {{display:inline-block;padding:5px 8px;border-radius:999px;font-size:12px;margin:0 5px 5px 0;background:#f0f1f3;}}
            .badge.good {{background:#e7f7ed;color:#176b36;}} .badge.warn {{background:#fff1d6;color:#8a5b00;}} .chip.strong {{background:#e9f0ff;}}
            .carrier-grid {{display:grid;grid-template-columns:1.1fr 1fr 1fr 1.3fr;gap:18px;border-top:1px solid #eee;padding-top:16px;}}
            .carrier-grid section {{min-width:0;line-height:1.5;}}
            .status-line {{border-top:1px solid #eee;margin-top:14px;padding-top:11px;}}
            .pagination {{display:flex;justify-content:center;gap:20px;align-items:center;margin:24px 0;}}
            .notice {{background:#fff9e8;border:1px solid #f1ddb0;padding:13px;border-radius:10px;line-height:1.45;}}
            @media(max-width:1000px) {{form {{grid-template-columns:1fr 1fr;}} .carrier-grid {{grid-template-columns:1fr 1fr;}}}}
            @media(max-width:650px) {{body {{padding:13px;}} form,.carrier-grid {{grid-template-columns:1fr;}} .carrier-head {{display:block;}} .score {{display:inline-block;margin-top:10px;}}}}
        </style>
    </head>
    <body><div class="container">
        <div class="panel">
            <div class="toplinks"><a href="/">← Калькулятор</a><a href="/careers">Карьеры</a></div>
            <h1>Перевозчики строительных грузов</h1>
            <p>В базе: <b>{len(carriers)}</b>. Найдено по фильтрам: <b>{total}</b>.</p>
            <p class="notice"><b>Важно:</b> отметка «ИНН корректен» означает только проверку контрольной суммы. Она не подтверждает действующий статус, отсутствие долгов или финансовую устойчивость. Для решения о сделке откройте карточку ФНС и бухгалтерскую отчётность на <a href="https://bo.nalog.gov.ru/" target="_blank" rel="noopener">ресурсе БФО ФНС ↗</a>.</p>
            <form method="GET">
                <div><label>Компания, ИНН или контакт</label><input name="q" value="{escape(q)}" placeholder="Например: 7716965531"></div>
                <div><label>Направление</label><select name="area">{select_options(all_areas, area, "Любое")}</select></div>
                <div><label>Машины</label><select name="vehicle">{select_options(all_vehicles, vehicle, "Любые")}</select></div>
                <div><label>Груз</label><select name="cargo">{select_options(all_cargo, cargo, "Любой")}</select></div>
                <div><label>Услуга</label><select name="service">{select_options(all_services, service, "Любая")}</select></div>
                <div><button type="submit">Найти</button></div>
            </form>
        </div>
        {cards_html}
        <div class="pagination">{pagination}</div>
    </div></body></html>
    """


@app.route("/careers")
def careers_list():
    df = load_data()
    group = request.args.get("group", "all")

    def filter_group(data, group_name):
        if group_name == "sand":
            return data[data["Вид товара"].str.contains("песок", case=False, na=False)]
        if group_name == "stone":
            return data[data["Вид товара"].str.contains("щебень", case=False, na=False)]
        if group_name == "recycled":
            return data[data["Вид товара"].str.contains("вторич|рецикл|рециклинг|дроблен", case=False, na=False)]
        if group_name == "mix":
            return data[data["Вид товара"].str.contains("смесь|щпс|с4|с5|с6|с/4|с/5|с/6", case=False, na=False)]
        if group_name == "gps":
            return data[data["Вид товара"].str.contains("гпс|пгс|гравийно-песчан|песчано-гравий|щебеночно-песчано-гравий", case=False, na=False)]
        if group_name == "screening":
            return data[data["Вид товара"].str.contains("отсев", case=False, na=False)]
        if group_name == "transfer":
            return data[data["Вид товара"].str.contains("перевалка", case=False, na=False)]
        return data

    filtered = filter_group(df, group)

    rows_html = ""
    grouped = filtered.groupby("Название")

    for career_name, career_df in grouped:
        first = career_df.iloc[0]
        products = "<ul>"
        for _, row in career_df.iterrows():
            price_text = str(row.get("Цена м3 текст", row["Цена м3"]))
            products += f"<li>{row['Вид товара']} — {price_text} ₽/м³</li>"
        products += "</ul>"

        lat = float(first["Широта"])
        lon = float(first["Долгота"])

        map_link = f"https://yandex.ru/maps/?pt={lon},{lat}&z=13&l=map"

        address = str(first.get("Адрес", "")).strip()
        if address == "" or address.lower() == "nan":
            address = f"{lat}, {lon}"

        region = str(first.get("Регион", "")).strip()
        if region == "" or region.lower() == "nan":
            region = region_by_coords(lat, lon)

        address_html = f"""
            <a href="{map_link}" target="_blank"><b>{address}</b></a>
            <br>
            <span style="font-size:13px;color:#666;">{region}</span>
        """

        map_html = f"""
        <a href="{map_link}" target="_blank" class="mini-map">
            <div class="mini-map-box">
                <div class="mini-pin">📍</div>
                <div class="mini-map-text">Открыть карту</div>
            </div>
        </a>
        """

        rows_html += f"""
        <tr>
            <td><a href="/career/{quote(str(career_name))}" target="_blank"><b>{career_name}</b></a></td>
            <td>{first.get("Юр лицо", "")}</td>
            <td>{first.get("Телефон", "")}</td>
            <td>{address_html}</td>
            <td>{products}</td>
            <td>{map_html}</td>
        </tr>
        """

    tabs = [
        ("all", "Все"),
        ("sand", "Песок"),
        ("stone", "Щебень"),
        ("recycled", "Вторичный / рецикл щебень"),
        ("mix", "Смеси"),
        ("gps", "ГПС"),
        ("screening", "Отсев"),
        ("transfer", "Перевалка"),
    ]

    tabs_html = ""
    for key, title in tabs:
        style = "background:#111;color:white;" if group == key else "background:white;color:#111;"
        tabs_html += f'<a href="/careers?group={key}" style="{style} padding:10px 14px; border-radius:10px; text-decoration:none; border:1px solid #ddd; margin-right:8px; display:inline-block; margin-bottom:8px;">{title}</a>'

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Список всех карьеров</title>
        <style>
            body {{font-family: Arial, sans-serif; background:#f4f5f7; margin:0; padding:30px;}}
            .container {{max-width:1400px; margin:auto;}}
            .card {{background:white; padding:25px; border-radius:14px; margin-bottom:20px; box-shadow:0 4px 14px rgba(0,0,0,0.08);}}
            table {{width:100%; border-collapse:collapse;}}
            th, td {{border-bottom:1px solid #ddd; padding:12px; text-align:left; vertical-align:top;}}
            li {{margin-bottom:6px;}}
            a {{color:#111;}}
            .mini-map {{
                text-decoration: none;
                display: block;
            }}
            .mini-map-box {{
                width: 180px;
                height: 120px;
                border-radius: 12px;
                background:
                    linear-gradient(135deg, #eef2f3 0%, #dfe7ea 100%);
                border: 1px solid #d4d4d4;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                color: #111;
                box-shadow: inset 0 0 0 1px rgba(255,255,255,0.5);
            }}
            .mini-pin {{
                font-size: 28px;
                margin-bottom: 8px;
            }}
            .mini-map-text {{
                font-size: 13px;
                font-weight: bold;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <p><a href="/">← Назад к калькулятору</a></p>
            <div class="card">
                <h1>Список всех карьеров</h1>
                <p>Всего карьеров в выборке: <b>{filtered['Название'].nunique()}</b></p>
                <div>{tabs_html}</div>
            </div>

            <div class="card">
                <table>
                    <tr>
                        <th>Карьер</th>
                        <th>Юр лицо</th>
                        <th>Телефон</th>
                        <th>Адрес / регион</th>
                        <th>Товары</th>
                        <th>Карта</th>
                    </tr>
                    {rows_html}
                </table>
            </div>
        </div>
    </body>
    </html>
    """


@app.route("/career/<path:career_name>")
def career_page(career_name):
    df = load_data()
    career_name = unquote(career_name)

    career_df = df[df["Название"].astype(str) == career_name]

    if career_df.empty:
        return "<h1>Карьер не найден</h1><p><a href='/'>Назад</a></p>"

    first = career_df.iloc[0]

    name = str(first["Название"])
    legal = str(first.get("Юр лицо", ""))
    phone = str(first.get("Телефон", ""))
    lat = float(first["Широта"])
    lon = float(first["Долгота"])
    address = str(first.get("Адрес", "Адрес не указан"))

    products_html = ""
    for _, row in career_df.iterrows():
        price_text = str(row.get("Цена м3 текст", row["Цена м3"]))
        products_html += f"""
            <tr>
                <td>{row["Вид товара"]}</td>
                <td>{price_text} ₽/м³</td>
            </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>{name}</title>
        <script src="https://api-maps.yandex.ru/2.1/?apikey={YANDEX_API_KEY}&lang=ru_RU"></script>
        <style>
            body {{font-family: Arial, sans-serif; background: #f4f5f7; margin: 0; padding: 30px;}}
            .container {{max-width: 1100px; margin: auto;}}
            .card {{background: white; padding: 25px; border-radius: 14px; margin-bottom: 20px; box-shadow: 0 4px 14px rgba(0,0,0,0.08);}}
            table {{width: 100%; border-collapse: collapse;}}
            th, td {{border-bottom: 1px solid #ddd; padding: 10px; text-align: left;}}
            #map {{width: 100%; height: 500px; border-radius: 14px; overflow: hidden;}}
            a {{color: #111;}}
        </style>
    </head>
    <body>
        <div class="container">
            <p><a href="/">← Назад к калькулятору</a></p>

            <div class="card">
                <h1>{name}</h1>
                <p><b>Юр лицо:</b> {legal}</p>
                <p><b>Телефон:</b> {phone}</p>
                <p><b>Адрес:</b> {address}</p>
                <p><b>Координаты:</b> {lat}, {lon}</p>
            </div>

            <div class="card">
                <h2>Товары на карьере</h2>
                <table>
                    <tr>
                        <th>Товар</th>
                        <th>Цена</th>
                    </tr>
                    {products_html}
                </table>
            </div>

            <div class="card">
                <h2>Карта</h2>
                <div id="map"></div>
            </div>
        </div>

        <script>
            ymaps.ready(function () {{
                var map = new ymaps.Map("map", {{
                    center: [{lat}, {lon}],
                    zoom: 12
                }});

                var placemark = new ymaps.Placemark(
                    [{lat}, {lon}],
                    {{
                        balloonContent: "<b>{name}</b><br>{address}<br>{phone}"
                    }},
                    {{
                        preset: "islands#redIcon"
                    }}
                );

                map.geoObjects.add(placemark);
            }});
        </script>
    </body>
    </html>
    """



CRM_USERS = {
    "artem": "1234",
    "manager1": "1234",
    "manager2": "1234",
}

def init_crm_db():
    conn = sqlite3.connect("crm.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager TEXT,
            client TEXT,
            material TEXT,
            supplier TEXT,
            purchase_sum TEXT,
            logistics_sum TEXT,
            client_sum TEXT,
            production_date TEXT,
            shipment_date TEXT,
            contract_notes TEXT,
            status TEXT DEFAULT 'В работе',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def money_to_float(value):
    try:
        value = str(value).replace(" ", "").replace(",", ".").replace("₽", "")
        return float(value)
    except:
        return 0

def calc_manager_commission(margin, scheme):
    if margin <= 0:
        return 0

    if scheme == "simple":
        if margin < 1_000_000:
            return margin * 0.10
        return margin * 0.20

    # progressive
    if margin < 100_000:
        return 0
    if margin < 200_000:
        return margin * 0.10
    if margin < 400_000:
        return margin * 0.12
    if margin < 600_000:
        return margin * 0.14
    if margin < 800_000:
        return margin * 0.16
    if margin < 1_000_000:
        return margin * 0.18
    return margin * 0.20

def get_manager_scheme(manager):
    conn = sqlite3.connect("crm.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS manager_settings (
            manager TEXT PRIMARY KEY,
            scheme TEXT DEFAULT 'progressive'
        )
    """)
    cur.execute("SELECT scheme FROM manager_settings WHERE manager=?", (manager,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "progressive"


def crm_login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "crm_user" not in session:
            return redirect("/crm/login")
        return func(*args, **kwargs)
    return wrapper

@app.route("/crm/login", methods=["GET", "POST"])
def crm_login():
    error = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if CRM_USERS.get(username) == password:
            session["crm_user"] = username
            return redirect("/crm")
        else:
            error = "Неверный логин или пароль"

    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>CRM вход</title>
        <style>
            body {{font-family:Arial;background:#f4f5f7;padding:40px;}}
            .card {{max-width:420px;margin:auto;background:white;padding:30px;border-radius:14px;box-shadow:0 4px 14px rgba(0,0,0,.08);}}
            input,button {{width:100%;padding:14px;margin:10px 0;font-size:16px;box-sizing:border-box;}}
            button {{background:#111;color:white;border:0;border-radius:10px;cursor:pointer;}}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Вход в CRM</h1>
            <form method="POST">
                <input name="username" placeholder="Логин">
                <input name="password" type="password" placeholder="Пароль">
                <button>Войти</button>
            </form>
            <p style="color:red;">{error}</p>
            <p><b>Тест:</b> artem / 1234</p>
        </div>
    </body>
    </html>
    """

@app.route("/crm/logout")
def crm_logout():
    session.pop("crm_user", None)
    return redirect("/crm/login")

@app.route("/crm")
@crm_login_required
def crm_dashboard():
    init_crm_db()
    user = session["crm_user"]

    conn = sqlite3.connect("crm.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM deals WHERE manager=? ORDER BY id DESC", (user,))
    deals = cur.fetchall()
    conn.close()

    scheme = get_manager_scheme(user)
    total_margin = 0
    total_commission = 0

    rows = ""
    for d in deals:
        purchase = money_to_float(d['purchase_sum'])
        logistics = money_to_float(d['logistics_sum'])
        client_sum = money_to_float(d['client_sum'])

        margin = client_sum - purchase - logistics
        commission = calc_manager_commission(margin, scheme)

        total_margin += margin
        total_commission += commission

        rows += f"""
        <tr>
            <td>{d['id']}</td>
            <td><a href="/crm/deal/{d['id']}">{d['client']}</a></td>
            <td>{d['material']}</td>
            <td>{d['supplier']}</td>
            <td>{client_sum:,.0f} ₽</td>
            <td>{margin:,.0f} ₽</td>
            <td>{commission:,.0f} ₽</td>
            <td>{d['status']}</td>
            <td>{d['created_at']}</td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>CRM</title>
        <style>
            body {{font-family:Arial;background:#f4f5f7;padding:30px;}}
            .card {{background:white;padding:25px;border-radius:14px;margin-bottom:20px;box-shadow:0 4px 14px rgba(0,0,0,.08);}}
            table {{width:100%;border-collapse:collapse;}}
            th,td {{padding:12px;border-bottom:1px solid #ddd;text-align:left;}}
            a.btn {{display:inline-block;background:#111;color:white;padding:12px 16px;border-radius:10px;text-decoration:none;}}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>CRM сделок</h1>
            <p>Менеджер: <b>{user}</b></p>
            <p><b>Схема мотивации:</b> {scheme}</p>
            <p><b>Общая маржа:</b> {total_margin:,.0f} ₽</p>
            <p><b>Заработок менеджера:</b> {total_commission:,.0f} ₽</p>
            <a class="btn" href="/crm/deal/new">+ Новая сделка</a>
            <a href="/crm/admin/commissions" style="margin-left:20px;">Настройки мотивации</a>
            <a href="/crm/logout" style="margin-left:20px;">Выйти</a>
        </div>

        <div class="card">
            <table>
                <tr>
                    <th>ID</th>
                    <th>Клиент</th>
                    <th>Материал</th>
                    <th>Поставщик</th>
                    <th>Сумма клиента</th>
                    <th>Маржа</th>
                    <th>Заработок менеджера</th>
                    <th>Статус</th>
                    <th>Создана</th>
                </tr>
                {rows}
            </table>
        </div>
    </body>
    </html>
    """

@app.route("/crm/deal/new", methods=["GET", "POST"])
@crm_login_required
def crm_new_deal():
    init_crm_db()
    user = session["crm_user"]

    if request.method == "POST":
        fields = [
            "client", "material", "supplier", "purchase_sum", "logistics_sum",
            "client_sum", "production_date", "shipment_date", "contract_notes", "status"
        ]
        values = [request.form.get(f, "") for f in fields]

        conn = sqlite3.connect("crm.db")
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO deals (
                manager, client, material, supplier, purchase_sum, logistics_sum,
                client_sum, production_date, shipment_date, contract_notes, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [user] + values)
        conn.commit()
        conn.close()

        return redirect("/crm")

    return """
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Новая сделка</title>
        <style>
            body {font-family:Arial;background:#f4f5f7;padding:30px;}
            .card {max-width:900px;margin:auto;background:white;padding:25px;border-radius:14px;box-shadow:0 4px 14px rgba(0,0,0,.08);}
            input,textarea,select,button {width:100%;padding:13px;margin:8px 0 16px;font-size:16px;box-sizing:border-box;}
            textarea {height:130px;}
            button {background:#111;color:white;border:0;border-radius:10px;cursor:pointer;}
        </style>
    </head>
    <body>
        <div class="card">
            <p><a href="/crm">← Назад</a></p>
            <h1>Новая сделка</h1>

            <form method="POST">
                <label>Клиент</label>
                <input name="client">

                <label>Материал</label>
                <input name="material" placeholder="Песок, щебень, ПГС...">

                <label>Поставщик / карьер</label>
                <input name="supplier">

                <label>Счет поставщика / закупка</label>
                <input name="purchase_sum">

                <label>Логистика / расходы</label>
                <input name="logistics_sum">

                <label>Счет клиенту / продажа</label>
                <input name="client_sum">

                <label>Дата производства</label>
                <input name="production_date" type="date">

                <label>Дата отгрузки</label>
                <input name="shipment_date" type="date">

                <label>Оценка договора / ключевые условия</label>
                <textarea name="contract_notes" placeholder="НДС, предоплата, сроки, штрафы, риски..."></textarea>

                <label>Статус</label>
                <select name="status">
                    <option>В работе</option>
                    <option>Ожидаем оплату</option>
                    <option>Оплачено клиентом</option>
                    <option>Производство</option>
                    <option>Отгрузка</option>
                    <option>Закрыта</option>
                </select>

                <button>Создать сделку</button>
            </form>
        </div>
    </body>
    </html>
    """


@app.route("/crm/admin/commissions", methods=["GET", "POST"])
@crm_login_required
def crm_admin_commissions():
    if session.get("crm_user") != "artem":
        return "<h1>Доступ запрещён</h1><p><a href='/crm'>Назад</a></p>"

    init_crm_db()

    if request.method == "POST":
        manager = request.form.get("manager", "")
        scheme = request.form.get("scheme", "progressive")

        conn = sqlite3.connect("crm.db")
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO manager_settings (manager, scheme)
            VALUES (?, ?)
            ON CONFLICT(manager) DO UPDATE SET scheme=excluded.scheme
        """, (manager, scheme))
        conn.commit()
        conn.close()

    conn = sqlite3.connect("crm.db")
    cur = conn.cursor()
    cur.execute("SELECT manager, scheme FROM manager_settings ORDER BY manager")
    settings = cur.fetchall()
    conn.close()

    rows = ""
    for manager, scheme in settings:
        rows += f"<tr><td>{manager}</td><td>{scheme}</td></tr>"

    options = ""
    for manager in CRM_USERS.keys():
        options += f"<option>{manager}</option>"

    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Настройки мотивации</title>
        <style>
            body {{font-family:Arial;background:#f4f5f7;padding:30px;}}
            .card {{max-width:900px;margin:auto;background:white;padding:25px;border-radius:14px;box-shadow:0 4px 14px rgba(0,0,0,.08);}}
            input,select,button {{width:100%;padding:13px;margin:8px 0 16px;font-size:16px;box-sizing:border-box;}}
            button {{background:#111;color:white;border:0;border-radius:10px;cursor:pointer;}}
            table {{width:100%;border-collapse:collapse;}}
            th,td {{padding:12px;border-bottom:1px solid #ddd;text-align:left;}}
        </style>
    </head>
    <body>
        <div class="card">
            <p><a href="/crm">← Назад</a></p>
            <h1>Настройки мотивации менеджеров</h1>

            <form method="POST">
                <label>Менеджер</label>
                <select name="manager">{options}</select>

                <label>Схема мотивации</label>
                <select name="scheme">
                    <option value="progressive">Прогрессивная: 0%, 10%, 12%, 14%, 16%, 18%, 20%</option>
                    <option value="simple">Простая: до 1 млн — 10%, от 1 млн — 20%</option>
                </select>

                <button>Сохранить</button>
            </form>

            <h2>Текущие настройки</h2>
            <table>
                <tr><th>Менеджер</th><th>Схема</th></tr>
                {rows}
            </table>
        </div>
    </body>
    </html>
    """


@app.route("/crm/deal/<int:deal_id>")
@crm_login_required
def crm_deal_page(deal_id):
    user = session["crm_user"]

    conn = sqlite3.connect("crm.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM deals WHERE id=? AND manager=?", (deal_id, user))
    d = cur.fetchone()
    conn.close()

    if not d:
        return "<h1>Сделка не найдена</h1><p><a href='/crm'>Назад</a></p>"

    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Сделка #{d['id']}</title>
        <style>
            body {{font-family:Arial;background:#f4f5f7;padding:30px;}}
            .card {{max-width:900px;margin:auto;background:white;padding:25px;border-radius:14px;box-shadow:0 4px 14px rgba(0,0,0,.08);}}
            p {{font-size:17px;}}
        </style>
    </head>
    <body>
        <div class="card">
            <p><a href="/crm">← Назад</a></p>
            <h1>Сделка #{d['id']}</h1>

            <p><b>Клиент:</b> {d['client']}</p>
            <p><b>Материал:</b> {d['material']}</p>
            <p><b>Поставщик:</b> {d['supplier']}</p>
            <p><b>Закупка:</b> {d['purchase_sum']}</p>
            <p><b>Логистика:</b> {d['logistics_sum']}</p>
            <p><b>Продажа клиенту:</b> {d['client_sum']}</p>
            <p><b>Дата производства:</b> {d['production_date']}</p>
            <p><b>Дата отгрузки:</b> {d['shipment_date']}</p>
            <p><b>Статус:</b> {d['status']}</p>

            <h2>Оценка договора / условия</h2>
            <p>{d['contract_notes']}</p>
        </div>
    </body>
    </html>
    """


if __name__ == "__main__":
    app.run(debug=True)
