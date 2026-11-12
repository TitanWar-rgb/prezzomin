"""Scraper asincrono per le pagine di ricerca Amazon Italia.

Utilizza Playwright (Chromium) con configurazioni stealth per estrarre
schede prodotto dalle SERP di Amazon.it, restituendo prezzi, sconti,
badge Prime e info sul fulfillment.

v2: resource blocking, Amazon Warehouse, CAPTCHA recovery con rotazione
User-Agent/viewport.
"""

import asyncio
import logging
import os
import random
import re
from typing import Any

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
)

logger = logging.getLogger(__name__)

# User-Agent Chrome Windows. La versione major viene presa dal Chromium realmente
# avviato, così UA, Sec-Ch-Ua (generato da Chromium stesso) e motore JS coincidono.
_UA_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)

# Marcatori della pagina di blocco Amazon servita agli IP datacenter (HTTP 503).
_IP_BLOCK_MARKERS = ("ci dispiace", "sorry! something went wrong", "service unavailable")


def _is_ip_block(status: int | None, title: str) -> bool:
    """True se Amazon ha servito la pagina di blocco 'Ci dispiace' / 503."""
    t = (title or "").lower()
    return status == 503 or any(m in t for m in _IP_BLOCK_MARKERS)

# --- Evolution 1: Resource Blocking ---
# Blocchiamo solo immagini, video, font e tracker esterni.
# NON blocchiamo stylesheet né domini interni amazon (come unagi o fls) che causerebbero l'errore "Ci dispiace"
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_URL_PATTERNS = [
    "googlesyndication", "doubleclick", "google-analytics", "googletagmanager",
    "facebook", "criteo", "hotjar",
]

# --- Evolution 2: Condition keywords ---
_CONDITION_KEYWORDS = [
    "Come nuovo", "Ottime condizioni", "Buone condizioni",
    "Condizioni accettabili", "Ricondizionato", "Usato", "Nuovo",
]

# --- Evolution 5: Viewport pool ---
_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1680, "height": 1050},
]


def _parse_italian_price(raw: str) -> float | None:
    """Converte un prezzo in formato italiano in float.

    Esempi: "1.299,99" → 1299.99, "29,90" → 29.90, "€ 5,00" → 5.0.
    Restituisce None se il parsing fallisce.
    """
    if not raw:
        return None
    # Rimuovi simbolo €, spazi, e caratteri non numerici tranne . e ,
    cleaned = re.sub(r"[^\d.,]", "", raw.strip())
    if not cleaned:
        return None
    try:
        # Formato italiano: punto = separatore migliaia, virgola = decimale
        cleaned = cleaned.replace(".", "").replace(",", ".")
        return float(cleaned)
    except ValueError:
        return None


async def _block_resources(route: Any, request: Any) -> None:
    """Route handler che blocca risorse non essenziali per velocizzare il caricamento."""
    if request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    elif any(pattern in request.url for pattern in BLOCKED_URL_PATTERNS):
        await route.abort()
    else:
        await route.continue_()


class AmazonScraper:
    """Scraper per le pagine di ricerca di Amazon Italia.

    Utilizza Playwright con impostazioni anti-detection per navigare
    le SERP ed estrarre dati sui prodotti.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Inizializza lo scraper con la configurazione fornita.

        Args:
            config: Dizionario di configurazione (da config.json).
        """
        self._config = config
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._consecutive_blocks: int = 0
        self._warmed_up: bool = False
        self._ip_blocked: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Avvia il browser Chromium con profilo stealth."""
        self._pw = await async_playwright().start()

        # PROXY_URL opzionale (es. socks5://127.0.0.1:40000 di Cloudflare WARP su
        # GitHub Actions). Se assente il traffico esce direttamente (PC di casa).
        proxy_url = os.environ.get("PROXY_URL", "").strip()
        proxy = {"server": proxy_url} if proxy_url else None
        if proxy:
            logger.info("Traffico browser instradato via proxy: %s", proxy_url)

        self._browser = await self._pw.chromium.launch(
            headless=True,
            proxy=proxy,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )

        await self._create_context()
        logger.info("Browser Chromium avviato con successo")

    async def stop(self) -> None:
        """Chiude il browser e rilascia le risorse."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None
        logger.info("Browser chiuso")

    def get_context(self) -> BrowserContext | None:
        """Restituisce il browser context corrente per condivisione col PriceValidator."""
        return self._context

    # ------------------------------------------------------------------
    # Context creation & rotation (Evolution 5)
    # ------------------------------------------------------------------

    async def _create_context(self) -> None:
        """Crea un nuovo browser context con UA e viewport casuali."""
        chrome_major = self._browser.version.split(".")[0]  # type: ignore[union-attr]
        ua_str = _UA_TEMPLATE.format(major=chrome_major)
        viewport = random.choice(_VIEWPORTS)
        # Leggera randomizzazione del viewport
        viewport = {
            "width": viewport["width"] + random.randint(-15, 15),
            "height": viewport["height"] + random.randint(-15, 15),
        }
        logger.info("Nuovo context — UA: %s, viewport: %s", ua_str[:60], viewport)

        self._context = await self._browser.new_context(  # type: ignore[union-attr]
            user_agent=ua_str,
            viewport=viewport,
            locale="it-IT",
            timezone_id="Europe/Rome",
            # Solo Accept-Language: gli header Sec-Fetch-* / Sec-Ch-Ua li genera
            # Chromium per ogni richiesta. Forzarli qui li applicava anche a
            # script e XHR (es. Sec-Fetch-Dest: document su un .js), segnale bot.
            extra_http_headers={
                "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )

        # Disabilita il flag navigator.webdriver e spoof proprietà del browser
        await self._context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32',
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['it-IT', 'it', 'en-US', 'en'],
            });
            window.chrome = { runtime: {} };
            """
        )

    async def _rotate_context(self) -> None:
        """Chiude il context corrente e ne crea uno nuovo con UA e viewport diversi.

        Usato dopo la rilevazione di un CAPTCHA per cercare di aggirare
        il blocco temporaneo.
        """
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        self._warmed_up = False
        await self._create_context()
        logger.info("Context ruotato dopo CAPTCHA")

    async def _warmup_session(self) -> None:
        """Visita la homepage di Amazon per ottenere i cookie di sessione reali."""
        if not self._context or self._warmed_up:
            return
        page: Page | None = None
        try:
            logger.info("Inizializzazione sessione Amazon (ottenimento cookie)...")
            page = await self._context.new_page()
            await page.route("**/*", _block_resources)
            resp = await page.goto("https://www.amazon.it/", wait_until="domcontentloaded", timeout=20_000)
            status = resp.status if resp else None
            title = (await page.title()) or ""
            if _is_ip_block(status, title):
                self._ip_blocked = True
                logger.error(
                    "Amazon ha bloccato questo IP già sulla homepage (HTTP %s, titolo '%s'). "
                    "Su GitHub Actions serve PROXY_URL (Cloudflare WARP) o un IP residenziale.",
                    status, title,
                )
                return
            self._ip_blocked = False
            cookie_btn = await page.query_selector("#sp-cc-accept, input[name='acceptCookie']")
            if cookie_btn:
                try:
                    await cookie_btn.click()
                    await asyncio.sleep(2)
                except Exception:
                    pass
            else:
                await asyncio.sleep(2)
            self._warmed_up = True
            logger.info("Sessione Amazon inizializzata con successo (HTTP %s, titolo '%s')", status, title[:60])
        except Exception as exc:
            logger.warning("Warmup sessione fallito (proseguo comunque): %s", exc)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _handle_captcha(self, url: str) -> None:
        """Gestisce la pausa e rotazione context dopo un CAPTCHA.

        Args:
            url: URL che ha innescato il CAPTCHA (per logging).
        """
        self._consecutive_blocks += 1
        if self._consecutive_blocks >= 3:
            pause = random.randint(1800, 1800)  # 30 minuti
            logger.warning(
                "CAPTCHA consecutivi (%d) — pausa estesa di %d secondi",
                self._consecutive_blocks, pause,
            )
        else:
            pause = random.randint(600, 900)
            logger.warning(
                "CAPTCHA rilevato (#%d) — pausa di %d secondi prima di riprovare",
                self._consecutive_blocks, pause,
            )

        # Countdown loggato ogni 60s
        remaining = pause
        while remaining > 0:
            chunk = min(remaining, 60)
            await asyncio.sleep(chunk)
            remaining -= chunk
            if remaining > 0:
                logger.info("CAPTCHA cooldown: %d secondi rimanenti", remaining)

        await self._rotate_context()

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    async def scrape_search_page(self, url: str, is_warehouse: bool = False) -> list[dict[str, Any]]:
        """Scrapa una singola pagina di ricerca Amazon.

        Args:
            url: URL completo della pagina di ricerca.
            is_warehouse: Se True, tagga i prodotti come warehouse deals.

        Returns:
            Lista di dizionari prodotto con chiavi: asin, title,
            current_price, original_price, discount_percent, url,
            is_prime, is_amazon_fulfilled, condition, is_warehouse.
        """
        if not self._context:
            logger.error("Browser non avviato — chiama start() prima")
            return []

        page: Page = await self._context.new_page()
        products: list[dict[str, Any]] = []

        try:
            # Evolution 1: resource blocking
            await page.route("**/*", _block_resources)

            logger.debug("Navigazione: %s", url[:120])
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Rileva CAPTCHA e Robot Check (in italiano e in inglese)
            title = (await page.title()) or ""

            # Blocco IP (pagina 'Ci dispiace' / 503): inutile attendere div[data-asin]
            status = resp.status if resp else None
            if _is_ip_block(status, title):
                self._ip_blocked = True
                logger.warning("Blocco IP Amazon (HTTP %s, titolo '%s'): %s", status, title, url[:80])
                return []
            page_text = (await page.text_content("body")) or ""
            page_lower = (title + " " + page_text).lower()

            is_bot_check = (
                await page.query_selector("#captchacharacters") is not None
                or await page.query_selector("form[action*='validateCaptcha']") is not None
                or "robot check" in page_lower
                or "inserisci i caratteri" in page_lower
                or "characters you see" in page_lower
                or "automated access" in page_lower
                or "something went wrong" in page_lower
            )
            if is_bot_check:
                logger.warning("Robot Check / CAPTCHA rilevato ('%s') — pagina saltata: %s", title, url[:80])
                await page.close()
                await self._handle_captcha(url)
                return []

            # Reset contatore CAPTCHA consecutivi al successo
            self._consecutive_blocks = 0

            # Attendi il caricamento dei risultati
            try:
                await page.wait_for_selector(
                    "div[data-asin]", timeout=15_000
                )
            except PlaywrightTimeout:
                current_title = await page.title()
                logger.warning(
                    "Nessun risultato trovato entro il timeout (Titolo pagina: '%s'): %s",
                    current_title, url[:80],
                )
                return []

            # Scroll umano: scendi a step casuali per caricare il lazy-load
            await self._human_scroll(page)

            # Estrai le card prodotto
            cards = await page.query_selector_all(
                "div[data-component-type='s-search-result'][data-asin]"
            )
            logger.debug("Trovate %d card nella pagina", len(cards))

            for card in cards:
                try:
                    product = await self._extract_product(card)
                    if product:
                        product["is_warehouse"] = is_warehouse
                        products.append(product)
                except Exception as exc:
                    logger.debug("Errore estrazione card: %s", exc)
                    continue

        except PlaywrightTimeout:
            logger.warning("Timeout navigazione: %s", url[:80])
        except Exception as exc:
            logger.error("Errore scraping pagina %s: %s", url[:80], exc)
        finally:
            await page.close()

        logger.info("Estratti %d prodotti da %s", len(products), url[:80])
        return products

    async def scrape_all(self) -> list[dict[str, Any]]:
        """Scrapa tutte le URL di ricerca configurate, incluse quelle Warehouse.

        Introduce jitter casuale tra le pagine per ridurre il rischio
        di blocco. Deduplica i risultati per ASIN.

        Returns:
            Lista deduplica di dizionari prodotto.
        """
        urls: list[str] = self._config.get("SEARCH_URLS", [])
        if not urls:
            logger.warning("Nessuna URL di ricerca configurata")
            return []

        seen_asins: set[str] = set()
        all_products: list[dict[str, Any]] = []

        # Scrapa le URL principali
        all_urls: list[tuple[str, bool]] = [(u, False) for u in urls]

        # Evolution 2: aggiungi URL Warehouse se abilitate
        if self._config.get("ENABLE_WAREHOUSE_DEALS", False):
            warehouse_urls = self._config.get("WAREHOUSE_SEARCH_URLS", [])
            all_urls.extend((u, True) for u in warehouse_urls)
            logger.info("Warehouse Deals abilitati: %d URL extra", len(warehouse_urls))

        # Inizializza cookie di sessione reali se non ancora fatto
        await self._warmup_session()
        if self._ip_blocked:
            # Riprova il warmup al prossimo ciclo invece di sprecare N timeout
            self._warmed_up = False
            logger.error("Ciclo saltato: IP bloccato da Amazon")
            return []

        for i, (url, is_wh) in enumerate(all_urls):
            try:
                page_products = await self.scrape_search_page(url, is_warehouse=is_wh)
                for p in page_products:
                    asin = p.get("asin")
                    if asin and asin not in seen_asins:
                        seen_asins.add(asin)
                        all_products.append(p)
            except Exception as exc:
                logger.error("Errore fatale sulla URL %s: %s", url[:80], exc)

            # Jitter tra una pagina e l'altra (esclusa l'ultima)
            if i < len(all_urls) - 1:
                delay = random.uniform(3.0, 8.0)
                logger.debug("Pausa %.1fs prima della prossima pagina", delay)
                await asyncio.sleep(delay)

        logger.info(
            "Scraping completato: %d prodotti unici da %d URL",
            len(all_products),
            len(all_urls),
        )
        return all_products

    # ------------------------------------------------------------------
    # Metodi interni
    # ------------------------------------------------------------------

    async def _human_scroll(self, page: Page) -> None:
        """Simula uno scroll umano sulla pagina."""
        for _ in range(random.randint(3, 6)):
            delta = random.randint(300, 700)
            await page.mouse.wheel(0, delta)
            await asyncio.sleep(random.uniform(0.4, 1.2))

    async def _extract_product(
        self, card: Any
    ) -> dict[str, Any] | None:
        """Estrae i dati di un singolo prodotto dalla card HTML.

        Restituisce None se mancano dati essenziali (ASIN, titolo).
        """
        asin = await card.get_attribute("data-asin")
        if not asin or not asin.strip():
            return None

        # Titolo
        title_el = await card.query_selector(
            "h2 a span, .a-text-normal"
        )
        title = (await title_el.inner_text()).strip() if title_el else None
        if not title:
            return None

        # Prezzo corrente (primo .a-price .a-offscreen nel card)
        current_price = await self._extract_price(
            card, ".a-price:not(.a-text-price) .a-offscreen"
        )

        # Prezzo originale (barrato)
        original_price = await self._extract_price(
            card, ".a-price.a-text-price .a-offscreen"
        )

        # Calcola lo sconto
        discount_percent = 0.0
        if current_price is not None and original_price and original_price > 0:
            discount_percent = (
                (original_price - current_price) / original_price
            ) * 100
            # Ignora sconti negativi o assurdi
            if discount_percent < 0:
                discount_percent = 0.0

        # Badge Prime
        prime_el = await card.query_selector(
            ".s-prime, i.a-icon-prime, [aria-label*='Prime']"
        )
        is_prime = prime_el is not None

        # Fulfillment Amazon
        is_fulfilled = False
        delivery_els = await card.query_selector_all(
            ".a-row.a-size-base.a-color-secondary span, "
            "[data-cy='delivery-recipe'] span, "
            ".s-align-children-center span"
        )
        for el in delivery_els:
            text = (await el.inner_text() or "").lower()
            if "amazon" in text:
                is_fulfilled = True
                break

        # Evolution 2: Condizione prodotto
        condition = await self._extract_condition(card)

        return {
            "asin": asin.strip(),
            "title": title,
            "current_price": current_price if current_price is not None else 0.0,
            "original_price": original_price if original_price is not None else 0.0,
            "discount_percent": round(discount_percent, 1),
            "url": f"https://www.amazon.it/dp/{asin.strip()}",
            "is_prime": is_prime,
            "is_amazon_fulfilled": is_fulfilled,
            "condition": condition,
            "is_warehouse": False,
        }

    async def _extract_condition(self, card: Any) -> str:
        """Estrae la condizione del prodotto dalla card (Warehouse deals).

        Cerca badge di condizione nei testi secondari della card.
        Default: "Nuovo".
        """
        # Cerca nei testi secondari della card
        secondary_els = await card.query_selector_all(
            ".a-size-small .a-color-secondary, "
            ".a-size-small.a-color-secondary, "
            ".a-badge-text, "
            ".a-text-bold.a-size-small"
        )
        for el in secondary_els:
            text = (await el.inner_text() or "").strip()
            for keyword in _CONDITION_KEYWORDS:
                if keyword.lower() in text.lower():
                    return keyword
        return "Nuovo"

    async def _extract_price(
        self, parent: Any, selector: str
    ) -> float | None:
        """Estrae e converte un prezzo dal selettore CSS indicato."""
        el = await parent.query_selector(selector)
        if not el:
            return None
        raw = await el.inner_text()
        return _parse_italian_price(raw)
