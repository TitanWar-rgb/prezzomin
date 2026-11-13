import requests
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}

r = requests.get("https://www.amazon.it/dp/B0GHQ2BPYJ", headers=headers)
for match in re.finditer(r".{0,100}96[,.]64.{0,100}", r.text):
    print("Match:", match.group(0))
