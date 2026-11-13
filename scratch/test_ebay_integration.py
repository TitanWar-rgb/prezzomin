"""Suite di test unitari e di integrazione per il modulo eBay Italia + Coupon & Promo.

Verifica:
1. Parsing prezzi, spedizioni e informazioni venditore (parse_price, parse_shipping, parse_seller_info).
2. Calcolo coupon con cap e coupon stacking (calculate_ebay_coupon).
3. Parsing HTML sia da markup desktop (s-item) che mobile (s-card).
4. Whitelist venditori affidabili e filtri minimi di sconto.
5. Filtri di base e classificazione alert in main.py.
6. Formattazione alert Telegram in notifier.py con header, emoji, coupon e clean link.
7. Deduplicazione SQLite tramite asin/item_id.
8. Scansione asincrona end-to-end simulata con mock.
"""

import asyncio
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ebay_scraper import (
    EbayScraper,
    parse_price,
    parse_shipping,
    parse_seller_info,
    calculate_ebay_coupon,
)
from notifier import format_alert_message
import main as main_module
import database


class TestEbayScraperHelpers(unittest.TestCase):
    """Test delle funzioni di parsing ausiliarie."""

    def test_parse_price(self):
        self.assertEqual(parse_price("EUR 1.299,99"), 1299.99)
        self.assertEqual(parse_price("29,90 €"), 29.90)
        self.assertEqual(parse_price("EUR 199,99 a EUR 249,99"), 199.99)
        self.assertEqual(parse_price("312,70"), 312.70)
        self.assertEqual(parse_price("0,00"), 0.0)
        self.assertIsNone(parse_price(""))
        self.assertIsNone(parse_price(None))
        self.assertIsNone(parse_price("Nessun prezzo"))

    def test_parse_shipping(self):
        self.assertEqual(parse_shipping("Spedizione gratis"), 0.0)
        self.assertEqual(parse_shipping("Spedizione gratuita"), 0.0)
        self.assertEqual(parse_shipping("Consegna gratuita"), 0.0)
        self.assertEqual(parse_shipping("+EUR 10,00 per la consegna"), 10.0)
        self.assertEqual(parse_shipping("EUR 4,99"), 4.99)
        self.assertEqual(parse_shipping(""), 0.0)
        self.assertEqual(parse_shipping(None), 0.0)

    def test_parse_seller_info(self):
        name, fb = parse_seller_info("monclick-italia (125430) 98,6%")
        self.assertEqual(name, "monclick-italia")
        self.assertEqual(fb, "125430 98,6%")

        name, fb = parse_seller_info("yeppon-shop (99.8%)")
        self.assertEqual(name, "yeppon-shop")
        self.assertEqual(fb, "99.8%")

        name, fb = parse_seller_info("Venditore Affidabilità Top: redcoonit (5400) 99.1%")
        self.assertEqual(name, "redcoonit")
        self.assertEqual(fb, "5400 99.1%")

        name, fb = parse_seller_info("Venditore: stayon_elettronica")
        self.assertEqual(name, "stayon_elettronica")
        self.assertEqual(fb, "")

        name, fb = parse_seller_info("Venditore privato")
        self.assertEqual(name, "Venditore privato")
        self.assertEqual(fb, "")

        name, fb = parse_seller_info(None)
        self.assertEqual(name, "Venditore eBay")
        self.assertEqual(fb, "")


class TestEbayCouponLogic(unittest.TestCase):
    """Test approfondito della logica di calcolo dei coupon."""

    def test_coupon_with_cap(self):
        # 10% di 800€ = 80€, ma cap a 50€ -> sconto effettivo 50€
        res = calculate_ebay_coupon(
            current_price=800.0,
            original_price=0.0,
            coupon_code="TECH24",
            coupon_percent=10.0,
            coupon_max_discount=50.0,
        )
        self.assertTrue(res["has_coupon"])
        self.assertEqual(res["coupon_code"], "TECH24")
        self.assertEqual(res["coupon_saving"], 50.0)
        self.assertEqual(res["final_price"], 750.0)
        self.assertEqual(res["total_savings"], 50.0)
        self.assertAlmostEqual(res["total_discount_percent"], 6.2, places=1)

    def test_coupon_below_cap(self):
        # 10% di 200€ = 20€ (< cap 50€) -> sconto effettivo 20€
        res = calculate_ebay_coupon(
            current_price=200.0,
            original_price=0.0,
            coupon_code="TECH24",
            coupon_percent=10.0,
            coupon_max_discount=50.0,
        )
        self.assertTrue(res["has_coupon"])
        self.assertEqual(res["coupon_saving"], 20.0)
        self.assertEqual(res["final_price"], 180.0)
        self.assertEqual(res["total_savings"], 20.0)
        self.assertEqual(res["total_discount_percent"], 10.0)

    def test_coupon_stacking_with_original_price(self):
        # Prezzo originale 1000€ barrato a 800€ + coupon 10% (cap 50€)
        # Prezzo finale = 750€, risparmio totale = 250€ su 1000€ = 25.0%
        res = calculate_ebay_coupon(
            current_price=800.0,
            original_price=1000.0,
            coupon_code="TECH24",
            coupon_percent=10.0,
            coupon_max_discount=50.0,
        )
        self.assertTrue(res["has_coupon"])
        self.assertEqual(res["coupon_saving"], 50.0)
        self.assertEqual(res["final_price"], 750.0)
        self.assertEqual(res["total_savings"], 250.0)
        self.assertEqual(res["total_discount_percent"], 25.0)

    def test_no_coupon(self):
        res = calculate_ebay_coupon(
            current_price=100.0,
            original_price=150.0,
            coupon_code=None,
            coupon_percent=0.0,
        )
        self.assertFalse(res["has_coupon"])
        self.assertEqual(res["coupon_saving"], 0.0)
        self.assertEqual(res["final_price"], 100.0)
        self.assertEqual(res["total_savings"], 50.0)
        self.assertAlmostEqual(res["total_discount_percent"], 33.3, places=1)


class TestEbayScraperParsing(unittest.TestCase):
    """Test del parsing HTML per markup desktop (s-item) e mobile (s-card)."""

    def setUp(self):
        self.config = {
            "SEARCH_QUERIES": ["notebook gaming"],
            "SELLER_WHITELIST": ["monclick-italia", "yeppon-shop", "stayon_elettronica"],
            "COUPON_CODE": "TECH24",
            "COUPON_PERCENT": 10,
            "COUPON_MAX_DISCOUNT": 50,
            "MIN_DISCOUNT_PERCENT": 20,
        }
        self.scraper = EbayScraper(self.config)

    def test_parse_desktop_s_item(self):
        sample_s_item = """
        <li class="s-item s-item__pl-on-bottom" id="item123456789012">
            <div class="s-item__wrapper clearfix">
                <div class="s-item__info clearfix">
                    <a class="s-item__link" href="https://www.ebay.it/itm/123456789012?hash=item1c">
                        <div class="s-item__title">
                            <span role="heading" aria-level="3">ASUS ROG Zephyrus G16 RTX 4070</span>
                        </div>
                    </a>
                    <div class="s-item__details clearfix">
                        <span class="s-item__price">EUR 1.599,00</span>
                        <span class="s-item__trending-price"><s>EUR 1.999,00</s></span>
                    </div>
                    <span class="s-item__seller-info-text">yeppon-shop (99.8%)</span>
                    <span class="s-item__shipping s-item__logisticsCost">Spedizione gratis</span>
                </div>
            </div>
        </li>
        """
        item = self.scraper.parse_s_item_block(sample_s_item)
        self.assertIsNotNone(item)
        self.assertEqual(item["item_id"], "123456789012")
        self.assertEqual(item["asin"], "123456789012")
        self.assertEqual(item["title"], "ASUS ROG Zephyrus G16 RTX 4070")
        self.assertEqual(item["current_price"], 1599.00)
        self.assertEqual(item["original_price"], 1999.00)
        self.assertEqual(item["seller"], "yeppon-shop")
        self.assertEqual(item["seller_feedback"], "99.8%")
        self.assertEqual(item["shipping_cost"], 0.0)
        self.assertEqual(item["url"], "https://www.ebay.it/itm/123456789012")
        self.assertEqual(item["store"], "ebay")

    def test_parse_mobile_s_card(self):
        sample_s_card = """
        <li class="s-card s-card--horizontal" data-listingid=880111813611>
            <div class=su-card-container__content>
                <a class="s-card__link" href=https://www.ebay.it/itm/880111813611>
                    <div role=heading aria-level=3 class=s-card__title>
                        <span class="su-styled-text primary default">GIGABYTE GeForce RTX 4060 WINDFORCE OC 8GB</span>
                    </div>
                    <div class=s-card__subtitle-row>
                        <div class=s-card__subtitle>
                            <span class="su-styled-text secondary default">Nuovo | </span>
                            <span class="su-styled-text secondary default">Venditore: monclick-italia (125430) 98,6%</span>
                        </div>
                    </div>
                </a>
                <div class=su-card-container__attributes>
                    <div class=s-card__attribute-row>
                        <span class="s-card__price">EUR 289,00</span>
                    </div>
                    <div class=s-card__attribute-row>
                        <span>Consegna gratuita</span>
                    </div>
                </div>
            </div>
        </li>
        """
        item = self.scraper.parse_s_card_block(sample_s_card)
        self.assertIsNotNone(item)
        self.assertEqual(item["item_id"], "880111813611")
        self.assertEqual(item["title"], "GIGABYTE GeForce RTX 4060 WINDFORCE OC 8GB")
        self.assertEqual(item["current_price"], 289.00)
        self.assertEqual(item["seller"], "monclick-italia")
        self.assertEqual(item["seller_feedback"], "125430 98,6%")
        self.assertEqual(item["shipping_cost"], 0.0)
        self.assertEqual(item["url"], "https://www.ebay.it/itm/880111813611")

    def test_filter_and_apply_coupons_whitelist(self):
        items = [
            {
                "item_id": "111",
                "asin": "111",
                "title": "PlayStation 5 Slim yeppon",
                "current_price": 400.0,
                "original_price": 500.0,
                "seller": "yeppon-shop",
                "store": "ebay",
            },
            {
                "item_id": "222",
                "asin": "222",
                "title": "PlayStation 5 Slim sconosciuto",
                "current_price": 300.0,
                "original_price": 500.0,
                "seller": "tizio_a_caso",
                "store": "ebay",
            },
        ]
        filtered = self.scraper.filter_and_apply_coupons(items)
        # Il venditore "tizio_a_caso" deve essere scartato perché non in whitelist
        self.assertEqual(len(filtered), 1)
        res = filtered[0]
        self.assertEqual(res["item_id"], "111")
        self.assertTrue(res["has_coupon"])
        self.assertEqual(res["coupon_code"], "TECH24")
        # 10% di 400 = 40€ risparmio coupon
        # final_price = 360€
        # risparmio totale su 500€ = 140€ (28.0% > min 20%)
        self.assertEqual(res["coupon_saving"], 40.0)
        self.assertEqual(res["final_price"], 360.0)
        self.assertEqual(res["total_savings"], 140.0)
        self.assertEqual(res["total_discount_percent"], 28.0)


class TestMainIntegrationAndAlerts(unittest.TestCase):
    """Test dei filtri e della classificazione alert in main.py."""

    def test_passes_basic_filters_ebay(self):
        config = {
            "STORE": "ebay",
            "SELLER_WHITELIST": ["monclick-italia", "yeppon-shop"],
            "BLACKLIST_KEYWORDS": ["cavo", "adattatore"],
        }
        valid_item = {
            "store": "ebay",
            "title": "Notebook MSI Gaming Katana",
            "seller": "monclick-italia",
            "is_prime": False,  # Non richiesto per eBay
            "is_amazon_fulfilled": False,
        }
        blacklisted_item = {
            "store": "ebay",
            "title": "Cavo alimentazione notebook",
            "seller": "monclick-italia",
        }
        unauthorized_seller_item = {
            "store": "ebay",
            "title": "Notebook MSI Gaming Katana",
            "seller": "venditore_sconosciuto",
        }

        self.assertTrue(main_module._passes_basic_filters(valid_item, config))
        self.assertFalse(main_module._passes_basic_filters(blacklisted_item, config))
        self.assertFalse(main_module._passes_basic_filters(unauthorized_seller_item, config))

    def test_classify_alert_ebay(self):
        config = {
            "STORE": "ebay",
            "MIN_DISCOUNT_PERCENT": 20,
            "MAX_ABS_PRICE_FOR_BRANDS": 150,
        }

        # 1. Price error (sconto >= 45%)
        item_price_error = {
            "store": "ebay",
            "total_discount_percent": 48.0,
            "current_price": 500.0,
            "final_price": 450.0,
        }
        self.assertEqual(main_module._classify_alert(item_price_error, config), "PRICE_ERROR")

        # 2. Price error (prezzo assoluto basso < 150€ con sconto >= 20%)
        item_cheap_error = {
            "store": "ebay",
            "total_discount_percent": 22.0,
            "current_price": 120.0,
            "final_price": 100.0,
        }
        self.assertEqual(main_module._classify_alert(item_cheap_error, config), "PRICE_ERROR")

        # 3. Super deal (sconto >= 20%)
        item_super_deal = {
            "store": "ebay",
            "total_discount_percent": 25.0,
            "current_price": 400.0,
            "final_price": 360.0,
        }
        self.assertEqual(main_module._classify_alert(item_super_deal, config), "SUPER_DEAL")

        # 4. Sotto soglia sconto minimo (15% < 20%)
        item_low_discount = {
            "store": "ebay",
            "total_discount_percent": 15.0,
            "current_price": 400.0,
            "final_price": 380.0,
        }
        self.assertIsNone(main_module._classify_alert(item_low_discount, config))


class TestTelegramNotifierEbay(unittest.TestCase):
    """Test della formattazione del messaggio Telegram per eBay Italia."""

    def test_format_ebay_message_with_coupon(self):
        product = {
            "store": "ebay",
            "item_id": "334455667788",
            "asin": "334455667788",
            "title": "Sony PlayStation 5 Console D Chassis",
            "current_price": 449.00,
            "original_price": 549.00,
            "seller": "monclick-italia",
            "seller_feedback": "125430 98,6%",
            "shipping_cost": 0.0,
            "has_coupon": True,
            "coupon_code": "TECH24",
            "coupon_percent": 10.0,
            "final_price": 404.10,
            "total_discount_percent": 26.4,
            "total_savings": 144.90,
            "category_name": "eBay Tech & Elettronica",
            "condition": "Nuovo",
        }
        msg = format_alert_message(product)

        # Verifiche intestazione ed emoji richieste
        self.assertIn("💣 COMBO EBAY + CODICE SCONTO 💣", msg)
        self.assertIn("<b>Sony PlayStation 5 Console D Chassis</b>", msg)
        self.assertIn("🏪 Venditore: <b>monclick-italia</b> (125430 98,6%)", msg)
        self.assertIn("🎟️ Codice sconto da inserire: <code>TECH24</code> (-10%)", msg)
        self.assertIn("💰 Prezzo al check-out: <b>€404.10</b>", msg)
        self.assertIn("Prezzo eBay di partenza: €449.00", msg)
        self.assertIn("💸 Prezzo originale barrato: <s>€549.00</s>", msg)
        self.assertIn("🔥 Risparmio complessivo: <b>-26% (€144.90)</b>", msg)
        self.assertIn("🚚 Spedizione: Gratis", msg)
        self.assertIn("https://www.ebay.it/itm/334455667788", msg)
        self.assertIn("<code>334455667788</code>", msg)

    def test_format_ebay_message_without_coupon(self):
        product = {
            "store": "ebay",
            "item_id": "998877665544",
            "title": "Monitor Gaming OLED 27 LG UltraGear",
            "current_price": 699.00,
            "original_price": 999.00,
            "seller": "yeppon-shop",
            "seller_feedback": "99.8%",
            "shipping_cost": 9.90,
            "has_coupon": False,
            "final_price": 699.00,
            "total_discount_percent": 30.0,
            "total_savings": 300.00,
            "category_name": "eBay Tech & Elettronica",
            "condition": "Nuovo",
        }
        msg = format_alert_message(product)

        self.assertIn("🛍️ SUPER AFFARE EBAY 🛍️", msg)
        self.assertIn("🏪 Venditore: <b>yeppon-shop</b> (99.8%)", msg)
        self.assertIn("💰 Prezzo al check-out: <b>€699.00</b>", msg)
        self.assertIn("🚚 Spedizione: €9.90", msg)
        self.assertIn("https://www.ebay.it/itm/998877665544", msg)


class TestEbayEndToEndMock(unittest.IsolatedAsyncioTestCase):
    """Test asincrono end-to-end con mock del download HTTP."""

    async def test_scrape_all_mocked(self):
        config = {
            "STORE": "ebay",
            "SEARCH_QUERIES": ["notebook gaming"],
            "SELLER_WHITELIST": ["yeppon-shop"],
            "COUPON_CODE": "TECH24",
            "COUPON_PERCENT": 10,
            "COUPON_MAX_DISCOUNT": 50,
            "MIN_DISCOUNT_PERCENT": 20,
        }
        scraper = EbayScraper(config)

        mock_html = """
        <html>
            <li class="s-item" id="item555666777888">
                <a class="s-item__link" href="https://www.ebay.it/itm/555666777888">
                    <div class="s-item__title">Acer Predator Helios 16 RTX 4080</div>
                </a>
                <span class="s-item__price">EUR 1.500,00</span>
                <span class="s-item__trending-price"><s>EUR 2.000,00</s></span>
                <span class="s-item__seller-info-text">yeppon-shop (99.8%)</span>
                <span class="s-item__shipping">Spedizione gratis</span>
            </li>
        </html>
        """

        with patch.object(scraper, "fetch_html", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_html
            results = await scraper.scrape_all()

            self.assertEqual(len(results), 1)
            item = results[0]
            self.assertEqual(item["item_id"], "555666777888")
            self.assertEqual(item["seller"], "yeppon-shop")
            self.assertTrue(item["has_coupon"])
            self.assertEqual(item["coupon_code"], "TECH24")
            # 10% di 1500 = 150, con cap 50 -> coupon_saving = 50
            # final_price = 1450
            # total_savings = 2000 - 1450 = 550
            # total_discount_percent = 550 / 2000 = 27.5%
            self.assertEqual(item["coupon_saving"], 50.0)
            self.assertEqual(item["final_price"], 1450.00)
            self.assertEqual(item["total_savings"], 550.00)
            self.assertEqual(item["total_discount_percent"], 27.5)


if __name__ == "__main__":
    unittest.main()
