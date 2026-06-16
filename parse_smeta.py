import requests
import pandas as pd
from bs4 import BeautifulSoup
import re
import time

rows = []

START_ID = 1
END_ID = 3000

for quarry_id in range(START_ID, END_ID + 1):

    url = f"https://smeta-n.ru/materials_and_suppliers/page_of_quarry-{quarry_id}"

    try:
        r = requests.get(url, timeout=15)

        if r.status_code != 200:
            continue

        html = r.text

        if "карьер" not in html.lower():
            continue

        print("Нашел карьер:", quarry_id)

        soup = BeautifulSoup(html, "lxml")

        title = soup.title.get_text(strip=True) if soup.title else ""

        phone = ""

        phone_match = re.search(r'(\+7[\d\-\s\(\)]{10,})', html)

        if phone_match:
            phone = phone_match.group(1)

        coords = re.findall(
            r'([0-9]{2}\.[0-9]+)\s*,\s*([0-9]{2}\.[0-9]+)',
            html
        )

        lat = ""
        lon = ""

        if coords:
            lat = coords[0][0]
            lon = coords[0][1]

        prices = re.findall(
            r'([А-Яа-яA-Za-z0-9\s\-\(\)\.]+)\s+([0-9]+)\s*руб',
            html
        )

        for item in prices:

            rows.append({
                "Название": title,
                "Юр лицо": "",
                "Вид товара": item[0].strip(),
                "Цена м3": item[1],
                "Широта": lat,
                "Долгота": lon,
                "Телефон": phone,
                "Стоимость доставки руб_км_м3": ""
            })

        time.sleep(0.3)

    except Exception as e:
        print(quarry_id, e)

df = pd.DataFrame(rows)

df.to_csv(
    "careers_from_smeta.csv",
    index=False,
    encoding="utf-8-sig"
)

print("Готово")
print("Строк найдено:", len(df))
