from telegram_lead_parser import extract_address, extract_products, extract_quantities, parse_message


def test_request_is_parsed():
    lead = parse_message(
        text=(
            "Нужно завтра: песок мытый 25 м3 и щебень вторичный 20 тонн.\n"
            "Доставка: Москва, Беговая 22с41. Тел. +7 999 123-45-67, @zakaz_stroy"
        ),
        chat_id=-100123,
        message_id=55,
        chat_title="Строительные заявки",
        sender_id=1,
        sender_name="Иван",
    )
    assert lead is not None
    assert lead.products == ["Песок", "Вторичный щебень"]
    assert lead.quantities == [{"value": "25", "unit": "м³"}, {"value": "20", "unit": "тонн"}]
    assert lead.phones == ["+79991234567"]
    assert lead.address == "Москва, Беговая 22с41"


def test_offer_is_not_a_lead():
    assert (
        parse_message(
            text="Продаём щебень, всегда в наличии, звоните",
            chat_id=-1001,
            message_id=1,
            chat_title="Продажи",
        )
        is None
    )


def test_product_and_address_helpers():
    assert extract_products("Нужен керамзит и ФБС") == ["Керамзит", "ФБС"]
    assert extract_quantities("3 рейса, 12 шт.") == [
        {"value": "3", "unit": "рейса"},
        {"value": "12", "unit": "шт."},
    ]
    assert extract_address("Объект: МО, Одинцовский район") == "МО, Одинцовский район"
