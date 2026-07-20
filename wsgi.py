import os

from flask import request

from app import YANDEX_API_KEY, app
from zbi_calculator import zbi_bp


app.config["YANDEX_API_KEY"] = os.getenv("YANDEX_API_KEY", YANDEX_API_KEY)

if "zbi" not in app.blueprints:
    app.register_blueprint(zbi_bp)


@app.after_request
def add_zbi_navigation(response):
    if request.path == "/" and response.mimetype == "text/html":
        html = response.get_data(as_text=True)
        if 'href="/zbi"' not in html:
            link = (
                '<a href="/zbi" style="position:fixed;right:20px;top:18px;'
                'z-index:9999;background:#1f6feb;color:white;padding:10px 16px;'
                'border-radius:10px;text-decoration:none;font-weight:bold">'
                'Калькулятор ЖБИ</a>'
            )
            response.set_data(html.replace("</body>", link + "</body>"))
    return response
from flask import request

from app import app
from zbi_calculator import zbi_bp


if "zbi" not in app.blueprints:
    app.register_blueprint(zbi_bp)


@app.after_request
def add_zbi_navigation(response):
    if request.path == "/" and response.mimetype == "text/html":
        html = response.get_data(as_text=True)
        if 'href="/zbi"' not in html:
            link = (
                '<a href="/zbi" style="position:fixed;right:20px;top:18px;'
                'z-index:9999;background:#1f6feb;color:white;padding:10px 16px;'
                'border-radius:10px;text-decoration:none;font-weight:bold">'
                'Калькулятор ЖБИ</a>'
            )
            response.set_data(html.replace("</body>", link + "</body>"))
    return response
