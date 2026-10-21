# ============================================================
# DARAZ PRODUCT SEARCH API
# FastAPI + Playwright
#
# Windows-safe synchronous Playwright version
#
# Search:
# GET /api/search?keyword=iphone+17+pro+max&limit=20
#
# v4.1.0: adds customer reviews (author, rating, stars, date,
#         text, images); removes referral_code, official_store,
#         daraz_choice.
# ============================================================

import json
import re
import sys
from typing import Optional, Any
from urllib.parse import quote, urljoin

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    title="Daraz Product Search API",
    description="Search and enrich Daraz products using Playwright",
    version="4.1.0",
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# CONSTANTS
# ============================================================

DARAZ_BASE_URL = "https://www.daraz.pk"

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

BROWSER_TIMEOUT = 60000
DETAIL_TIMEOUT = 30000

# Set this to False if you only want search-page scraping.
ENRICH_DETAILS = True

# Reviews are collected on each product's detail page
# (only works when ENRICH_DETAILS is True).
ENRICH_REVIEWS = True
MAX_REVIEWS = 50          # max reviews per product
REVIEW_PAGE_SIZE = 5      # reviews per API request
MAX_REVIEW_PAGES = 20     # safety cap on API pages per product


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    value = re.sub(r"\s+", " ", str(value))
    value = value.strip()

    return value if value else None


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""

    value = str(value).lower()

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


# ============================================================
# NUMBER HELPERS
# ============================================================

def parse_number(value: Any):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return value

    value = str(value)

    value = value.replace(",", "")
    value = value.replace("₨", "")
    value = value.replace("Rs.", "")
    value = value.replace("Rs", "")
    value = value.strip()

    match = re.search(
        r"\d+(?:\.\d+)?",
        value,
    )

    if not match:
        return None

    try:
        number = match.group(0)

        if "." in number:
            return float(number)

        return int(number)

    except (ValueError, TypeError):
        return None


def parse_review_count(value: Any):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return int(value)

    value = str(value).lower().strip()

    # Examples:
    # 1,234
    # 1.2k
    # 2m
    match = re.search(
        r"([\d,.]+)\s*([km])?",
        value,
        re.IGNORECASE,
    )

    if not match:
        return None

    try:
        number = float(
            match.group(1).replace(",", "")
        )

        multiplier = match.group(2)

        if multiplier == "k":
            number *= 1000

        elif multiplier == "m":
            number *= 1000000

        return int(number)

    except (ValueError, TypeError):
        return None


def parse_rating(value: Any):
    if value is None:
        return None

    value = str(value)

    match = re.search(
        r"(?<!\d)([0-5](?:\.\d+)?)(?!\d)",
        value,
    )

    if not match:
        return None

    try:
        rating = float(match.group(1))

        if 0 <= rating <= 5:
            return rating

    except ValueError:
        pass

    return None


# ============================================================
# URL HELPERS
# ============================================================

def normalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    url = url.strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return urljoin(
            DARAZ_BASE_URL,
            url,
        )

    return url


def extract_product_id(
    url: Optional[str],
) -> Optional[str]:

    if not url:
        return None

    patterns = [
        r"-i(\d+)-",
        r"-i(\d+)\.html",
        r"/i(\d+)\.html",
        r"i(\d+)\.html",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            url,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

    return None


# ============================================================
# GENERIC LOCATOR HELPERS
# ============================================================

def get_first_text(
    card,
    selectors,
    minimum_length=1,
):
    for selector in selectors:

        try:
            locator = card.locator(selector)

            count = locator.count()

            if count == 0:
                continue

            for i in range(min(count, 10)):

                try:
                    text = clean_text(
                        locator.nth(i).inner_text(
                            timeout=2500
                        )
                    )

                    if (
                        text
                        and len(text) >= minimum_length
                    ):
                        return text

                except Exception:
                    continue

        except Exception:
            continue

    return None


def get_first_attribute(
    locator,
    attributes,
):
    for attribute in attributes:
        try:
            value = locator.get_attribute(attribute)

            if value:
                value = clean_text(value)

                if value:
                    return value

        except Exception:
            continue

    return None


# ============================================================
# SEARCH PAGE - TITLE
# ============================================================

def extract_title(card):

    selectors = [
        '[data-qa-locator="product-title"]',
        '[data-qa-locator="product-item-title"]',
        '[class*="title"]',
        '[class*="Title"]',
        "h1",
        "h2",
        "h3",
        "h4",
    ]

    title = get_first_text(
        card,
        selectors,
        minimum_length=5,
    )

    if title:
        return title

    try:
        links = card.locator("a")

        count = links.count()

        for i in range(min(count, 15)):

            text = clean_text(
                links.nth(i).inner_text(
                    timeout=2000
                )
            )

            if text and len(text) >= 10:
                return text

    except Exception:
        pass

    return None


# ============================================================
# SEARCH PAGE - PRODUCT URL
# ============================================================

def extract_product_url(card):

    try:
        links = card.locator("a")

        count = links.count()

        for i in range(count):

            try:
                href = links.nth(i).get_attribute("href")

                href = normalize_url(href)

                if not href:
                    continue

                if "/products/" in href:
                    return href

            except Exception:
                continue

    except Exception:
        pass

    return None


# ============================================================
# SEARCH PAGE - IMAGE
# ============================================================

def extract_image(card):

    try:
        images = card.locator("img")

        count = images.count()

        for i in range(count):

            image = images.nth(i)

            value = get_first_attribute(
                image,
                [
                    "src",
                    "data-src",
                    "data-original",
                    "data-lazy-src",
                    "data-ks-lazyload",
                ],
            )

            if value:
                return normalize_url(value)

    except Exception:
        pass

    return None


# ============================================================
# SEARCH PAGE - PRICE
# ============================================================

def extract_price(card):

    selectors = [
        '[data-qa-locator="product-price"]',
        '[class*="price"]',
        '[class*="Price"]',
    ]

    for selector in selectors:

        try:
            locator = card.locator(selector)

            count = locator.count()

            for i in range(min(count, 10)):

                try:
                    text = clean_text(
                        locator.nth(i).inner_text(
                            timeout=2000
                        )
                    )

                    if not text:
                        continue

                    if (
                        "Rs" in text
                        or "₨" in text
                    ):
                        value = parse_number(text)

                        if value is not None:
                            return value

                except Exception:
                    continue

        except Exception:
            continue

    try:
        text = clean_text(
            card.inner_text()
        )

        if text:

            match = re.search(
                r"(?:Rs\.?|₨)\s*([\d,]+(?:\.\d+)?)",
                text,
                re.IGNORECASE,
            )

            if match:
                return parse_number(
                    match.group(1)
                )

    except Exception:
        pass

    return None


# ============================================================
# SEARCH PAGE - ORIGINAL PRICE
# ============================================================

def extract_original_price(card):

    selectors = [
        '[data-qa-locator="product-original-price"]',
        "del",
        "s",
        '[class*="origin"]',
        '[class*="Origin"]',
        '[class*="original"]',
        '[class*="Original"]',
    ]

    for selector in selectors:

        try:
            locator = card.locator(selector)

            count = locator.count()

            for i in range(min(count, 10)):

                try:
                    text = clean_text(
                        locator.nth(i).inner_text(
                            timeout=2000
                        )
                    )

                    value = parse_number(text)

                    if value:
                        return value

                except Exception:
                    continue

        except Exception:
            continue

    return None


# ============================================================
# SEARCH PAGE - DISCOUNT
# ============================================================

def extract_discount(card):

    selectors = [
        '[data-qa-locator="product-discount"]',
        '[class*="discount"]',
        '[class*="Discount"]',
    ]

    for selector in selectors:

        try:
            locator = card.locator(selector)

            count = locator.count()

            for i in range(min(count, 10)):

                try:
                    text = clean_text(
                        locator.nth(i).inner_text(
                            timeout=2000
                        )
                    )

                    if not text:
                        continue

                    match = re.search(
                        r"-?\s*\d+\s*%",
                        text,
                    )

                    if match:
                        return match.group(0)

                except Exception:
                    continue

        except Exception:
            continue

    return None


# ============================================================
# SEARCH PAGE - RATING
# ============================================================

def extract_rating(card):

    selectors = [
        '[data-qa-locator="product-rating"]',
        '[class*="rating"]',
        '[class*="Rating"]',
    ]

    for selector in selectors:

        try:
            locator = card.locator(selector)

            count = locator.count()

            for i in range(min(count, 10)):

                try:
                    text = clean_text(
                        locator.nth(i).inner_text(
                            timeout=2000
                        )
                    )

                    rating = parse_rating(text)

                    if rating is not None:
                        return rating

                except Exception:
                    continue

        except Exception:
            continue

    return None


# ============================================================
# SEARCH PAGE - REVIEW COUNT
# ============================================================

def extract_review_count(card):

    try:
        text = clean_text(
            card.inner_text()
        )

        if not text:
            return None

        patterns = [
            r"([\d,.]+\s*[km]?)\s*Ratings?",
            r"([\d,.]+\s*[km]?)\s*Reviews?",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                text,
                re.IGNORECASE,
            )

            if match:
                return parse_review_count(
                    match.group(1)
                )

    except Exception:
        pass

    return None


# ============================================================
# SEARCH PAGE - SELLER
# ============================================================

def extract_seller(card):

    selectors = [
        '[data-qa-locator="shop-name"]',
        '[class*="shop-name"]',
        '[class*="shopName"]',
        '[class*="seller"]',
        '[class*="Seller"]',
        '[class*="store"]',
        '[class*="Store"]',
    ]

    return get_first_text(
        card,
        selectors,
        minimum_length=2,
    )


# ============================================================
# SEARCH PAGE - BRAND
# ============================================================

def extract_brand(card):

    selectors = [
        '[data-qa-locator="product-brand"]',
        '[class*="brand"]',
        '[class*="Brand"]',
    ]

    return get_first_text(
        card,
        selectors,
        minimum_length=2,
    )


# ============================================================
# SEARCH PAGE - SHIPPING
# ============================================================

def extract_shipping(card):

    try:
        text = clean_text(
            card.inner_text()
        )

        if not text:
            return None

        patterns = [
            r"Free Shipping",
            r"Free Delivery",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                text,
                re.IGNORECASE,
            )

            if match:
                return clean_text(
                    match.group(0)
                )

    except Exception:
        pass

    return None


# ============================================================
# SEARCH PAGE - DELIVERY
# ============================================================

def extract_delivery(card):

    try:
        text = clean_text(
            card.inner_text()
        )

        if not text:
            return None

        patterns = [
            r"Delivery.{0,100}",
            r"Deliver.{0,100}",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                text,
                re.IGNORECASE,
            )

            if match:

                result = clean_text(
                    match.group(0)
                )

                if (
                    result
                    and len(result) < 150
                ):
                    return result

    except Exception:
        pass

    return None


# ============================================================
# SEARCH PAGE - STOCK
# ============================================================

def extract_stock(
    card,
    price,
):

    try:

        text = normalize_text(
            card.inner_text()
        )

        out_of_stock_phrases = [
            "out of stock",
            "sold out",
            "currently unavailable",
            "unavailable",
        ]

        for phrase in out_of_stock_phrases:

            if phrase in text:

                return {
                    "availability": "Out of Stock",
                    "in_stock": False,
                }

        if price is not None:

            return {
                "availability": "In Stock",
                "in_stock": True,
            }

    except Exception:
        pass

    return {
        "availability": "Unknown",
        "in_stock": None,
    }


# ============================================================
# SEARCH PAGE PRODUCT
# ============================================================

def extract_search_product(
    card,
    keyword,
):

    title = extract_title(card)

    if not title:
        return None

    product_url = extract_product_url(card)

    image = extract_image(card)

    price = extract_price(card)

    original_price = extract_original_price(card)

    discount = extract_discount(card)

    rating = extract_rating(card)

    review_count = extract_review_count(card)

    seller = extract_seller(card)

    brand = extract_brand(card)

    shipping = extract_shipping(card)

    delivery = extract_delivery(card)

    stock = extract_stock(
        card,
        price,
    )

    product_id = extract_product_id(
        product_url
    )

    return {
        "title": title,
        "price": price,
        "original_price": original_price,
        "discount": discount,
        "currency": "PKR",
        "availability": stock["availability"],
        "in_stock": stock["in_stock"],
        "seller": seller,
        "seller_rating": None,
        "rating": rating,
        "review_count": review_count,
        "image": image,
        "product_url": product_url,
        "product_id": product_id,
        "brand": brand,
        "shipping": shipping,
        "delivery": delivery,
        "reviews": [],
        "_relevance": calculate_relevance(
            keyword,
            title,
        ),
    }


# ============================================================
# RELEVANCE
# ============================================================

def calculate_relevance(
    keyword: str,
    title: Optional[str],
) -> float:

    keyword_normalized = normalize_text(
        keyword
    )

    title_normalized = normalize_text(
        title
    )

    if not keyword_normalized:
        return 0.0

    if not title_normalized:
        return 0.0

    if keyword_normalized == title_normalized:
        return 2.0

    score = 0.0

    keyword_words = keyword_normalized.split()
    title_words = title_normalized.split()

    if not keyword_words:
        return 0.0

    for word in keyword_words:

        if word in title_words:
            score += 1.0

        elif any(
            word in title_word
            for title_word in title_words
        ):
            score += 0.5

    score /= len(keyword_words)

    if keyword_normalized in title_normalized:
        score += 0.5

    return round(
        min(score, 2.0),
        4,
    )


# ============================================================
# DETAIL PAGE HELPERS
# ============================================================

def safe_body_text(page) -> str:

    try:
        return clean_text(
            page.locator("body").inner_text(
                timeout=5000
            )
        ) or ""

    except Exception:
        return ""


def extract_meta_content(
    page,
    selectors,
):

    for selector in selectors:

        try:
            locator = page.locator(selector)

            count = locator.count()

            for i in range(min(count, 10)):

                value = locator.nth(i).get_attribute(
                    "content"
                )

                if value:
                    return clean_text(value)

        except Exception:
            continue

    return None


# ============================================================
# JSON-LD
# ============================================================

def get_json_ld_objects(page):

    objects = []

    try:

        scripts = page.locator(
            'script[type="application/ld+json"]'
        )

        count = scripts.count()

        for i in range(count):

            try:

                raw = scripts.nth(i).inner_text(
                    timeout=2000
                )

                if not raw:
                    continue

                parsed = json.loads(raw)

                if isinstance(parsed, list):
                    objects.extend(parsed)

                else:
                    objects.append(parsed)

            except Exception:
                continue

    except Exception:
        pass

    return objects


def find_product_jsonld(
    objects,
):

    for obj in objects:

        if not isinstance(obj, dict):
            continue

        obj_type = obj.get("@type")

        if obj_type == "Product":
            return obj

        if isinstance(obj_type, list):
            if "Product" in obj_type:
                return obj

    return None


# ============================================================
# EXTRACT DETAIL JSON-LD
# ============================================================

def extract_jsonld_data(page):

    result = {}

    objects = get_json_ld_objects(page)

    product = find_product_jsonld(objects)

    if not product:
        return result

    # --------------------------------------------------------
    # Name
    # --------------------------------------------------------

    if product.get("name"):
        result["title"] = clean_text(
            product.get("name")
        )

    # --------------------------------------------------------
    # Brand
    # --------------------------------------------------------

    brand = product.get("brand")

    if isinstance(brand, dict):
        brand = brand.get("name")

    if brand:
        result["brand"] = clean_text(
            brand
        )

    # --------------------------------------------------------
    # Image
    # --------------------------------------------------------

    image = product.get("image")

    if isinstance(image, list):
        image = image[0] if image else None

    if image:
        result["image"] = normalize_url(
            image
        )

    # --------------------------------------------------------
    # Rating
    # --------------------------------------------------------

    aggregate = product.get(
        "aggregateRating"
    )

    if isinstance(aggregate, dict):

        rating = aggregate.get(
            "ratingValue"
        )

        review_count = aggregate.get(
            "reviewCount"
        )

        if rating is not None:
            result["rating"] = parse_rating(
                rating
            )

        if review_count is not None:
            result["review_count"] = parse_review_count(
                review_count
            )

    # --------------------------------------------------------
    # Offers
    # --------------------------------------------------------

    offers = product.get("offers")

    if isinstance(offers, list):
        offers = offers[0] if offers else None

    if isinstance(offers, dict):

        price = offers.get("price")

        currency = offers.get(
            "priceCurrency"
        )

        availability = offers.get(
            "availability"
        )

        if price is not None:
            result["price"] = parse_number(
                price
            )

        if currency:
            result["currency"] = currency

        if availability:

            if "OutOfStock" in availability:
                result["availability"] = "Out of Stock"
                result["in_stock"] = False

            elif "InStock" in availability:
                result["availability"] = "In Stock"
                result["in_stock"] = True

    return result


# ============================================================
# DETAIL PAGE - PRICE
# ============================================================

def detail_price(page):

    selectors = [
        '[data-qa-locator="product-price"]',
        '[class*="pdp-price"]',
        '[class*="price"]',
        '[class*="Price"]',
    ]

    for selector in selectors:

        try:

            locator = page.locator(selector)

            count = locator.count()

            for i in range(min(count, 15)):

                text = clean_text(
                    locator.nth(i).inner_text(
                        timeout=1500
                    )
                )

                if not text:
                    continue

                value = parse_number(text)

                if value is not None:
                    return value

        except Exception:
            continue

    return None


# ============================================================
# DETAIL PAGE - ORIGINAL PRICE
# ============================================================

def detail_original_price(page):

    selectors = [
        "del",
        "s",
        '[class*="original"]',
        '[class*="Original"]',
        '[class*="origin"]',
    ]

    for selector in selectors:

        try:

            locator = page.locator(selector)

            count = locator.count()

            for i in range(min(count, 10)):

                text = clean_text(
                    locator.nth(i).inner_text(
                        timeout=1500
                    )
                )

                value = parse_number(text)

                if value:
                    return value

        except Exception:
            continue

    return None


# ============================================================
# DETAIL PAGE - RATING
# ============================================================

def detail_rating(page):

    selectors = [
        '[data-qa-locator*="rating"]',
        '[class*="rating"]',
        '[class*="Rating"]',
    ]

    for selector in selectors:

        try:

            locator = page.locator(selector)

            count = locator.count()

            for i in range(min(count, 15)):

                text = clean_text(
                    locator.nth(i).inner_text(
                        timeout=1500
                    )
                )

                if not text:
                    continue

                rating = parse_rating(text)

                if rating is not None:
                    return rating

        except Exception:
            continue

    return None


# ============================================================
# DETAIL PAGE - REVIEW COUNT
# ============================================================

def detail_review_count(page):

    text = safe_body_text(page)

    if not text:
        return None

    patterns = [
        r"([\d,.]+\s*[km]?)\s*Ratings?",
        r"([\d,.]+\s*[km]?)\s*Reviews?",
        r"([\d,.]+\s*[km]?)\s*ratings?",
        r"([\d,.]+\s*[km]?)\s*reviews?",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:

            return parse_review_count(
                match.group(1)
            )

    return None


# ============================================================
# DETAIL PAGE - SELLER
# ============================================================

def detail_seller(page):

    selectors = [
        '[data-qa-locator="shop-name"]',
        '[data-qa-locator*="shop"]',
        '[class*="shop-name"]',
        '[class*="shopName"]',
        '[class*="seller-name"]',
        '[class*="sellerName"]',
        '[class*="seller"]',
        '[class*="Seller"]',
    ]

    value = get_first_text(
        page,
        selectors,
        minimum_length=2,
    )

    if value:
        return value

    # Try links that look like shop links.
    try:

        links = page.locator("a")

        count = links.count()

        for i in range(min(count, 200)):

            href = links.nth(i).get_attribute(
                "href"
            )

            if not href:
                continue

            text = clean_text(
                links.nth(i).inner_text(
                    timeout=1000
                )
            )

            if not text:
                continue

            href_lower = href.lower()

            if (
                "/shop/" in href_lower
                or "/shopview/" in href_lower
                or "/store/" in href_lower
            ):
                return text

    except Exception:
        pass

    return None


# ============================================================
# DETAIL PAGE - SELLER RATING
# ============================================================

def detail_seller_rating(page):

    selectors = [
        '[data-qa-locator*="seller-rating"]',
        '[class*="seller-rating"]',
        '[class*="sellerRating"]',
        '[class*="rating"]',
    ]

    for selector in selectors:

        try:

            locator = page.locator(selector)

            count = locator.count()

            for i in range(min(count, 20)):

                text = clean_text(
                    locator.nth(i).inner_text(
                        timeout=1500
                    )
                )

                if not text:
                    continue

                # Seller ratings are often percentages.
                percentage = re.search(
                    r"(\d+(?:\.\d+)?)\s*%",
                    text,
                )

                if percentage:

                    value = float(
                        percentage.group(1)
                    )

                    if 0 <= value <= 100:
                        return value

        except Exception:
            continue

    return None


# ============================================================
# DETAIL PAGE - BRAND
# ============================================================

def detail_brand(page):

    selectors = [
        '[data-qa-locator="product-brand"]',
        '[data-qa-locator*="brand"]',
        '[class*="brand"]',
        '[class*="Brand"]',
    ]

    return get_first_text(
        page,
        selectors,
        minimum_length=2,
    )


# ============================================================
# DETAIL PAGE - IMAGE
# ============================================================

def detail_image(page):

    meta_image = extract_meta_content(
        page,
        [
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
        ],
    )

    if meta_image:
        return normalize_url(meta_image)

    try:

        images = page.locator("img")

        count = images.count()

        for i in range(min(count, 100)):

            image = images.nth(i)

            alt = clean_text(
                image.get_attribute("alt")
            )

            src = get_first_attribute(
                image,
                [
                    "src",
                    "data-src",
                    "data-original",
                ],
            )

            if not src:
                continue

            if (
                alt
                and len(alt) > 5
            ):
                return normalize_url(src)

    except Exception:
        pass

    return None


# ============================================================
# DETAIL PAGE - SHIPPING
# ============================================================

def detail_shipping(page):

    text = safe_body_text(page)

    if not text:
        return None

    patterns = [
        r"Free Shipping",
        r"Free Delivery",
        r"Shipping Fee",
        r"Delivery Fee",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:

            start = max(
                0,
                match.start() - 20,
            )

            end = min(
                len(text),
                match.end() + 100,
            )

            result = clean_text(
                text[start:end]
            )

            if result:
                return result

    return None


# ============================================================
# DETAIL PAGE - DELIVERY
# ============================================================

def detail_delivery(page):

    text = safe_body_text(page)

    if not text:
        return None

    patterns = [
        r"Delivery.{0,120}",
        r"Deliver.{0,120}",
        r"Arrives.{0,120}",
        r"Estimated Delivery.{0,120}",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:

            result = clean_text(
                match.group(0)
            )

            if (
                result
                and len(result) < 180
            ):
                return result

    return None


# ============================================================
# DETAIL PAGE - STOCK
# ============================================================

def detail_stock(page):

    text = normalize_text(
        safe_body_text(page)
    )

    out_of_stock = [
        "out of stock",
        "sold out",
        "currently unavailable",
        "unavailable",
    ]

    for phrase in out_of_stock:

        if phrase in text:

            return {
                "availability": "Out of Stock",
                "in_stock": False,
            }

    # Check visible purchase buttons.
    try:

        buttons = page.locator(
            "button"
        )

        count = buttons.count()

        for i in range(min(count, 50)):

            button_text = normalize_text(
                buttons.nth(i).inner_text(
                    timeout=1000
                )
            )

            if (
                "add to cart" in button_text
                or "buy now" in button_text
            ):

                return {
                    "availability": "In Stock",
                    "in_stock": True,
                }

    except Exception:
        pass

    return {
        "availability": "Unknown",
        "in_stock": None,
    }


# ============================================================
# REVIEWS (author, rating, stars, date, text, images)
# ============================================================

def format_rating_stars(rating):

    if rating is None:
        return None

    try:
        rating = max(
            0,
            min(5, int(round(float(rating)))),
        )

    except (TypeError, ValueError):
        return None

    return "★" * rating + "☆" * (5 - rating)


def clean_review_images(values):

    images = []

    for value in values or []:

        if isinstance(value, dict):
            value = (
                value.get("url")
                or value.get("src")
                or value.get("image")
                or value.get("original")
            )

        if not value:
            continue

        url = normalize_url(
            clean_text(value)
        )

        if (
            url
            and url.startswith("http")
            and url not in images
        ):
            images.append(url)

    return images[:10]


def make_review(
    author,
    rating,
    date,
    text,
    images,
):

    author = clean_text(author)

    if author:
        author = re.sub(
            r"^by\s+",
            "",
            author,
            flags=re.IGNORECASE,
        ).strip() or None

    text = clean_text(text)
    date = clean_text(date)

    rating = parse_rating(rating)

    if rating is not None and float(rating).is_integer():
        rating = int(rating)

    images = clean_review_images(images)

    if not author and not text and not images:
        return None

    return {
        "author": author,
        "rating": rating,
        "rating_stars": format_rating_stars(rating),
        "date": date,
        "text": text,
        "images": images,
    }


def merge_reviews(*review_lists):

    merged = []
    seen = set()

    for review_list in review_lists:

        for review in review_list:

            if not review:
                continue

            key = (
                (review.get("author") or "").casefold(),
                (review.get("date") or "").casefold(),
                (review.get("text") or "").casefold(),
            )

            if key in seen:
                continue

            seen.add(key)
            merged.append(review)

            if len(merged) >= MAX_REVIEWS:
                return merged

    return merged


REVIEW_FETCH_SCRIPT = """
async ({itemId, pageNo, pageSize}) => {
  const url = `/pdp/review/getReviewList?itemId=${itemId}&pageSize=${pageSize}&filter=0&sort=0&pageNo=${pageNo}`;
  try {
    const response = await fetch(url, {
      credentials: 'include',
      headers: {'accept': 'application/json, text/plain, */*'}
    });
    const raw = await response.text();
    try { return JSON.parse(raw); } catch (e) { return null; }
  } catch (e) {
    return null;
  }
}
"""


def fetch_reviews_api(
    page,
    product_id,
):
    """Primary method: Daraz's own review endpoint, called from the product page."""

    reviews = []

    if not product_id:
        return reviews

    page_no = 1

    while (
        len(reviews) < MAX_REVIEWS
        and page_no <= MAX_REVIEW_PAGES
    ):

        try:
            data = page.evaluate(
                REVIEW_FETCH_SCRIPT,
                {
                    "itemId": product_id,
                    "pageNo": page_no,
                    "pageSize": REVIEW_PAGE_SIZE,
                },
            )

        except Exception as e:
            print(
                "  Review API failed:",
                repr(e),
            )
            break

        model = (
            data.get("model")
            if isinstance(data, dict)
            else None
        )

        if not isinstance(model, dict):
            break

        items = model.get("items") or []

        if not items:
            break

        for item in items:

            if not isinstance(item, dict):
                continue

            images = (
                item.get("images")
                or item.get("reviewImages")
                or []
            )

            if isinstance(images, (str, dict)):
                images = [images]

            review = make_review(
                item.get("buyerName")
                or item.get("userName")
                or item.get("nickName"),
                item.get("rating")
                or item.get("ratingValue"),
                item.get("reviewTime")
                or item.get("boughtDate"),
                item.get("reviewContent")
                or item.get("content"),
                images,
            )

            if review:
                reviews.append(review)

        paging = model.get("paging") or {}

        total_pages = parse_number(
            paging.get("totalPages")
        )

        if total_pages and page_no >= total_pages:
            break

        page_no += 1

    return merge_reviews(reviews)


REVIEW_DOM_SCRIPT = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const dateRe = /\b\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b/;
  const urlFromStyle = s => { const m = (s || '').match(/url\(["']?(.*?)["']?\)/i); return m ? m[1] : null; };
  const junk = l => /^(verified purchase|helpful|report|seller response|reply|like|\d+)$/i.test(l);

  let items = [...document.querySelectorAll('.mod-reviews .item')];
  if (!items.length) items = [...document.querySelectorAll('[class*="review-item"], [class*="review-card"]')];
  items = items.filter(el => !items.some(o => o !== el && el.contains(o)));

  const out = [];
  for (const item of items) {
    const lines = (item.innerText || '').split(/\n+/).map(clean).filter(Boolean);

    // rating: filled stars come first, so count images sharing the first image's src
    let rating = null;
    const starImgs = [...item.querySelectorAll('.container-star img, img.star, [class*="star"] img')];
    if (starImgs.length) {
      const srcs = starImgs.map(i => i.currentSrc || i.src || '');
      const distinct = new Set(srcs);
      rating = distinct.size > 1 ? srcs.filter(s => s === srcs[0]).length : Math.min(5, srcs.length);
    }

    let author = null;
    for (const l of lines) { const m = l.match(/^by\s+(.+)$/i); if (m) { author = m[1]; break; } }
    if (!author) {
      const el = item.querySelector('[class*="name"], [class*="author"]');
      if (el) author = clean(el.innerText);
    }

    let date = null;
    for (const l of lines) { const m = l.match(dateRe); if (m) { date = m[0]; break; } }

    const contentEl = item.querySelector('.content, [class*="review-content"]');
    let text = contentEl ? clean(contentEl.innerText) : null;
    if (!text) {
      const candidates = lines.filter(l => !/^by\s+/i.test(l) && !dateRe.test(l) && !junk(l) && !/^[A-Za-z ]+:\s/.test(l));
      candidates.sort((a, b) => b.length - a.length);
      text = candidates.length ? candidates[0] : null;
    }

    const images = [];
    for (const el of item.querySelectorAll('[class*="review-image"], [class*="review-image"] *')) {
      const bg = urlFromStyle(el.getAttribute('style'));
      if (bg) images.push(bg);
      if (el.tagName === 'IMG') {
        const s = el.currentSrc || el.src || el.getAttribute('data-src');
        if (s) images.push(s);
      }
    }

    out.push({author, rating, date, text, images});
  }
  return out;
}
"""


def fetch_reviews_dom(page):
    """Fallback: read the rendered 'Ratings & Reviews' section."""

    try:

        for _ in range(10):
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(500)

        page.wait_for_timeout(1000)

        data = page.evaluate(REVIEW_DOM_SCRIPT)

    except Exception as e:
        print(
            "  Review DOM failed:",
            repr(e),
        )
        return []

    reviews = []

    for item in data or []:

        review = make_review(
            item.get("author"),
            item.get("rating"),
            item.get("date"),
            item.get("text"),
            item.get("images"),
        )

        if review:
            reviews.append(review)

    return merge_reviews(reviews)


def extract_reviews(
    page,
    product_id,
):

    reviews = fetch_reviews_api(
        page,
        product_id,
    )

    if not reviews:
        reviews = fetch_reviews_dom(page)

    print(
        "  Reviews extracted:",
        len(reviews),
    )

    return reviews


# ============================================================
# DETAIL PAGE ENRICHMENT
# ============================================================

def enrich_product(
    page,
    product,
):

    url = product.get("product_url")

    if not url:
        return product

    try:

        print(
            "  Enriching:",
            product.get("title", "")[:70],
        )

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=DETAIL_TIMEOUT,
        )

        if response:
            print(
                "  Detail HTTP:",
                response.status,
            )

        # Give Daraz JavaScript time to render.
        page.wait_for_timeout(2500)

        # ----------------------------------------------------
        # JSON-LD
        # ----------------------------------------------------

        jsonld = extract_jsonld_data(page)

        # Only overwrite when real data was found.
        for key, value in jsonld.items():

            if value is not None:
                product[key] = value

        # ----------------------------------------------------
        # Meta title
        # ----------------------------------------------------

        if not product.get("title"):

            title = extract_meta_content(
                page,
                [
                    'meta[property="og:title"]',
                    'meta[name="twitter:title"]',
                ],
            )

            if title:
                product["title"] = title

        # ----------------------------------------------------
        # Price
        # ----------------------------------------------------

        value = detail_price(page)

        if value is not None:
            product["price"] = value

        # ----------------------------------------------------
        # Original price
        # ----------------------------------------------------

        value = detail_original_price(page)

        if value is not None:
            product["original_price"] = value

        # ----------------------------------------------------
        # Calculate discount ONLY from real prices
        # ----------------------------------------------------

        if (
            product.get("price") is not None
            and product.get("original_price") is not None
            and product["original_price"] > product["price"]
        ):

            discount_value = (
                (
                    product["original_price"]
                    - product["price"]
                )
                / product["original_price"]
            ) * 100

            product["discount"] = (
                f"-{round(discount_value)}%"
            )

        # ----------------------------------------------------
        # Rating
        # ----------------------------------------------------

        value = detail_rating(page)

        if value is not None:
            product["rating"] = value

        # ----------------------------------------------------
        # Reviews count
        # ----------------------------------------------------

        value = detail_review_count(page)

        if value is not None:
            product["review_count"] = value

        # ----------------------------------------------------
        # Seller
        # ----------------------------------------------------

        value = detail_seller(page)

        if value:
            product["seller"] = value

        # ----------------------------------------------------
        # Seller rating
        # ----------------------------------------------------

        value = detail_seller_rating(page)

        if value is not None:
            product["seller_rating"] = value

        # ----------------------------------------------------
        # Brand
        # ----------------------------------------------------

        value = detail_brand(page)

        if value:
            product["brand"] = value

        # ----------------------------------------------------
        # Image
        # ----------------------------------------------------

        value = detail_image(page)

        if value:
            product["image"] = value

        # ----------------------------------------------------
        # Shipping
        # ----------------------------------------------------

        value = detail_shipping(page)

        if value:
            product["shipping"] = value

        # ----------------------------------------------------
        # Delivery
        # ----------------------------------------------------

        value = detail_delivery(page)

        if value:
            product["delivery"] = value

        # ----------------------------------------------------
        # Stock
        # ----------------------------------------------------

        stock = detail_stock(page)

        if stock["availability"] != "Unknown":
            product["availability"] = stock[
                "availability"
            ]

        if stock["in_stock"] is not None:
            product["in_stock"] = stock[
                "in_stock"
            ]

        # ----------------------------------------------------
        # Customer reviews (author, rating, stars, date,
        # text, images)
        # ----------------------------------------------------

        if ENRICH_REVIEWS:

            reviews = extract_reviews(
                page,
                product.get("product_id"),
            )

            if reviews:

                product["reviews"] = reviews

                if product.get("review_count") is None:
                    product["review_count"] = len(
                        reviews
                    )

    except PlaywrightTimeoutError:

        print(
            "  Detail timeout:",
            url,
        )

    except Exception as e:

        print(
            "  Detail enrichment failed:",
            repr(e),
        )

    return product


# ============================================================
# FIND SEARCH PRODUCT CARDS
# ============================================================

def find_product_cards(page):

    selectors = [
        '[data-qa-locator="product-item"]',
        'div[data-qa-locator="product-item"]',
        'div[class*="product"]',
        'div[class*="Product"]',
    ]

    best_locator = None
    best_count = 0

    for selector in selectors:

        try:

            locator = page.locator(selector)

            count = locator.count()

            if count > best_count:

                best_count = count
                best_locator = locator

        except Exception:
            continue

    if best_count > 0:
        return best_locator

    return None


# ============================================================
# SEARCH ENDPOINT
# ============================================================

@app.get("/api/search")
def search_products(

    keyword: str = Query(
        ...,
        min_length=1,
        description="Product search keyword",
    ),

    limit: int = Query(
        DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Number of products",
    ),
):

    keyword = clean_text(keyword)

    if not keyword:

        raise HTTPException(
            status_code=400,
            detail="Keyword cannot be empty.",
        )

    encoded_keyword = quote(keyword)

    search_url = (
        f"{DARAZ_BASE_URL}/catalog/"
        f"?q={encoded_keyword}"
    )

    print()
    print("=" * 70)
    print(
        "Searching Daraz:",
        keyword,
    )
    print(
        "URL:",
        search_url,
    )
    print("=" * 70)

    products = []

    # ========================================================
    # PLAYWRIGHT
    # ========================================================

    try:

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True,
            )

            context = browser.new_context(

                viewport={
                    "width": 1440,
                    "height": 900,
                },

                locale="en-PK",

                timezone_id="Asia/Karachi",

                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/140.0.0.0 "
                    "Safari/537.36"
                ),
            )

            page = context.new_page()

            # =================================================
            # SEARCH PAGE
            # =================================================

            print("Opening Daraz...")

            response = page.goto(
                search_url,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT,
            )

            if response:
                print(
                    "HTTP status:",
                    response.status,
                )

            print(
                "Page title:",
                page.title(),
            )

            page.wait_for_timeout(4000)

            # -------------------------------------------------
            # Scroll
            # -------------------------------------------------

            print(
                "Loading search products..."
            )

            for _ in range(6):

                page.mouse.wheel(
                    0,
                    1200,
                )

                page.wait_for_timeout(
                    1000
                )

            # -------------------------------------------------
            # Find cards
            # -------------------------------------------------

            cards = find_product_cards(page)

            if cards is None:

                browser.close()

                return {
                    "success": True,
                    "keyword": keyword,
                    "search_url": search_url,
                    "count": 0,
                    "products": [],
                    "message": (
                        "Daraz did not expose "
                        "product cards."
                    ),
                }

            card_count = cards.count()

            print(
                "Product elements found:",
                card_count,
            )

            # Inspect extra cards because some
            # may be duplicates.
            extraction_limit = min(
                card_count,
                max(limit * 3, limit),
            )

            # -------------------------------------------------
            # Extract search results
            # -------------------------------------------------

            for index in range(
                extraction_limit
            ):

                try:

                    card = cards.nth(index)

                    product = extract_search_product(
                        card,
                        keyword,
                    )

                    if product:

                        products.append(
                            product
                        )

                        print(
                            f"[{len(products)}] "
                            f"{product['title'][:80]}"
                        )

                except Exception as e:

                    print(
                        f"Card {index} failed:",
                        repr(e),
                    )

            # =================================================
            # DEDUPLICATE
            # =================================================

            unique_products = {}

            for product in products:

                unique_key = (
                    product.get("product_id")
                    or product.get("product_url")
                    or normalize_text(
                        product.get("title")
                    )
                )

                if not unique_key:
                    continue

                if unique_key not in unique_products:

                    unique_products[
                        unique_key
                    ] = product

                else:

                    existing = unique_products[
                        unique_key
                    ]

                    if (
                        product.get("_relevance", 0)
                        > existing.get(
                            "_relevance",
                            0,
                        )
                    ):

                        unique_products[
                            unique_key
                        ] = product

            products = list(
                unique_products.values()
            )

            # =================================================
            # SORT BY RELEVANCE
            # =================================================

            products.sort(
                key=lambda product: (
                    product.get(
                        "_relevance",
                        0,
                    ),
                    product.get(
                        "rating"
                    ) or 0,
                    product.get(
                        "review_count"
                    ) or 0,
                ),
                reverse=True,
            )

            # =================================================
            # DETAIL PAGE ENRICHMENT
            # =================================================

            if ENRICH_DETAILS:

                print()
                print(
                    "=" * 70
                )

                print(
                    "Enriching product details..."
                )

                print(
                    "Products:",
                    min(
                        len(products),
                        limit,
                    ),
                )

                print(
                    "=" * 70
                )

                # Create a separate detail page.
                detail_page = context.new_page()

                # Only enrich the products that
                # will actually be returned.
                products_to_enrich = products[
                    :limit
                ]

                for index, product in enumerate(
                    products_to_enrich,
                    start=1,
                ):

                    print()
                    print(
                        f"DETAIL {index}/"
                        f"{len(products_to_enrich)}"
                    )

                    enrich_product(
                        detail_page,
                        product,
                    )

                detail_page.close()

            # =================================================
            # LIMIT
            # =================================================

            products = products[:limit]

            # =================================================
            # REMOVE INTERNAL FIELDS
            # =================================================

            for product in products:

                product.pop(
                    "_relevance",
                    None,
                )

            # =================================================
            # CLOSE
            # =================================================

            context.close()
            browser.close()

    except PlaywrightTimeoutError:

        raise HTTPException(
            status_code=504,
            detail=(
                "Daraz took too long "
                "to respond."
            ),
        )

    except Exception as e:

        print(
            "Playwright error:",
            repr(e),
        )

        raise HTTPException(
            status_code=500,
            detail={
                "message": "Playwright failed.",
                "error": str(e),
            },
        )

    # ========================================================
    # RESULT
    # ========================================================

    print()
    print(
        "=" * 70
    )

    print(
        "Final results:",
        len(products),
    )

    print(
        "=" * 70
    )

    return {
        "success": True,
        "keyword": keyword,
        "search_url": search_url,
        "count": len(products),
        "products": products,
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "ok",
        "service": "Daraz Product Search API",
        "playwright": True,
        "detail_enrichment": ENRICH_DETAILS,
        "reviews": ENRICH_REVIEWS,
        "platform": sys.platform,
    }


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "name": "Daraz Product Search API",
        "version": "4.1.0",
        "status": "running",
        "search_endpoint": (
            "/api/search?keyword=iphone"
        ),
        "documentation": "/docs",
        "health": "/health",
        "detail_enrichment": ENRICH_DETAILS,
        "reviews": ENRICH_REVIEWS,
    }


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    import uvicorn

    print()
    print("=" * 70)
    print(
        "DARAZ PRODUCT SEARCH API"
    )
    print("=" * 70)
    print()

    print(
        "Server: "
        "http://127.0.0.1:8000"
    )

    print(
        "Docs:   "
        "http://127.0.0.1:8000/docs"
    )

    print(
        "Health: "
        "http://127.0.0.1:8000/health"
    )

    print()

    print(
        "Playwright mode: SYNCHRONOUS"
    )

    print(
        "Detail enrichment:",
        ENRICH_DETAILS,
    )

    print(
        "Reviews:",
        ENRICH_REVIEWS,
    )

    print()

    print("=" * 70)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False,
    )