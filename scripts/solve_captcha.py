#!/usr/bin/env python3
"""Quick hCaptcha solver using Playwright inside pirate-dock."""
import asyncio, sys, json, time, os
from pathlib import Path

os.environ['DISPLAY'] = ':1'

async def main():
    from playwright.async_api import async_playwright
    
    md5 = "8e102d213b37052a57e6b06934038a04"
    url = f"https://annas-archive.gl/slow_download/{md5}/0/0"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,720"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="en-GB",
        )
        page = await context.new_page()
        
        print(f"Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        
        # Wait for hCaptcha iframe
        print("Looking for hCaptcha iframe...")
        try:
            frame = await page.wait_for_selector('iframe[src*="hcaptcha"]', timeout=15000)
            f = await frame.content_frame()
            if f:
                print("Found hCaptcha frame, looking for checkbox...")
                # Try to click the checkbox
                checkbox = await f.wait_for_selector('#checkbox, input[type=checkbox], .checkbox', timeout=10000)
                if checkbox:
                    print("Clicking checkbox...")
                    await checkbox.click()
                    await asyncio.sleep(5)
                    
                    # Check if puzzle appeared
                    await page.screenshot(path="/downloads/hcaptcha_result.png")
                    
                    # Check page title/url
                    title = await page.title()
                    print(f"Page title after click: {title}")
                    print(f"URL after click: {page.url}")
                    
                    # Check for visual puzzle
                    challenge_frame = await f.wait_for_selector('.task-grid, .challenge, [class*=challenge]', timeout=5000)
                    if challenge_frame:
                        print("VISUAL PUZZLE DETECTED - needs human")
                        print(json.dumps({"status": "puzzle"}))
                    else:
                        # Check if we're through
                        await asyncio.sleep(3)
                        new_title = await page.title()
                        if "DDoS" not in new_title and "Checking" not in new_title:
                            print("PASSED - no puzzle needed!")
                            print(f"Title: {new_title}")
                            print(f"URL: {page.url}")
                            # Extract download links
                            links = await page.evaluate("""() => JSON.stringify(
                                Array.from(document.querySelectorAll('a[href]'))
                                    .filter(a => /download|\.epub|\.pdf/i.test(a.textContent + a.href))
                                    .map(a => ({text: a.textContent.trim().substring(0,80), href: a.href}))
                            )""")
                            print(f"Download links: {links}")
                            print(json.dumps({"status": "success", "title": new_title}))
                        else:
                            print("Still on challenge page")
                            print(json.dumps({"status": "still_challenged", "title": new_title}))
                else:
                    print("No checkbox found")
                    print(json.dumps({"status": "no_checkbox"}))
            else:
                print("Could not get hCaptcha frame content")
                print(json.dumps({"status": "no_frame_content"}))
        except Exception as e:
            print(f"Error: {e}")
            await page.screenshot(path="/downloads/error.png")
            print(json.dumps({"status": "error", "message": str(e)}))
        
        # Keep browser open for inspection
        print("Keeping browser open for 30s...")
        await asyncio.sleep(30)
        await browser.close()

asyncio.run(main())
