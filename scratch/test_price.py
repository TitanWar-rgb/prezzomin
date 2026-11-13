import requests
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}

for asin in ["B0D4QVTZ5Y", "B0GHQ2BPYJ", "B0FXXJGTFF"]:
    r = requests.get(f"https://www.amazon.it/dp/{asin}", headers=headers, timeout=10)
    print(f"=== ASIN: {asin} ===")
    
    # Title
    m_title = re.search(r'<span id="productTitle"[^>]*>(.*?)</span>', r.text, re.DOTALL)
    title = m_title.group(1).strip() if m_title else "N/A"
    print("Title:", title[:60])
    
    # Core price
    m_core = re.search(r'id="corePriceDisplay_desktop_feature_div".*?<span class="a-offscreen">([^<]+)</span>', r.text, re.DOTALL)
    if m_core:
        print("Core price:", m_core.group(1).strip())
        
    m_core2 = re.search(r'id="corePrice_desktop".*?<span class="a-offscreen">([^<]+)</span>', r.text, re.DOTALL)
    if m_core2:
        print("Core price 2:", m_core2.group(1).strip())

    # All offscreen prices in top 30000 chars of body
    offscreen = re.findall(r'<span class="a-offscreen">([^<]+)</span>', r.text)
    print("Top offscreen prices:", offscreen[:5])
