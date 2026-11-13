"""Test suite completa per SubitoScraper, promozioni carrello Amazon e notifiche Telegram.

Verifica:
1. SubitoScraper: parsing feature, prezzi, spedizione TuttoSubito, annunci in regalo e semi-nuovi (inclusa chiamata live ad Hades API).
2. Promozioni carrello: parsing regex 'sconto immediato direttamente a carrello' e 'risparmia XX€ al check-out'.
3. Notifier: formattazione template Subito (regalo e semi-nuovo) e riga promozione carrello.
4. Main: logica di filtraggio e classificazione alert (PRICE_ERROR per regali, SUPER_DEAL per semi-nuovi).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Aggiungi root directory al path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from subito_scraper import SubitoScraper, LIKE_NEW_KEYWORDS
from scraper import parse_cart_promo
from notifier import format_alert_message
from main import _passes_basic_filters, _classify_alert


def test_cart_promo_parsing():
    """Valida il riconoscimento delle promozioni a carrello Amazon."""
    print("=== Test 1: Parsing promozioni a carrello ===")

    # Caso 1: Risparmia XX€ al check-out
    text1 = "Notebook Gaming RTX 4060. Risparmia 50,00 € al check-out. Consegna gratuita."
    has1, val1, desc1 = parse_cart_promo(text1)
    assert has1 is True, f"Atteso True, ottenuto {has1}"
    assert val1 == 50.0, f"Atteso 50.0, ottenuto {val1}"
    print("  [OK] 'Risparmia 50,00 € al check-out' -> discount: 50.00€")

    # Caso 2: Risparmia € XX al check-out
    text2 = "Smartphone Samsung Galaxy S24. Risparmia € 100 al check-out."
    has2, val2, desc2 = parse_cart_promo(text2)
    assert has2 is True
    assert val2 == 100.0
    print("  [OK] 'Risparmia € 100 al check-out' -> discount: 100.00€")

    # Caso 3: Sconto immediato direttamente a carrello con importo dopo
    text3 = "Samsung Bundle: sconto immediato direttamente a carrello di 75€ sul totale."
    has3, val3, desc3 = parse_cart_promo(text3)
    assert has3 is True
    assert val3 == 75.0
    print("  [OK] 'sconto immediato direttamente a carrello di 75€' -> discount: 75.00€")

    # Caso 4: Sconto immediato direttamente a carrello con importo prima
    text4 = "Offerta speciale: 30€ sconto immediato direttamente a carrello al pagamento."
    has4, val4, desc4 = parse_cart_promo(text4)
    assert has4 is True
    assert val4 == 30.0
    print("  [OK] '30€ sconto immediato direttamente a carrello' -> discount: 30.00€")

    # Caso 5: Frase generica senza sconto
    text5 = "Prodotto standard senza promozioni. Prezzo: 29.99 €."
    has5, val5, desc5 = parse_cart_promo(text5)
    assert has5 is False
    assert val5 == 0.0
    print("  [OK] Testo senza cart promo correttamente escluso")


def test_subito_unit_parsing():
    """Valida i metodi interni di estrazione di SubitoScraper."""
    print("\n=== Test 2: Unit test SubitoScraper parsing ===")
    config = {
        "CATEGORY_NAME": "Subito.it Tech: Regali & Semi-Nuovi",
        "STORE": "subito",
        "CATEGORIES": [10, 11, 12, 13],
    }
    scraper = SubitoScraper(config)

    # Test estrazione prezzo
    ad_free = {
        "urn": "id:ad:12345",
        "subject": "Cavo HDMI regalasi",
        "features": [
            {"uri": "/price", "values": [{"key": "0", "value": "0 €"}]},
            {"uri": "/item_shipping_allowed", "values": [{"key": "1", "value": "Sì"}]},
        ],
        "geo": {
            "town": {"value": "Milano"},
            "region": {"value": "Lombardia"},
        },
        "urls": {"default": "https://www.subito.it/informatica/cavo-12345.htm"},
    }

    price = scraper._extract_price(ad_free)
    assert price == 0.0, f"Atteso 0.0, ottenuto {price}"
    assert scraper._is_shipping_allowed(ad_free) is True

    parsed_free = scraper.parse_ad(ad_free, is_free_mode=True)
    assert parsed_free["asin"] == "subito_id:ad:12345"
    assert parsed_free["is_free"] is True
    assert parsed_free["condition"] == "In regalo"
    assert parsed_free["store"] == "subito"
    assert parsed_free["location"] == "Milano (Lombardia)"
    assert parsed_free["shipping_allowed"] is True
    print("  [OK] Annuncio in regalo parsato correttamente")

    # Test annuncio semi-nuovo con prezzo
    ad_like_new = {
        "urn": "id:ad:67890",
        "subject": "PlayStation 5 pari al nuovo sigillato",
        "features": [
            {"uri": "/price", "values": [{"key": "350", "value": "350 €"}]},
            {"uri": "/item_shipping_allowed", "values": [{"key": "1", "value": "Sì"}]},
            {"uri": "/item_condition", "values": [{"value": "Come nuovo"}]},
        ],
        "geo": {
            "town": {"value": "Roma"},
            "region": {"value": "Lazio"},
        },
        "urls": {"default": "https://www.subito.it/videogiochi/ps5-67890.htm"},
    }

    parsed_deal = scraper.parse_ad(ad_like_new, is_free_mode=False)
    assert parsed_deal["current_price"] == 350.0
    assert parsed_deal["is_free"] is False
    assert parsed_deal["condition"] == "Come nuovo"
    assert parsed_deal["location"] == "Roma (Lazio)"
    assert parsed_deal["shipping_allowed"] is True
    print("  [OK] Annuncio semi-nuovo parsato correttamente")


def test_notifier_formatting():
    """Valida la formattazione dei messaggi Telegram in notifier.py."""
    print("\n=== Test 3: Formattazione messaggi Telegram ===")

    # 1. Annuncio Subito in regalo
    subito_free_prod = {
        "store": "subito",
        "asin": "subito_ad_9999",
        "title": "Monitor LCD 24 pollici funzionante",
        "current_price": 0.0,
        "is_free": True,
        "condition": "In regalo",
        "location": "Bologna (Emilia-Romagna)",
        "shipping_allowed": True,
        "url": "https://www.subito.it/informatica/monitor-9999.htm",
        "category_name": "Subito Tech Regali",
    }
    msg_free = format_alert_message(subito_free_prod)
    assert "🎁 OGGETTO IN REGALO (0 €) SU SUBITO 🎁" in msg_free
    assert "GRATIS (0 €)" in msg_free
    assert "📍 Località: Bologna (Emilia-Romagna)" in msg_free
    assert "📦 Spedizione TuttoSubito: Disponibile" in msg_free
    print("  [OK] Template Telegram Subito Regalo verificato")

    # 2. Annuncio Subito semi-nuovo
    subito_deal_prod = {
        "store": "subito",
        "asin": "subito_ad_8888",
        "title": "iPhone 15 Pro 128GB sigillato mai aperto",
        "current_price": 799.0,
        "is_free": False,
        "condition": "Come nuovo",
        "location": "Napoli (Campania)",
        "shipping_allowed": True,
        "url": "https://www.subito.it/telefonia/iphone-8888.htm",
    }
    msg_deal = format_alert_message(subito_deal_prod)
    assert "💎 OCCASIONE TECH SEMI-NUOVO SU SUBITO 💎" in msg_deal
    assert "€799.00" in msg_deal
    assert "📍 Località: Napoli (Campania)" in msg_deal
    assert "📦 Spedizione TuttoSubito: Disponibile" in msg_deal
    print("  [OK] Template Telegram Subito Semi-Nuovo verificato")

    # 3. Amazon con cart promo
    amazon_promo_prod = {
        "store": "amazon",
        "asin": "B0TESTAMZ1",
        "title": "Samsung Galaxy Book4 con bundle accessori",
        "current_price": 699.0,
        "original_price": 899.0,
        "discount_percent": 22.2,
        "final_price": 649.0,
        "has_cart_promo": True,
        "cart_promo_discount": 50.0,
        "cart_promo_desc": "Risparmia 50,00 € al check-out",
        "is_prime": True,
        "is_amazon_fulfilled": True,
    }
    msg_amz = format_alert_message(amazon_promo_prod)
    assert "🛒 Promozione Carrello: <b>-50.00€</b> di sconto immediato al check-out!" in msg_amz
    print("  [OK] Riga Promozione Carrello verificata su Amazon")


def test_main_filters_and_classification():
    """Valida filtri e classificazione in main.py."""
    print("\n=== Test 4: Filtri e classificazione in main.py ===")
    config = {
        "BRAND_WHITELIST": ["Apple", "Samsung", "Sony"],
        "BLACKLIST_KEYWORDS": ["custodia", "cover", "cavo"],
        "MAX_ABS_PRICE_FOR_BRANDS": 150,
        "MIN_DISCOUNT_PERCENT": 30,
    }

    # Annuncio Subito in regalo: passa anche senza Prime e con prezzo 0
    subito_item = {
        "store": "subito",
        "title": "Tastiera meccanica regalo",
        "current_price": 0.0,
        "is_free": True,
        "is_prime": False,
        "is_amazon_fulfilled": False,
    }
    passes = _passes_basic_filters(subito_item, config)
    assert passes is True, "Subito item dovrebbe passare senza controllo Prime"

    alert_type = _classify_alert(subito_item, config)
    assert alert_type == "PRICE_ERROR", f"Atteso PRICE_ERROR, ottenuto {alert_type}"
    print("  [OK] Subito in regalo classificato come PRICE_ERROR")

    # Annuncio Subito semi-nuovo: classificato come SUPER_DEAL
    subito_deal = {
        "store": "subito",
        "title": "Samsung Galaxy S23 come nuovo",
        "current_price": 350.0,
        "is_free": False,
    }
    alert_type_deal = _classify_alert(subito_deal, config)
    assert alert_type_deal == "SUPER_DEAL"
    print("  [OK] Subito semi-nuovo classificato come SUPER_DEAL")

    # Annuncio con keyword blacklist scartato
    subito_spam = {
        "store": "subito",
        "title": "Cover custodia in silicone",
        "current_price": 0.0,
        "is_free": True,
    }
    assert _passes_basic_filters(subito_spam, config) is False
    print("  [OK] Annuncio con keyword in blacklist correttamente scartato")


async def test_subito_live_api():
    """Valida la chiamata reale all'API Hades di Subito.it."""
    print("\n=== Test 5: Chiamata Live ad API Hades Subito.it ===")
    config = {
        "CATEGORIES": [10],  # Informatica
        "SEARCH_QUERIES": ["notebook"],
    }
    scraper = SubitoScraper(config)
    await scraper.start()

    try:
        # Test 1: Ricerca annunci in regalo su cat 10
        free_items = await scraper.scrape_free_items()
        print(f"  [LIVE] Annunci in regalo trovati su cat 10: {len(free_items)}")
        assert isinstance(free_items, list)
        if free_items:
            first = free_items[0]
            print(f"  [LIVE] Esempio regalo: '{first['title'][:40]}...' | Prezzo: {first['current_price']}€ | Loc: {first['location']}")
            assert first["store"] == "subito"
            assert first["is_free"] is True
            assert first["condition"] == "In regalo"
            assert first["asin"].startswith("subito_")

        # Test 2: Ricerca annunci semi-nuovi su cat 10
        like_new = await scraper.scrape_like_new_deals()
        print(f"  [LIVE] Annunci semi-nuovi trovati su cat 10: {len(like_new)}")
        assert isinstance(like_new, list)
        if like_new:
            first = like_new[0]
            print(f"  [LIVE] Esempio semi-nuovo: '{first['title'][:40]}...' | Prezzo: {first['current_price']}€ | Ship: {first['shipping_allowed']}")
            assert first["shipping_allowed"] is True
            assert first["condition"] == "Come nuovo"

    finally:
        await scraper.stop()
        print("  [OK] SubitoScraper live test completato con successo!")


async def main_async():
    test_cart_promo_parsing()
    test_subito_unit_parsing()
    test_notifier_formatting()
    test_main_filters_and_classification()
    await test_subito_live_api()
    print("\n🎉 TUTTI I TEST SONO PASSATI CON SUCCESSO! 🎉")


if __name__ == "__main__":
    asyncio.run(main_async())
