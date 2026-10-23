"""
Shopon.pk Product Scraper API
=============================
Extracts real product data from Shopon.pk search and product-detail pages.
Shopon runs on CS-Cart, so selectors target CS-Cart markup (ty-* classes and
id="sec_discounted_price_<id>" style ids). Anything not found stays None/empty:
nothing is ever invented.

Install:
    pip install fastapi uvicorn playwright beautifulsoup4
    python -m playwright install chromium

Run:
    python shopon.py

API:
    http://127.0.0.1:8000/api/search?keyword=harpic&limit=20
    http://127.0.0.1:8000/api/debug?url=<product page url>   (diagnostics)
    http://127.0.0.1:8000/docs
"""

import json
import re
import sys
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://shopon.pk"
SEARCH_URL = f"{BASE_URL}/?dispatch=products.search&search_performed=Y"
PLATFORM = "Shopon"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
BROWSER_TIMEOUT = 60000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

app = FastAPI(
    title="Shopon.pk Product Search API",
    description="Extracts product data from Shopon.pk search and product pages.",
    version="2.0.0",
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
    if value is None or isinstance(value, (dict, list)):
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
    if node is None:
        return None
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            value = clean_text(found.get_text(" ", strip=True))
            if value:
                return value
    return None


def first_attr(node: Any, selector_attrs: list[tuple[str, str]]) -> Optional[str]:
    if node is None:
        return None
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
    if node is None:
        return result
    for selector in selectors:
        for element in node.select(selector):
            value = clean_text(element.get_text(" ", strip=True))
            if value and value not in seen:
                seen.add(value)
                result.append(value)
    return result


def first_number(node: Any, selectors: list[str]) -> Optional[int | float]:
    """Return the first selector match whose text actually contains a number.

    This matters on CS-Cart: the price is rendered as
        <span class="ty-price-num">Rs.</span><span class="ty-price-num">1,200</span>
    so a plain select_one('.ty-price-num') grabs the currency symbol and the
    parsed price is None. Here every match is checked until a number is found.
    """
    if node is None:
        return None
    for selector in selectors:
        for element in node.select(selector):
            n = number(element.get_text(" ", strip=True))
            if n is not None:
                return n
    return None


def is_thumbnail_junk(url: str) -> bool:
    low = url.lower()
    return any(term in low for term in (
        "placeholder", "blank.gif", "pinterest.", "/design/themes/", "/images/logos/",
        "footer-logo", "header-logo", "payment-icon", "social-", "no_image",
    ))


def all_images(node: Any, base_url: str) -> list[str]:
    result, seen = [], set()
    if node is None:
        return result
    for img in node.select("img"):
        candidate = (img.get("data-src") or img.get("data-original") or
                     img.get("data-lazy-src") or img.get("src"))
        url = absolute_url(candidate, base_url)
        if url and url not in seen and not is_thumbnail_junk(url):
            seen.add(url)
            result.append(url)
    return result


def stock_status(value: Any) -> tuple[Optional[str], Optional[bool]]:
    text = (clean_text(value) or "").lower()
    if not text:
        return None, None
    if any(x in text for x in ("out of stock", "out-of-stock", "sold out", "unavailable")):
        return "Out of Stock", False
    if any(x in text for x in ("in stock", "in-stock", "instock", "available")):
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


def blank_product() -> dict[str, Any]:
    return {
        "platform": PLATFORM, "title": None, "price": None, "original_price": None,
        "discount": None, "currency": "PKR", "availability": None, "in_stock": None,
        "purchasable": None, "seller": None, "seller_rating": None, "rating": None,
        "rating_count": None, "review_count": None, "reviews": [], "image": None,
        "images": [], "product_url": None, "product_id": None, "sku": None,
        "brand": None, "brand_store_url": None, "category": None, "breadcrumbs": [],
        "description": None, "highlights": [], "specifications": {}, "variants": [],
        "shipping": None, "shipping_cost": None, "delivery": None, "delivery_area": None,
        "return_policy": None, "payment_options": [], "official_store": None,
        "seller_url": None, "meta_description": None,
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


def get_offer(obj: dict[str, Any]) -> dict[str, Any]:
    offers = obj.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def apply_jsonld(product: dict[str, Any], obj: dict[str, Any], page_url: str = BASE_URL) -> None:
    offer = get_offer(obj)
    product["title"] = product["title"] or clean_text(obj.get("name"))
    product["product_url"] = product["product_url"] or absolute_url(obj.get("url"), page_url)
    product["sku"] = product["sku"] or clean_text(obj.get("sku") or obj.get("mpn"))
    product["product_id"] = product["product_id"] or clean_text(obj.get("productID") or obj.get("sku"))
    price_spec = offer.get("priceSpecification")
    if isinstance(price_spec, list):
        price_spec = price_spec[0] if price_spec else {}
    if not isinstance(price_spec, dict):
        price_spec = {}
    if product["price"] is None:
        product["price"] = number(offer.get("price") or offer.get("lowPrice") or price_spec.get("price"))
    if offer.get("priceCurrency"):
        product["currency"] = clean_text(offer.get("priceCurrency")) or product["currency"]
    status, in_stock = stock_status(str(offer.get("availability") or "").rsplit("/", 1)[-1])
    product["availability"] = product["availability"] or status
    if product["in_stock"] is None:
        product["in_stock"] = in_stock
    brand = obj.get("brand")
    product["brand"] = product["brand"] or clean_text(brand.get("name") if isinstance(brand, dict) else brand)
    aggregate = obj.get("aggregateRating")
    if isinstance(aggregate, dict):
        if product["rating"] is None:
            product["rating"] = number(aggregate.get("ratingValue"))
        if product["rating_count"] is None:
            product["rating_count"] = number(aggregate.get("ratingCount"))
        if product["review_count"] is None:
            product["review_count"] = number(aggregate.get("reviewCount"))
    if not product["images"]:
        raw = obj.get("image")
        raw = raw if isinstance(raw, list) else ([raw] if raw else [])
        for item in raw:
            if isinstance(item, dict):
                item = item.get("url") or item.get("contentUrl")
            url = absolute_url(item, page_url)
            if url and url not in product["images"]:
                product["images"].append(url)
    product["image"] = product["image"] or (product["images"][0] if product["images"] else None)
    product["description"] = product["description"] or clean_text(obj.get("description"))


# ------------------------------ CS-Cart price logic ------------------------------

# Selectors are ordered from most to least specific.
SALE_PRICE_SELECTORS = [
    '[id^="sec_discounted_price_"]',
    '[id^="discounted_price_"]',
    '[id^="line_discounted_price_"] .ty-price-num',
    ".ty-price .ty-price-num",
    ".ty-price-num",
    ".ty-price",
]
LIST_PRICE_SELECTORS = [
    '[id^="sec_list_price_"]',
    '[id^="list_price_"]',
    ".ty-list-price .ty-price-num",
    ".ty-list-price",
    ".ty-strike .ty-price-num",
    ".ty-strike",
    "del",
    "s",
]
RS_REGEX = re.compile(r"(?:Rs\.?|PKR)\s*([\d,]+(?:\.\d+)?)", re.I)


def extract_prices(scope: Any) -> tuple[Optional[float], Optional[float]]:
    """Return (price, original_price) read from the real price markup in `scope`."""
    price = first_number(scope, SALE_PRICE_SELECTORS)
    original = first_number(scope, LIST_PRICE_SELECTORS)

    # Text fallback: look at "Rs. 1,200" patterns, but only inside price containers.
    if price is None and scope is not None:
        for container in scope.select('[class*="price"], [id*="price"]'):
            amounts = [number(m) for m in RS_REGEX.findall(container.get_text(" ", strip=True))]
            amounts = [a for a in amounts if a is not None]
            if amounts:
                price = amounts[0]
                if original is None and len(amounts) > 1 and amounts[1] > price:
                    original = amounts[1]
                break

    if original is not None and price is not None and original <= price:
        original = None
    return price, original


def card_stock(scope: Any) -> tuple[Optional[str], Optional[bool]]:
    text = first_text(scope, [
        '[id^="in_stock_info_"]', ".ty-qty-in-stock", ".ty-qty-out-of-stock",
        ".stock", ".availability",
    ])
    if not text:
        text = first_attr(scope, [("[itemprop='availability']", "href")])
    return stock_status(text)


# ------------------------------ Search cards ------------------------------

CARD_SELECTORS = [
    ".ty-grid-list__item", ".ty-product-list", ".ty-compact-list__item",
    ".ty-column3", ".ty-column4", ".ty-column5", ".ty-scroller-list__item",
]
TITLE_SELECTORS = [
    "a.product-title", ".ty-grid-list__item-name a", ".ty-product-list__item-name a",
    ".ty-product-name a", ".product-title a", "[itemprop='name']",
]


def parse_card(card: Any, page_url: str) -> Optional[dict[str, Any]]:
    p = blank_product()
    anchor = None
    for selector in TITLE_SELECTORS:
        anchor = card.select_one(selector)
        if anchor and anchor.get("href"):
            break
        anchor = None
    if anchor is None:
        return None
    p["title"] = clean_text(anchor.get("title") or anchor.get_text(" ", strip=True))
    p["product_url"] = absolute_url(anchor.get("href"), page_url)
    if not p["title"] or not p["product_url"]:
        return None

    p["price"], p["original_price"] = extract_prices(card)
    p["discount"] = discount_percent(p["price"], p["original_price"])
    p["images"] = all_images(card, p["product_url"])
    p["image"] = p["images"][0] if p["images"] else None
    p["availability"], p["in_stock"] = card_stock(card)
    p["product_id"] = first_attr(card, [
        ("input[name$='[product_id]']", "value"), ("[data-ca-product-id]", "data-ca-product-id"),
    ])
    return p


def products_from_images(soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    """Fallback when card classes are unknown: product images live under
    /images/thumbnails/.../detailed/..., and sit inside the product's <a>."""
    results, seen = [], set()
    for img in soup.select('img[src*="/images/thumbnails/"][src*="/detailed/"], '
                           'img[data-src*="/images/thumbnails/"]'):
        anchor = img.find_parent("a", href=True)
        if not anchor:
            continue
        url = absolute_url(anchor.get("href"), page_url)
        if not url or url in seen or not is_shopon_url(url):
            continue
        path = urlparse(url).path.strip("/")
        if not path or "dispatch=" in url:
            continue
        seen.add(url)
        node = anchor
        for _ in range(6):  # climb to the container that also holds the price
            parent = node.parent
            if parent is None or parent.name in ("body", "html"):
                break
            node = parent
            if node.select_one('[class*="price"]') and len(node.select("img")) <= 3:
                break
        item = parse_card(node, page_url) or blank_product()
        item["title"] = item["title"] or clean_text(anchor.get("title") or img.get("alt") or img.get("title"))
        item["product_url"] = item["product_url"] or url
        if not item["title"]:
            continue
        if not item["images"]:
            item["images"] = all_images(anchor, page_url)
            item["image"] = item["images"][0] if item["images"] else None
        results.append(item)
    return results


def is_shopon_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in {"shopon.pk", "www.shopon.pk"}


def products_from_soup(soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    products, seen = [], set()
    for selector in CARD_SELECTORS:
        for card in soup.select(selector):
            item = parse_card(card, page_url)
            if item and is_shopon_url(item["product_url"]):
                url = item["product_url"].split("#", 1)[0]
                if url not in seen:
                    seen.add(url)
                    item["product_url"] = url
                    products.append(item)
        if products:
            return products
    for item in products_from_images(soup, page_url):
        url = item["product_url"].split("#", 1)[0]
        if url not in seen:
            seen.add(url)
            item["product_url"] = url
            products.append(item)
    if products:
        return products
    for obj in jsonld_product_objects(soup):
        item = blank_product()
        apply_jsonld(item, obj, page_url)
        if item["title"] and item["product_url"] and is_shopon_url(item["product_url"]):
            products.append(item)
    return products


def page_looks_blocked(title: str, soup: BeautifulSoup) -> bool:
    text = (soup.get_text(" ", strip=True)[:6000]).lower()
    markers = ("access denied", "403 forbidden", "captcha", "verify you are human",
               "checking your browser", "just a moment", "request blocked")
    return any(m in (title or "").lower() or m in text for m in markers)


# ------------------------------ Detail page ------------------------------


def stars_from(node: Any) -> Optional[float]:
    """CS-Cart renders ratings as icon classes: ty-icon-star, ty-icon-star-half, ty-icon-star-empty."""
    if node is None:
        return None
    stars = node.select(".ty-icon-star, .ty-icon-star-half, .ty-icon-star-empty")
    if not stars:
        return None
    total = 0.0
    for s in stars:
        classes = " ".join(s.get("class", []))
        if "star-empty" in classes:
            continue
        total += 0.5 if "star-half" in classes else 1.0
    return total


def extract_features(soup: BeautifulSoup) -> dict[str, str]:
    features: dict[str, str] = {}
    for row in soup.select(".ty-product-feature"):
        label = first_text(row, [".ty-product-feature__label", ".ty-product-feature__prefix"])
        value = first_text(row, [".ty-product-feature__value", ".ty-product-feature__suffix"])
        if label and value:
            features[label.rstrip(": ")] = value
    return features


def extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    values = all_text(soup, [
        ".ty-breadcrumbs a", ".ty-breadcrumbs__current", ".ty-breadcrumbs__current-title",
        "[itemtype*='BreadcrumbList'] [itemprop='name']",
    ])
    return [v for v in values if v.lower() not in ("home", "you are here:")]


def extract_variants(scope: Any) -> list[dict[str, Any]]:
    variants, seen = [], set()
    if scope is None:
        return variants
    for option in scope.select("select[id^='option_'] option, .ty-product-options select option"):
        label, value = clean_text(option.get_text(" ", strip=True)), option.get("value")
        if not label or value in (None, "", "0"):
            continue
        if (label, str(value)) not in seen:
            seen.add((label, str(value)))
            variants.append({"label": label, "value": str(value)})
    return variants


def extract_reviews(soup: BeautifulSoup) -> list[dict[str, Any]]:
    reviews, seen = [], set()
    for item in soup.select("#content_discussion .ty-discussion-post, .ty-discussion-post"):
        author = first_text(item, [".ty-discussion-post__author", ".ty-discussion-post__user"])
        body = first_text(item, [".ty-discussion-post__message", ".ty-discussion-post__content"])
        date = first_text(item, [".ty-discussion-post__date", "time"])
        rating = stars_from(item)
        if not any((author, body, date)):
            continue
        key = (author, body, date)
        if key not in seen:
            seen.add(key)
            reviews.append({"author": author, "rating": rating, "title": None, "text": body, "date": date})
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
            page.wait_for_selector('[class*="price"]', timeout=8000)
        except PlaywrightTimeoutError:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(500)
        soup = BeautifulSoup(page.content(), "html.parser")

        for obj in jsonld_product_objects(soup):
            apply_jsonld(product, obj, url)
            if product["title"]:
                break

        # Restrict price/stock lookups to the main product block so that prices from
        # "related products" carousels are never mistaken for this product's price.
        main = (soup.select_one(".ty-product-block")
                or soup.select_one("form[name^='product_form_']")
                or soup.select_one(".ty-product-detail")
                or soup)

        product["title"] = product["title"] or first_text(soup, [
            "h1.ty-product-block-title", ".ty-product-block-title", "h1"])

        price, original = extract_prices(main)
        if product["price"] is None:
            product["price"] = price
            if product["original_price"] is None:
                product["original_price"] = original
        elif product["original_price"] is None:
            product["original_price"] = original
        if product["price"] is None:
            product["price"] = number(first_attr(soup, [
                ("meta[property='product:price:amount']", "content"),
                ("meta[property='og:price:amount']", "content"),
                ("[itemprop='price']", "content"),
            ]))
        if product["original_price"] is not None and product["price"] is not None \
                and product["original_price"] <= product["price"]:
            product["original_price"] = None
        product["discount"] = product["discount"] or discount_percent(product["price"], product["original_price"])

        cur = first_attr(soup, [("meta[property='product:price:currency']", "content"),
                                ("[itemprop='priceCurrency']", "content")])
        product["currency"] = cur or product["currency"] or "PKR"

        features = extract_features(soup)
        if not product["brand"]:
            for key in ("brand", "manufacturer", "company"):
                for label, value in features.items():
                    if key in label.lower():
                        product["brand"] = value
                        break
                if product["brand"]:
                    break
        product["brand"] = product["brand"] or first_text(main, [".ty-product-block__brand", ".ty-brand-name"])

        product["sku"] = product["sku"] or first_text(main, ['[id^="sku_"]', ".ty-product-block__sku .ty-control-group__item"])
        product["product_id"] = (product["product_id"]
                                 or first_attr(main, [("input[name$='[product_id]']", "value"),
                                                      ("input[name='product_id']", "value")])
                                 or product["sku"])

        status, available = card_stock(main)
        product["availability"] = product["availability"] or status
        if product["in_stock"] is None:
            product["in_stock"] = available
        button = main.select_one('[id^="button_cart_"], .ty-btn__add-to-cart') if main else None
        if button is not None:
            product["purchasable"] = not button.has_attr("disabled")
        elif available is not None:
            product["purchasable"] = available

        # Gallery: CS-Cart stores large images in the previewer link href.
        gallery = []
        for el in main.select("a.cm-image-previewer[href], a[data-ca-image-id][href]"):
            img = absolute_url(el.get("href"), url)
            if img and not is_thumbnail_junk(img) and img not in gallery:
                gallery.append(img)
        if not gallery:
            root = main.select_one(".ty-product-img, .ty-product-block__img-wrapper, .ty-product-thumbnails") or main
            gallery = all_images(root, url)
        if gallery:
            product["images"] = gallery
            product["image"] = gallery[0]

        product["description"] = product["description"] or first_text(soup, [
            "#content_description", ".ty-product-block__description", "[itemprop='description']",
            ".ty-wysiwyg-content"])
        product["breadcrumbs"] = product["breadcrumbs"] or extract_breadcrumbs(soup)
        product["category"] = product["category"] or (
            product["breadcrumbs"][-2] if len(product["breadcrumbs"]) >= 2 else None)
        product["variants"] = product["variants"] or extract_variants(main)

        rating_box = soup.select_one(".ty-discussion__rating-wrapper, .ty-product-block__rating, .ty-discussion-rating")
        if product["rating"] is None:
            product["rating"] = number(first_attr(soup, [("[itemprop='ratingValue']", "content")])) \
                or stars_from(rating_box)
            if product["rating"] == 0:
                product["rating"] = None
        reviews_tab = first_text(soup, ["#discussion", ".ty-discussion__review-numbers",
                                        ".ty-product-review-reviews-count"])
        if product["review_count"] is None and reviews_tab and re.search(r"review|rating", reviews_tab, re.I):
            product["review_count"] = number(reviews_tab)
        if not product["reviews"]:
            product["reviews"] = extract_reviews(soup)
        if product["review_count"] is None and product["reviews"]:
            product["review_count"] = len(product["reviews"])

        product["shipping"] = product["shipping"] or first_text(soup, [".shipping-info", "[class*='shipping']"])
        product["delivery"] = product["delivery"] or first_text(soup, [".delivery-info", "[class*='delivery']"])
        product["return_policy"] = product["return_policy"] or first_text(soup, [".return-policy", "[class*='return-policy']"])

        meta = soup.select_one("meta[name='description']")
        if meta:
            product["meta_description"] = clean_text(meta.get("content"))
    except Exception as exc:
        print(f"Detail enrichment skipped for {url}: {exc}", file=sys.stderr)
    return product


# ------------------------------ Scraper ------------------------------


def new_context(playwright: Any) -> tuple[Any, Any]:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000}, locale="en-PK",
        timezone_id="Asia/Karachi", user_agent=USER_AGENT,
    )
    return browser, context


def scrape_shopon(keyword: str, limit: int) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    diagnostics: list[dict[str, Any]] = []
    max_pages = min(max((limit + 19) // 20, 1), 10)

    with sync_playwright() as playwright:
        browser, context = new_context(playwright)
        page = context.new_page()
        try:
            for page_number in range(1, max_pages + 1):
                search_url = f"{SEARCH_URL}&q={quote_plus(keyword)}"
                if page_number > 1:
                    search_url += f"&page={page_number}"
                print(f"Searching Shopon: {search_url}")
                try:
                    response = page.goto(search_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
                except PlaywrightTimeoutError:
                    diagnostics.append({"url": search_url, "status": "timeout"})
                    break
                status = response.status if response else None
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(800)
                for _ in range(3):  # trigger lazy-loaded cards
                    page.mouse.wheel(0, 900)
                    page.wait_for_timeout(250)

                soup = BeautifulSoup(page.content(), "html.parser")
                title = clean_text(page.title()) or ""
                blocked = page_looks_blocked(title, soup)
                page_products = [] if (blocked or (status and status >= 400)) else products_from_soup(soup, page.url)
                diagnostics.append({
                    "url": search_url, "final_url": page.url, "http_status": status, "title": title,
                    "blocked_or_challenge": blocked, "html_length": len(str(soup)),
                    "product_candidates": len(page_products),
                })

                added = 0
                for item in page_products:
                    url = item["product_url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    products.append(item)
                    added += 1
                    if len(products) >= limit:
                        break
                if len(products) >= limit or added == 0:
                    break

            if products:
                products = products[:limit]
                detail_page = context.new_page()
                try:
                    for i, product in enumerate(products, start=1):
                        print(f"Enriching product {i}/{len(products)}: {product.get('product_url')}")
                        detail_enrich(detail_page, product)
                finally:
                    detail_page.close()
        finally:
            context.close()
            browser.close()

    if not products:
        blocked = any(d.get("blocked_or_challenge") for d in diagnostics)
        raise HTTPException(status_code=502, detail={
            "reason": "Shopon served a bot-check/access-denied page" if blocked
            else "No products were detected on the search page",
            "attempts": diagnostics,
        })

    removed_fields = {
        "in_stock", "purchasable", "sku", "brand_store_url", "highlights", "specifications",
        "shipping", "shipping_cost", "return_policy", "payment_options", "official_store",
        "seller_url", "meta_description",
    }
    for product in products:
        product["platform"] = PLATFORM
        if product.get("discount") is None:
            product["discount"] = discount_percent(product.get("price"), product.get("original_price"))
        if not product.get("image") and product.get("images"):
            product["image"] = product["images"][0]
        if product.get("rating") is None:
            product["rating"] = "No ratings yet"
        if not product.get("reviews"):
            product["reviews"] = "No reviews"
        for field in removed_fields:
            product.pop(field, None)
    return products


# ------------------------------ API ------------------------------


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "Shopon.pk Product Search API", "platform": PLATFORM, "status": "running",
        "search_endpoint": "/api/search?keyword=harpic&limit=20",
        "debug_endpoint": "/api/debug?url=https://shopon.pk/<product-page>/",
        "documentation": "/docs", "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "Shopon.pk Product Search API", "platform": PLATFORM}


@app.get("/api/search")
@app.get("/scrape", include_in_schema=False)
def search_products(
    keyword: str = Query(..., min_length=1, max_length=120, description="Product search keyword"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum products to return"),
) -> dict[str, Any]:
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Provide a keyword, e.g. ?keyword=harpic&limit=20")
    try:
        products = scrape_shopon(keyword, limit)
    except PlaywrightTimeoutError as exc:
        raise HTTPException(status_code=504, detail="Shopon.pk took too long to respond.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        print("Shopon scraper error:", repr(exc), file=sys.stderr)
        raise HTTPException(status_code=502, detail="Could not scrape Shopon.pk. The site may be unavailable, "
                                                    "blocking automation, or using a changed page structure.") from exc
    return {
        "success": True, "platform": PLATFORM, "source": BASE_URL, "keyword": keyword,
        "search_url": f"{SEARCH_URL}&q={quote_plus(keyword)}",
        "count": len(products), "products": products,
        "note": ("Values are extracted from public Shopon.pk listing/product-page content. "
                 "Fields not published on the page remain null or empty."),
    }


@app.get("/api/debug")
def debug_page(url: str = Query(..., description="A Shopon.pk product or search URL")) -> dict[str, Any]:
    """Diagnostics: saves the rendered HTML to debug_shopon.html and reports what each
    price selector sees. If a price is still null, send me that HTML file."""
    if not is_shopon_url(url):
        raise HTTPException(status_code=400, detail="Only shopon.pk URLs are allowed.")
    with sync_playwright() as playwright:
        browser, context = new_context(playwright)
        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1000)
            html = page.content()
        finally:
            context.close()
            browser.close()
    with open("debug_shopon.html", "w", encoding="utf-8") as fh:
        fh.write(html)
    soup = BeautifulSoup(html, "html.parser")
    price, original = extract_prices(soup)
    return {
        "saved_html": "debug_shopon.html", "html_length": len(html),
        "title": clean_text(soup.title.string if soup.title else None),
        "looks_blocked": page_looks_blocked(soup.title.string if soup.title else "", soup),
        "parsed_price": price, "parsed_original_price": original,
        "selector_hits": {s: [clean_text(e.get_text(" ", strip=True)) for e in soup.select(s)][:5]
                          for s in SALE_PRICE_SELECTORS + LIST_PRICE_SELECTORS},
        "rs_matches_in_text": RS_REGEX.findall(soup.get_text(" ", strip=True))[:10],
        "jsonld_products": len(jsonld_product_objects(soup)),
        "price_meta_tags": [m.get("content") for m in soup.select(
            "meta[property='product:price:amount'], meta[property='og:price:amount']")],
    }


if __name__ == "__main__":
    import uvicorn
    print("=" * 55)
    print("SHOPON.PK PRODUCT SEARCH API")
    print("Server: http://127.0.0.1:8000")
    print("Docs:   http://127.0.0.1:8000/docs")
    print("=" * 55)
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)