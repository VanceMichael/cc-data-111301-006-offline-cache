from pathlib import Path
import json
from playwright.sync_api import sync_playwright
from .http_source import source_server


def smoke():
    with source_server('fixtures') as (address, state), sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page()
            page.goto(address, wait_until='domcontentloaded')
            page.get_by_text('资料就绪', exact=True).wait_for(timeout=5000)
            databases = page.evaluate('async () => await indexedDB.databases()')
            page.locator('#append').click()
            page.get_by_text('走访已保存', exact=True).wait_for(timeout=5000)
            return {'title':page.title(),'databases':databases,'状态':page.locator('#status').inner_text()}
        finally:
            browser.close()


if __name__ == '__main__':
    print(json.dumps(smoke(), ensure_ascii=False, indent=2))
