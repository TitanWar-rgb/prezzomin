"""Scraper asincrono ad alte prestazioni per eBay Italia.

Estrae annunci 'Compralo Subito' dalle SERP di ricerca di eBay.it,
supporta parsing resiliente sia per markup desktop (blocchi s-item) che mobile (s-card),
applica whitelist venditori affidabili e calcola combinazioni di sconto con Codici Promo & Coupon.
"""

import asyncio
import logging
import os
import re
from html import unescape
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

EBAY_SEARCH_BASE_URL = "https://www.ebay.it/sch/i.html?_nkw={query}&LH_BIN=1&_sop=15"

# User-Agent pool per rotazione e resilienza Akamai
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]


def parse_price(raw: str | None) -> float | None:
    """Estrae e converte un prezzo italiano in formato float.

    Gestisce formati come:
    - 'EUR 1.299,99' -> 1299.99
    - '29,90 €' -> 29.90
    - 'EUR 199,99 a EUR 249,99' -> 199.99 (prezzo minimo del range)
    """
    if not raw:
        return None
    cleaned = raw.replace("\xa0", " ").strip()
    match = re.search(r"(\d+(?:\.\d+)*(?:,\d+)?)", cleaned)
    if not match:
        return None
    val_str = match.group(1).replace(".", "").replace(",", ".")
    try:
        return round(float(val_str), 2)
    except ValueError:
        return None


def parse_shipping(raw: str | None) -> float | str:
    """Estrae il costo di spedizione.

    Restituisce 0.0 per spedizione gratuita, float per costi determinati,
    oppure una stringa descrittiva.
    """
    if not raw:
        return 0.0
    text = raw.lower().strip()
    if any(k in text for k in ("gratis", "gratuita", "consegna gratuita")):
        return 0.0
    parsed = parse_price(text)
    if parsed is not None:
        return parsed
    return raw.strip()


def parse_seller_info(raw: str | None) -> tuple[str, str]:
    """Estrae nome venditore e feedback da stringhe grezze di eBay.

    Esempi:
    - 'monclick-italia (125430) 98,6%' -> ('monclick-italia', '125430 98,6%')
    - 'yeppon-shop (99.8%)' -> ('yeppon-shop', '99.8%')
    - 'Venditore Affidabilità Top: redcoonit (5400) 99.1%' -> ('redcoonit', '5400 99.1%')
    - 'Venditore: stayon_elettronica' -> ('stayon_elettronica', '')
    - 'Venditore privato' -> ('Venditore privato', '')
    """
    if not raw:
        return "Venditore eBay", ""
    text = raw.strip()
    if text.lower() == "venditore privato":
        return "Venditore privato", ""

    # Se ci sono segmenti separati da | o trattino, cerca il segmento con venditore/feedback
    parts = [p.strip() for p in text.split("|")]
    for p in parts:
        if "venditore" in p.lower() or "(" in p or "%" in p:
            text = p
            break

    # Rimuovi prefissi venditore / affidabilità top
    text = re.sub(
        r"^(?:venditore(?:\s+affidabilit[aà]\s+top)?|affidabilit[aà]\s+top|da)\s*[:\-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    m = re.search(r"([a-zA-Z0-9_\-\.]+)(?:\s*\(([^)]+)\))?(?:\s*([\d.,]+%))?", text)
    if m:
        name = m.group(1).strip()
        parts = [p.strip() for p in (m.group(2), m.group(3)) if p]
        feedback = " ".join(parts)
        return name, feedback
    return text, ""


def calculate_ebay_coupon(
    current_price: float,
    original_price: float = 0.0,
    coupon_code: str | None = None,
    coupon_percent: float = 0.0,
    coupon_max_discount: float = 0.0,
) -> dict[str, Any]:
    """Calcola sconto coupon, prezzo finale al check-out e risparmio totale.

    Logica:
    - coupon_saving = min((current_price * coupon_percent) / 100, max_discount)
    - final_price = current_price - coupon_saving
    - total_discount_percent e total_savings calcolati sul prezzo di partenza / originale.
    """
    current_price = max(0.0, round(current_price, 2))
    original_price = max(0.0, round(original_price, 2))

    if coupon_code and coupon_percent > 0:
        raw_saving = (current_price * coupon_percent) / 100.0
        if coupon_max_discount > 0:
            coupon_saving = min(raw_saving, coupon_max_discount)
        else:
            coupon_saving = raw_saving
        coupon_saving = round(coupon_saving, 2)
        final_price = round(max(0.0, current_price - coupon_saving), 2)

        if original_price > current_price:
            total_savings = round(original_price - final_price, 2)
            total_discount_percent = round(
                ((original_price - final_price) / original_price) * 100.0, 1
            )
        else:
            total_savings = coupon_saving
            total_discount_percent = (
                round((coupon_saving / current_price) * 100.0, 1)
                if current_price > 0
                else 0.0
            )

        return {
            "has_coupon": True,
            "coupon_code": coupon_code,
            "coupon_percent": coupon_percent,
            "coupon_max_discount": coupon_max_discount,
            "coupon_saving": coupon_saving,
            "final_price": final_price,
            "total_discount_percent": total_discount_percent,
            "total_savings": total_savings,
        }

    # Nessun coupon applicato
    base_savings = (
        round(original_price - current_price, 2)
        if (original_price > current_price)
        else 0.0
    )
    base_discount = (
        round(((original_price - current_price) / original_price) * 100.0, 1)
        if (original_price > current_price)
        else 0.0
    )
    return {
        "has_coupon": False,
        "coupon_code": None,
        "coupon_percent": 0.0,
        "coupon_max_discount": 0.0,
        "coupon_saving": 0.0,
        "final_price": current_price,
        "total_discount_percent": base_discount,
        "total_savings": base_savings,
    }


class EbayScraper:
    """Scraper asincrono ad alte prestazioni per eBay Italia."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.search_queries: list[str] = config.get("SEARCH_QUERIES", [])
        self.search_urls: list[str] = config.get("SEARCH_URLS", [])
        self.seller_whitelist: list[str] = [
            s.lower().strip() for s in config.get("SELLER_WHITELIST", []) if s.strip()
        ]
        self.coupon_code: str | None = config.get("COUPON_CODE")
        self.coupon_percent: float = float(config.get("COUPON_PERCENT", 0.0))
        self.coupon_max_discount: float = float(config.get("COUPON_MAX_DISCOUNT", 0.0))
        self.min_discount_percent: float = float(config.get("MIN_DISCOUNT_PERCENT", 0.0))
        self.proxy_url: str = os.environ.get("PROXY_URL", "").strip() or config.get(
            "PROXY_URL", ""
        ).strip()

        self._httpx_client: Any = None
        self._aiohttp_session: Any = None

    async def start(self) -> None:
        """Inizializza la sessione HTTP asincrona."""
        default_headers = {
            "User-Agent": USER_AGENTS[0],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

        if HAS_HTTPX:
            proxy_arg = self.proxy_url if self.proxy_url else None
            self._httpx_client = httpx.AsyncClient(
                http2=True,
                headers=default_headers,
                proxy=proxy_arg,
                timeout=httpx.Timeout(15.0, connect=10.0),
                follow_redirects=True,
            )
            logger.info(
                "EbayScraper avviato con httpx HTTP/2%s",
                f" (proxy: {self.proxy_url})" if self.proxy_url else "",
            )
        elif HAS_AIOHTTP:
            connector = None
            self._aiohttp_session = aiohttp.ClientSession(
                headers=default_headers,
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            logger.info("EbayScraper avviato con aiohttp")
        else:
            logger.warning("Nessuna libreria asincrona (httpx/aiohttp) disponibile!")

    async def stop(self) -> None:
        """Chiude le sessioni HTTP aperte."""
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None
        if self._aiohttp_session:
            await self._aiohttp_session.close()
            self._aiohttp_session = None

    def get_context(self) -> None:
        """Ritorna None per compatibilità d'interfaccia con Playwright."""
        return None

    def build_search_url(self, query: str) -> str:
        """Genera l'URL di ricerca eBay per compralo subito ordinato per prezzo/spedizione."""
        return EBAY_SEARCH_BASE_URL.format(query=quote_plus(query))

    async def fetch_html(self, url: str) -> str | None:
        """Esegue la richiesta HTTP con gestione rotazione User-Agent e retry."""
        for attempt, ua in enumerate(USER_AGENTS, start=1):
            headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            try:
                if self._httpx_client:
                    resp = await self._httpx_client.get(url, headers=headers)
                    if resp.status_code == 200:
                        return resp.text
                    elif resp.status_code == 403:
                        logger.debug("Tentativo %d: eBay 403 con UA %s", attempt, ua[:30])
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        logger.warning("eBay status code %d per URL %s", resp.status_code, url)
                elif self._aiohttp_session:
                    proxy = self.proxy_url if self.proxy_url else None
                    async with self._aiohttp_session.get(url, headers=headers, proxy=proxy) as resp:
                        if resp.status == 200:
                            return await resp.text()
                        elif resp.status == 403:
                            logger.debug("Tentativo %d: aiohttp 403 con UA %s", attempt, ua[:30])
                            await asyncio.sleep(0.5)
                            continue
            except Exception as exc:
                logger.debug("Errore download eBay (tentativo %d): %s", attempt, exc)
                await asyncio.sleep(0.5)

        logger.warning("Impossibile scaricare la pagina eBay dopo %d tentativi: %s", len(USER_AGENTS), url)
        return None

    def parse_s_item_block(self, block_html: str) -> dict[str, Any] | None:
        """Estrae i dati di un annuncio dal markup desktop eBay (s-item)."""
        # Estrai Item ID
        m_id = re.search(r"/itm/(\d{9,15})", block_html)
        if not m_id:
            m_id = re.search(r'data-listingid[=\s"\']+(\d+)', block_html)
        if not m_id:
            m_id = re.search(r'id=["\']?item([a-f0-9]+)["\']?', block_html)
        if not m_id:
            return None
        item_id = m_id.group(1)

        # Estrai Titolo
        m_title = re.search(
            r'class=["\']?[^"\'>]*s-item__title[^"\'>]*["\']?[^>]*>(?:<span[^>]*>)?(.*?)(?:</span>)?(?:</div>|</h3>)',
            block_html,
            re.DOTALL | re.IGNORECASE,
        )
        if not m_title:
            m_title = re.search(r'<span role=["\']?heading["\']?[^>]*>(.*?)</span>', block_html, re.DOTALL | re.IGNORECASE)
        if not m_title:
            return None

        raw_title = m_title.group(1)
        # Rimuovi tag badge (es. "Nuova inserzione", "Sponsorizzato")
        title = unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        title = re.sub(r"^(?:Nuova inserzione|Sponsorizzato)\s*", "", title, flags=re.IGNORECASE).strip()
        if not title or "Shop on eBay" in title or "Acquisti su eBay" in title:
            return None

        # Estrai Prezzo corrente
        m_price = re.search(
            r'class=["\']?[^"\'>]*s-item__price[^"\'>]*["\']?[^>]*>(.*?)</span>',
            block_html,
            re.DOTALL | re.IGNORECASE,
        )
        if not m_price:
            return None
        current_price = parse_price(unescape(re.sub(r"<[^>]+>", "", m_price.group(1))))
        if not current_price or current_price <= 0:
            return None

        # Estrai Prezzo originale barrato (se presente)
        original_price = 0.0
        m_orig = re.search(
            r'class=["\']?[^"\'>]*(?:s-item__trending-price|s-item__original-price)[^"\'>]*["\']?[^>]*>(?:<s[^>]*>|<span[^>]*>)?(.*?)(?:</s>|</span>)',
            block_html,
            re.DOTALL | re.IGNORECASE,
        )
        if not m_orig:
            m_orig = re.search(r'<s[^>]*>(.*?)</s>', block_html, re.DOTALL | re.IGNORECASE)
        if m_orig:
            parsed_orig = parse_price(unescape(re.sub(r"<[^>]+>", "", m_orig.group(1))))
            if parsed_orig and parsed_orig > current_price:
                original_price = parsed_orig

        # Estrai Venditore e feedback
        seller = "Venditore eBay"
        seller_feedback = ""
        m_seller = re.search(
            r'class=["\']?[^"\'>]*s-item__seller-info-text[^"\'>]*["\']?[^>]*>(.*?)</span>',
            block_html,
            re.DOTALL | re.IGNORECASE,
        )
        if m_seller:
            seller_str = unescape(re.sub(r"<[^>]+>", "", m_seller.group(1))).strip()
            seller, seller_feedback = parse_seller_info(seller_str)

        # Estrai Spedizione
        shipping_cost: float | str = 0.0
        m_ship = re.search(
            r'class=["\']?[^"\'>]*s-item__shipping[^"\'>]*["\']?[^>]*>(.*?)</span>',
            block_html,
            re.DOTALL | re.IGNORECASE,
        )
        if m_ship:
            shipping_cost = parse_shipping(unescape(re.sub(r"<[^>]+>", "", m_ship.group(1))))

        return {
            "item_id": item_id,
            "asin": item_id,
            "title": title,
            "current_price": current_price,
            "original_price": original_price,
            "discount_percent": round(((original_price - current_price) / original_price) * 100.0, 1)
            if (original_price and original_price > current_price)
            else 0.0,
            "seller": seller,
            "seller_feedback": seller_feedback,
            "shipping_cost": shipping_cost,
            "url": f"https://www.ebay.it/itm/{item_id}",
            "store": "ebay",
            "condition": "Nuovo",
            "is_prime": False,
            "is_amazon_fulfilled": False,
        }

    def parse_s_card_block(self, block_html: str) -> dict[str, Any] | None:
        """Estrae i dati di un annuncio dal markup mobile/card eBay (s-card)."""
        # Estrai Item ID
        m_id = re.search(r'data-listingid[=\s"\']+(\d+)', block_html)
        if not m_id:
            m_id = re.search(r"/itm/(\d{9,15})", block_html)
        if not m_id:
            return None
        item_id = m_id.group(1)

        # Estrai Titolo
        m_title = re.search(
            r'class=["\']?[^"\'>]*s-card__title[^"\'>]*["\']?[^>]*>(?:<span[^>]*>)?(.*?)(?:</span>)?(?:</div>|</h3>)',
            block_html,
            re.DOTALL | re.IGNORECASE,
        )
        if not m_title:
            return None
        title = unescape(re.sub(r"<[^>]+>", "", m_title.group(1))).strip()
        if not title:
            return None

        # Estrai Prezzo corrente
        m_price = re.search(
            r'class=["\']?[^"\'>]*s-card__price[^"\'>]*["\']?[^>]*>(?:<span[^>]*>)?(.*?)(?:</span>)?(?:</div>|</span>)',
            block_html,
            re.DOTALL | re.IGNORECASE,
        )
        if not m_price:
            return None
        current_price = parse_price(unescape(re.sub(r"<[^>]+>", "", m_price.group(1))))
        if not current_price or current_price <= 0:
            return None

        # Estrai Prezzo originale barrato
        original_price = 0.0
        m_orig = re.search(r'<s[^>]*>(.*?)</s>', block_html, re.DOTALL | re.IGNORECASE)
        if m_orig:
            parsed_orig = parse_price(unescape(re.sub(r"<[^>]+>", "", m_orig.group(1))))
            if parsed_orig and parsed_orig > current_price:
                original_price = parsed_orig

        # Estrai Sottotitoli / Info venditore / Condizione
        seller = "Venditore eBay"
        seller_feedback = ""
        condition = "Nuovo"
        subtitles = re.findall(
            r'class=["\']?[^"\'>]*s-card__subtitle[^"\'>]*["\']?[^>]*>(.*?)</div>',
            block_html,
            re.DOTALL | re.IGNORECASE,
        )
        for sub in subtitles:
            clean_sub = unescape(re.sub(r"<[^>]+>", "", sub)).strip()
            parts = [p.strip() for p in clean_sub.split("|")]
            for p in parts:
                p_lower = p.lower()
                if "venditore" in p_lower or "(" in p or "%" in p:
                    seller, seller_feedback = parse_seller_info(p)
                elif any(c in p_lower for c in ("nuovo", "usato", "ricondizionato", "seconda mano")):
                    condition = p

        # Spedizione
        shipping_cost: float | str = 0.0
        m_ship = re.search(
            r"(\+?EUR\s*[\d.,]+\s*per la consegna|Spedizione gratuita|Consegna gratuita)",
            block_html,
            re.IGNORECASE,
        )
        if m_ship:
            shipping_cost = parse_shipping(m_ship.group(1))

        return {
            "item_id": item_id,
            "asin": item_id,
            "title": title,
            "current_price": current_price,
            "original_price": original_price,
            "discount_percent": round(((original_price - current_price) / original_price) * 100.0, 1)
            if (original_price and original_price > current_price)
            else 0.0,
            "seller": seller,
            "seller_feedback": seller_feedback,
            "shipping_cost": shipping_cost,
            "url": f"https://www.ebay.it/itm/{item_id}",
            "store": "ebay",
            "condition": condition,
            "is_prime": False,
            "is_amazon_fulfilled": False,
        }

    def parse_html(self, html: str) -> list[dict[str, Any]]:
        """Estrae tutti i prodotti validi dall'HTML (supporta s-item e s-card)."""
        products: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # 1. Parsing blocchi s-item (desktop standard)
        s_item_blocks = re.findall(
            r'<li[^>]*class=["\']?[^"\'>]*s-item(?![a-zA-Z0-9_\-])[^"\'>]*["\']?[^>]*>.*?</li>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        for block in s_item_blocks:
            item = self.parse_s_item_block(block)
            if item and item["item_id"] not in seen_ids:
                seen_ids.add(item["item_id"])
                products.append(item)

        # 2. Parsing blocchi s-card (mobile / responsive modern)
        if not products:
            s_card_blocks = re.findall(
                r'<li[^>]*class=["\']?[^"\'>]*s-card(?![a-zA-Z0-9_\-])[^"\'>]*["\']?[^>]*>.*?</li>',
                html,
                re.DOTALL | re.IGNORECASE,
            )
            for block in s_card_blocks:
                item = self.parse_s_card_block(block)
                if item and item["item_id"] not in seen_ids:
                    seen_ids.add(item["item_id"])
                    products.append(item)

        return products

    def filter_and_apply_coupons(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filtra per whitelist venditori e applica il coupon configurato."""
        filtered: list[dict[str, Any]] = []

        for item in items:
            seller = item.get("seller", "").lower()

            # Whitelist venditori (se specificata)
            if self.seller_whitelist:
                is_whitelisted = any(w in seller for w in self.seller_whitelist)
                if not is_whitelisted:
                    logger.debug("Venditore '%s' non in whitelist — scartato", item.get("seller"))
                    continue

            # Applicazione Coupon & Codici Promo
            coupon_metrics = calculate_ebay_coupon(
                current_price=item.get("current_price", 0.0),
                original_price=item.get("original_price", 0.0),
                coupon_code=self.coupon_code,
                coupon_percent=self.coupon_percent,
                coupon_max_discount=self.coupon_max_discount,
            )
            item.update(coupon_metrics)

            # Filtro sconto minimo
            if self.min_discount_percent > 0:
                if item.get("total_discount_percent", 0.0) < self.min_discount_percent:
                    continue

            filtered.append(item)

        return filtered

    async def scrape_all(self) -> list[dict[str, Any]]:
        """Esegue la scansione di tutte le query e gli URL configurati."""
        all_results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        urls_to_scrape: list[str] = list(self.search_urls)
        for q in self.search_queries:
            urls_to_scrape.append(self.build_search_url(q))

        logger.info("EbayScraper: scansione di %d URL di ricerca...", len(urls_to_scrape))

        for url in urls_to_scrape:
            logger.info("Scraping eBay: %s", url)
            html = await self.fetch_html(url)
            if not html:
                continue

            parsed_items = self.parse_html(html)
            logger.info("Estratti %d annunci da %s", len(parsed_items), url)

            valid_items = self.filter_and_apply_coupons(parsed_items)
            for item in valid_items:
                iid = item["item_id"]
                if iid not in seen_ids:
                    seen_ids.add(iid)
                    all_results.append(item)

            # Pausa educata tra le richieste
            await asyncio.sleep(1.0)

        logger.info("EbayScraper: %d offerte valide trovate", len(all_results))
        return all_results
