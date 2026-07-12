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

app = Flask(__name__)
app.register_blueprint(crm_bp)
app.secret_key = "change-this-secret-key"

YANDEX_API_KEY = "aaaac1c5-442b-4970-9bd9-1f1929227a78"
CSV_FILE = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSg5caW9yrTC7JLaz6YpYxH1WT20GyocPLToq_2tbvAktDz5yImYlF0z_C2xueHwk2F6l18xvKf3nKL/pub?output=csv"

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


def load_data():
    response = requests.get(CSV_FILE, timeout=20)
    response.encoding = "utf-8"
    csv_text = response.text
    df = pd.read_csv(StringIO(csv_text))
    df["Цена м3 текст"] = df["Цена м3"].astype(str)

    df["Цена м3"] = (
        df["Цена м3"]
        .astype(str)
        .str.replace(",", ".", regex=False)
        .str.replace("по запросу", "0", case=False, regex=False)
        .str.replace("По запросу", "0", regex=False)
    )
    df["Цена м3"] = pd.to_numeric(df["Цена м3"], errors="coerce").fillna(0)
    df["Широта"] = pd.to_numeric(df["Широта"], errors="coerce")
    df["Долгота"] = pd.to_numeric(df["Долгота"], errors="coerce")
    df["Стоимость доставки руб_км_м3"] = pd.to_numeric(df["Стоимость доставки руб_км_м3"], errors="coerce")
    df["Группа материала"] = df["Вид товара"].apply(material_group)
    return df.dropna(subset=["Название", "Юр лицо", "Вид товара", "Цена м3", "Широта", "Долгота"])

@app.route("/", methods=["GET", "POST"])
def home():
    df = load_data()
    preferred_groups = [
        "Песок — тонкий",
        "Песок — мелкий",
        "Песок — средний",
        "Песок — крупный",
        "Песок — крупность не указана",
        "Щебень",
        "Вторичный / рецикл щебень",
        "Смеси / ЩПС",
        "ГПС / ПГС",
        "Отсев",
        "Перевалка",
        "Другие материалы",
    ]
    available_groups = set(df["Группа материала"].dropna().unique())
    sand_types = ["Любой материал / ближайший карьер"] + [
        group for group in preferred_groups if group in available_groups
    ]

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

        if sand_type == "Любой материал / ближайший карьер":
            filtered = df
        else:
            filtered = df[df["Группа материала"] == sand_type]

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
        })

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
                <a href="/office/login" style="font-size:18px; font-weight:bold;">CRM сделок</a>
            </p>

            <form method="POST" class="card">
                <label>Адрес клиента</label>
                <input name="address" placeholder="Например: Москва, ул. Ленина 10" required>
                <label>Вид песка</label>
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
        </style>
    </head>
    <body>
        <div class="container">
            <p><a href="/">← Назад к калькулятору</a></p>

            <div class="card">
                <h1>Заявка на перевозку</h1>
                <p><b>Карьер:</b> {career_name}</p>
                <p><b>Юр лицо:</b> {first.get("Юр лицо", "")}</p>
                <p><b>Телефон карьера:</b> {first.get("Телефон", "")}</p>
            </div>

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
                return document.getElementById(id).value;
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
