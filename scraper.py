import asyncio
import logging

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

HTX_URL = "https://www.htx.com/en-us/financial/earn/home?activeTab=new"

# Multiple selector strategies for "View More" button
VIEW_MORE_SELECTORS = [
    'text="View More"',
    'text="Load More"',
    '[class*="loadMore"]',
    '[class*="load-more"]',
    '[class*="view-more"]',
    'button:has-text("More")',
]

# Selector strategies for earn items
ITEM_SELECTORS = [
    '[class*="earn-item"]',
    '[class*="product-item"]',
    '[class*="earnItem"]',
    '[class*="productItem"]',
    '[class*="staking-item"]',
]


class ScraperError(Exception):
    pass


async def _find_and_click_view_more(page) -> bool:
    """Try to find and click the View More button. Returns True if clicked."""
    for sel in VIEW_MORE_SELECTORS:
        try:
            btn = page.locator(sel).first
            count = await btn.count()
            if count == 0:
                continue
            await btn.scroll_into_view_if_needed(timeout=3000)
            await asyncio.sleep(0.5)
            await btn.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


async def _get_page_height(page) -> int:
    return await page.evaluate("document.body.scrollHeight")


async def _scroll_to_bottom(page):
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(1)


async def _expand_once(page, max_attempts: int = 20) -> None:
    """Click View More once and confirm page height increased."""
    prev_height = await _get_page_height(page)

    for attempt in range(max_attempts):
        logger.info("Expand attempt %d, prev_height=%d", attempt + 1, prev_height)
        await _scroll_to_bottom(page)

        clicked = await _find_and_click_view_more(page)
        if not clicked:
            logger.warning("View More button not found on attempt %d", attempt + 1)
            await asyncio.sleep(3)
            continue

        # Wait and check height every second for 10 seconds
        for _ in range(10):
            await asyncio.sleep(1)
            new_height = await _get_page_height(page)
            if new_height > prev_height:
                logger.info("Page expanded: %d → %d", prev_height, new_height)
                return

        logger.warning("Height unchanged after click, attempt %d", attempt + 1)

    raise ScraperError(f"Не удалось расширить страницу за {max_attempts} попыток")


async def _extract_fixed_rows(page) -> list[dict]:
    """Extract Fixed-type rows from the DOM."""
    rows = []

    # Try each item selector
    items = []
    for sel in ITEM_SELECTORS:
        try:
            found = await page.query_selector_all(sel)
            if found:
                items = found
                logger.info("Found %d items with selector: %s", len(found), sel)
                break
        except Exception:
            continue

    if not items:
        # Fallback: try to find any elements containing "Fixed"
        logger.warning("No items found with standard selectors, using fallback")
        items = await page.query_selector_all('[class*="item"], [class*="row"], [class*="card"]')

    for item in items:
        try:
            item_text = await item.inner_text()

            # Must contain "Fixed" but exclude pure "Flexible" or "Flexible/Fixed"
            if "Fixed" not in item_text:
                continue
            # Skip items that are Flexible/Fixed (both options) — user wants Fixed only
            if "Flexible/Fixed" in item_text or ("Flexible" in item_text and item_text.count("Fixed") == 1 and "Flexible" in item_text):
                continue

            token = await _extract_text(item, [
                '[class*="coin-name"]', '[class*="coinName"]',
                '[class*="token-name"]', '[class*="tokenName"]',
                '[class*="currency"]', '[class*="symbol"]',
                'h3', 'h4',
            ])
            # Clean up token name (remove newlines, extra whitespace)
            if token and token != "?":
                token = " ".join(token.split())

            apy = await _extract_text(item, [
                '[class*="apy"]', '[class*="APY"]', '[class*="rate"]',
                '[class*="yield"]', '[class*="interest"]', '[class*="profit"]',
                '[class*="percent"]',
            ])
            term = await _extract_text(item, [
                '[class*="duration"]', '[class*="period"]',
                '[class*="day"]', '[class*="days"]',
                '[class*="term"]', '[class*="lock"]',
            ])

            if token and token != "?":
                rows.append({"token": token, "apy": apy or "?", "term": term or "?"})
        except Exception as e:
            logger.debug("Error parsing item: %s", e)
            continue

    return rows


async def _extract_text(element, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await element.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return "?"


async def run_scrape() -> tuple[bytes, list[dict]]:
    """
    Main scrape function.
    Returns (screenshot_bytes, fixed_rows).
    Raises ScraperError on failure.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            java_script_enabled=True,
        )

        # Remove automation fingerprint
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        page = await context.new_page()

        try:
            logger.info("Opening HTX Earn page...")
            await page.goto(HTX_URL, wait_until="domcontentloaded", timeout=60000)

            # Extra wait for JS rendering
            await asyncio.sleep(5)

            # Try networkidle with timeout fallback
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                logger.warning("networkidle timeout, proceeding anyway")

            # Expand page twice (2 View More clicks)
            logger.info("Expanding page (click 1)...")
            await _expand_once(page)

            logger.info("Expanding page (click 2)...")
            await _expand_once(page)

            # Scroll to bottom before screenshot
            await _scroll_to_bottom(page)
            await asyncio.sleep(2)

            logger.info("Taking full-page screenshot...")
            screenshot = await page.screenshot(full_page=True, type="png")

            logger.info("Extracting Fixed rows from DOM...")
            rows = await _extract_fixed_rows(page)
            logger.info("Extracted %d Fixed rows", len(rows))

        finally:
            await browser.close()

    return screenshot, rows
