
# IMPORTANT: Windows event-loop policy must be configured before
# importing or starting Playwright or Uvicorn.

import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsProactorEventLoopPolicy()
    )

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urljoin, urlparse, urlencode

from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


BASE_URL = "https://alfatah.pk"
MAX_SEARCH_PAGES = 20
MAX_CONCURRENT_PRODUCTS = 4

app = FastAPI(
    title="Al-Fatah Product Scraper",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# GENERAL HELPERS
# ---------------------------------------------------------

def clean_text(value):
    if value is None:
        return None

    value = BeautifulSoup(str(value), "html.parser").get_text(
        " ", strip=True
    )
    value = re.sub(r"\s+", " ", value).strip()

    return value or None


def clean_url(url):
    if not url:
        return None

    url = urljoin(BASE_URL, str(url).strip())
    parsed = urlparse(url)

    if parsed.netloc.lower() not in {
        "alfatah.pk",
        "www.alfatah.pk",
    }:
        return None

    path = parsed.path.rstrip("/")

    if not path:
        return None

    return f"{BASE_URL}{path}"


def price_to_integer(value, already_rupees=True):
    """
    Convert a real price to an integer.

    Visible prices and JSON-LD prices are in rupees.
    Shopify product JSON prices are often in rupees as strings,
    but integer values may represent minor units depending on source.
    Call with already_rupees=False only for confirmed paise values.
    """
    if value is None or isinstance(value, bool):
        return None

    try:
        if isinstance(value, str):
            value = value.replace(",", "")
            value = re.sub(r"(?i)\bPKR\b|\bRs\.?\b|₨", "", value)
            value = value.strip()

            if not value:
                return None

        number = Decimal(str(value))

        if not number.is_finite() or number < 0:
            return None

        if not already_rupees:
            number /= Decimal("100")

        return int(number.quantize(
            Decimal("1"),
            rounding=ROUND_DOWN,
        ))

    except (InvalidOperation, ValueError, TypeError):
        return None


def empty_product(url, title=None):
    return {
        "platform": "alfatah",
        "title": clean_text(title),
        "price": None,
        "currency": "PKR",
        "description": None,
        "image": None,
        "url": clean_url(url),
        "sku": None,
        "brand": None,
        "availability": None,
    }


def absolute_image_url(value):
    if not value:
        return None

    value = str(value).strip()

    if value.startswith("data:"):
        return None

    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/"):
        value = urljoin(BASE_URL, value)

    if value.startswith(("https://", "http://")):
        return value

    return None


def extract_image(soup):
    selectors = [
        'meta[property="og:image"]',
        'meta[name="twitter:image"]',
        'meta[itemprop="image"]',
    ]

    for selector in selectors:
        node = soup.select_one(selector)

        if node:
            image = absolute_image_url(node.get("content"))
            if image:
                return image

    selectors = [
        ".product__media img",
        ".product-single__media img",
        ".product__main-photos img",
        ".product-gallery img",
        "img[itemprop='image']",
        "main img",
    ]

    for selector in selectors:
        for node in soup.select(selector):
            value = (
                node.get("data-src")
                or node.get("data-original")
                or node.get("data-lazy-src")
                or node.get("src")
            )

            image = absolute_image_url(value)

            if image:
                return image

            srcset = node.get("srcset")

            if srcset:
                candidate = srcset.split(",")[0].strip().split(" ")[0]
                image = absolute_image_url(candidate)

                if image:
                    return image

    return None


# ---------------------------------------------------------
# PRODUCT JSON-LD
# ---------------------------------------------------------

def extract_json_ld(soup):
    output = []

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        entries = data if isinstance(data, list) else [data]

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            output.append(entry)

            graph = entry.get("@graph")

            if isinstance(graph, list):
                output.extend(
                    item for item in graph
                    if isinstance(item, dict)
                )

    return output


def find_product_schema(soup):
    for item in extract_json_ld(soup):
        item_type = item.get("@type", [])

        if isinstance(item_type, str):
            item_type = [item_type]

        if any(str(value).lower() == "product" for value in item_type):
            return item

    return {}


def extract_schema_price(schema):
    offers = schema.get("offers")

    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    if not isinstance(offers, dict):
        return None

    return price_to_integer(
        offers.get("price"),
        already_rupees=True,
    )


def extract_meta_price(soup):
    selectors = [
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
    ]

    for selector in selectors:
        node = soup.select_one(selector)

        if node and node.get("content"):
            value = price_to_integer(
                node["content"],
                already_rupees=True,
            )

            if value is not None:
                return value

    return None


# ---------------------------------------------------------
# SEARCH RESULTS AND PAGINATION
# ---------------------------------------------------------

def extract_search_products(html):
    soup = BeautifulSoup(html, "html.parser")
    products = {}

    card_selectors = [
        ".grid__item",
        "li.product",
        ".product-item",
        ".product-card",
        ".card-wrapper",
        ".product-grid-item",
        "[data-product-id]",
    ]

    for selector in card_selectors:
        for card in soup.select(selector):
            link = card.select_one('a[href*="/products/"]')

            if not link:
                continue

            url = clean_url(link.get("href"))

            if not url:
                continue

            title_node = card.select_one(
                ".card__heading, .product-title, "
                ".product-item__title, .full-unstyled-link, "
                "h2, h3, [class*='title']"
            ) or link

            title = clean_text(
                title_node.get("title")
                or title_node.get("aria-label")
                or title_node.get_text(" ", strip=True)
            )

            image_node = card.select_one("img")
            image = None

            if image_node:
                image = absolute_image_url(
                    image_node.get("data-src")
                    or image_node.get("data-original")
                    or image_node.get("src")
                )

            price_node = card.select_one(
                ".price-item--sale, .price-item--regular, "
                ".price, [class*='price']"
            )

            price = None

            if price_node:
                price = price_to_integer(
                    price_node.get_text(" ", strip=True),
                    already_rupees=True,
                )

            products[url] = {
                "url": url,
                "title": title,
                "image": image,
                "price": price,
            }

    # Also collect product links outside recognized card classes.
    for link in soup.select('a[href*="/products/"]'):
        url = clean_url(link.get("href"))

        if not url or url in products:
            continue

        title = clean_text(
            link.get("title")
            or link.get("aria-label")
            or link.get_text(" ", strip=True)
        )

        if not title:
            image_node = link.select_one("img")
            if image_node:
                title = clean_text(image_node.get("alt"))

        if title:
            products[url] = {
                "url": url,
                "title": title,
                "image": None,
                "price": None,
            }

    return list(products.values())


async def load_page(page, url):
    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=45000,
    )

    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=4000,
        )
    except PlaywrightTimeoutError:
        pass


async def scrape_search_pages(browser, keyword, limit):
    context = await browser.new_context(
        locale="en-PK",
        viewport={"width": 1365, "height": 900},
    )

    page = await context.new_page()
    found = {}
    empty_pages = 0

    try:
        for page_number in range(1, MAX_SEARCH_PAGES + 1):
            query = urlencode({
                "q": keyword,
                "type": "product",
                "page": page_number,
            })

            search_url = f"{BASE_URL}/search?{query}"

            try:
                await load_page(page, search_url)
                html = await page.content()
            except Exception:
                empty_pages += 1

                if empty_pages >= 2:
                    break

                continue

            items = extract_search_products(html)
            added = 0

            for item in items:
                if item["url"] not in found:
                    found[item["url"]] = item
                    added += 1

                if len(found) >= limit:
                    break

            if len(found) >= limit:
                break

            if added == 0:
                empty_pages += 1
            else:
                empty_pages = 0

            if empty_pages >= 2:
                break

    finally:
        await context.close()

    return list(found.values())[:limit]


# ---------------------------------------------------------
# DESCRIPTION EXTRACTION
# ---------------------------------------------------------

DESCRIPTION_XPATH = (
    "/html[1]/body[1]/main[1]/div[2]/div[1]/div[1]/div[2]/"
    "div[4]/div[1]/h2[1]/button[1]"
)


async def get_description(page):
    button = None

    # First try the exact XPath provided by the user.
    try:
        candidate = page.locator(
            f"xpath={DESCRIPTION_XPATH}"
        ).first

        if await candidate.count() > 0:
            button = candidate
    except Exception:
        pass

    # Fallback: find a Description button by its visible text.
    if button is None:
        try:
            buttons = page.locator("main button")

            for index in range(await buttons.count()):
                candidate = buttons.nth(index)

                try:
                    label = clean_text(await candidate.inner_text())

                    if label and re.fullmatch(
                        r"description\s*:?",
                        label,
                        flags=re.IGNORECASE,
                    ):
                        button = candidate
                        break
                except Exception:
                    continue
        except Exception:
            pass

    # Expand the accordion so its content can be read.
    if button is not None:
        try:
            await button.scroll_into_view_if_needed(timeout=3000)

            expanded = await button.get_attribute("aria-expanded")

            if expanded != "true":
                await button.click(timeout=5000)
                await page.wait_for_timeout(300)
        except Exception:
            pass

        # Read the accordion panel from the live DOM.
        try:
            text = await button.evaluate(
                """(button) => {
                    const textOf = el =>
                        (el?.innerText || el?.textContent || "")
                            .replace(/\\\\s+/g, " ")
                            .trim();

                    const useful = el => {
                        const text = textOf(el);
                        return text.length > 20 &&
                            !/^description:?$/i.test(text);
                    };

                    // Follow aria-controls and target references.
                    const refs = [
                        button.getAttribute("aria-controls"),
                        button.getAttribute("data-target"),
                        button.getAttribute("data-bs-target"),
                        button.getAttribute("href")
                    ].filter(Boolean);

                    for (let ref of refs) {
                        ref = ref.trim();

                        if (ref.startsWith("#")) {
                            ref = ref.slice(1);
                        }

                        const panel = document.getElementById(ref);

                        if (useful(panel)) {
                            return textOf(panel);
                        }
                    }

                    // Check elements immediately after the heading.
                    const heading = button.closest(
                        "h1,h2,h3,h4,h5,h6"
                    );

                    if (heading) {
                        let node = heading.nextElementSibling;

                        for (let i = 0; node && i < 5; i++) {
                            if (useful(node)) {
                                return textOf(node);
                            }

                            node = node.nextElementSibling;
                        }
                    }

                    // Search the accordion's content descendants.
                    const wrappers = [
                        button.closest("details"),
                        button.closest(".accordion"),
                        button.closest(".accordion-item"),
                        button.closest(".product__accordion"),
                        button.parentElement?.parentElement,
                        button.parentElement
                    ].filter(Boolean);

                    for (const wrapper of wrappers) {
                        const panels = wrapper.querySelectorAll(
                            "[role='region'], [id], " +
                            ".accordion__content, " +
                            ".accordion__content-inner, " +
                            ".rte, [class*='panel']"
                        );

                        for (const panel of panels) {
                            if (panel.contains(button)) continue;

                            if (useful(panel)) {
                                return textOf(panel);
                            }
                        }
                    }

                    return "";
                }"""
            )

            text = clean_text(text)

            if text and len(text) > 20:
                return text

        except Exception:
            pass

    # Standard Shopify/theme description selectors.
    selectors = [
        ".product__description",
        ".product-single__description",
        ".product-description",
        "[itemprop='description']",
        ".accordion__content .rte",
        ".accordion__content-inner",
        ".product__accordion .rte",
        ".product__info-container .rte",
        ".rte.product__description",
        "#description",
    ]

    for selector in selectors:
        try:
            nodes = page.locator(selector)

            for index in range(min(await nodes.count(), 5)):
                text = clean_text(
                    await nodes.nth(index).inner_text()
                )

                if text and len(text) > 20:
                    return text
        except Exception:
            continue

    # Final fallback: inspect HTML currently in the browser.
    try:
        soup = BeautifulSoup(await page.content(), "html.parser")

        for selector in selectors:
            node = soup.select_one(selector)

            if node:
                text = clean_text(node.get_text(" ", strip=True))

                if text and len(text) > 20:
                    return text
    except Exception:
        pass

    return None


# ---------------------------------------------------------
# PRODUCT DETAIL SCRAPER
# ---------------------------------------------------------

async def scrape_product(browser, item, semaphore):
    async with semaphore:
        url = clean_url(item.get("url"))

        if not url:
            return None

        context = await browser.new_context(
            locale="en-PK",
            viewport={"width": 1365, "height": 900},
        )

        page = await context.new_page()
        product = empty_product(url, item.get("title"))

        try:
            await load_page(page, url)

            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            schema = find_product_schema(soup)

            # Title
            h1 = soup.select_one("h1")

            product["title"] = clean_text(
                schema.get("name")
                or (h1.get_text(" ", strip=True) if h1 else None)
                or item.get("title")
            )

            # Price: schema and visible prices are rupee amounts.
            price = extract_schema_price(schema)

            if price is None:
                price = extract_meta_price(soup)

            if price is None:
                price = item.get("price")

            product["price"] = price

            # One primary image only.
            image = schema.get("image")

            if isinstance(image, list):
                image = image[0] if image else None

            if isinstance(image, dict):
                image = image.get("url") or image.get("contentUrl")

            product["image"] = (
                absolute_image_url(image)
                or extract_image(soup)
                or item.get("image")
            )

            # SKU
            product["sku"] = clean_text(schema.get("sku"))

            # Brand
            brand = schema.get("brand")

            if isinstance(brand, dict):
                brand = brand.get("name")

            product["brand"] = clean_text(brand)

            # Availability
            offers = schema.get("offers")

            if isinstance(offers, list):
                offers = offers[0] if offers else {}

            if isinstance(offers, dict):
                availability = offers.get("availability")

                if availability:
                    product["availability"] = availability.rsplit(
                        "/", 1
                    )[-1]

            # Description: read the accordion from the rendered DOM.
            product["description"] = await get_description(page)

            # If the description wasn't found in the DOM, check JSON-LD.
            if not product["description"]:
                product["description"] = clean_text(
                    schema.get("description")
                )

        except Exception as exc:
            product["_error"] = str(exc)

        finally:
            await context.close()

        return product


# ---------------------------------------------------------
# API ROUTES
# ---------------------------------------------------------

@app.get("/")
async def home():
    return {
        "name": "Al-Fatah Product Scraper",
        "status": "running",
        "endpoints": {
            "search": "/api/search?keyword=surf&limit=20",
            "product": (
                "/api/product?handle="
                "lattafa-khamrah-qahwa-for-unisex-edp-100ml"
            ),
        },
    }


@app.get("/api/search")
async def search_products(
    keyword: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
):
    keyword = keyword.strip()

    if not keyword:
        return {
            "keyword": keyword,
            "requested_limit": limit,
            "count": 0,
            "products": [],
        }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        try:
            items = await scrape_search_pages(
                browser,
                keyword,
                limit,
            )

            semaphore = asyncio.Semaphore(
                MAX_CONCURRENT_PRODUCTS
            )

            tasks = [
                scrape_product(browser, item, semaphore)
                for item in items
            ]

            results = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

            products = []

            for result in results:
                if isinstance(result, Exception) or not result:
                    continue

                result.pop("_error", None)
                products.append(result)

            return {
                "keyword": keyword,
                "requested_limit": limit,
                "count": len(products),
                "products": products,
            }

        finally:
            await browser.close()


@app.get("/api/product")
async def get_product(
    url: str = Query(None),
    handle: str = Query(None),
):
    if not url and handle:
        url = f"{BASE_URL}/products/{handle.strip('/')}"

    if not url:
        return {
            "error": "Provide either url or handle.",
        }

    url = clean_url(url)

    if not url:
        return {
            "error": "Invalid URL. Use an alfatah.pk product URL.",
        }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        try:
            product = await scrape_product(
                browser,
                {"url": url},
                asyncio.Semaphore(1),
            )

            if not product:
                return {"error": "Could not scrape this product."}

            product.pop("_error", None)
            return product

        finally:
            await browser.close()


if __name__ == "__main__":
    import uvicorn

    # Running Uvicorn in this process ensures the Windows event-loop
    # policy configured at the top of this file is applied.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False,
        workers=1,
    )
