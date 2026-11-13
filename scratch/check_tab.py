import requests, re

r = requests.get("https://www.amazon.it/dp/B0FXXJGTFF", headers={"User-Agent": "Mozilla/5.0"})
print("Status:", r.status_code)
# Search for prices
prices = re.findall(r'(\d+[\.,]\d{2})\s*€', r.text)
print("Prices on B0FXXJGTFF:", set(prices[:15]))
