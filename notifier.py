"""Modulo di notifica Telegram.

Invia alert formattati in HTML tramite l'API Bot di Telegram,
con retry esponenziale in caso di errori di rete.

v2: supporto alert PRICE_ERROR / SUPER_DEAL, condizione prodotto,
prezzo Idealo e risparmio reale.
"""

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

MAX_RETRIES = 3
BACKOFF_BASE = 2  # secondi


def _send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
) -> bool:
    """Invia un messaggio Telegram con retry esponenziale.

    Args:
        bot_token: Token del bot Telegram.
        chat_id: ID della chat di destinazione.
        text: Corpo del messaggio.
        parse_mode: Modalità di parsing (default HTML).

    Returns:
        True se il messaggio è stato inviato con successo.
    """
    url = TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                return True
            logger.warning(
                "Telegram API ha risposto ok=false: %s (tentativo %d/%d)",
                data.get("description", "sconosciuto"),
                attempt,
                MAX_RETRIES,
            )
        except requests.RequestException as exc:
            logger.warning(
                "Errore invio Telegram (tentativo %d/%d): %s",
                attempt,
                MAX_RETRIES,
                exc,
            )

        if attempt < MAX_RETRIES:
            wait = BACKOFF_BASE ** attempt
            logger.debug("Attesa %ds prima del prossimo tentativo…", wait)
            time.sleep(wait)

    logger.error("Impossibile inviare il messaggio dopo %d tentativi", MAX_RETRIES)
    return False


def format_alert_message(product: dict[str, Any]) -> str:
    """Formatta il messaggio di alert HTML per Telegram.

    Supporta alert Amazon (PRICE_ERROR, SUPER_DEAL, coupon stacking)
    e alert specifici eBay Italia con coupon e venditori affidabili.
    """
    store = product.get("store", "amazon")
    alert_level = product.get("alert_level", "PRICE_ERROR")
    asin = product.get("asin") or product.get("item_id", "N/A")
    title = product.get("title", "Titolo non disponibile")
    current = product.get("current_price", 0.0)
    original = product.get("original_price", 0.0)
    discount = product.get("discount_percent", 0.0)
    condition = product.get("condition", "Nuovo")
    category_name = product.get("category_name")

    has_coupon = product.get("has_coupon", False)
    has_cart_promo = product.get("has_cart_promo", False)
    cart_promo_discount = product.get("cart_promo_discount", 0.0)
    final_price = product.get("final_price", current)
    total_discount_percent = product.get("total_discount_percent", discount)
    total_savings = product.get("total_savings", 0.0)

    # Gestione specifica per Subito.it
    if store == "subito":
        item_id = product.get("item_id") or asin
        is_free = product.get("is_free", False) or condition == "In regalo" or current == 0.0
        location = product.get("location", "Italia")
        link = product.get("url") or "https://www.subito.it"

        if is_free:
            header = "🎁 OGGETTO IN REGALO (0 €) SU SUBITO 🎁"
        else:
            header = "💎 OCCASIONE TECH SEMI-NUOVO SU SUBITO 💎"

        lines: list[str] = [
            f"{header}\n",
            f"<b>{_escape_html(title)}</b>\n",
        ]
        if category_name:
            lines.append(f"🏷️ Reparto: <b>{_escape_html(category_name)}</b>")

        if is_free:
            lines.append("💰 Prezzo: <b>GRATIS (0 €)</b>")
            lines.append("📦 Condizione: In regalo")
        else:
            lines.append(f"💰 Prezzo: <b>€{current:.2f}</b>")
            lines.append(f"📦 Condizione: {condition}")

        lines.append(f"📍 Località: {_escape_html(location)}")
        lines.append("📦 Spedizione TuttoSubito: Disponibile")

        if has_cart_promo:
            lines.append(
                f"🛒 Promozione Carrello: <b>-{cart_promo_discount:.2f}€</b> di sconto immediato al check-out!"
            )

        lines.append("")
        lines.append(f'🔗 <a href="{link}">Vai all\'annuncio su Subito</a>')
        lines.append("")
        lines.append(f"<code>{item_id}</code>")
        return "\n".join(lines)

    # Gestione specifica per eBay Italia
    if store == "ebay":
        item_id = product.get("item_id") or asin
        seller = product.get("seller", "Venditore eBay")
        seller_feedback = product.get("seller_feedback", "")
        coupon_code = product.get("coupon_code", "")
        coupon_percent = product.get("coupon_percent", 0.0)
        shipping_cost = product.get("shipping_cost")
        link = f"https://www.ebay.it/itm/{item_id}"

        if has_coupon:
            header = "💣 COMBO EBAY + CODICE SCONTO 💣"
        else:
            header = "🛍️ SUPER AFFARE EBAY 🛍️"

        lines: list[str] = [
            f"{header}\n",
            f"<b>{_escape_html(title)}</b>\n",
        ]
        if category_name:
            lines.append(f"🏷️ Reparto: <b>{_escape_html(category_name)}</b>")

        feedback_fmt = f" ({_escape_html(seller_feedback)})" if seller_feedback else ""
        lines.append(f"🏪 Venditore: <b>{_escape_html(seller)}</b>{feedback_fmt}")
        lines.append(f"📦 Condizione: {condition}")

        if has_coupon:
            lines.append(f"🎟️ Codice sconto da inserire: <code>{coupon_code}</code> (-{coupon_percent:.0f}%)")
            lines.append(f"💰 Prezzo al check-out: <b>€{final_price:.2f}</b>")
            lines.append(f"Prezzo eBay di partenza: €{current:.2f}")
            if original > 0:
                lines.append(f"💸 Prezzo originale barrato: <s>€{original:.2f}</s>")
            lines.append(f"🔥 Risparmio complessivo: <b>-{total_discount_percent:.0f}% (€{total_savings:.2f})</b>")
        else:
            lines.append(f"💰 Prezzo al check-out: <b>€{final_price:.2f}</b>")
            if original > 0:
                lines.append(f"💸 Prezzo originale barrato: <s>€{original:.2f}</s>")
                lines.append(f"🔥 Risparmio complessivo: <b>-{total_discount_percent:.0f}% (€{total_savings:.2f})</b>")
            elif total_discount_percent > 0:
                lines.append(f"🔥 Sconto: <b>-{total_discount_percent:.0f}%</b>")

        if shipping_cost is not None:
            if isinstance(shipping_cost, (int, float)):
                ship_label = "Gratis" if shipping_cost == 0 else f"€{shipping_cost:.2f}"
            else:
                ship_label = str(shipping_cost)
            lines.append(f"🚚 Spedizione: {ship_label}")

        lines.append("")
        lines.append(f'🔗 <a href="{link}">Vai al prodotto</a>')
        lines.append("")
        lines.append(f"<code>{item_id}</code>")
        return "\n".join(lines)

    # Gestione Amazon
    is_prime = product.get("is_prime", False)
    is_fulfilled = product.get("is_amazon_fulfilled", False)
    idealo_best = product.get("idealo_best_price")
    real_discount = product.get("real_discount_percent", 0.0)
    price_validated = product.get("price_validated", False)
    coupon_label = product.get("coupon_label", "")

    link = f"https://www.amazon.it/dp/{asin}"
    prime_label = "✅ Prime" if is_prime else "❌ Non Prime"
    fulfilled_label = (
        "📦 Venduto/Spedito da Amazon" if is_fulfilled else "📦 Venditore terzo"
    )

    if has_coupon and total_discount_percent >= 50:
        header = "💣 SUPER COMBO SCONTO + COUPON 💣"
    elif alert_level == "PRICE_ERROR":
        header = "🚨 ERRORE DI PREZZO PROBABILE 🚨"
    else:
        header = "🔥 SUPER AFFARE VERIFICATO 🔥"

    lines: list[str] = [
        f"{header}\n",
        f"<b>{_escape_html(title)}</b>\n",
    ]
    if category_name:
        lines.append(f"🏷️ Reparto: <b>{_escape_html(category_name)}</b>")

    lines.append(f"📦 Condizione: {condition}")

    if has_coupon:
        lines.append(f"💰 Prezzo al check-out: <b>€{final_price:.2f}</b>")
        lines.append(f"🎟️ Coupon: <b>{coupon_label}</b> (spunta la casella verde prima di acquistare!)")
        lines.append(f"Prezzo Amazon di partenza: €{current:.2f}")
        if original > 0:
            lines.append(f"💸 Prezzo originale barrato: <s>€{original:.2f}</s>")
        lines.append(f"🔥 Risparmio complessivo: <b>-{total_discount_percent:.0f}% (€{total_savings:.2f})</b>")
        if price_validated and idealo_best is not None:
            lines.append(f"🏪 Miglior prezzo Idealo: €{idealo_best:.2f}")
    else:
        lines.append(f"💰 Prezzo Amazon: <b>€{current:.2f}</b>")
        if alert_level == "PRICE_ERROR" and original > 0:
            lines.append(f"💸 Prezzo originale: <s>€{original:.2f}</s>")

        if price_validated and idealo_best is not None:
            savings = idealo_best - current
            lines.append(f"🏪 Miglior prezzo Idealo: €{idealo_best:.2f}")
            lines.append(
                f"🔥 Risparmio reale: <b>-{real_discount:.0f}% (€{savings:.2f})</b>"
            )
        else:
            savings = original - current if original > current else 0.0
            lines.append("⚠️ Non trovato su Idealo — sconto basato su prezzo Amazon")
            lines.append(
                f"🔥 Sconto Amazon: <b>-{discount:.0f}% (€{savings:.2f})</b>"
            )

    if has_cart_promo:
        lines.append(
            f"🛒 Promozione Carrello: <b>-{cart_promo_discount:.2f}€</b> di sconto immediato al check-out!"
        )

    lines.append("")
    lines.append(prime_label)
    if alert_level == "PRICE_ERROR" or (has_coupon and total_discount_percent >= 50):
        lines.append(fulfilled_label)

    lines.append("")
    lines.append(f'🔗 <a href="{link}">Vai al prodotto</a>')
    lines.append("")
    lines.append(f"<code>{asin}</code>")

    return "\n".join(lines)


def send_telegram_alert(
    bot_token: str,
    chat_id: str,
    product: dict[str, Any],
) -> bool:
    """Invia un alert formattato in base all'alert_level del prodotto.

    Supporta alert PRICE_ERROR, SUPER_DEAL e combinazioni coupon stacking.

    Args:
        bot_token: Token del bot Telegram.
        chat_id: ID della chat di destinazione.
        product: Dizionario prodotto arricchito dal PriceValidator e scraper.

    Returns:
        True se inviato con successo.
    """
    alert_level = product.get("alert_level", "PRICE_ERROR")
    asin = product.get("item_id") or product.get("asin", "N/A")
    current = product.get("current_price", 0.0)
    has_coupon = product.get("has_coupon", False)
    final_price = product.get("final_price", current)
    total_discount = product.get("total_discount_percent", product.get("discount_percent", 0.0))
    coupon_label = product.get("coupon_label") or product.get("coupon_code", "")

    text = format_alert_message(product)

    store_label = (
        "Subito Item"
        if product.get("store") == "subito"
        else ("eBay Item" if product.get("store") == "ebay" else "ASIN")
    )

    logger.info(
        "Invio alert [%s] per %s %s – €%.2f (-%0.f%%)%s",
        alert_level,
        store_label,
        asin,
        final_price if has_coupon else current,
        total_discount,
        f" [COUPON {coupon_label}]" if has_coupon else "",
    )
    return _send_message(bot_token, chat_id, text)


def send_startup_message(bot_token: str, chat_id: str) -> bool:
    """Invia un messaggio di conferma avvio del monitor.

    Args:
        bot_token: Token del bot Telegram.
        chat_id: ID della chat di destinazione.

    Returns:
        True se inviato con successo.
    """
    text = (
        "🟢 <b>PrezzoMin v2 avviato</b>\n"
        "\n"
        "Il monitoraggio automatico degli errori di prezzo è attivo.\n"
        "Riceverai notifiche a due livelli: 🚨 ERRORE DI PREZZO e 🔥 SUPER AFFARE."
    )
    return _send_message(bot_token, chat_id, text)


def _escape_html(text: str) -> str:
    """Escape dei caratteri speciali HTML per Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
