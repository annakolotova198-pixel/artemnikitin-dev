import requests
import time

URL = "http://127.0.0.1:5000"

tests = [
    ("Москва, Красная площадь", "Песок любой"),
    ("Москва, Варшавское шоссе 100", "Щебень любой"),
    ("Химки, Юбилейный проспект 1", "ГПС любая"),
    ("Мытищи, Олимпийский проспект 10", "Смесь любая"),
    ("Балашиха, шоссе Энтузиастов 1", "Любой материал / ближайший карьер"),
    ("Красногорск, Павшинский бульвар 1", "Песок любой"),
    ("Одинцово, Можайское шоссе 10", "Щебень любой"),
    ("Люберцы, Октябрьский проспект 100", "ГПС любая"),
    ("Домодедово, Каширское шоссе 1", "Смесь любая"),
    ("Подольск, ул. Кирова 10", "Любой материал / ближайший карьер"),
    ("Сергиев Посад, Красной Армии 1", "Песок любой"),
    ("Коломна, ул. Октябрьской Революции 1", "Щебень любой"),
    ("Серпухов, ул. Ворошилова 1", "ГПС любая"),
    ("Клин, Советская площадь 1", "Смесь любая"),
    ("Электросталь, проспект Ленина 1", "Любой материал / ближайший карьер"),
]

success = 0
errors = 0
times = []

for i, (address, material) in enumerate(tests, start=1):
    start = time.time()

    try:
        r = requests.post(
            URL,
            data={
                "address": address,
                "sand_type": material,
                "volume": "20",
                "carrier_rate": "15",
                "sale_rate": "25"
            },
            timeout=120
        )

        elapsed = round(time.time() - start, 1)
        times.append(elapsed)

        if r.status_code == 200:
            success += 1
            print(f"{i}. OK | {material} | {address} | {elapsed} сек")
        else:
            errors += 1
            print(f"{i}. ERROR {r.status_code} | {address}")

    except Exception as e:
        errors += 1
        print(f"{i}. FAIL | {address} | {e}")

print("\n========== ИТОГ ==========")
print("Успешно:", success)
print("Ошибок:", errors)

if times:
    print("Среднее время:", round(sum(times)/len(times), 1), "сек")
    print("Минимум:", min(times), "сек")
    print("Максимум:", max(times), "сек")
