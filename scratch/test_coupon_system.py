"""Suite di test per il Radar Coupon & Doppi Sconti (Coupon Stacking).

Verifica:
1. Parsing coupon percentuali e fissi da testi Amazon reali.
2. Calcolo metriche checkout, risparmio totale e percentuale combo.
3. Classificazione alert PRICE_ERROR e SUPER_DEAL in main.py.
4. Generazione corretta dei messaggi Telegram con coupon stacking.
5. Estrazione mock da card scraper.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Aggiungi root del progetto al sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scraper import AmazonScraper, calculate_coupon_metrics, parse_coupon
from main import _classify_alert
from notifier import format_alert_message


class TestCouponParser(unittest.TestCase):
    """Test estrazione e riconoscimento coupon percentuali e fissi."""

    def test_percentage_coupons(self):
        cases = [
            ("Applica coupon 20%", 100.0, "-20%", 20.0),
            ("Coupon 30%", 50.0, "-30%", 15.0),
            ("Coupon: 50%", 200.0, "-50%", 100.0),
            ("Risparmia 15% al check-out", 80.0, "-15%", 12.0),
            ("Risparmia 10 % con coupon", 60.0, "-10%", 6.0),
            ("Applica coupon di 25%", 40.0, "-25%", 10.0),
        ]
        for text, current_price, expected_label, expected_discount in cases:
            with self.subTest(text=text):
                res = parse_coupon(text, current_price)
                self.assertIsNotNone(res, f"Fallito parsing di: '{text}'")
                label, discount = res
                self.assertEqual(label, expected_label)
                self.assertEqual(round(discount, 2), expected_discount)

    def test_fixed_euro_coupons(self):
        cases = [
            ("Risparmia 15 € al check-out", 50.0, "-15.00€", 15.0),
            ("Coupon di 10,00€", 29.99, "-10.00€", 10.0),
            ("Coupon di 10,00 €", 60.0, "-10.00€", 10.0),
            ("Coupon 10€", 40.0, "-10.00€", 10.0),
            ("Applica coupon di 5€", 25.0, "-5.00€", 5.0),
            ("Risparmia € 20 al check-out", 120.0, "-20.00€", 20.0),
            ("Risparmia 50,00 € al check-out", 250.0, "-50.00€", 50.0),
        ]
        for text, current_price, expected_label, expected_discount in cases:
            with self.subTest(text=text):
                res = parse_coupon(text, current_price)
                self.assertIsNotNone(res, f"Fallito parsing di: '{text}'")
                label, discount = res
                self.assertEqual(label, expected_label)
                self.assertEqual(round(discount, 2), expected_discount)

    def test_coupon_context_element(self):
        """Elementi specifici .s-coupon possono contenere solo '20%' o '15,00 €'."""
        res_pct = parse_coupon("20%", 100.0, is_coupon_context=True)
        self.assertEqual(res_pct, ("-20%", 20.0))

        res_fixed = parse_coupon("15,00 €", 50.0, is_coupon_context=True)
        self.assertEqual(res_fixed, ("-15.00€", 15.0))

        # Senza contesto coupon, '20%' da solo non deve essere scambiato per un coupon
        res_none = parse_coupon("20%", 100.0, is_coupon_context=False)
        self.assertIsNone(res_none)

    def test_ignored_or_invalid_cases(self):
        self.assertIsNone(parse_coupon("Iscriviti e risparmia: 5%", 50.0))
        self.assertIsNone(parse_coupon("Nessun coupon presente", 45.0))
        self.assertIsNone(parse_coupon("Consegna gratuita", 30.0))
        self.assertIsNone(parse_coupon("", 50.0))
        self.assertIsNone(parse_coupon("Coupon 20%", 0.0))


class TestCouponMetrics(unittest.TestCase):
    """Test calcolo metriche checkout, total discount e savings."""

    def test_combo_discount_with_original_price(self):
        # Listino 100€, Prezzo Amazon 80€ (sconto 20%), Coupon 20% (-16€)
        # Prezzo finale = 64€, Risparmio totale = 36€, Sconto complessivo = 36%
        metrics = calculate_coupon_metrics(
            current_price=80.0,
            original_price=100.0,
            coupon_label="-20%",
            coupon_discount=16.0,
            base_discount_percent=20.0,
        )
        self.assertTrue(metrics["has_coupon"])
        self.assertEqual(metrics["coupon_label"], "-20%")
        self.assertEqual(metrics["coupon_discount"], 16.0)
        self.assertEqual(metrics["final_price"], 64.0)
        self.assertEqual(metrics["total_discount_percent"], 36.0)
        self.assertEqual(metrics["total_savings"], 36.0)

    def test_super_combo_discount(self):
        # Listino 100€, Prezzo Amazon 60€, Coupon 20% (-12€)
        # Prezzo finale = 48€, Risparmio totale = 52€, Sconto complessivo = 52%
        metrics = calculate_coupon_metrics(
            current_price=60.0,
            original_price=100.0,
            coupon_label="-20%",
            coupon_discount=12.0,
            base_discount_percent=40.0,
        )
        self.assertTrue(metrics["has_coupon"])
        self.assertEqual(metrics["final_price"], 48.0)
        self.assertEqual(metrics["total_discount_percent"], 52.0)
        self.assertEqual(metrics["total_savings"], 52.0)

    def test_fixed_coupon_without_original_price(self):
        # Prezzo Amazon 50€, nessun prezzo barrato (original_price=0), Coupon 15€ fisso
        # Prezzo finale = 35€, Risparmio = 15€, Sconto = 30.0%
        metrics = calculate_coupon_metrics(
            current_price=50.0,
            original_price=0.0,
            coupon_label="-15.00€",
            coupon_discount=15.0,
            base_discount_percent=0.0,
        )
        self.assertTrue(metrics["has_coupon"])
        self.assertEqual(metrics["coupon_label"], "-15.00€")
        self.assertEqual(metrics["final_price"], 35.0)
        self.assertEqual(metrics["total_discount_percent"], 30.0)
        self.assertEqual(metrics["total_savings"], 15.0)

    def test_no_coupon_fallback(self):
        # Prodotto normale senza coupon
        metrics = calculate_coupon_metrics(
            current_price=80.0,
            original_price=100.0,
            coupon_label=None,
            coupon_discount=0.0,
            base_discount_percent=20.0,
        )
        self.assertFalse(metrics["has_coupon"])
        self.assertIsNone(metrics["coupon_label"])
        self.assertEqual(metrics["coupon_discount"], 0.0)
        self.assertEqual(metrics["final_price"], 80.0)
        self.assertEqual(metrics["total_discount_percent"], 20.0)
        self.assertEqual(metrics["total_savings"], 20.0)


class TestAlertClassification(unittest.TestCase):
    """Test classificazione alert con coupon in main._classify_alert."""

    def setUp(self):
        self.config = {
            "MAX_ABS_PRICE_FOR_BRANDS": 150,
            "MIN_DISCOUNT_PERCENT": 45,
        }

    def test_price_error_total_discount_over_45(self):
        # Sconto complessivo >= 45% -> PRICE_ERROR
        product = {
            "has_coupon": True,
            "current_price": 60.0,
            "final_price": 48.0,
            "total_discount_percent": 52.0,
            "discount_percent": 40.0,
        }
        level = _classify_alert(product, self.config)
        self.assertEqual(level, "PRICE_ERROR")

    def test_price_error_low_absolute_price_with_coupon(self):
        # final_price < 150 e total_discount_percent >= 20% -> PRICE_ERROR
        product = {
            "has_coupon": True,
            "current_price": 100.0,
            "final_price": 80.0,  # < 150
            "total_discount_percent": 20.0,
            "discount_percent": 0.0,
        }
        level = _classify_alert(product, self.config)
        self.assertEqual(level, "PRICE_ERROR")

    def test_super_deal_total_discount_over_25(self):
        # final_price >= 150 (es. 200€) ma sconto complessivo >= 25% -> SUPER_DEAL
        product = {
            "has_coupon": True,
            "current_price": 250.0,
            "final_price": 187.50,  # >= 150
            "total_discount_percent": 25.0,
            "discount_percent": 10.0,
        }
        level = _classify_alert(product, self.config)
        self.assertEqual(level, "SUPER_DEAL")

    def test_no_alert_low_discount_coupon(self):
        # final_price >= 150 e sconto complessivo < 25% -> None
        product = {
            "has_coupon": True,
            "current_price": 300.0,
            "final_price": 270.0,  # >= 150
            "total_discount_percent": 10.0,
            "discount_percent": 0.0,
        }
        level = _classify_alert(product, self.config)
        self.assertIsNone(level)


class TestTelegramMessageFormatting(unittest.TestCase):
    """Test formattazione alert Telegram con coupon in notifier.py."""

    def test_super_combo_header_and_body(self):
        # Sconto complessivo >= 50% con coupon
        product = {
            "alert_level": "PRICE_ERROR",
            "asin": "B0TESTCOMBO",
            "title": "Logitech G Pro X Superlight Gaming Mouse",
            "current_price": 60.0,
            "original_price": 100.0,
            "discount_percent": 40.0,
            "is_prime": True,
            "is_amazon_fulfilled": True,
            "condition": "Nuovo",
            "has_coupon": True,
            "coupon_label": "-20%",
            "coupon_discount": 12.0,
            "final_price": 48.0,
            "total_discount_percent": 52.0,
            "total_savings": 52.0,
        }
        msg = format_alert_message(product)

        # Verifica header Super Combo
        self.assertIn("💣 SUPER COMBO SCONTO + COUPON 💣", msg)
        # Verifica riga coupon e istruzioni spunta verde
        self.assertIn("🎟️ Coupon: <b>-20%</b> (spunta la casella verde prima di acquistare!)", msg)
        # Verifica prezzo al check-out
        self.assertIn("💰 Prezzo al check-out: <b>€48.00</b>", msg)
        # Verifica prezzo di partenza Amazon
        self.assertIn("Prezzo Amazon di partenza: €60.00", msg)
        # Verifica prezzo originale barrato
        self.assertIn("Prezzo originale barrato: <s>€100.00</s>", msg)
        # Verifica risparmio complessivo
        self.assertIn("🔥 Risparmio complessivo: <b>-52% (€52.00)</b>", msg)

    def test_fixed_coupon_message(self):
        # Coupon fisso senza prezzo originale barrato
        product = {
            "alert_level": "PRICE_ERROR",
            "asin": "B0TESTFIXED",
            "title": "Asus TUF Gaming Headset",
            "current_price": 50.0,
            "original_price": 0.0,
            "discount_percent": 0.0,
            "is_prime": True,
            "is_amazon_fulfilled": True,
            "condition": "Nuovo",
            "has_coupon": True,
            "coupon_label": "-15.00€",
            "coupon_discount": 15.0,
            "final_price": 35.0,
            "total_discount_percent": 30.0,
            "total_savings": 15.0,
        }
        msg = format_alert_message(product)

        # Header per sconto < 50%
        self.assertIn("🚨 ERRORE DI PREZZO PROBABILE 🚨", msg)
        self.assertIn("🎟️ Coupon: <b>-15.00€</b> (spunta la casella verde prima di acquistare!)", msg)
        self.assertIn("💰 Prezzo al check-out: <b>€35.00</b>", msg)
        self.assertIn("Prezzo Amazon di partenza: €50.00", msg)
        self.assertNotIn("Prezzo originale barrato", msg)
        self.assertIn("🔥 Risparmio complessivo: <b>-30% (€15.00)</b>", msg)

    def test_standard_product_without_coupon(self):
        product = {
            "alert_level": "SUPER_DEAL",
            "asin": "B0NORMAL01",
            "title": "Samsung SSD 980 Pro 1TB",
            "current_price": 75.0,
            "original_price": 100.0,
            "discount_percent": 25.0,
            "is_prime": True,
            "is_amazon_fulfilled": True,
            "condition": "Nuovo",
            "has_coupon": False,
        }
        msg = format_alert_message(product)

        self.assertIn("🔥 SUPER AFFARE VERIFICATO 🔥", msg)
        self.assertIn("💰 Prezzo Amazon: <b>€75.00</b>", msg)
        self.assertNotIn("Coupon:", msg)
        self.assertNotIn("Prezzo al check-out:", msg)


class TestScraperExtraction(unittest.IsolatedAsyncioTestCase):
    """Test estrazione asincrona del coupon da mock card di Playwright."""

    async def test_extract_coupon_from_element(self):
        scraper = AmazonScraper({})
        card = AsyncMock()

        coupon_el = AsyncMock()
        coupon_el.inner_text = AsyncMock(return_value="Applica coupon 20%")

        # Simula query_selector che trova l'elemento .s-coupon-unclipped
        async def fake_query_selector(sel):
            if ".s-coupon-unclipped" in sel:
                return coupon_el
            return None

        card.query_selector = AsyncMock(side_effect=fake_query_selector)

        data = await scraper._extract_coupon(
            card=card,
            current_price=100.0,
            original_price=120.0,
            discount_percent=16.7,
        )

        self.assertTrue(data["has_coupon"])
        self.assertEqual(data["coupon_label"], "-20%")
        self.assertEqual(data["coupon_discount"], 20.0)
        self.assertEqual(data["final_price"], 80.0)
        self.assertEqual(data["total_discount_percent"], 33.3)
        self.assertEqual(data["total_savings"], 40.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
