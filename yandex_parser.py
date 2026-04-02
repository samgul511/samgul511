import asyncio
import logging
import random
import re
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright_stealth import stealth_async

# ================== CONFIG ==================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("YandexParser")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]


# ================== HELPERS ==================

async def safe_goto(page: Page, url: str, retries: int = 3) -> bool:
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Retry %s | Ошибка загрузки %s | %s", attempt, url, exc)
            await asyncio.sleep(2)
    return False


async def human_delay(a: float = 1.0, b: float = 2.5) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def scroll_results(page: Page) -> None:
    """Надёжный скролл контейнера."""
    last_count = 0

    for _ in range(10):
        await page.evaluate(
            """
            const el = document.querySelector('.scroll__container');
            if (el) el.scrollTop = el.scrollHeight;
            """
        )
        await asyncio.sleep(2)

        cards = await page.query_selector_all(".search-snippet-view")
        if len(cards) == last_count:
            break

        last_count = len(cards)
        logger.info("📜 Карточек: %s", last_count)


async def extract_emails(page: Page) -> list[str]:
    emails: set[str] = set()

    try:
        content = await page.content()
        emails.update(EMAIL_REGEX.findall(content))

        mailtos = await page.eval_on_selector_all(
            "a[href^='mailto:']", "els => els.map(e => e.href)"
        )
        for mailto in mailtos:
            emails.add(mailto.replace("mailto:", "").strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось извлечь email: %s", exc)

    return sorted({email.lower() for email in emails if "@" in email})


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return url

    return None


async def fetch_emails_from_url(context: BrowserContext, url: str | None) -> list[str]:
    clean_url = normalize_url(url)
    if not clean_url:
        return []

    page = await context.new_page()

    try:
        if not await safe_goto(page, clean_url):
            return []

        await human_delay()
        emails = await extract_emails(page)

        if emails:
            logger.info("✅ Найдено email: %s", len(emails))

        return emails
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка сайта %s: %s", clean_url, str(exc)[:120])
        return []
    finally:
        await page.close()


# ================== MAIN ==================

async def parse_yandex_maps(query: str, limit: int = 10) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            ignore_https_errors=True,
        )

        page = await context.new_page()
        await stealth_async(page)

        url = f"https://yandex.ru/maps/213/moscow/search/{query}/"
        logger.info("🚀 Запрос: %s", query)

        if not await safe_goto(page, url):
            await browser.close()
            return []

        await asyncio.sleep(3)
        await scroll_results(page)

        cards = await page.query_selector_all(".search-snippet-view")
        logger.info("📊 Найдено карточек: %s", len(cards))

        for i, card in enumerate(cards[:limit], start=1):
            try:
                name_elem = await card.query_selector(".search-snippet-view__title-inline")
                name = (await name_elem.inner_text()).strip() if name_elem else "Неизвестно"

                await card.click()
                await page.wait_for_selector(".card-title-view__title", timeout=5000)
                await human_delay()

                phone_elem = await page.query_selector(".card-phones-view__number")
                phone = (await phone_elem.inner_text()).strip() if phone_elem else "Не указан"

                site_elem = await page.query_selector(".business-urls-view__link")
                site = await site_elem.get_attribute("href") if site_elem else None
                site = normalize_url(site)

                emails = await fetch_emails_from_url(context, site) if site else []

                results.append(
                    {
                        "name": name,
                        "phone": phone,
                        "site": site,
                        "emails": emails,
                    }
                )

                logger.info("✔ %s. %s", i, name)
                await human_delay()
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка карточки %s: %s", i, exc)

        await browser.close()

    return results


# ================== RUN ==================

if __name__ == "__main__":
    query = "стоматология москва"
    data = asyncio.run(parse_yandex_maps(query, limit=10))

    print("\n=== RESULT ===\n")
    for item in data:
        print(item)
