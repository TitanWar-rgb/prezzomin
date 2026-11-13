"""PrezzoMin — Entry point del monitoraggio errori di prezzo Amazon Italia.

Coordina scraper, validatore prezzi Idealo, filtri, notifiche Telegram
e persistenza database in un loop infinito con intervalli casuali.

v2: classificazione alert PRICE_ERROR / SUPER_DEAL, validazione prezzi
tramite Idealo.it, supporto Amazon Warehouse.
"""

import asyncio
import json
import logging
import os
import random
import signal
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from database import init_db, is_recently_notified, mark_notified, cleanup_old_entries
from ebay_scraper import EbayScraper
from notifier import send_telegram_alert, send_startup_message
from price_validator import PriceValidator
from scraper import AmazonScraper
from subito_scraper import SubitoScraper

CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_PATH = Path(__file__).parent / "prezzomin.log"


def _setup_logging() -> None:
    """Configura il logging su console e file."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(fmt))
    root.addHandler(console)

    # File
    file_handler = logging.FileHandler(str(LOG_PATH), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(file_handler)


def _load_config() -> dict[str, Any]:
    """Carica e valida il file di configurazione JSON.

    Usa config.json come base e, se è specificata la variabile CATEGORY,
    applica le impostazioni specializzate da categories/<category>.json,
    isolando lo storico in db/worker_<category>.db per evitare conflitti.
    """
    if not CONFIG_PATH.exists():
        logging.error("File di configurazione non trovato: %s", CONFIG_PATH)
        sys.exit(1)

    with open(CONFIG_PATH, encoding="utf-8") as fh:
        config = json.load(fh)

    category = os.environ.get("CATEGORY", "").strip().lower()
    if category:
        cat_file = Path(__file__).parent / "categories" / f"{category}.json"
        if cat_file.exists():
            with open(cat_file, encoding="utf-8") as fh:
                cat_config = json.load(fh)
            config.update(cat_config)
            logging.info("Caricamento configurazione categoria '%s': %s", category, cat_file)
            os.environ.setdefault("DB_NAME", f"db/worker_{category}.db")
        else:
            logging.warning("File categoria '%s' non trovato in categories/, uso config.json", category)

    # Sovrascrivi con variabili d'ambiente (es. Secrets di GitHub Actions) se presenti
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if env_token:
        config["TELEGRAM_BOT_TOKEN"] = env_token
    if env_chat:
        config["TELEGRAM_CHAT_ID"] = env_chat

    # Supporto categorie e query eBay
    store = config.get("STORE", "").lower()
    if store == "ebay" or category.startswith("ebay"):
        if not config.get("SEARCH_URLS") and config.get("SEARCH_QUERIES"):
            config["SEARCH_URLS"] = [
                f"https://www.ebay.it/sch/i.html?_nkw={quote_plus(q)}&LH_BIN=1&_sop=15"
                for q in config.get("SEARCH_QUERIES", [])
            ]

    # Validazione minima
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    is_subito = (
        store == "subito"
        or category.startswith("subito")
        or config.get("CATEGORY_NAME", "").lower().startswith("subito")
    )
    if not is_subito and not (config.get("SEARCH_URLS") or config.get("SEARCH_QUERIES")):
        logging.error("Nessun SEARCH_URLS o SEARCH_QUERIES specificato nella configurazione!")
        sys.exit(1)

    for key in required:
        if not config.get(key):
            logging.error("Chiave di configurazione mancante o vuota: %s", key)
            sys.exit(1)

    return config


def _matches_brand(title: str, whitelist: list[str]) -> bool:
    """Verifica se il titolo contiene almeno un brand nella whitelist."""
    title_lower = title.lower()
    return any(brand.lower() in title_lower for brand in whitelist)


def _matches_blacklist(title: str, blacklist: list[str]) -> bool:
    """Verifica se il titolo contiene una keyword in blacklist."""
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in blacklist)


def _passes_basic_filters(product: dict[str, Any], config: dict[str, Any]) -> bool:
    """Applica i filtri di base: brand, blacklist, Prime/fulfilled (Amazon) o seller whitelist (eBay).

    Args:
        product: Dizionario prodotto dallo scraper.
        config: Configurazione globale.

    Returns:
        True se il prodotto supera i filtri di base.
    """
    title = product.get("title", "")
    whitelist = config.get("BRAND_WHITELIST", [])
    blacklist = config.get("BLACKLIST_KEYWORDS", [])

    # Filtri specifici per prodotti Subito.it (disabilita Prime, consente prezzo 0 in regalo)
    if product.get("store") == "subito":
        if blacklist and _matches_blacklist(title, blacklist):
            return False
        is_free = product.get("is_free") or product.get("current_price", 0.0) == 0
        if not is_free and whitelist and not _matches_brand(title, whitelist):
            return False
        return True

    # Filtri specifici per prodotti eBay
    if product.get("store") == "ebay":
        seller_whitelist = config.get("SELLER_WHITELIST", [])
        if seller_whitelist:
            seller = product.get("seller", "").lower()
            if not any(w.lower() in seller for w in seller_whitelist):
                return False

        if blacklist and _matches_blacklist(title, blacklist):
            return False

        if whitelist and not _matches_brand(title, whitelist):
            return False

        return True

    # Filtri Amazon standard
    if not _matches_brand(title, whitelist):
        return False

    if _matches_blacklist(title, blacklist):
        return False

    if not (product.get("is_prime") or product.get("is_amazon_fulfilled")):
        return False

    return True


def _classify_alert(product: dict[str, Any], config: dict[str, Any]) -> str | None:
    """Classify the alert level for a product.

    Returns:
        "PRICE_ERROR" — price is >=45% below Idealo best, branded product under MAX_ABS_PRICE, free item, or high coupon combo
        "SUPER_DEAL" — price is >=25% below Idealo best, or >=25% Amazon discount, or like-new Subito deal
        None — does not qualify for any alert
    """
    # Oggetti in regalo o con prezzo 0 (es. Subito regali tech) -> sempre PRICE_ERROR
    if product.get("is_free") or product.get("current_price", 0.0) == 0:
        return "PRICE_ERROR"

    # Classificazione specifica per Subito.it (annunci tech semi-nuovi con spedizione)
    if product.get("store") == "subito":
        return "SUPER_DEAL"

    # Classificazione specifica per eBay Italia
    if product.get("store") == "ebay":
        total_discount = product.get("total_discount_percent", product.get("discount_percent", 0.0))
        final_price = product.get("final_price", product.get("current_price", 0.0))
        max_price = config.get("MAX_ABS_PRICE_FOR_BRANDS", 150)
        min_discount = config.get("MIN_DISCOUNT_PERCENT", 20)

        if total_discount >= 45 or (final_price < max_price and total_discount >= min_discount):
            return "PRICE_ERROR"
        if total_discount >= min_discount:
            return "SUPER_DEAL"
        return None

    # Valutazione convenienza Coupon & Doppi Sconti (Coupon Stacking)
    if product.get("has_coupon"):
        final_price = product.get("final_price", product.get("current_price", 0.0))
        total_discount = product.get("total_discount_percent", product.get("discount_percent", 0.0))
        max_price = config.get("MAX_ABS_PRICE_FOR_BRANDS", 150)

        if total_discount >= 45 or (final_price < max_price and total_discount >= 20):
            return "PRICE_ERROR"
        if total_discount >= 25:
            return "SUPER_DEAL"
        return None

    price_validated = product.get("price_validated", False)
    real_discount = product.get("real_discount_percent", 0.0)
    discount = product.get("discount_percent", 0.0)
    current_price = product.get("current_price", 0.0)
    is_warehouse = product.get("is_warehouse", False)
    max_price = config.get("MAX_ABS_PRICE_FOR_BRANDS", 150)
    min_discount = config.get("MIN_DISCOUNT_PERCENT", 45)

    # Regola tassativa: se discount <= 0 e nessuno sconto reale da Idealo, scarta immediatamente
    if discount <= 0 and (not price_validated or real_discount <= 0):
        return None

    # Override: branded product sotto la soglia di prezzo assoluto (es. laptop a meno di 150€)
    # Scatta SOLO se c'è uno sconto effettivo (discount >= 15 o real_discount >= 20)
    has_effective_discount = discount >= 15 or (price_validated and real_discount >= 20)
    if current_price > 0 and current_price < max_price and has_effective_discount:
        return "PRICE_ERROR"

    if price_validated:
        # Validazione Idealo disponibile
        if real_discount >= 45:
            return "PRICE_ERROR"
        if real_discount >= 25:
            return "SUPER_DEAL"
        return None
    else:
        # Fallback allo sconto interno Amazon
        if is_warehouse:
            if discount >= config.get("MIN_DISCOUNT_PERCENT_WAREHOUSE", 35):
                return "PRICE_ERROR"
            if discount >= 20:
                return "SUPER_DEAL"
        else:
            if discount >= min_discount:
                return "PRICE_ERROR"
            if discount >= 25:
                return "SUPER_DEAL"
        return None


async def _main_loop(config: dict[str, Any]) -> None:
    """Loop principale di monitoraggio.

    Args:
        config: Configurazione globale.
    """
    logger = logging.getLogger("prezzomin")
    bot_token = config["TELEGRAM_BOT_TOKEN"]
    chat_id = config["TELEGRAM_CHAT_ID"]
    cooldown_hours = config.get("NOTIFICATION_COOLDOWN_HOURS", 24)
    interval_min = config.get("SCAN_INTERVAL_MIN", 120)
    interval_max = config.get("SCAN_INTERVAL_MAX", 300)

    store = config.get("STORE", "").lower()
    category = os.environ.get("CATEGORY", "").strip().lower()
    is_subito = (
        store == "subito"
        or category.startswith("subito")
        or config.get("CATEGORY_NAME", "").lower().startswith("subito")
    )
    is_ebay = (
        store == "ebay"
        or category.startswith("ebay")
        or config.get("CATEGORY_NAME", "").lower().startswith("ebay")
    )

    if is_subito:
        logger.info("Modulo store: Subito.it (SubitoScraper)")
        scraper = SubitoScraper(config)
    elif is_ebay:
        logger.info("Modulo store: eBay Italia (EbayScraper)")
        scraper = EbayScraper(config)
    else:
        logger.info("Modulo store: Amazon Italia (AmazonScraper)")
        scraper = AmazonScraper(config)

    await scraper.start()

    # Crea il PriceValidator solo se è attivo un context browser
    context = scraper.get_context()
    validator = PriceValidator(context) if context else None

    # Flag per lo shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: int, _frame: Any) -> None:
        logger.info("Segnale %s ricevuto — arresto in corso…", signal.Signals(sig).name)
        shutdown_event.set()

    # Registra handler per SIGINT e SIGTERM (SIGTERM non disponibile su Windows in tutti i casi)
    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (OSError, AttributeError):
        pass  # SIGTERM non supportato su Windows con asyncio

    try:
        # Supporto per sessioni a durata controllata (es. su GitHub Actions)
        max_minutes = float(os.environ.get("RUN_MINUTES", 0))
        max_cycles = int(os.environ.get("RUN_CYCLES", 0))
        loop_start_time = asyncio.get_running_loop().time()
        is_scheduled = max_minutes > 0 or max_cycles > 0
        cycle_count = 0

        # Invia messaggio di avvio solo se non siamo su un runner schedulato
        if not is_scheduled or os.environ.get("SEND_STARTUP_MSG") == "true":
            send_startup_message(bot_token, chat_id)

        while not shutdown_event.is_set():
            cycle_count += 1
            try:
                logger.info("=" * 50)
                logger.info("Avvio scansione…")

                products = await scraper.scrape_all()
                logger.info("Prodotti trovati: %d", len(products))

                # Aggiorna il context del validator se è stato ruotato
                new_ctx = scraper.get_context()
                if validator and new_ctx:
                    validator._context = new_ctx
                elif new_ctx and not validator:
                    validator = PriceValidator(new_ctx)

                alerts_sent = 0
                for product in products:
                    product["category_name"] = config.get("CATEGORY_NAME", "Generale")
                    # Filtri di base (brand, blacklist, Prime)
                    if not _passes_basic_filters(product, config):
                        continue

                    asin = product.get("asin", "")
                    if is_recently_notified(asin, hours=cooldown_hours):
                        logger.debug("ASIN %s già notificato — skip", asin)
                        continue

                    # Validazione prezzo Idealo (per Amazon)
                    if validator and product.get("store") not in ("ebay", "subito"):
                        try:
                            product = await validator.validate_price(product)
                        except Exception as exc:
                            logger.warning(
                                "Errore validazione Idealo per %s: %s", asin, exc
                            )

                    # Classificazione alert
                    alert_level = _classify_alert(product, config)
                    if alert_level is None:
                        continue

                    product["alert_level"] = alert_level

                    # Invia alert e registra nel DB
                    success = send_telegram_alert(bot_token, chat_id, product)
                    if success:
                        mark_notified(
                            asin=asin,
                            title=product.get("title", ""),
                            price=product.get("final_price", product.get("current_price", 0.0)),
                            original_price=product.get("original_price", 0.0),
                            discount_percent=product.get("total_discount_percent", product.get("discount_percent", 0.0)),
                        )
                        alerts_sent += 1

                logger.info("Alert inviati in questa scansione: %d", alerts_sent)

                # Pulizia record vecchi
                cleanup_old_entries(hours=72)

            except Exception as exc:
                logger.exception("Errore durante la scansione: %s", exc)
                # Attendi un minuto prima di riprovare in caso di errore grave
                await asyncio.sleep(60)
                continue

            # Controllo limite di tempo (minuti) o cicli (per GitHub Actions)
            if max_minutes > 0:
                elapsed_min = (asyncio.get_running_loop().time() - loop_start_time) / 60
                if elapsed_min >= max_minutes:
                    logger.info(
                        "Raggiunta la durata massima della sessione (%.1f / %.1f min) — Uscita pulita per salvataggio.",
                        elapsed_min, max_minutes,
                    )
                    break
            elif max_cycles > 0 and cycle_count >= max_cycles:
                logger.info(
                    "Raggiunto il limite di cicli impostato (%d/%d) — Uscita pulita per GitHub Actions.",
                    cycle_count, max_cycles,
                )
                break

            # Attesa tra le scansioni (su GitHub Actions usa un intervallo di 90-140s)
            if is_scheduled:
                delay = random.randint(90, 140)
            else:
                delay = random.randint(interval_min, interval_max)

            logger.info("Prossima scansione tra %d secondi", delay)

            # Attendi con possibilità di interruzione
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass  # Il timeout è il comportamento atteso

    finally:
        logger.info("Chiusura browser…")
        await scraper.stop()
        logger.info("PrezzoMin arrestato.")


def main() -> None:
    """Entry point del programma."""
    _setup_logging()
    logger = logging.getLogger("prezzomin")
    logger.info("PrezzoMin v2 — Monitoraggio Errori di Prezzo Amazon Italia")

    config = _load_config()
    logger.info(
        "Configurazione caricata: %d URL, %d brand, %d blacklist keywords",
        len(config.get("SEARCH_URLS", [])),
        len(config.get("BRAND_WHITELIST", [])),
        len(config.get("BLACKLIST_KEYWORDS", [])),
    )

    init_db()
    asyncio.run(_main_loop(config))


if __name__ == "__main__":
    main()
