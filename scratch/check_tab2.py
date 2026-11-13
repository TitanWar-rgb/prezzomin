import requests, re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}
r = requests.get("https://www.amazon.it/dp/B0FXXJGTFF", headers=headers)
m = re.findall(r'<span class="a-offscreen">([^<]+)</span>', r.text)
print("Offscreen prices:", m[:10])
