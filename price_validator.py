"""Validatore prezzi di mercato tramite Idealo.it.

Confronta il prezzo Amazon con il miglior prezzo reale sul mercato italiano
per distinguere veri errori di prezzo da finti sconti con listino gonfiato.
"""

import asyncio
import logging
import random
import re
from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# Stessi filtri di risorse usati dallo scraper Amazon
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
_BLOCKED_URL_PATTERNS = [
    "googlesyndication", "doubleclick", "google-analytics", "googletagmanager",
    "facebook", "amazon-adsystem", "criteo", "hotjar",
]

# Parole rumore da rimuovere dalle query di ricerca
_NOISE_WORDS = {
    "con", "per", "del", "della", "delle", "dei", "degli", "di", "da",
    "in", "su", "il", "la", "le", "lo", "gli", "un", "una", "uno",
    "e", "o", "a", "al", "alla", "alle", "ai", "agli", "che", "è",
    "and", "for", "the", "with", "from", "new", "nuovo", "nuova",
}


def _clean_search_query(title: str) -> str:
    """Extract meaningful product words from an Amazon title for Idealo search.

    Strips noise like parenthesized specs, bracket content, and generic adjectives.
    Returns at most 8 words.
    """
    # Rimuovi contenuti tra parentesi/bracket
    cleaned = re.sub(r"\([^)]*\)", " ", title)
    cleaned = re.sub(r"\[[^\]]*\]", " ", cleaned)

    # Rimuovi caratteri speciali
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)

    words: list[str] = []
    for w in cleaned.split():
        if w.lower() in _NOISE_WORDS:
            continue
        if len(w) < 2:
            continue
        words.append(w)
        if len(words) >= 8:
            break

    return " ".join(words)


async def _block_resources_idealo(route: Any, request: Any) -> None:
    """Blocca risorse non essenziali sulle pagine Idealo."""
    if request.resource_type in _BLOCKED_RESOURCE_TYPES:
        await route.abort()
    elif any(p in request.url for p in _BLOCKED_URL_PATTERNS):
        await route.abort()
    else:
        await route.continue_()


class PriceValidator:
    """Valida i prezzi Amazon confrontandoli con Idealo.it.

    Condivide il browser context del scraper per evitare di lanciare
    una seconda istanza di Chromium.
    """

    def __init__(self, context: BrowserContext) -> None:
        """Inizializza il validatore con un browser context esistente.

        Args:
            context: BrowserContext Playwright condiviso con lo scraper.
        """
        self._context = context

    async def validate_price(self, product: dict[str, Any]) -> dict[str, Any]:
        """Confronta il prezzo Amazon con Idealo e arricchisce il prodotto.

        Non solleva mai eccezioni: in caso di errore restituisce il prodotto
        con ``price_validated=False``.

        Args:
            product: Dizionario prodotto con almeno ``title``, ``asin``,
                     ``current_price``.

        Returns:
            Dizionario prodotto arricchito con ``idealo_best_price``,
            ``idealo_url``, ``real_discount_percent`` e ``price_validated``.
        """
        result = dict(product)
        result.setdefault("idealo_best_price", None)
        result.setdefault("idealo_url", None)
        result.setdefault("real_discount_percent", 0.0)
        result.setdefault("price_validated", False)

        title = product.get("title", "")
        current_price = product.get("current_price", 0.0)
        if not title or current_price <= 0:
            return result

        query = _clean_search_query(title)
        if not query:
            return result

        idealo_url = f"https://www.idealo.it/risultati.html?q={quote_plus(query)}"
        result["idealo_url"] = idealo_url

        # Delay casuale anti-rate-limit
        await asyncio.sleep(random.uniform(2.0, 4.0))

        page: Page | None = None
        try:
            page = await self._context.new_page()
            await page.route("**/*", _block_resources_idealo)

            await page.goto(idealo_url, wait_until="domcontentloaded", timeout=20_000)

            # Attendi un selettore prezzi
            try:
                await page.wait_for_selector(
                    "[data-price], .productOffers-listItemOfferPrice, "
                    ".offerList-item-price, .productOffers-listItem-price",
                    timeout=8_000,
                )
            except PlaywrightTimeout:
                logger.debug("Nessun risultato Idealo per: %s", query[:60])
                return result

            best_price = await self._extract_best_price(page)
            if best_price is not None and best_price > 0:
                result["idealo_best_price"] = best_price
                result["price_validated"] = True
                if best_price > current_price:
                    result["real_discount_percent"] = round(
                        ((best_price - current_price) / best_price) * 100, 1
                    )
                else:
                    result["real_discount_percent"] = 0.0
                logger.info(
                    "Idealo: %s → €%.2f (Amazon €%.2f, sconto reale %.1f%%)",
                    query[:40], best_price, current_price,
                    result["real_discount_percent"],
                )
        except PlaywrightTimeout:
            logger.warning("Timeout Idealo per: %s", query[:60])
        except Exception as exc:
            logger.warning("Errore Idealo per %s: %s", query[:60], exc)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

        return result

    async def _extract_best_price(self, page: Page) -> float | None:
        """Estrae il prezzo più basso dalla pagina risultati Idealo.

        Prova più selettori CSS in cascata per gestire varianti di layout.

        Returns:
            Il miglior prezzo trovato, o None.
        """
        selectors = [
            "[data-price]",
            ".productOffers-listItemOfferPrice",
            ".offerList-item-price",
            ".productOffers-listItem-price",
            ".product-price",
        ]

        prices: list[float] = []

        # Metodo 1: attributo data-price
        data_els = await page.query_selector_all("[data-price]")
        for el in data_els:
            raw = await el.get_attribute("data-price")
            if raw:
                try:
                    prices.append(float(raw))
                except ValueError:
                    pass

        # Metodo 2: testo da selettori CSS
        for selector in selectors[1:]:
            els = await page.query_selector_all(selector)
            for el in els:
                text = await el.inner_text()
                price = self._parse_price_text(text)
                if price is not None:
                    prices.append(price)

        return min(prices) if prices else None

    @staticmethod
    def _parse_price_text(raw: str) -> float | None:
        """Converte un testo prezzo Idealo (formato italiano) in float."""
        if not raw:
            return None
        cleaned = re.sub(r"[^\d.,]", "", raw.strip())
        if not cleaned:
            return None
        try:
            cleaned = cleaned.replace(".", "").replace(",", ".")
            val = float(cleaned)
            return val if val > 0 else None
        except ValueError:
            return None
