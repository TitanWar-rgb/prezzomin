import requests

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
})

# Warmup
r1 = session.get("https://www.amazon.it/")
print("Cookies after homepage:", session.cookies.get_dict())

# Set zip code to 00100 (Roma)
glow_url = "https://www.amazon.it/portal-migration/hz/glow/address-change?actionSource=glow"
payload = {
    "locationType": "LOCATION_INPUT",
    "zipCode": "00100",
    "deviceType": "web",
    "pageType": "Search"
}
r2 = session.post(glow_url, json=payload)
print("Glow status:", r2.status_code)
print("Cookies after glow:", session.cookies.get_dict())
