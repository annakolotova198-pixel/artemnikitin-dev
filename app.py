from flask import Flask, request
import pandas as pd
import requests

app = Flask(__name__)

YANDEX_API_KEY = "aaaac1c5-442b-4970-9bd9-1f1929227a78"
CSV_FILE = "careers.csv"

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
    coords = str(lon) + "," + str(lat)
    params = {"apikey": YANDEX_API_KEY, "geocode": coords, "format": "json", "lang": "ru_RU"}
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
    df = pd.read_csv(CSV_FILE)
    df["Цена м3"] = pd.to_numeric(df["Цена м3"], errors="coerce")
    df["Широта"] = pd.to_numeric(df["Широта"], errors="coerce")
    df["Долгота"] = pd.to_numeric(df["Долгота"], errors="coerce")
    df["Стоимость доставки руб_км_м3"] = pd.to_numeric(df["Стоимость доставки руб_км_м3"], errors="coerce")
    df = df.dropna(subset=["Название", "Юр лицо", "Вид товара", "Цена м3", "Широта", "Долгота", "Стоимость доставки руб_км_м3"])
    return df

@app.route("/", methods=["GET", "POST"])
def home():
    df = load_data()
    sand_types = ["Любой песок / ближайший карьер"] + sorted(df["Вид товара"].dropna().unique())
    result_html = ""

    if request.method == "POST":
        address = request.form.get("address", "").strip()
        sand_type = request.form.get("sand_type", "").strip()
        volume_raw = request.form.get("volume", "").strip()

        try:
            volume = float(volume_raw.replace(",", "."))
        except Exception:
            return "<h1>Ошибка</h1><p>Объем должен быть числом.</p><a href='/'>Назад</a>"

        client_lat, client_lon = geocode_address(address)

        if client_lat is None or client_lon is None:
            return "<h1>Ошибка</h1><p>Адрес не найден через Яндекс.</p><a href='/'>Назад</a>"

        if sand_type == "Любой песок / ближайший карьер":
            filtered = df
        else:
            filtered = df[df["Вид товара"] == sand_type]

        routes = []

        for _, row in filtered.iterrows():
            distance_km, duration_min = get_route(row["Широта"], row["Долгота"], client_lat, client_lon)

            if distance_km is None:
                continue

            delivery_price_m3 = distance_km * row["Стоимость доставки руб_км_м3"]
            total_price_m3 = row["Цена м3"] + delivery_price_m3
            total_sum = total_price_m3 * volume

            item = {
                "career": row["Название"],
                "legal": row["Юр лицо"],
                "sand_type": row["Вид товара"],
                "phone": row.get("Телефон", ""),
                "distance": round(distance_km, 1),
                "duration": round(duration_min),
                "sand_price": round(row["Цена м3"], 2),
                "delivery_rate": round(row["Стоимость доставки руб_км_м3"], 2),
                "delivery_price_m3": round(delivery_price_m3, 2),
                "total_price_m3": round(total_price_m3, 2),
                "total_sum": round(total_sum, 2),
                "career_lat": row["Широта"],
                "career_lon": row["Долгота"],
                "career_address": reverse_geocode(row["Широта"], row["Долгота"])
            }

            routes.append(item)

        if not routes:
            return "<h1>Ошибка</h1><p>Не удалось построить маршрут через OSRM.</p><a href='/'>Назад</a>"

        top_distance = sorted(routes, key=lambda x: x["distance"])[:10]
        top_price = sorted(routes, key=lambda x: x["total_price_m3"])[:10]
        best = top_distance[0]

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
        result_html += "<p><b>Доставка:</b> " + str(best["delivery_price_m3"]) + " ₽/м³</p>"
        result_html += "<p><b>Итого за 1 м³:</b> " + str(best["total_price_m3"]) + " ₽</p>"
        result_html += "<h2>Общая сумма: " + str(best["total_sum"]) + " ₽</h2>"
        result_html += "<h3>Все виды песка на этом карьере</h3>"
        result_html += products_html
        
        result_html += "<h3>Карта</h3>"
        result_html += "<div id='map' style='width: 100%; height: 450px; border-radius: 14px; overflow: hidden;'></div>"
        result_html += "<script src='https://api-maps.yandex.ru/2.1/?apikey=" + YANDEX_API_KEY + "&lang=ru_RU'></script>"
        result_html += "<script>"
        result_html += "ymaps.ready(function () {"
        result_html += "var map = new ymaps.Map('map', {center: [" + str(client_lat) + ", " + str(client_lon) + "], zoom: 9});"
        result_html += "var clientPlacemark = new ymaps.Placemark([" + str(client_lat) + ", " + str(client_lon) + "], {balloonContent: 'Клиент'});"
        result_html += "var careerPlacemark = new ymaps.Placemark([" + str(best["career_lat"]) + ", " + str(best["career_lon"]) + "], {balloonContent: 'Карьер: " + str(best["career"]) + "'});"
        result_html += "map.geoObjects.add(clientPlacemark);"
        result_html += "map.geoObjects.add(careerPlacemark);"
        result_html += "map.setBounds(map.geoObjects.getBounds(), {checkZoomRange: true, zoomMargin: 40});"
        result_html += "});"
        result_html += "</script>"
        result_html += "</div>"

        result_html += "<div class='result'><h2>Топ-10 ближайших карьеров</h2>"

        result_html += "<table><tr><th>Карьер</th><th>Песок</th><th>Км</th><th>Мин</th><th>Цена песка</th><th>Итого ₽/м³</th></tr>"
        for item in top_distance:
            result_html += "<tr>"
            result_html += "<td>" + str(item["career"]) + "</td>"
            result_html += "<td>" + str(item["sand_type"]) + "</td>"
            result_html += "<td>" + str(item["distance"]) + "</td>"
            result_html += "<td>" + str(item["duration"]) + "</td>"
            result_html += "<td>" + str(item["sand_price"]) + "</td>"
            result_html += "<td>" + str(item["total_price_m3"]) + "</td>"
            result_html += "</tr>"
        result_html += "</table></div>"

        result_html += "<div class='result'><h2>Топ-10 по цене</h2>"
        result_html += "<table><tr><th>Карьер</th><th>Песок</th><th>Км</th><th>Мин</th><th>Цена песка</th><th>Итого ₽/м³</th></tr>"
        for item in top_price:
            result_html += "<tr>"
            result_html += "<td>" + str(item["career"]) + "</td>"
            result_html += "<td>" + str(item["sand_type"]) + "</td>"
            result_html += "<td>" + str(item["distance"]) + "</td>"
            result_html += "<td>" + str(item["duration"]) + "</td>"
            result_html += "<td>" + str(item["sand_price"]) + "</td>"
            result_html += "<td>" + str(item["total_price_m3"]) + "</td>"
            result_html += "</tr>"
        result_html += "</table></div>"

    options = ""
    for sand in sand_types:
        options += '<option value="' + str(sand) + '">' + str(sand) + '</option>'

    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Калькулятор доставки песка</title>
        <style>
            body {font-family: Arial, sans-serif; background: #f4f5f7; margin: 0; padding: 30px;}
            .container {max-width: 1200px; margin: auto;}
            .card, .result {background: white; padding: 25px; border-radius: 14px; margin-bottom: 20px; box-shadow: 0 4px 14px rgba(0,0,0,0.08);}
            input, select, button {width: 100%; padding: 14px; margin-top: 8px; margin-bottom: 18px; font-size: 16px; box-sizing: border-box;}
            button {background: #111; color: white; border: none; border-radius: 10px; cursor: pointer; font-size: 18px;}
            table {width: 100%; border-collapse: collapse;}
            th, td {border-bottom: 1px solid #ddd; padding: 10px; text-align: left; vertical-align: top;}
            li {margin-bottom: 8px;}
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
                <button type="submit">Рассчитать</button>
            </form>
            """ + result_html + """
        </div>
    </body>
    </html>
    """

if __name__ == "__main__":
    app.run(debug=True)
