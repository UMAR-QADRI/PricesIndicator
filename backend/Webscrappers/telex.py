"""
Telex.pk Product Scraper API  (v2.0)
====================================
Telex.pk is a Shopify store. This scraper is theme-independent:
  1. Finds product links (/products/<handle>) on the search pages (48 per page).
  2. Pulls full data from Shopify's /products/<handle>.js endpoint.
  3. Parses the product page HTML for specs, breadcrumbs, reviews, shipping, etc.

Install:
    pip install fastapi uvicorn playwright beautifulsoup4
    python -m playwright install chromium

Run:
    python telex.py

API:
    http://127.0.0.1:8000/api/search?keyword=iphone%2017&limit=20
    http://127.0.0.1:8000/docs

Unavailable fields stay None / empty; nothing is fabricated.
"""

import asyncio
import json
import re
import sys
from typing import Any, Optional, Union
from urllib.parse import quote, urljoin, urlparse

# On Windows, Playwright needs the Proactor loop to spawn the browser process.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://www.telex.pk"
SEARCH_URL = f"{BASE_URL}/search"
PLATFORM = "Telex"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
PER_PAGE = 48
BROWSER_TIMEOUT = 60000
TELEX_HOSTS = ("telex.pk", "www.telex.pk")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PRICE_RE = re.compile(r"(?:Rs\.?|PKR)\s*([\d,]+(?:\.\d+)?)", re.I)

app = FastAPI(
    title="Telex.pk Product Search API",
    description="Extracts product data from Telex.pk search and product pages.",
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


def norm_num(n: Any) -> Optional[Union[int, float]]:
    if n is None:
        return None
    n = float(n)
    return int(n) if n.is_integer() else round(n, 2)


def number(value: Any) -> Optional[Union[int, float]]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return norm_num(value)
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text.replace("\xa0", " "))
    if not match:
        return None
    try:
        return norm_num(float(match.group(0).replace(",", "")))
    except (TypeError, ValueError):
        return None


def absolute_url(value: Any, base: str = BASE_URL) -> Optional[str]:
    if not value:
        return None
    value = str(value).strip()
    if value.startswith(("data:", "javascript:", "blob:", "#")):
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
    for img in node.select("img, source"):
        candidate = (
            img.get("data-src") or img.get("data-original") or img.get("data-lazy-src")
            or img.get("data-srcset") or img.get("src") or img.get("srcset")
        )
        if candidate and "," in candidate and " " in candidate:
            candidate = candidate.split(",")[0].strip().split(" ")[0]
        url = absolute_url(candidate, base_url)
        if url and url not in seen and not any(
            m in url.lower() for m in ("placeholder", "no-image", "loading", "logo", "payment-method")
        ):
            seen.add(url)
            result.append(url)
    return result


def stock_status(value: Any) -> tuple[Optional[str], Optional[bool]]:
    text = (clean_text(value) or "").lower()
    if not text:
        return None, None
    compact = re.sub(r"[\s_\-]+", "", text)
    if any(x in compact for x in ("outofstock", "soldout", "unavailable", "notavailable", "discontinued")):
        return "Out of Stock", False
    if any(x in compact for x in ("instock", "available", "limitedavailability", "onlineonly")):
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


def handle_of(href: Any) -> Optional[str]:
    if not href:
        return None
    m = re.search(r"/products/([^/?#]+)", str(href))
    return m.group(1) if m else None


def is_internal(href: Any) -> bool:
    host = (urlparse(urljoin(BASE_URL, str(href))).hostname or "").lower()
    return host in TELEX_HOSTS


def dedupe_repeat(text: Optional[str]) -> Optional[str]:
    """'Foo Bar Foo Bar' -> 'Foo Bar' (themes often repeat the title in hidden spans)."""
    if not text:
        return text
    words = text.split()
    n = len(words)
    if n >= 2 and n % 2 == 0 and words[: n // 2] == words[n // 2:]:
        return " ".join(words[: n // 2])
    return text


def html_to_text(html: Any) -> Optional[str]:
    if not html:
        return None
    soup = BeautifulSoup(str(html), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text or None


def blank_product() -> dict[str, Any]:
    return {
        "platform": PLATFORM,
        "title": None,
        "handle": None,
        "price": None,
        "original_price": None,
        "discount": None,
        "currency": "PKR",
        "availability": None,
        "in_stock": None,
        "purchasable": None,
        "seller": None,
        "seller_rating": None,
        "seller_url": None,
        "vendor": None,
        "brand": None,
        "brand_store_url": None,
        "official_store": None,
        "rating": None,
        "rating_count": None,
        "review_count": None,
        "reviews": [],
        "image": None,
        "images": [],
        "product_url": None,
        "product_id": None,
        "sku": None,
        "barcode": None,
        "product_type": None,
        "category": None,
        "breadcrumbs": [],
        "tags": [],
        "description": None,
        "highlights": [],
        "specifications": {},
        "options": [],
        "variants": [],
        "weight_grams": None,
        "shipping": None,
        "shipping_cost": None,
        "delivery": None,
        "delivery_area": None,
        "return_policy": None,
        "payment_options": [],
        "created_at": None,
        "published_at": None,
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


def jsonld_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    for obj in jsonld_objects(soup):
        if type_matches(obj, "BreadcrumbList"):
            items = [i for i in (obj.get("itemListElement") or []) if isinstance(i, dict)]
            items.sort(key=lambda i: number(i.get("position")) or 0)
            names = []
            for i in items:
                name = i.get("name")
                if not name and isinstance(i.get("item"), dict):
                    name = i["item"].get("name")
                name = clean_text(name)
                if name:
                    names.append(name)
            if names:
                return names
    return []


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
    product["title"] = product["title"] or clean_text(obj.get("name"))
    product["sku"] = product["sku"] or clean_text(obj.get("sku") or obj.get("mpn"))

    price_spec = offer.get("priceSpecification")
    if isinstance(price_spec, list):
        price_spec = price_spec[0] if price_spec else {}
    if not isinstance(price_spec, dict):
        price_spec = {}
    if product["price"] is None:
        product["price"] = number(offer.get("price") or offer.get("lowPrice") or price_spec.get("price"))
    product["currency"] = clean_text(offer.get("priceCurrency")) or product["currency"] or "PKR"

    status, in_stock = stock_status(str(offer.get("availability") or "").rsplit("/", 1)[-1])
    product["availability"] = product["availability"] or status
    if product["in_stock"] is None:
        product["in_stock"] = in_stock
    if product["purchasable"] is None and in_stock is not None:
        product["purchasable"] = in_stock

    seller = offer.get("seller")
    product["seller"] = product["seller"] or clean_text(seller.get("name") if isinstance(seller, dict) else seller)
    product["brand"] = product["brand"] or brand_name(obj.get("brand"))

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
            url = absolute_url(item)
            if url and url not in product["images"]:
                product["images"].append(url)
    product["description"] = product["description"] or clean_text(obj.get("description"))

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


# ------------------------------ Network helpers ------------------------------

def fetch_json(context: Any, path: str) -> Optional[Any]:
    url = path if path.startswith("http") else BASE_URL + path
    for _ in range(2):
        try:
            response = context.request.get(url, timeout=20000, headers={"Accept": "application/json"})
            if response.status == 404:
                return None
            if response.ok:
                return response.json()
        except Exception as exc:
            print(f"fetch_json failed for {url}: {exc}", file=sys.stderr)
    return None


def fetch_text(context: Any, url: str) -> Optional[str]:
    for _ in range(2):
        try:
            response = context.request.get(url, timeout=25000)
            if response.ok:
                return response.text()
            if response.status == 404:
                return None
        except Exception as exc:
            print(f"fetch_text failed for {url}: {exc}", file=sys.stderr)
    return None


# ------------------------------ Specs from description HTML ------------------------------

def specs_from_html(html: Any) -> tuple[dict[str, str], list[str]]:
    specs: dict[str, str] = {}
    highlights: list[str] = []
    if not html:
        return specs, highlights
    soup = BeautifulSoup(str(html), "html.parser")

    def put(key: Any, value: Any, section: Optional[str] = None) -> None:
        key, value = clean_text(key), clean_text(value)
        if not key or not value:
            return
        key = key.rstrip(": ").strip()
        if not key or len(key) > 80:
            return
        if key in specs and specs[key] != value and section:
            key = f"{section} - {key}"
        specs.setdefault(key, value)

    for table in soup.select("table"):
        section = None
        for row in table.select("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                put(cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True), section)
            elif len(cells) == 1:
                section = clean_text(cells[0].get_text(" ", strip=True))
    for dl in soup.select("dl"):
        for label, value in zip(dl.select("dt"), dl.select("dd")):
            put(label.get_text(" ", strip=True), value.get_text(" ", strip=True))

    if not specs:  # fall back to "Key: Value" lines
        for line in soup.get_text("\n").splitlines():
            m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 /&()\-\.]{1,40}?)\s*:\s*(\S.*)$", line)
            if m:
                put(m.group(1), m.group(2))

    for li in soup.select("li"):
        text = clean_text(li.get_text(" ", strip=True))
        if text and len(text) <= 250 and text not in highlights:
            highlights.append(text)
    return specs, highlights[:30]


# ------------------------------ Search page parsing ------------------------------

def find_cards(soup: BeautifulSoup) -> tuple[list[str], dict[str, Any]]:
    """
    Theme-independent: find product links, then climb from each link to the largest
    ancestor that still contains only that one product's links = the product card.
    """
    def collect(scope: Any, exclude_chrome: bool) -> list[Any]:
        result = []
        for a in scope.select("a[href*='/products/']"):
            href = a.get("href")
            if not handle_of(href) or not is_internal(href):
                continue
            if exclude_chrome and a.find_parent(["header", "footer", "nav"]):
                continue
            result.append(a)
        return result

    main = soup.select_one("main, #MainContent, #main-content, [role='main']")
    anchors = collect(main, True) if main else []
    if not anchors:
        anchors = collect(soup, True)
    if not anchors:
        anchors = collect(soup, False)

    order: list[str] = []
    cards: dict[str, Any] = {}
    for a in anchors:
        h = handle_of(a.get("href"))
        if h in cards:
            continue
        node = a
        for _ in range(8):
            parent = node.parent
            if parent is None or parent.name in ("body", "html", "[document]"):
                break
            handles = {handle_of(x.get("href")) for x in parent.select("a[href*='/products/']")}
            handles.discard(None)
            if len(handles) > 1:
                break
            node = parent
        cards[h] = node
        order.append(h)
    return order, cards


def parse_card(handle: str, card: Any) -> dict[str, Any]:
    p = blank_product()
    p["handle"] = handle
    p["product_url"] = f"{BASE_URL}/products/{handle}"

    candidates: list[str] = []
    for a in card.select("a[href*='/products/']"):
        if handle_of(a.get("href")) != handle:
            continue
        for raw in (a.get("title"), a.get_text(" ", strip=True)):
            t = dedupe_repeat(clean_text(raw))
            if t:
                candidates.append(t)
    for img in card.select("img[alt]"):
        t = clean_text(img.get("alt"))
        if t:
            candidates.append(t)
    p["title"] = candidates[0] if candidates else None

    # Prices
    orig_node = card.select_one("del, s, strike, [class*='compare']")
    price_nodes = card.select("[class*='price'], .money")
    scope_text = " ".join(n.get_text(" ", strip=True) for n in price_nodes) or card.get_text(" ", strip=True)
    amounts: list[Union[int, float]] = []
    for m in PRICE_RE.findall(scope_text):
        n = number(m)
        if n is not None and n not in amounts:
            amounts.append(n)
    orig = number(orig_node.get_text(" ", strip=True)) if orig_node else None
    if orig is not None:
        price = next((a for a in amounts if a != orig), None)
    else:
        price = amounts[0] if amounts else None
        if len(amounts) > 1 and amounts[1] > amounts[0]:
            orig = amounts[1]
    if orig is not None and (price is None or orig <= price):
        orig = None
    p["price"], p["original_price"] = price, orig
    p["discount"] = discount_percent(price, orig)

    p["images"] = all_images(card, p["product_url"])
    p["image"] = p["images"][0] if p["images"] else None

    card_text = card.get_text(" ", strip=True)
    if re.search(r"sold out|out of stock", card_text, re.I):
        p["availability"], p["in_stock"] = "Out of Stock", False
        p["purchasable"] = False

    p["vendor"] = first_text(card, ["[class*='vendor']", "[class*='brand']", "[itemprop='brand']"])
    p["brand"] = p["seller"] = p["vendor"]
    return p


def suggest_fallback(context: Any, keyword: str, limit: int) -> list[dict[str, Any]]:
    data = fetch_json(
        context,
        f"/search/suggest.json?q={quote(keyword)}&resources%5Btype%5D=product&resources%5Blimit%5D={min(limit, 10)}",
    )
    items = (((data or {}).get("resources") or {}).get("results") or {}).get("products") or []
    out = []
    for it in items:
        h = handle_of(it.get("url")) or it.get("handle")
        if not h:
            continue
        p = blank_product()
        p["handle"] = h
        p["product_url"] = f"{BASE_URL}/products/{h}"
        p["title"] = clean_text(it.get("title"))
        p["price"] = number(it.get("price"))
        p["vendor"] = p["brand"] = p["seller"] = clean_text(it.get("vendor"))
        img = absolute_url(it.get("image"))
        if img:
            p["images"], p["image"] = [img], img
        out.append(p)
    return out[:limit]


def collect_search_results(page: Any, context: Any, keyword: str, limit: int) -> tuple[list[dict[str, Any]], Optional[int]]:
    found: dict[str, dict[str, Any]] = {}
    total: Optional[int] = None
    max_pages = min(-(-limit // PER_PAGE), 10)

    for page_number in range(1, max_pages + 1):
        url = f"{SEARCH_URL}?q={quote(keyword)}&type=product"
        if page_number > 1:
            url += f"&page={page_number}"
        print(f"Searching Telex: {url}")

        response = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
        status = response.status if response else None
        if status and status >= 400:
            raise HTTPException(status_code=502, detail=f"Telex.pk returned HTTP {status}.")
        try:
            page.wait_for_selector("a[href*='/products/']", state="attached", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        for _ in range(3):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(250)

        soup = BeautifulSoup(page.content(), "html.parser")
        title = clean_text(soup.title.get_text()) if soup.title else ""
        if re.search(r"just a moment|attention required|access denied|captcha", title or "", re.I):
            raise HTTPException(status_code=502, detail="Telex.pk served a bot-check page.")
        if total is None:
            m = re.search(r"(\d[\d,]*)\s+results?", title or "", re.I)
            if m:
                total = int(m.group(1).replace(",", ""))

        order, cards = find_cards(soup)
        print(f"  page {page_number}: status={status} title={title!r} products_found={len(order)}")

        new = 0
        for h in order:
            if h in found:
                continue
            found[h] = parse_card(h, cards[h])
            new += 1
            if len(found) >= limit:
                break
        if len(found) >= limit or new == 0:
            break

    if not found:
        print("  no cards parsed; trying Shopify suggest endpoint")
        for p in suggest_fallback(context, keyword, limit):
            found[p["handle"]] = p

    return list(found.values())[:limit], total


# ------------------------------ Shopify product JSON (.js) ------------------------------

def minor_units(variants: list[dict[str, Any]], hint: Any) -> bool:
    """Shopify .js usually gives paisa (x100). Detect the unit instead of assuming."""
    raws = []
    for v in variants:
        r = v.get("price")
        if isinstance(r, str) and "." in r:
            return False
        n = number(r)
        if n is not None:
            raws.append(n)
    if not raws:
        return True
    if hint is not None and abs(min(raws) - float(hint)) < 1:
        return False
    return True


def apply_shopify_js(p: dict[str, Any], data: dict[str, Any], hint: Any) -> None:
    p["title"] = clean_text(data.get("title")) or p["title"]
    if data.get("id") is not None:
        p["product_id"] = str(data["id"])
    vendor = clean_text(data.get("vendor"))
    if vendor:
        p["vendor"] = p["brand"] = p["seller"] = vendor
    p["product_type"] = clean_text(data.get("type"))
    p["created_at"] = clean_text(data.get("created_at"))
    p["published_at"] = clean_text(data.get("published_at"))

    tags = data.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    p["tags"] = [t for t in (tags or []) if t]

    options = []
    for o in data.get("options") or []:
        if isinstance(o, dict):
            options.append({"name": clean_text(o.get("name")), "values": o.get("values") or []})
        else:
            options.append({"name": clean_text(o), "values": []})
    p["options"] = options

    variants = [v for v in (data.get("variants") or []) if isinstance(v, dict)]
    minor = minor_units(variants, hint)

    def conv(raw: Any) -> Optional[Union[int, float]]:
        n = number(raw)
        if n is None:
            return None
        return norm_num(n / 100 if minor else n)

    parsed = []
    for v in variants:
        image = v.get("featured_image")
        parsed.append({
            "label": clean_text(v.get("public_title") or v.get("title")),
            "value": str(v.get("id")) if v.get("id") is not None else None,
            "sku": clean_text(v.get("sku")),
            "barcode": clean_text(v.get("barcode")),
            "price": conv(v.get("price")),
            "compare_at_price": conv(v.get("compare_at_price")),
            "available": v.get("available") if isinstance(v.get("available"), bool) else None,
            "options": [v.get(k) for k in ("option1", "option2", "option3") if v.get(k)],
            "weight_grams": v.get("weight"),
            "image": absolute_url(image.get("src")) if isinstance(image, dict) else None,
        })

    if parsed:
        priced = [v for v in parsed if v["price"] is not None]
        pool = [v for v in priced if v["available"]] or priced
        best = min(pool, key=lambda v: v["price"]) if pool else parsed[0]
        if best["price"] is not None:
            p["price"] = best["price"]
        cmp_price = best["compare_at_price"]
        p["original_price"] = cmp_price if (cmp_price and p["price"] is not None and cmp_price > p["price"]) else None
        p["sku"] = best["sku"] or next((v["sku"] for v in parsed if v["sku"]), None)
        p["barcode"] = best["barcode"] or next((v["barcode"] for v in parsed if v["barcode"]), None)
        p["weight_grams"] = best["weight_grams"]

        availability = [v["available"] for v in parsed if v["available"] is not None]
        if availability:
            p["in_stock"] = any(availability)
            p["purchasable"] = p["in_stock"]
            p["availability"] = "In Stock" if p["in_stock"] else "Out of Stock"

        only_default = len(parsed) == 1 and (parsed[0]["label"] in (None, "", "Default Title"))
        p["variants"] = [] if only_default else parsed
    elif isinstance(data.get("available"), bool):
        p["in_stock"] = p["purchasable"] = data["available"]
        p["availability"] = "In Stock" if data["available"] else "Out of Stock"

    p["discount"] = discount_percent(p["price"], p["original_price"])

    images = []
    for item in data.get("images") or []:
        raw = item.get("src") if isinstance(item, dict) else item
        u = absolute_url(raw)
        if u and u not in images:
            images.append(u)
    if not images and data.get("featured_image"):
        raw = data["featured_image"]
        u = absolute_url(raw.get("src") if isinstance(raw, dict) else raw)
        if u:
            images.append(u)
    if images:
        p["images"] = images
        p["image"] = images[0]

    desc_html = data.get("description")
    p["description"] = html_to_text(desc_html)
    specs, highlights = specs_from_html(desc_html)
    p["specifications"] = specs
    p["highlights"] = highlights


# ------------------------------ Product page HTML ------------------------------

def extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    values = all_text(soup, [
        ".breadcrumbs a", "nav[aria-label='Breadcrumb'] a", "nav[aria-label='breadcrumb'] a",
        ".breadcrumb a", "[itemtype*='BreadcrumbList'] [itemprop='name']",
    ])
    values = [v for v in values if v.lower() not in ("home", "you are here", "/", ">")]
    return values or jsonld_breadcrumbs(soup)


def extract_reviews(soup: BeautifulSoup) -> list[dict[str, Any]]:
    reviews, seen = [], set()
    for item in soup.select(
        ".review, .review-item, .spr-review, [itemprop='review'], .jdgm-rev, .okeReviews-reviews-review"
    ):
        author = first_text(item, [".review-author", ".spr-review-header-byline", "[itemprop='author']", ".jdgm-rev__author"])
        title = first_text(item, [".review-title", ".spr-review-header-title", "[itemprop='name']", ".jdgm-rev__title"])
        body = first_text(item, [".review-content", ".spr-review-content-body", "[itemprop='reviewBody']", ".jdgm-rev__body"])
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


def apply_page_html(p: dict[str, Any], soup: BeautifulSoup, url: str) -> None:
    for obj in jsonld_product_objects(soup):
        apply_jsonld(p, obj)
        break

    p["title"] = p["title"] or first_text(soup, ["h1"])
    if p["price"] is None:
        p["price"] = number(
            first_attr(soup, [("[itemprop='price']", "content"), ("meta[property='product:price:amount']", "content")])
            or first_text(soup, [".price-item--sale", ".product__price", ".price"])
        )

    p["breadcrumbs"] = extract_breadcrumbs(soup)
    if p["breadcrumbs"] and p["title"] and p["breadcrumbs"][-1].strip().lower() == p["title"].strip().lower():
        p["breadcrumbs"] = p["breadcrumbs"][:-1]
    p["category"] = p["category"] or (p["breadcrumbs"][-1] if p["breadcrumbs"] else None)

    if not p["description"]:
        p["description"] = first_text(soup, [
            ".product__description", ".product-description", "[itemprop='description']", "#ProductInfo-description", ".rte",
        ])
    if not p["specifications"]:
        scope = soup.select_one("main") or soup
        p["specifications"] = specs_from_html(str(scope))[0]
    if not p["highlights"]:
        p["highlights"] = all_text(soup, [".product-highlights li", ".product__description li", ".product-details li"])

    if not p["images"]:
        gallery = soup.select_one(".product__media-wrapper, .product__media-list, .product-gallery, .product__media")
        imgs = all_images(gallery, url) if gallery is not None else []
        og = absolute_url(first_attr(soup, [("meta[property='og:image']", "content")]), url)
        for u in imgs or ([og] if og else []):
            if u not in p["images"]:
                p["images"].append(u)
        p["image"] = p["image"] or (p["images"][0] if p["images"] else None)

    if p["rating"] is None:
        p["rating"] = number(first_attr(soup, [("[itemprop='ratingValue']", "content"), ("[data-rating]", "data-rating")]))
    if p["rating_count"] is None:
        p["rating_count"] = number(first_attr(soup, [("[itemprop='ratingCount']", "content")]))
    if p["review_count"] is None:
        p["review_count"] = number(
            first_attr(soup, [("[itemprop='reviewCount']", "content")])
            or first_text(soup, [".rating-count", ".reviews-count", "[class*='review-count']"])
        )
    if not p["reviews"]:
        p["reviews"] = extract_reviews(soup)

    p["shipping"] = p["shipping"] or first_text(soup, [".shipping-info", ".shipping-policy", "[class*='shipping']"])
    p["shipping_cost"] = p["shipping_cost"] or number(first_text(soup, [".shipping-cost", ".delivery-charge", "[data-shipping-cost]"]))
    p["delivery"] = p["delivery"] or first_text(soup, [".delivery-estimate", ".delivery-info", "[class*='delivery']"])
    p["delivery_area"] = p["delivery_area"] or first_text(soup, [".delivery-area", "[class*='delivery-area']"])
    p["return_policy"] = p["return_policy"] or first_text(soup, [".return-policy", ".returns-policy", "[class*='return-policy']"])
    p["payment_options"] = p["payment_options"] or all_text(soup, [".payment-methods li", ".payment-options li", "[class*='payment'] li"])

    vendor_link = first_attr(soup, [("a[href*='/collections/vendors']", "href")])
    p["seller_url"] = p["seller_url"] or absolute_url(vendor_link, url)
    p["brand_store_url"] = p["brand_store_url"] or p["seller_url"]

    meta = soup.select_one("meta[name='description']")
    if meta:
        p["meta_description"] = clean_text(meta.get("content"))

    # Fallback availability if .js was unavailable
    if p["in_stock"] is None:
        stock_raw = first_attr(soup, [("[itemprop='availability']", "href")]) or first_text(
            soup, [".product__inventory", ".inventory", ".stock", ".availability"]
        )
        status, avail = stock_status(stock_raw)
        p["availability"] = p["availability"] or status
        p["in_stock"] = avail
        if p["purchasable"] is None:
            p["purchasable"] = avail


def enrich_product(context: Any, p: dict[str, Any]) -> None:
    handle, url = p.get("handle"), p.get("product_url")
    if not handle or not url:
        return
    try:
        data = fetch_json(context, f"/products/{handle}.js")
        if isinstance(data, dict) and data.get("title"):
            apply_shopify_js(p, data, p.get("price"))
        html = fetch_text(context, url)
        if html:
            apply_page_html(p, BeautifulSoup(html, "html.parser"), url)
    except Exception as exc:
        print(f"Enrichment skipped for {url}: {exc}", file=sys.stderr)


def finalize(p: dict[str, Any]) -> None:
    p["platform"] = PLATFORM
    p["product_id"] = p["product_id"] or p["handle"]
    p["category"] = p["category"] or p["product_type"]
    p["discount"] = p["discount"] or discount_percent(p["price"], p["original_price"])
    if not p["image"] and p["images"]:
        p["image"] = p["images"][0]
    p["currency"] = p["currency"] or "PKR"
    if p["rating"] is None:
        p["rating"] = "No ratings yet"
    if not p["reviews"]:
        p["reviews"] = "No reviews"


# ------------------------------ Scraper ------------------------------

def _block_heavy_resources(route: Any) -> None:
    if route.request.resource_type in ("image", "media", "font"):
        route.abort()
    else:
        route.continue_()


def scrape_telex(keyword: str, limit: int) -> tuple[list[dict[str, Any]], Optional[int]]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-PK",
            timezone_id="Asia/Karachi",
            user_agent=USER_AGENT,
            extra_http_headers={"Accept-Language": "en-PK,en;q=0.9"},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        context.route("**/*", _block_heavy_resources)
        page = context.new_page()
        try:
            products, total = collect_search_results(page, context, keyword, limit)
            for i, product in enumerate(products, start=1):
                print(f"Enriching {i}/{len(products)}: {product.get('product_url')}")
                enrich_product(context, product)
                finalize(product)
        finally:
            context.close()
            browser.close()
    return products, total


# ------------------------------ API ------------------------------

@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "Telex.pk Product Search API",
        "platform": PLATFORM,
        "status": "running",
        "search_endpoint": "/api/search?keyword=iphone%2017&limit=20",
        "documentation": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "Telex.pk Product Search API", "platform": PLATFORM}


@app.get("/api/search")
@app.get("/scrape", include_in_schema=False)
def search_products(
    keyword: str = Query(..., min_length=1, max_length=120, description="Product search keyword"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum products to return"),
) -> dict[str, Any]:
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Provide a keyword, e.g. ?keyword=iphone&limit=20")
    try:
        products, total = scrape_telex(keyword, limit)
    except PlaywrightTimeoutError as exc:
        raise HTTPException(status_code=504, detail="Telex.pk took too long to respond.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        print("Telex scraper error:", repr(exc), file=sys.stderr)
        raise HTTPException(
            status_code=502,
            detail="Could not scrape Telex.pk. The site may be unavailable or blocking automation.",
        ) from exc

    return {
        "success": True,
        "platform": PLATFORM,
        "source": BASE_URL,
        "keyword": keyword,
        "search_url": f"{SEARCH_URL}?q={quote(keyword)}&type=product",
        "total_results": total,
        "count": len(products),
        "products": products,
        "note": (
            "Values come from public Telex.pk search pages, Shopify product JSON and product page HTML. "
            "Fields not published remain null or empty. Each product is enriched individually, "
            "so large limits take longer."
        ),
    }


if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("TELEX.PK PRODUCT SEARCH API")
    print("Server: http://127.0.0.1:8000")
    print("Docs:   http://127.0.0.1:8000/docs")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)