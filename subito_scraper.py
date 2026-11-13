"""Scraper asincrono ad alte prestazioni per Subito.it.

Interroga l'API ufficiale Hades (https://hades.subito.it/v1/search/items) per estrarre:
1. Annunci IN REGALO (prezzo 0 €) sulle categorie tech (Informatica, Telefonia, Audio/Video, Console/Videogiochi).
2. Annunci TECH SEMI-NUOVI ("come nuovo", "sigillato", "pari al nuovo", "mai aperto")
   con spedizione protetta TuttoSubito attiva.
"""

import asyncio
import logging
import os
import re
from typing import Any
from urllib.parse import quote_plus

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logger = logging.getLogger(__name__)

HADES_SEARCH_URL = "https://hades.subito.it/v1/search/items"

# Categorie tech predefinite:
# 10: Informatica, 11: Telefonia, 12: Audio/Video, 13: Console e Videogiochi
DEFAULT_TECH_CATEGORIES = [10, 11, 12, 13]

LIKE_NEW_KEYWORDS = [
    "come nuovo",
    "sigillato",
    "pari al nuovo",
    "mai aperto",
]


class SubitoScraper:
    """Scraper asincrono ad alte prestazioni per annunci Subito.it basato su API Hades."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Inizializza lo scraper per Subito.it.

        Args:
            config: Configurazione del bot / categoria.
        """
        self.config = config
        self.categories: list[int] = config.get("CATEGORIES", DEFAULT_TECH_CATEGORIES)
        self.search_queries: list[str] = config.get("SEARCH_QUERIES", [])
        self.proxy_url: str = os.environ.get("PROXY_URL", "").strip() or config.get(
            "PROXY_URL", ""
        ).strip()
        self.min_discount_percent: float = float(config.get("MIN_DISCOUNT_PERCENT", 0.0))

        self._httpx_client: Any = None
        self._aiohttp_session: Any = None

    async def start(self) -> None:
        """Inizializza il client HTTP asincrono."""
        headers = {
            "User-Agent": "curl/8.4.0",
            "Accept": "application/json",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        if HAS_HTTPX:
            proxy_arg = self.proxy_url if self.proxy_url else None
            self._httpx_client = httpx.AsyncClient(
                http2=True,
                headers=headers,
                proxy=proxy_arg,
                timeout=httpx.Timeout(15.0, connect=10.0),
                follow_redirects=True,
            )
            logger.info(
                "SubitoScraper avviato con httpx HTTP/2%s",
                f" (proxy: {self.proxy_url})" if self.proxy_url else "",
            )
        elif HAS_AIOHTTP:
            self._aiohttp_session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            logger.info("SubitoScraper avviato con aiohttp")
        else:
            logger.warning("Nessuna libreria asincrona (httpx/aiohttp) disponibile!")

    async def stop(self) -> None:
        """Chiude il client HTTP asincrono."""
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None
        if self._aiohttp_session:
            await self._aiohttp_session.close()
            self._aiohttp_session = None

    def get_context(self) -> None:
        """Interfaccia compatibile con Playwright; restituisce None."""
        return None

    async def fetch_json(self, url: str) -> dict[str, Any] | None:
        """Esegue una richiesta HTTP GET e restituisce il JSON decodificato."""
        for attempt in range(1, 3):
            try:
                if self._httpx_client:
                    resp = await self._httpx_client.get(url)
                    if resp.status_code == 200:
                        return resp.json()
                    logger.warning(
                        "Subito Hades API status %d per %s (tentativo %d/2)",
                        resp.status_code,
                        url,
                        attempt,
                    )
                elif self._aiohttp_session:
                    async with self._aiohttp_session.get(url) as resp:
                        if resp.status == 200:
                            return await resp.json(content_type=None)
                        logger.warning(
                            "Subito Hades API status %d per %s (tentativo %d/2)",
                            resp.status,
                            url,
                            attempt,
                        )
            except Exception as exc:
                logger.warning(
                    "Errore richiesta Subito Hades %s (tentativo %d/2): %s",
                    url,
                    attempt,
                    exc,
                )

            await asyncio.sleep(1.0)

        return None

    def _extract_price(self, ad: dict[str, Any]) -> float:
        """Estrae il prezzo float dal blocco features (/price) dell'annuncio."""
        for f in ad.get("features", []):
            if f.get("uri") == "/price":
                values = f.get("values", [])
                if values:
                    # Spesso presente in 'key': es. "0", "149", "149.99"
                    raw_key = str(values[0].get("key", "")).strip()
                    if raw_key:
                        cleaned = raw_key.replace(".", "").replace(",", ".")
                        try:
                            return round(float(cleaned), 2)
                        except ValueError:
                            pass
                    # Fallback sul campo 'value': es. "149 €", "0 €"
                    val_str = str(values[0].get("value", "")).strip()
                    m = re.search(r"(\d+(?:\.\d+)*(?:,\d+)?)", val_str)
                    if m:
                        cleaned = m.group(1).replace(".", "").replace(",", ".")
                        try:
                            return round(float(cleaned), 2)
                        except ValueError:
                            pass
        return 0.0

    def _is_shipping_allowed(self, ad: dict[str, Any]) -> bool:
        """Verifica se la spedizione protetta TuttoSubito è abilitata."""
        for f in ad.get("features", []):
            uri = f.get("uri")
            if uri in ("/item_shipping_allowed", "/item_shippable"):
                values = f.get("values", [])
                if values:
                    val_key = str(values[0].get("key", "")).strip()
                    val_text = str(values[0].get("value", "")).lower().strip()
                    if val_key == "1" or val_text in ("sì", "si", "true", "disponibile"):
                        return True
        return False

    def _extract_condition_text(self, ad: dict[str, Any]) -> str:
        """Estrae la condizione dichiarata nel blocco features o default."""
        for f in ad.get("features", []):
            if f.get("uri") == "/item_condition":
                values = f.get("values", [])
                if values:
                    val_text = values[0].get("value", "").strip()
                    if val_text:
                        return val_text
        return ""

    def parse_ad(self, ad: dict[str, Any], is_free_mode: bool = False) -> dict[str, Any] | None:
        """Estrae i campi normalizzati da un annuncio Hades di Subito.it."""
        urn = ad.get("urn") or ad.get("id")
        if not urn:
            return None

        title = ad.get("subject", "").strip()
        if not title:
            return None

        current_price = self._extract_price(ad)
        is_free = (current_price == 0.0) or is_free_mode
        if is_free:
            current_price = 0.0
            condition = "In regalo"
        else:
            condition = "Come nuovo"

        shipping_allowed = self._is_shipping_allowed(ad)

        # Estrazione località
        geo = ad.get("geo", {})
        town = ""
        if isinstance(geo.get("town"), dict):
            town = geo.get("town", {}).get("value", "")
        if not town and isinstance(geo.get("city"), dict):
            town = geo.get("city", {}).get("value", "")
        if not town:
            town = "Italia"

        region = ""
        if isinstance(geo.get("region"), dict):
            region = geo.get("region", {}).get("value", "")

        location = f"{town} ({region})" if region else town

        url = ad.get("urls", {}).get("default") or ad.get("urls", {}).get("mobile") or ""

        asin = f"subito_{urn}"

        return {
            "asin": asin,
            "item_id": asin,
            "title": title,
            "current_price": current_price,
            "original_price": 0.0,
            "final_price": current_price,
            "condition": condition,
            "url": url,
            "store": "subito",
            "location": location,
            "shipping_allowed": shipping_allowed,
            "is_free": is_free,
            "discount_percent": 100.0 if is_free else 0.0,
            "total_discount_percent": 100.0 if is_free else 0.0,
            "total_savings": 0.0,
            "is_prime": False,
            "is_amazon_fulfilled": False,
        }

    async def scrape_free_items(self) -> list[dict[str, Any]]:
        """Scansiona annunci tech IN REGALO (ps=0&pe=0)."""
        free_items: list[dict[str, Any]] = []
        seen_urns: set[str] = set()

        for cat_id in self.categories:
            url = f"{HADES_SEARCH_URL}?c={cat_id}&ps=0&pe=0"
            logger.info("SubitoScraper: ricerca annunci in regalo c=%s...", cat_id)
            data = await self.fetch_json(url)
            if not data:
                continue

            ads = data.get("ads", [])
            logger.info("SubitoScraper: %d annunci in regalo trovati per c=%s", len(ads), cat_id)

            for ad in ads:
                item = self.parse_ad(ad, is_free_mode=True)
                if item and item["asin"] not in seen_urns:
                    seen_urns.add(item["asin"])
                    free_items.append(item)

            await asyncio.sleep(0.5)

        return free_items

    async def scrape_like_new_deals(self) -> list[dict[str, Any]]:
        """Scansiona annunci tech SEMI-NUOVI con spedizione protetta attiva."""
        deals: list[dict[str, Any]] = []
        seen_urns: set[str] = set()

        # 1. Ricerche per SEARCH_QUERIES configurate (es. iphone, playstation, switch, ecc.)
        queries_to_search = list(self.search_queries)

        for query in queries_to_search:
            for cat_id in self.categories:
                url = f"{HADES_SEARCH_URL}?c={cat_id}&q={quote_plus(query)}&item_shipping_allowed=1"
                logger.info("SubitoScraper: ricerca semi-nuovi '%s' c=%s...", query, cat_id)
                data = await self.fetch_json(url)
                if not data:
                    continue

                ads = data.get("ads", [])
                for ad in ads:
                    item = self.parse_ad(ad, is_free_mode=False)
                    if not item:
                        continue

                    # Filtra: spedizione protetta attiva
                    if not item.get("shipping_allowed"):
                        continue

                    # Filtra: presenza di keyword semi-nuovo nel titolo, body o condizione
                    title_text = item.get("title", "").lower()
                    body_text = str(ad.get("body", "")).lower()
                    cond_text = self._extract_condition_text(ad).lower()

                    full_text = f"{title_text} {body_text} {cond_text}"
                    is_like_new = any(kw in full_text for kw in LIKE_NEW_KEYWORDS)

                    if is_like_new and item["asin"] not in seen_urns:
                        seen_urns.add(item["asin"])
                        deals.append(item)

                await asyncio.sleep(0.5)

        # 2. Ricerche dirette per keyword di condizione (es. "come nuovo", "sigillato", ecc.)
        for kw in LIKE_NEW_KEYWORDS:
            for cat_id in self.categories:
                url = f"{HADES_SEARCH_URL}?c={cat_id}&q={quote_plus(kw)}&item_shipping_allowed=1"
                logger.info("SubitoScraper: ricerca semi-nuovi diretta '%s' c=%s...", kw, cat_id)
                data = await self.fetch_json(url)
                if not data:
                    continue

                ads = data.get("ads", [])
                for ad in ads:
                    item = self.parse_ad(ad, is_free_mode=False)
                    if not item:
                        continue

                    if not item.get("shipping_allowed"):
                        continue

                    if item["asin"] not in seen_urns:
                        seen_urns.add(item["asin"])
                        deals.append(item)

                await asyncio.sleep(0.5)

        return deals

    async def scrape_all(self) -> list[dict[str, Any]]:
        """Esegue la scansione completa di Subito.it (regali + semi-nuovi)."""
        logger.info("SubitoScraper: avvio scansione annunci...")
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # 1. Annunci in regalo
        free_items = await self.scrape_free_items()
        for item in free_items:
            if item["asin"] not in seen_ids:
                seen_ids.add(item["asin"])
                results.append(item)

        # 2. Annunci semi-nuovi
        deals = await self.scrape_like_new_deals()
        for item in deals:
            if item["asin"] not in seen_ids:
                seen_ids.add(item["asin"])
                results.append(item)

        logger.info(
            "SubitoScraper: completato con %d annunci validi (%d in regalo, %d semi-nuovi)",
            len(results),
            len(free_items),
            len(deals),
        )
        return results
