import requests
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}

r = requests.get("https://www.amazon.it/s?k=monitor+pc+gaming+144hz", headers=headers)
print("Search page status:", r.status_code)
for match in re.finditer(r".{0,100}(?:96[,.]64|98[,.]28).{0,100}", r.text):
    print("Match:", match.group(0))
