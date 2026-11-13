import requests, re

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
})

# Warmup
session.get("https://www.amazon.it/")

# Set zip code to 00100 (Roma)
glow_url = "https://www.amazon.it/portal-migration/hz/glow/address-change?actionSource=glow"
payload = {
    "locationType": "LOCATION_INPUT",
    "zipCode": "00100",
    "deviceType": "web",
    "pageType": "Search"
}
session.post(glow_url, json=payload)

# Now check search page
r_search = session.get("https://www.amazon.it/s?k=monitor+pc+gaming+144hz")
print("Search page status:", r_search.status_code)
# Let's find prices around Dell or SE2426HG
for block in r_search.text.split("data-asin=\"B0GHQ2BPYJ\"")[1:2]:
    m = re.findall(r'<span class="a-offscreen">([^<]+)</span>', block[:2000])
    print("B0GHQ2BPYJ prices in search:", m)
