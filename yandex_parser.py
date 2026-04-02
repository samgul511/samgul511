import argparse
import asyncio
import importlib
import importlib.util
import json
import logging
import random
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

# ================== CONFIG ==================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("YandexParser")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]

_ps_spec = importlib.util.find_spec("playwright_stealth")
_playwright_stealth = importlib.import_module("playwright_stealth") if _ps_spec else None


@dataclass
class CompanyCard:
    name: str
    phone: str
    site: str | None
    emails: list[str]


# ================== HELPERS ==================

async def apply_stealth(page: Page) -> None:
    """Применяет stealth, если пакет доступен и поддерживает API текущей версии."""
    if _playwright_stealth is None:
        logger.warning("playwright_stealth не установлен. Продолжаем без stealth.")
        return

    stealth_async = getattr(_playwright_stealth, "stealth_async", None)
    if callable(stealth_async):
        await stealth_async(page)
        return

    stealth_sync = getattr(_playwright_stealth, "stealth", None)
    if callable(stealth_sync):
        stealth_sync(page)
        return

    logger.warning("В playwright_stealth не найдено stealth_async/stealth. Продолжаем без stealth.")


async def safe_goto(page: Page, url: str, retries: int = 3) -> bool:
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Retry %s/%s | Ошибка загрузки %s | %s", attempt, retries, url, exc)
            await asyncio.sleep(2)
    return False


async def human_delay(a: float = 0.8, b: float = 2.2) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def scroll_results(page: Page, max_rounds: int = 12) -> None:
    """Плавно скроллит список результатов до стабилизации количества карточек."""
    last_count = 0

    for _ in range(max_rounds):
        await page.evaluate(
            """
            const el = document.querySelector('.scroll__container');
            if (el) {
              el.scrollTop = el.scrollHeight;
            } else {
              window.scrollTo(0, document.body.scrollHeight);
            }
            """
        )
        await asyncio.sleep(1.6)

        cards = await page.query_selector_all(".search-snippet-view")
        current = len(cards)
        logger.info("📜 Карточек: %s", current)

        if current == last_count:
            break
        last_count = current


async def extract_emails(page: Page) -> list[str]:
    emails: set[str] = set()

    try:
        content = await page.content()
        emails.update(EMAIL_REGEX.findall(content))

        mailtos = await page.eval_on_selector_all(
            "a[href^='mailto:']", "els => els.map(e => e.href)"
        )
        for m in mailtos:
            emails.add(m.replace("mailto:", "").strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось извлечь email: %s", exc)

    return sorted({email.lower() for email in emails if "@" in email})


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url.strip()

    return None


async def fetch_emails_from_url(context: BrowserContext, url: str | None) -> list[str]:
    clean_url = normalize_url(url)
    if not clean_url:
        return []

    page = await context.new_page()
    try:
        if not await safe_goto(page, clean_url, retries=2):
            return []

        await human_delay()
        found = await extract_emails(page)
        if found:
            logger.info("✅ Найдено email на %s: %s", clean_url, len(found))
        return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка обработки сайта %s: %s", clean_url, str(exc)[:160])
        return []
    finally:
        await page.close()


# ================== MAIN ==================

async def parse_yandex_maps(query: str, limit: int = 10, city_id: int = 213) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    encoded_query = quote(query)
    search_url = f"https://yandex.ru/maps/{city_id}/search/{encoded_query}/"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            ignore_https_errors=True,
            locale="ru-RU",
        )

        page = await context.new_page()
        await apply_stealth(page)

        logger.info("🚀 Запрос: %s", query)
        if not await safe_goto(page, search_url):
            await browser.close()
            return []

        await asyncio.sleep(2.5)
        await scroll_results(page)

        cards = await page.query_selector_all(".search-snippet-view")
        logger.info("📊 Найдено карточек: %s", len(cards))

        for i, card in enumerate(cards[:limit], start=1):
            try:
                name_el = await card.query_selector(".search-snippet-view__title-inline")
                name = (await name_el.inner_text()).strip() if name_el else "Неизвестно"

                await card.click()
                await page.wait_for_selector(".card-title-view__title", timeout=7000)
                await human_delay(0.7, 1.4)

                phone_el = await page.query_selector(".card-phones-view__number")
                phone = (await phone_el.inner_text()).strip() if phone_el else "Не указан"

                site_el = await page.query_selector(".business-urls-view__link")
                site = await site_el.get_attribute("href") if site_el else None
                site = normalize_url(site)

                emails = await fetch_emails_from_url(context, site) if site else []

                row = CompanyCard(name=name, phone=phone, site=site, emails=emails)
                results.append(asdict(row))

                logger.info("✔ %s/%s %s", i, min(limit, len(cards)), name)
                await human_delay(0.6, 1.2)
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка карточки %s: %s", i, exc)

        await browser.close()

    return results


# ================== CLI ==================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Парсер организаций из Yandex Maps")
    parser.add_argument("--query", default="стоматология москва", help="Поисковый запрос")
    parser.add_argument("--limit", type=int, default=10, help="Сколько карточек обработать")
    parser.add_argument("--city-id", type=int, default=213, help="ID города в Yandex Maps")
    parser.add_argument("--output", default="", help="Путь для JSON (опционально)")
    return parser


async def _main() -> None:
    args = build_parser().parse_args()
    data = await parse_yandex_maps(args.query, limit=args.limit, city_id=args.city_id)

    print("\n=== RESULT ===\n")
    for item in data:
        print(item)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("💾 Сохранено: %s", args.output)


if __name__ == "__main__":
    asyncio.run(_main())
