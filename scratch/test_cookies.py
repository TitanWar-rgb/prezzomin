import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Create context with Italian locale and timezone
        context = await browser.new_context(
            locale="it-IT",
            timezone_id="Europe/Rome",
            extra_http_headers={
                "Accept-Language": "it-IT,it;q=0.9",
            }
        )
        page = await context.new_page()
        await page.goto("https://www.amazon.it/", wait_until="domcontentloaded")
        cookies = await context.cookies()
        for c in cookies:
            print(f"Cookie: {c['name']} = {c['value']}")
        await browser.close()

asyncio.run(main())
