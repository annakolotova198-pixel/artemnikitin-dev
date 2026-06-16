from flask import Flask, request
import pandas as pd
import requests
import json
from io import StringIO

app = Flask(__name__)

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

def load_data():
    response = requests.get(CSV_FILE, timeout=20)
    response.encoding = "utf-8"
    csv_text = response.text
    df = pd.read_csv(StringIO(csv_text))
    df["Цена м3"] = pd.to_numeric(df["Цена м3"], errors="coerce")
    df["Широта"] = pd.to_numeric(df["Широта"], errors="coerce")
    df["Долгота"] = pd.to_numeric(df["Долгота"], errors="coerce")
    df["Стоимость доставки руб_км_м3"] = pd.to_numeric(df["Стоимость доставки руб_км_м3"], errors="coerce")
    return df.dropna(subset=["Название", "Юр лицо", "Вид товара", "Цена м3", "Широта", "Долгота", "Стоимость доставки руб_км_м3"])

@app.route("/", methods=["GET", "POST"])
def home():
    df = load_data()
    sand_types = ["Любой песок / ближайший карьер"] + sorted(df["Вид товара"].dropna().unique())

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

        filtered = df if sand_type == "Любой песок / ближайший карьер" else df[df["Вид товара"] == sand_type]

        routes = []

        for _, row in filtered.iterrows():
            distance_km, duration_min = get_route(row["Широта"], row["Долгота"], client_lat, client_lon)

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
                "career_address": reverse_geocode(row["Широта"], row["Долгота"])
            })

        if not routes:
            return "<h1>Ошибка</h1><p>Не удалось построить маршрут.</p><a href='/'>Назад</a>"

        top_distance = sorted(routes, key=lambda x: x["distance"])[:10]
        top_price = sorted(routes, key=lambda x: x["total_price_m3"])[:10]
        best = top_distance[0]

        route_data = {
            "client_lat": client_lat,
            "client_lon": client_lon,
            "career_lat": best["career_lat"],
            "career_lon": best["career_lon"],
            "client_address": address,
            "career_name": best["career"]
        }

        career_products = df[df["Название"] == best["career"]].sort_values("Цена м3")

        products_html = "<ul>"
        for _, product in career_products.iterrows():
            products_html += "<li>" + str(product["Вид товара"]) + " — " + str(product["Цена м3"]) + " ₽/м³</li>"
        products_html += "</ul>"

        result_html += "<div class='result'>"
        result_html += "<h2>Лучший ближайший карьер</h2>"
        result_html += "<p><b>Карьер:</b> " + str(best["career"]) + "</p>"
        result_html += "<p><b>Юр лицо:</b> " + str(best["legal"]) + "</p>"
        result_html += "<p><b>Телефон:</b> " + str(best["phone"]) + "</p>"
        result_html += "<p><b>Адрес карьера:</b> " + str(best["career_address"]) + "</p>"
        result_html += "<p><b>Выбранный песок:</b> " + str(best["sand_type"]) + "</p>"
        result_html += "<p><b>Расстояние:</b> " + str(best["distance"]) + " км</p>"
        result_html += "<p><b>Время:</b> " + str(best["duration"]) + " мин</p>"
        result_html += "<p><b>Цена песка:</b> " + str(best["sand_price"]) + " ₽/м³</p>"
        result_html += "<p><b>Доставка перевозчик:</b> " + str(best["carrier_delivery_m3"]) + " ₽/м³</p>"
        result_html += "<p><b>Доставка продажа:</b> " + str(best["sale_delivery_m3"]) + " ₽/м³</p>"
        result_html += "<p><b>Цена закупки за 1 м³:</b> " + str(best["purchase_price_m3"]) + " ₽</p>"
        result_html += "<p><b>Цена продажи за 1 м³:</b> " + str(best["sale_price_m3"]) + " ₽</p>"
        result_html += "<p><b>Заработок за 1 м³:</b> " + str(best["profit_m3"]) + " ₽</p>"
        
        result_html += "<h2>Общая закупка: " + str(best["total_purchase"]) + " ₽</h2>"
        result_html += "<h2>Общая продажа: " + str(best["total_sale"]) + " ₽</h2>"
        result_html += "<h2>Общий заработок: " + str(best["total_profit"]) + " ₽</h2>"
        result_html += "<h3>Все виды песка на этом карьере</h3>"
        result_html += products_html
        result_html += "</div>"

        result_html += "<div class='result'><h2>Топ-10 ближайших карьеров</h2>"
        result_html += "<table><tr><th>Карьер</th><th>Песок</th><th>Км</th><th>Мин</th><th>Цена песка</th><th>Итого ₽/м³</th></tr>"
        for item in top_distance:
            result_html += "<tr><td>" + str(item["career"]) + "</td><td>" + str(item["sand_type"]) + "</td><td>" + str(item["distance"]) + "</td><td>" + str(item["duration"]) + "</td><td>" + str(item["sand_price"]) + "</td><td>" + str(item["total_price_m3"]) + "</td></tr>"
        result_html += "</table></div>"

        result_html += "<div class='result'><h2>Топ-10 по цене</h2>"
        result_html += "<table><tr><th>Карьер</th><th>Песок</th><th>Км</th><th>Мин</th><th>Цена песка</th><th>Итого ₽/м³</th></tr>"
        for item in top_price:
            result_html += "<tr><td>" + str(item["career"]) + "</td><td>" + str(item["sand_type"]) + "</td><td>" + str(item["distance"]) + "</td><td>" + str(item["duration"]) + "</td><td>" + str(item["sand_price"]) + "</td><td>" + str(item["total_price_m3"]) + "</td></tr>"
        result_html += "</table></div>"

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
            table {width: 100%; border-collapse: collapse;}
            th, td {border-bottom: 1px solid #ddd; padding: 10px; text-align: left; vertical-align: top;}
            li {margin-bottom: 8px;}
            #map {width: 100%; height: 520px; border-radius: 14px; overflow: hidden;}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Калькулятор доставки песка</h1>

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

if __name__ == "__main__":
    app.run(debug=True)
