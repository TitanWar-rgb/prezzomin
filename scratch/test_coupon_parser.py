import re

def parse_coupon(text: str, current_price: float):
    text_lower = text.lower()
    
    # 1. Coupon percentuale: "coupon 20%", "applica coupon 30%", "risparmia 15%"
    m_pct = re.search(r"(?:coupon|risparmia|sconto)[^\d%]{0,15}(\d+)\s*%", text_lower)
    if m_pct:
        pct = float(m_pct.group(1))
        if 0 < pct <= 90:
            discount_val = (current_price * pct) / 100
            return {
                "type": "percent",
                "value": pct,
                "discount_amount": round(discount_val, 2),
                "final_price": round(max(0.0, current_price - discount_val), 2),
                "label": f"{int(pct)}%"
            }

    # 2. Coupon fisso in euro: "coupon 10€", "applica coupon 15,50 €", "risparmia 20 €"
    m_eur = re.search(r"(?:coupon|risparmia|sconto)[^\d€]{0,15}(?:€\s*(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*€)", text_lower)
    if m_eur:
        raw_val = m_eur.group(1) or m_eur.group(2)
        val = float(raw_val.replace(",", "."))
        if 0 < val < current_price:
            return {
                "type": "fixed",
                "value": val,
                "discount_amount": val,
                "final_price": round(current_price - val, 2),
                "label": f"€{val:.2f}"
            }
            
    return None

# Test vari formati Amazon reali
tests = [
    ("Applica coupon 20%", 100.0),
    ("Risparmia 15 € al check-out", 50.0),
    ("Coupon: 50%", 200.0),
    ("Applica coupon di 10,00€", 29.99),
    ("Nessun coupon presente", 45.0)
]

for t, p in tests:
    res = parse_coupon(t, p)
    print(f"'{t}' on {p}€ -> {res}")
