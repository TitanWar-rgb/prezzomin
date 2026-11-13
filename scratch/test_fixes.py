import os
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from main import _classify_alert
from database import is_recently_notified

config = {
    "MAX_ABS_PRICE_FOR_BRANDS": 150,
    "MIN_DISCOUNT_PERCENT": 45,
    "MIN_DISCOUNT_PERCENT_WAREHOUSE": 35,
}

# Test 1: Sconto 0% (monitor a 89.99€ prezzo pieno) -> Deve essere None
p1 = {"asin": "B01", "title": "Monitor 24", "current_price": 89.99, "discount_percent": 0.0, "price_validated": False}
assert _classify_alert(p1, config) is None, "FAIL: Sconto 0% deve restituire None"

# Test 2: Laptop < 150€ con sconto 40% -> Deve essere PRICE_ERROR
p2 = {"asin": "B02", "title": "HP Laptop", "current_price": 120.0, "discount_percent": 40.0, "price_validated": False}
assert _classify_alert(p2, config) == "PRICE_ERROR", "FAIL: Override prezzo assoluto con sconto fallito"

# Test 3: Prodotto < 150€ con sconto 5% (basso) -> Deve essere None
p3 = {"asin": "B03", "title": "Mouse", "current_price": 40.0, "discount_percent": 5.0, "price_validated": False}
assert _classify_alert(p3, config) is None, "FAIL: Sconto 5% sotto soglia non deve scattare"

# Test 4: Deduplicazione cross-worker
db_dir = Path("db")
db_dir.mkdir(exist_ok=True)
test_db = db_dir / "worker_warehouse_mobile.db"
conn = sqlite3.connect(str(test_db))
conn.execute("CREATE TABLE IF NOT EXISTS notified_asins (asin TEXT PRIMARY KEY, title TEXT, price REAL, original_price REAL, discount_percent REAL, notified_at TIMESTAMP)")
now = datetime.now(timezone.utc).isoformat()
conn.execute("INSERT OR REPLACE INTO notified_asins VALUES (?, ?, ?, ?, ?, ?)", ("B0FXXJGTFF", "Galaxy Tab", 199.0, 399.0, 50.0, now))
conn.commit()
conn.close()

os.environ["DB_NAME"] = "db/worker_smartphones.db"
assert is_recently_notified("B0FXXJGTFF", hours=24) is True, "FAIL: Cross-worker deduplication fallita!"
assert is_recently_notified("B0NOTEXIST", hours=24) is False, "FAIL: Falso positivo ASIN inesistente"

print("TUTTI I TEST SUPERATI CON SUCCESSO!")
