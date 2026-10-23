"""
Naheed.pk Product Scraper API
=============================
Extracts real product data from Naheed.pk search and product-detail pages.
Unavailable fields are None/empty lists; values are never fabricated.

Install:
    pip install fastapi uvicorn playwright beautifulsoup4
    python -m playwright install chromium

Run:
    python naheed.py

API:
    http://127.0.0.1:8000/api/search?keyword=surf&limit=20
    http://127.0.0.1:8000/docs

Only public query inputs: keyword and limit.
"""

import json
import re
import sys
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://www.naheed.pk"
SEARCH_URL = f"{BASE_URL}/catalogsearch/result/"
PLATFORM = "Naheed"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
BROWSER_TIMEOUT = 60000

app = FastAPI(
    title="Naheed.pk Product Search API",
    description="Extracts product data from Naheed.pk search and product pages.",
    version="3.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ------------------------------ Helpers ------------------------------

def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    value = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def number(value: Any) -> Optional[int | float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)", text.replace("\xa0", " "))
    if not match:
        return None
    try:
        n = float(match.group(1).replace(",", ""))
        return int(n) if n.is_integer() else n
    except (TypeError, ValueError):
        return None


def absolute_url(value: Any, base: str = BASE_URL) -> Optional[str]:
    if not value:
        return None
    value = str(value).strip()
    if value.startswith(("data:", "javascript:", "blob:")):
        return None
    result = urljoin(base, value)
    if urlparse(result).scheme not in ("http", "https"):
        return None
    return result


def first_text(node: Any, selectors: list[str]) -> Optional[str]:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            value = clean_text(found.get_text(" ", strip=True))
            if value:
                return value
    return None


def first_attr(node: Any, selector_attrs: list[tuple[str, str]]) -> Optional[str]:
    for selector, attr in selector_attrs:
        found = node.select_one(selector)
        if not found:
            continue
        value = found.get(attr)
        if isinstance(value, list):
            value = " ".join(str(x) for x in value)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def all_text(node: Any, selectors: list[str]) -> list[str]:
    result, seen = [], set()
    for selector in selectors:
        for element in node.select(selector):
            value = clean_text(element.get_text(" ", strip=True))
            if value and value not in seen:
                seen.add(value)
                result.append(value)
    return result


def all_images(node: Any, base_url: str) -> list[str]:
    result, seen = [], set()
    for img in node.select("img"):
        candidate = (
            img.get("data-src") or img.get("data-original") or
            img.get("data-lazy-src") or img.get("src") or
            img.get("data-zoom-image")
        )
        url = absolute_url(candidate, base_url)
        if url and url not in seen and "placeholder" not in url.lower():
            seen.add(url)
            result.append(url)
    # Some Magento galleries store additional images in data-mage-init.
    for element in node.select("[data-gallery-role], [data-zoom-image]"):
        url = absolute_url(element.get("data-zoom-image"), base_url)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def stock_status(value: Any) -> tuple[Optional[str], Optional[bool]]:
    text = (clean_text(value) or "").lower()
    if not text:
        return None, None
    if any(x in text for x in ("out of stock", "out-of-stock", "sold out", "unavailable")):
        return "Out of Stock", False
    if any(x in text for x in ("in stock", "in-stock", "available")):
        return "In Stock", True
    return clean_text(value), None


def discount_percent(price: Any, original: Any) -> Optional[str]:
    if price is None or original is None:
        return None
    try:
        price, original = float(price), float(original)
        if original <= 0 or price >= original:
            return None
        return f"{round((original - price) / original * 100)}%"
    except (TypeError, ValueError):
        return None


def product_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    # Naheed product URLs don't always contain the product ID.
    patterns = [r"/(\d+)(?:\.html)?(?:[/?#]|$)", r"-(\d+)\.html(?:[/?#]|$)"]
    for pattern in patterns:
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1)
    return None


def blank_product() -> dict[str, Any]:
    return {
        "platform": PLATFORM,
        "title": None,
        "price": None,
        "original_price": None,
        "discount": None,
        "currency": "PKR",
        "availability": None,
        "in_stock": None,
        "purchasable": None,
        "seller": None,
        "seller_rating": None,
        "rating": None,
        "rating_count": None,
        "review_count": None,
        "reviews": [],
        "image": None,
        "images": [],
        "product_url": None,
        "product_id": None,
        "sku": None,
        "brand": None,
        "brand_store_url": None,
        "category": None,
        "breadcrumbs": [],
        "description": None,
        "highlights": [],
        "specifications": {},
        "variants": [],
        "shipping": None,
        "shipping_cost": None,
        "delivery": None,
        "delivery_area": None,
        "return_policy": None,
        "payment_options": [],
        "official_store": None,
        "seller_url": None,
        "meta_description": None,
    }


# ------------------------------ JSON-LD ------------------------------

def jsonld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            found.append(value)
            for key, child in value.items():
                if key != "@context" and isinstance(child, (dict, list)):
                    walk(child)

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if raw:
            try:
                walk(json.loads(raw.strip()))
            except (json.JSONDecodeError, TypeError):
                continue
    return found


def type_matches(obj: dict[str, Any], wanted: str) -> bool:
    value = obj.get("@type", [])
    if isinstance(value, str):
        value = [value]
    return any(str(x).lower() == wanted.lower() for x in value)


def jsonld_product_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    unique, seen = [], set()
    for obj in jsonld_objects(soup):
        if type_matches(obj, "Product"):
            key = str(obj.get("sku") or obj.get("url") or obj.get("name") or "")
            if key and key not in seen:
                seen.add(key)
                unique.append(obj)
    return unique


def get_offer(product: dict[str, Any]) -> dict[str, Any]:
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def brand_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    return clean_text(value)


def apply_jsonld(product: dict[str, Any], obj: dict[str, Any]) -> None:
    offer = get_offer(obj)
    if product["title"] is None:
        product["title"] = clean_text(obj.get("name"))
    if product["product_url"] is None:
        product["product_url"] = absolute_url(obj.get("url"))
    if product["sku"] is None:
        product["sku"] = clean_text(obj.get("sku") or obj.get("mpn"))
    if product["product_id"] is None:
        product["product_id"] = clean_text(obj.get("productID") or obj.get("productId") or product["sku"])

    price_spec = offer.get("priceSpecification")
    if isinstance(price_spec, list):
        price_spec = price_spec[0] if price_spec else {}
    if not isinstance(price_spec, dict):
        price_spec = {}
    if product["price"] is None:
        product["price"] = number(offer.get("price") or offer.get("lowPrice") or price_spec.get("price"))
    if product["currency"] is None or product["currency"] == "PKR":
        product["currency"] = clean_text(offer.get("priceCurrency")) or "PKR"

    availability_raw = str(offer.get("availability") or "").rsplit("/", 1)[-1]
    status, in_stock = stock_status(availability_raw)
    if product["availability"] is None:
        product["availability"] = status
    if product["in_stock"] is None:
        product["in_stock"] = in_stock
    if product["purchasable"] is None and in_stock is not None:
        product["purchasable"] = in_stock

    if product["seller"] is None:
        seller = offer.get("seller")
        product["seller"] = clean_text(seller.get("name") if isinstance(seller, dict) else seller)
    if product["brand"] is None:
        product["brand"] = brand_name(obj.get("brand"))

    aggregate = obj.get("aggregateRating")
    if isinstance(aggregate, dict):
        if product["rating"] is None:
            product["rating"] = number(aggregate.get("ratingValue"))
        if product["rating_count"] is None:
            product["rating_count"] = number(aggregate.get("ratingCount"))
        if product["review_count"] is None:
            product["review_count"] = number(aggregate.get("reviewCount"))

    if not product["images"]:
        raw_images = obj.get("image")
        if not isinstance(raw_images, list):
            raw_images = [raw_images] if raw_images else []
        for item in raw_images:
            if isinstance(item, dict):
                item = item.get("url") or item.get("contentUrl")
            url = absolute_url(item, product["product_url"] or BASE_URL)
            if url and url not in product["images"]:
                product["images"].append(url)
    if product["image"] is None and product["images"]:
        product["image"] = product["images"][0]

    description = clean_text(obj.get("description"))
    if product["description"] is None and description:
        product["description"] = description

    # Product JSON-LD review objects, if the page actually publishes them.
    reviews = obj.get("review")
    if isinstance(reviews, dict):
        reviews = [reviews]
    if isinstance(reviews, list) and not product["reviews"]:
        for review in reviews:
            if not isinstance(review, dict):
                continue
            author = review.get("author")
            rating = review.get("reviewRating")
            product["reviews"].append({
                "author": clean_text(author.get("name") if isinstance(author, dict) else author),
                "rating": number(rating.get("ratingValue")) if isinstance(rating, dict) else None,
                "title": clean_text(review.get("name")),
                "text": clean_text(review.get("reviewBody")),
                "date": clean_text(review.get("datePublished")),
            })


# ------------------------------ Search cards ------------------------------

def parse_card(card: Any) -> Optional[dict[str, Any]]:
    p = blank_product()
    p["title"] = first_text(card, [
        ".product-item-name a", ".product-item-link", ".product-name a",
        ".product-name", "a.product-item-link", "[itemprop='name']", "h2", "h3"
    ])
    href = first_attr(card, [
        (".product-item-name a", "href"), (".product-item-link", "href"),
        (".product-name a", "href"), ("a[itemprop='url']", "href"),
        ("a[href*='.html']", "href")
    ])
    p["product_url"] = absolute_url(href)
    if not p["title"] or not p["product_url"]:
        return None

    p["price"] = number(first_attr(card, [
        (".special-price [data-price-amount]", "data-price-amount"),
        ("[data-price-amount]", "data-price-amount"),
        ("[itemprop='price']", "content"), ("[data-price]", "data-price")
    ]) or first_text(card, [".special-price .price", ".price-final_price .price", ".price-box .price", ".price"]))

    original = number(first_attr(card, [
        (".old-price [data-price-amount]", "data-price-amount"),
        ("del [data-price-amount]", "data-price-amount"), ("[data-old-price]", "data-old-price")
    ]) or first_text(card, [".old-price .price", ".price-old", "del .price", "del", "s"]))
    if original is not None and p["price"] is not None and original <= p["price"]:
        original = None
    p["original_price"] = original
    p["discount"] = discount_percent(p["price"], original)

    currency = first_attr(card, [("[itemprop='priceCurrency']", "content"), ("[data-currency]", "data-currency")])
    if currency:
        p["currency"] = currency

    p["images"] = all_images(card, p["product_url"])
    p["image"] = p["images"][0] if p["images"] else absolute_url(first_attr(card, [("img", "src")]), p["product_url"])

    stock_raw = first_text(card, [".stock", ".availability", "[class*='stock']", "[class*='availability']"])
    if not stock_raw:
        stock_raw = first_attr(card, [("[itemprop='availability']", "href")])
    p["availability"], p["in_stock"] = stock_status(stock_raw)
    p["purchasable"] = p["in_stock"]

    p["brand"] = first_text(card, [".product-brand", "[itemprop='brand']", ".brand-name", "[class*='brand']"])
    p["sku"] = first_attr(card, [("[itemprop='sku']", "content"), ("[data-sku]", "data-sku")])
    p["product_id"] = first_attr(card, [
        ("[data-product-id]", "data-product-id"), ("[data-productid]", "data-productid"),
        ("[data-sku]", "data-sku"), ("[itemprop='sku']", "content")
    ]) or p["sku"] or product_id_from_url(p["product_url"])

    p["rating"] = number(first_attr(card, [
        ("[itemprop='ratingValue']", "content"), ("[data-rating]", "data-rating")
    ]) or first_text(card, [".rating-result", "[class*='rating']"]))
    p["review_count"] = number(first_attr(card, [
        ("[itemprop='reviewCount']", "content"), ("[itemprop='ratingCount']", "content")
    ]) or first_text(card, [".reviews-actions", ".review-count", "[class*='review']"]))

    p["seller"] = first_text(card, ["[itemprop='seller']", ".product-seller", ".seller-name"])
    return p


# ------------------------------ Detail-page extraction ------------------------------

def extract_specifications(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for table in soup.select(
        "#product-attribute-specs-table tr, .additional-attributes tr, "
        ".product.attribute tr, table#product-attribute-specs-table tr"
    ):
        key = first_text(table, ["th", "td.col.label", ".label"])
        value = first_text(table, ["td", "td.col.data", ".value"])
        if key and value and key.lower() not in ("", "more information"):
            specs[key.rstrip(": ")] = value
    # Magento sometimes renders specifications as label/value definition lists.
    for row in soup.select(".product.attribute dl, .additional-attributes-wrapper dl"):
        labels = row.select("dt")
        values = row.select("dd")
        for label, value in zip(labels, values):
            k, v = clean_text(label.get_text(" ", strip=True)), clean_text(value.get_text(" ", strip=True))
            if k and v:
                specs[k.rstrip(": ")] = v
    return specs


def extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    values = all_text(soup, [
        ".breadcrumbs li", "nav[aria-label='Breadcrumb'] a",
        "[itemtype*='BreadcrumbList'] [itemprop='name']"
    ])
    return [v for v in values if v.lower() not in ("home", "you are here:")]


def extract_variants(soup: BeautifulSoup) -> list[dict[str, Any]]:
    variants = []
    seen = set()
    for option in soup.select("select option"):
        label = clean_text(option.get_text(" ", strip=True))
        value = option.get("value")
        if not label or not value or value == "" or label.lower() in ("choose an option", "please select"):
            continue
        key = (label, str(value))
        if key not in seen:
            seen.add(key)
            variants.append({"label": label, "value": str(value)})
    for element in soup.select("[role='option'], .swatch-option[option-label], .swatch-option[aria-label]"):
        label = clean_text(element.get("option-label") or element.get("aria-label") or element.get_text(" ", strip=True))
        value = element.get("option-id") or element.get("data-option-id") or element.get("data-value")
        if label and (label, str(value)) not in seen:
            seen.add((label, str(value)))
            variants.append({"label": label, "value": str(value) if value is not None else None})
    return variants


def extract_reviews(soup: BeautifulSoup) -> list[dict[str, Any]]:
    reviews = []
    selectors = [
        "#reviews .review-item", ".review-list .review-item",
        ".items.review-items .review-item", "[itemprop='review']"
    ]
    seen = set()
    for selector in selectors:
        for item in soup.select(selector):
            author = first_text(item, [".review-details .review-author", ".review-author", "[itemprop='author']", ".nickname"])
            title = first_text(item, [".review-title", "[itemprop='name']", "h3", "h4"])
            body = first_text(item, [".review-content", ".review-text", "[itemprop='reviewBody']", ".description"])
            date = first_text(item, ["time", ".review-date", "[itemprop='datePublished']"])
            rating = number(first_attr(item, [("[itemprop='ratingValue']", "content"), ("[data-rating]", "data-rating")]))
            if not any((author, title, body, date, rating is not None)):
                continue
            key = (author, title, body, date)
            if key in seen:
                continue
            seen.add(key)
            reviews.append({"author": author, "rating": rating, "title": title, "text": body, "date": date})
    return reviews


def detail_enrich(page: Any, product: dict[str, Any]) -> dict[str, Any]:
    url = product.get("product_url")
    if not url:
        return product
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=35000)
        if response and response.status >= 400:
            return product
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(500)
        soup = BeautifulSoup(page.content(), "html.parser")

        # Merge structured Product data first, since it is usually the cleanest source.
        for obj in jsonld_product_objects(soup):
            apply_jsonld(product, obj)
            if product.get("title"):
                break

        if product["title"] is None:
            product["title"] = first_text(soup, ["h1.page-title span", "h1.page-title", "h1"])
        if product["price"] is None:
            product["price"] = number(
                first_attr(soup, [("[itemprop='price']", "content"), ("[data-price-amount]", "data-price-amount")])
                or first_text(soup, [".product-info-price .price", ".price-box .price", ".product-info-main .price"])
            )
        if product["original_price"] is None:
            original = number(
                first_attr(soup, [(".old-price [data-price-amount]", "data-price-amount"), ("[data-old-price]", "data-old-price")])
                or first_text(soup, [".old-price .price", ".old.price", "del .price"])
            )
            if original is not None and product["price"] is not None and original > product["price"]:
                product["original_price"] = original
        product["discount"] = product["discount"] or discount_percent(product["price"], product["original_price"])

        if product["currency"] is None:
            product["currency"] = first_attr(soup, [("[itemprop='priceCurrency']", "content")]) or "PKR"

        if product["brand"] is None:
            product["brand"] = first_text(soup, [
                ".product-brand", ".brand-name", "[itemprop='brand'] [itemprop='name']", "[itemprop='brand']"
            ])
            if product["brand"]:
                product["brand"] = re.sub(r"\s+Visit the Store$", "", product["brand"], flags=re.I).strip()

        # Correct common Magento brand text contamination ("Surf Visit the Store").
        if product["brand"]:
            product["brand"] = re.sub(r"\s+Visit the Store$", "", product["brand"], flags=re.I).strip() or None

        if product["sku"] is None:
            product["sku"] = first_attr(soup, [("[itemprop='sku']", "content"), ("[itemprop='sku']", "data-sku")])
        if product["product_id"] is None:
            product["product_id"] = first_attr(soup, [
                ("[data-product-id]", "data-product-id"), ("input[name='product']", "value")
            ]) or product["sku"] or product_id_from_url(url)

        # Stock: don't treat absence of a stock label as proof of availability.
        stock = first_text(soup, [".stock", ".product-info-stock-sku .stock", ".availability", "[class*='stock']"])
        if not stock:
            stock = first_attr(soup, [("[itemprop='availability']", "href")])
        status, available = stock_status(stock)
        if product["availability"] is None and status:
            product["availability"] = status
        if product["in_stock"] is None and available is not None:
            product["in_stock"] = available
        if product["purchasable"] is None:
            add_button = soup.select_one("#product-addtocart-button, button.action.tocart, button[type='submit'][title*='Cart']")
            if add_button:
                product["purchasable"] = not add_button.has_attr("disabled")
            elif product["in_stock"] is not None:
                product["purchasable"] = product["in_stock"]

        # Product media/gallery.
        gallery_images = []
        for element in soup.select(".fotorama__stage__frame, .fotorama__nav__frame, [data-zoom-image], .product.media img, .gallery-placeholder img"):
            raw = element.get("data-zoom-image") or element.get("href") or element.get("src") or element.get("data-src")
            img = absolute_url(raw, url)
            if img and "placeholder" not in img.lower() and img not in gallery_images:
                gallery_images.append(img)
        if not gallery_images:
            gallery_images = all_images(soup.select_one(".product.media") or soup, url)
        for img in gallery_images:
            if img not in product["images"]:
                product["images"].append(img)
        if product["image"] is None and product["images"]:
            product["image"] = product["images"][0]

        # Description and highlights.
        if product["description"] is None:
            product["description"] = first_text(soup, [
                "#description .value", ".product.attribute.description .value",
                "[itemprop='description']", ".product-description"
            ])
        if not product["highlights"]:
            product["highlights"] = all_text(soup, [
                ".product-highlights li", ".product-highlights p",
                ".product.attribute.overview li", "#description li"
            ])

        product["specifications"] = product["specifications"] or extract_specifications(soup)
        product["breadcrumbs"] = product["breadcrumbs"] or extract_breadcrumbs(soup)
        product["category"] = product["category"] or (
            product["breadcrumbs"][-1] if product["breadcrumbs"] else None
        )
        product["variants"] = product["variants"] or extract_variants(soup)

        if product["rating"] is None:
            product["rating"] = number(first_attr(soup, [
                ("[itemprop='ratingValue']", "content"), (".rating-result", "title")
            ]) or first_text(soup, [".rating-summary .rating-result", "[itemprop='ratingValue']"]))
        if product["rating_count"] is None:
            product["rating_count"] = number(first_attr(soup, [("[itemprop='ratingCount']", "content")]))
        if product["review_count"] is None:
            product["review_count"] = number(first_attr(soup, [("[itemprop='reviewCount']", "content")]))
            if product["review_count"] is None:
                review_count_text = first_text(soup, [".reviews-actions a", "#reviews .toolbar-number"])
                product["review_count"] = number(review_count_text)

        if not product["reviews"]:
            product["reviews"] = extract_reviews(soup)

        # Seller and brand-store links.
        if product["seller"] is None:
            product["seller"] = first_text(soup, ["[itemprop='seller']", ".product-seller", ".seller-name"])
        store_link = soup.select_one("a[href*='brand'], a[href*='manufacturer']")
        if store_link:
            product["brand_store_url"] = absolute_url(store_link.get("href"), url)
        if product["seller_url"] is None:
            seller_link = soup.select_one("[itemprop='seller'] a, .product-seller a, .seller-name a")
            if seller_link:
                product["seller_url"] = absolute_url(seller_link.get("href"), url)

        # Delivery/returns/payment are only populated when text is found on the page.
        page_text = clean_text(soup.get_text(" ", strip=True)) or ""
        product["shipping"] = product["shipping"] or first_text(soup, [
            ".shipping-info", ".shipping-message", ".delivery-info", "[class*='shipping']"
        ])
        product["shipping_cost"] = product["shipping_cost"] or number(first_text(soup, [
            ".shipping-cost", ".delivery-charge", "[data-shipping-cost]"
        ]))
        product["delivery"] = product["delivery"] or first_text(soup, [
            ".delivery-estimate", ".delivery-time", ".shipping-estimate", "[class*='delivery']"
        ])
        product["delivery_area"] = product["delivery_area"] or first_text(soup, [
            ".delivery-area", ".delivery-location", "[class*='delivery-area']"
        ])
        product["return_policy"] = product["return_policy"] or first_text(soup, [
            ".return-policy", ".returns-policy", "[class*='return-policy']"
        ])
        product["payment_options"] = product["payment_options"] or all_text(soup, [
            ".payment-methods li", ".payment-options li", "[class*='payment'] li"
        ])

        meta = soup.select_one("meta[name='description']")
        if meta:
            product["meta_description"] = clean_text(meta.get("content"))

        # Final currency fallback is the site's stated currency, not a guessed price.
        if not product["currency"]:
            product["currency"] = "PKR"
        return product
    except Exception as exc:
        print(f"Detail enrichment skipped for {url}: {exc}", file=sys.stderr)
        return product


# ------------------------------ Scraper ------------------------------

def scrape_naheed(keyword: str, limit: int) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    max_pages = min(max((limit + 19) // 20, 1), 10)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-PK",
            timezone_id="Asia/Karachi",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            for page_number in range(1, max_pages + 1):
                # p is internal pagination, not a user-facing API parameter.
                search_url = f"{SEARCH_URL}?q={quote(keyword)}"
                if page_number > 1:
                    search_url += f"&p={page_number}"
                print(f"Searching Naheed: {search_url}")

                response = page.goto(search_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
                if response and response.status >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Naheed.pk returned HTTP {response.status}.",
                    )
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1000)

                # Scroll to encourage lazy loading.
                for _ in range(2):
                    page.mouse.wheel(0, 850)
                    page.wait_for_timeout(250)

                soup = BeautifulSoup(page.content(), "html.parser")
                cards = []
                for selector in [
                    "li.product-item", ".products-grid .product-item",
                    ".product-items .product-item", ".product-item",
                    ".item.product", ".products-grid .item", ".product-card"
                ]:
                    cards = soup.select(selector)
                    if cards:
                        break

                page_products = [p for p in (parse_card(card) for card in cards) if p]
                if not page_products:
                    for obj in jsonld_product_objects(soup):
                        p = blank_product()
                        apply_jsonld(p, obj)
                        if p["title"] and p["product_url"]:
                            page_products.append(p)

                added = 0
                for product in page_products:
                    url = product.get("product_url")
                    if not url or url in seen_urls:
                        continue
                    host = (urlparse(url).hostname or "").lower()
                    if host not in ("naheed.pk", "www.naheed.pk"):
                        continue
                    seen_urls.add(url)
                    products.append(product)
                    added += 1
                    if len(products) >= limit:
                        break

                if len(products) >= limit or added == 0:
                    break

            # Visit product pages to collect detail-page fields and reviews.
            if products:
                detail_page = context.new_page()
                try:
                    for index, product in enumerate(products, start=1):
                        print(f"Enriching product {index}/{len(products)}: {product.get('product_url')}")
                        detail_enrich(detail_page, product)
                finally:
                    detail_page.close()
        finally:
            context.close()
            browser.close()

    # Keep a stable schema and marketplace identifier on every record.
    # Return a compact comparison-friendly schema. Remove fields the user
    # does not want in the output, even if internal extraction used them.
    removed_fields = {
        "in_stock", "purchasable", "sku", "brand_store_url", "highlights",
        "specifications", "shipping", "shipping_cost", "return_policy",
        "payment_options", "official_store", "seller_url", "meta_description",
    }
    for product in products[:limit]:
        product["platform"] = PLATFORM
        if product.get("discount") is None:
            product["discount"] = discount_percent(product.get("price"), product.get("original_price"))
        if not product.get("image") and product.get("images"):
            product["image"] = product["images"][0]
        # Human-readable fallback labels instead of null/empty values.
        if product.get("rating") is None:
            product["rating"] = "No ratings yet"
        if not product.get("reviews"):
            product["reviews"] = "No reviews"
        for field in removed_fields:
            product.pop(field, None)
    return products[:limit]


# ------------------------------ API ------------------------------

@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "Naheed.pk Product Search API",
        "platform": PLATFORM,
        "status": "running",
        "search_endpoint": "/api/search?keyword=surf&limit=20",
        "documentation": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "Naheed.pk Product Search API", "platform": PLATFORM}


@app.get("/api/search")
@app.get("/scrape", include_in_schema=False)
def search_products(
    keyword: str = Query(..., min_length=1, max_length=120, description="Product search keyword"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum products to return"),
) -> dict[str, Any]:
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Provide a keyword, e.g. ?keyword=surf&limit=20")
    try:
        products = scrape_naheed(keyword, limit)
    except PlaywrightTimeoutError as exc:
        raise HTTPException(status_code=504, detail="Naheed.pk took too long to respond.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        print("Naheed scraper error:", repr(exc), file=sys.stderr)
        raise HTTPException(
            status_code=502,
            detail="Could not scrape Naheed.pk. The site may be unavailable, blocking automation, or using a changed page structure.",
        ) from exc

    return {
        "success": True,
        "platform": PLATFORM,
        "source": BASE_URL,
        "keyword": keyword,
        "search_url": f"{SEARCH_URL}?q={quote(keyword)}",
        "count": len(products),
        "products": products,
        "note": (
            "Values are extracted from public Naheed.pk listing/product-page content. "
            "Fields not published or not accessible remain null or empty. "
            "Product pages are visited for detail enrichment, so requests can take longer."
        ),
    }


if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("NAHEED.PK PRODUCT SEARCH API")
    print("Server: http://127.0.0.1:8000")
    print("Docs:   http://127.0.0.1:8000/docs")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
