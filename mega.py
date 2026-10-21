"""
Mega.pk Product Search API  (v5)
FastAPI + Playwright + BeautifulSoup

Install:
    python -m pip install fastapi uvicorn playwright beautifulsoup4 lxml
    python -m playwright install chromium

Run:
    python mega.py

Test:
    http://127.0.0.1:8000/docs
    http://127.0.0.1:8000/api/search?keyword=iphone&limit=10
    http://127.0.0.1:8000/api/search?keyword=iphone&limit=10&details=false   <- faster, no warranty
    http://127.0.0.1:8000/api/search?keyword=iphone&debug=true

Fixed in v5:
  - Correct current price vs original (struck-through) price + discount %
  - Warranty added (read from each product's detail page)
"""

import html as htmllib
import re
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.mega.pk/"

FALLBACK_SEARCH_URLS = [
    "https://www.mega.pk/search/?q={kw}",
    "https://www.mega.pk/search.php?q={kw}",
    "https://www.mega.pk/search/{kw}/",
    "https://www.mega.pk/search.php?search={kw}",
    "https://www.mega.pk/search?q={kw}",
]

SEARCH_INPUT_SELECTORS = [
    "input[type='search']",
    "input[name='q']",
    "input[name='search']",
    "input[name='keyword']",
    "input[name='keywords']",
    "input[id*='search' i]",
    "input[class*='search' i]",
    "input[placeholder*='search' i]",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PRODUCT_RE = re.compile(r"/([A-Za-z0-9]+)_products/(\d+)/", re.I)

PRICE_AFTER_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+|\d{3,})(?:\.\d+)?\s*[-–]?\s*(?:PKR|Rs\.?|₨)",
    re.I,
)
PRICE_BEFORE_RE = re.compile(
    r"(?:PKR|Rs\.?|₨)\s*[-–:]?\s*(\d{1,3}(?:,\d{3})+|\d{3,})(?!\s*(?:GB|TB|MB|MP|mAh|Hz|GHz|W\b))",
    re.I,
)

DISCOUNT_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*off", re.I)
WARRANTY_RE = re.compile(
    r"Warranty\s*:\s*(.{2,80}?)(?=\s+(?:Share|Price Alert|Tell a Friend|SKU|Item Condition|Stock Info)\b|$)",
    re.I,
)

STRUCK_SELECTOR = (
    "del, s, strike, [style*='line-through' i], "
    "[class*='strike' i], [class*='old-price' i], [class*='oldprice' i], [class*='cut-price' i]"
)

BRANDS = (
    "Apple", "Samsung", "Google", "Xiaomi", "Redmi", "Oppo", "Vivo", "Infinix",
    "Tecno", "Realme", "OnePlus", "Huawei", "Honor", "Nothing", "HP", "Dell",
    "Lenovo", "Asus", "Acer", "MSI", "Microsoft", "Sony", "Canon", "Nikon",
    "Anker", "Logitech", "Dawlance", "Haier", "Orient", "PEL", "TCL", "Hisense",
    "Gree", "LG", "Panasonic", "GoPro", "DJI", "Insta360", "Razer", "Kindle",
    "ViewSonic", "Wiwu", "Onikuma", "Carez", "Oculus", "Meta", "Casio",
)

app = FastAPI(title="Mega.pk Product Search API", version="5.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def clean(value):
    if value is None:
        return None
    value = htmllib.unescape(re.sub(r"\s+", " ", str(value))).strip()
    return value or None


def all_prices(text):
    """All distinct PKR prices in text, in order of appearance."""
    text = clean(text)
    if not text:
        return []
    found = []
    for rx in (PRICE_AFTER_RE, PRICE_BEFORE_RE):
        for m in rx.finditer(text):
            try:
                found.append((m.start(), float(m.group(1).replace(",", ""))))
            except ValueError:
                pass
    found.sort(key=lambda t: t[0])
    out = []
    for _, v in found:
        if v not in out:
            out.append(v)
    return out


def parse_price(text):
    prices = all_prices(text)
    return prices[0] if prices else None


def resolve_prices(root, text):
    """
    Returns (current_price, original_price).
    Original = the struck-through price. If no struck element is found and
    there are two close prices, the lower is current and the higher is original.
    """
    prices = all_prices(text)
    if not prices:
        return None, None

    struck = None
    for el in root.select(STRUCK_SELECTOR):
        v = parse_price(el.get_text(" ", strip=True))
        if v is not None and v in prices:
            struck = v
            break

    if struck is not None:
        others = [p for p in prices if p != struck]
        if others:
            return others[0], struck
        return struck, None

    if len(prices) >= 2:
        lo, hi = min(prices), max(prices)
        if lo > 0 and hi / lo <= 2:
            return lo, hi

    return prices[0], None


def calc_discount(current, original, text):
    if current is not None and original is not None and original > current:
        return round((original - current) / original * 100, 2)
    m = DISCOUNT_RE.search(text or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def parse_warranty(text):
    m = WARRANTY_RE.search(text or "")
    return clean(m.group(1)) if m else None


def is_homepage(url):
    p = urlparse(url)
    return p.netloc.lower().endswith("mega.pk") and p.path in ("", "/") and not p.query


def count_product_links(html_text):
    return len(set(m.group(2) for m in PRODUCT_RE.finditer(html_text)))


def looks_blocked(html_text):
    low = html_text.lower()
    return any(
        t in low
        for t in (
            "verify you are human",
            "checking your browser",
            "g-recaptcha",
            "cf-challenge",
            "access denied",
            "request blocked",
        )
    )


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def _check_status(response, url):
    if response is None:
        return
    if response.status in (401, 403, 429):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Mega.pk rejected the browser request.",
                "upstream_status": response.status,
                "requested_url": url,
                "hint": "Automated access may be restricted for this client.",
            },
        )


def _wait_for_products(page, timeout=12000):
    try:
        page.wait_for_selector("a[href*='_products/']", timeout=timeout)
    except PlaywrightTimeoutError:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass


def _search_via_ui(page, keyword, attempts):
    response = page.goto(BASE_URL, wait_until="domcontentloaded")
    _check_status(response, BASE_URL)

    box = None
    for selector in SEARCH_INPUT_SELECTORS:
        loc = page.locator(selector)
        try:
            n = min(loc.count(), 6)
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    box = el
                    break
            except Exception:
                continue
        if box:
            attempts.append(f"ui: using search input '{selector}'")
            break

    if box is None:
        attempts.append("ui: no visible search input found")
        return None

    box.click()
    box.fill(keyword)
    box.press("Enter")

    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    _wait_for_products(page)

    url = page.url
    html_text = page.content()
    n = count_product_links(html_text)
    attempts.append(f"ui: landed on {url} ({n} product links)")

    if is_homepage(url) or n == 0:
        return None
    return html_text, url


def _search_via_url(page, template, keyword, attempts):
    url = template.format(kw=quote(keyword, safe=""))
    try:
        response = page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeoutError:
        attempts.append(f"url: {url} -> timeout")
        return None
    _check_status(response, url)
    _wait_for_products(page, timeout=8000)

    final = page.url
    html_text = page.content()
    n = count_product_links(html_text)
    status = response.status if response else "?"
    attempts.append(f"url: {url} -> {status}, final={final}, {n} product links")

    if (response and response.status >= 400) or is_homepage(final) or n == 0:
        return None
    return html_text, final


def enrich_from_detail(page, product, attempts):
    """Open the product page: exact price, original price, discount, warranty."""
    url = product["url"]
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if resp and resp.status >= 400:
            attempts.append(f"detail: {url} -> {resp.status}")
            return
        try:
            page.wait_for_selector("text=Warranty", timeout=4000)
        except PlaywrightTimeoutError:
            pass
        soup = BeautifulSoup(page.content(), "lxml")
    except Exception as exc:
        attempts.append(f"detail: {url} -> {type(exc).__name__}")
        return

    text = clean(soup.get_text(" ", strip=True)) or ""

    # Price block: "Price: 399,999 - PKR 424,999 - PKR Buy Now ..."
    m = re.search(
        r"\bPrice\s*:\s*(.{0,150}?)(?=Buy Now|Order through|Add to Cart|Call us|$)",
        text,
        re.I,
    )
    section = m.group(1) if m else ""
    current, original = resolve_prices(soup, section) if section else (None, None)

    if current is not None:
        product["price"] = current
        product["original_price"] = original
        product["discount"] = calc_discount(current, original, text)

    warranty = parse_warranty(text)
    if warranty:
        product["warranty"] = warranty

    if re.search(r"Stock Info\s*:\s*Out of Stock", text, re.I):
        product["availability"] = "OutOfStock"
    elif re.search(r"Stock Info\s*:\s*Available", text, re.I):
        product["availability"] = "InStock"


def fetch_and_parse(keyword, limit, details):
    attempts = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-PK",
                    viewport={"width": 1365, "height": 900},
                )
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in ("image", "font", "media")
                    else route.continue_(),
                )
                page = context.new_page()
                page.set_default_timeout(25000)

                result = None
                try:
                    result = _search_via_ui(page, keyword, attempts)
                except PlaywrightTimeoutError:
                    attempts.append("ui: timed out")

                if result is None:
                    for template in FALLBACK_SEARCH_URLS:
                        result = _search_via_url(page, template, keyword, attempts)
                        if result:
                            break

                if not result:
                    return None, None, None, attempts

                raw, final_url = result
                soup = BeautifulSoup(raw, "lxml")
                products = extract_products(soup, final_url, limit)

                if details and not looks_blocked(raw):
                    for p in products:
                        enrich_from_detail(page, p, attempts)

                return raw, final_url, products, attempts
            finally:
                browser.close()

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Could not run Chromium via Playwright.",
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "hint": "Run: python -m playwright install chromium",
                "attempts": attempts,
            },
        )


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def find_card(anchor, pid):
    card = anchor
    node = anchor.parent
    while isinstance(node, Tag) and node.name not in ("body", "html"):
        ids = set()
        for a in node.select("a[href]"):
            m = PRODUCT_RE.search(a.get("href", ""))
            if m:
                ids.add(m.group(2))
        if ids - {pid}:
            break
        card = node
        node = node.parent
    return card


def card_image(card, base_url):
    for img in card.select("img"):
        for attr in ("data-src", "data-original", "data-lazy-src", "src"):
            v = img.get(attr)
            if v and not str(v).startswith("data:"):
                return urljoin(base_url, str(v).strip())
    return None


def card_title(card, pid):
    candidates = []
    for a in card.select("a[href]"):
        m = PRODUCT_RE.search(a.get("href", ""))
        if not m or m.group(2) != pid:
            continue
        candidates.append(a.get("title"))
        candidates.append(a.get_text(" ", strip=True))
    for h in card.select("h1, h2, h3, h4"):
        candidates.append(h.get_text(" ", strip=True))
    for img in card.select("img[alt]"):
        alt = re.sub(r"\s*price in pakistan\s*$", "", img.get("alt", ""), flags=re.I)
        candidates.append(alt)

    cleaned = [clean(c) for c in candidates]
    cleaned = [c for c in cleaned if c and len(c) >= 3 and c.lower() not in ("detail", "compare")]

    full = [c for c in cleaned if not c.endswith(("...", "…"))]
    pool = full or cleaned
    return max(pool, key=len) if pool else None


def card_specs(card):
    specs = []
    for li in card.select("li"):
        t = clean(li.get_text(" ", strip=True))
        if t and len(t) < 160:
            specs.append(t)
    return specs[:8]


def detect_brand(title):
    if not title:
        return None
    low = title.lower()
    for b in BRANDS:
        if low.startswith(b.lower() + " ") or low == b.lower():
            return b
    return None


def extract_products(soup, page_url, limit):
    results, seen = [], set()

    for anchor in soup.select("a[href]"):
        href = urljoin(page_url, str(anchor.get("href", "")).strip())
        parsed = urlparse(href)
        if parsed.netloc.lower() not in ("mega.pk", "www.mega.pk"):
            continue

        m = PRODUCT_RE.search(parsed.path)
        if not m:
            continue
        category, pid = m.group(1), m.group(2)
        if pid in seen:
            continue

        card = find_card(anchor, pid)
        text = clean(card.get_text(" ", strip=True)) or ""
        title = card_title(card, pid)
        if not title:
            continue

        price, old = resolve_prices(card, text)
        discount = calc_discount(price, old, text)

        availability = None
        if re.search(r"\bcoming soon\b", text, re.I):
            availability = "ComingSoon"
        elif re.search(r"\bout of stock\b|\bsold out\b", text, re.I):
            availability = "OutOfStock"
        elif price is not None:
            availability = "InStock"

        specs = card_specs(card)

        results.append(
            {
                "platform": "mega.pk",
                "title": title,
                "price": price,
                "original_price": old,
                "discount": discount,
                "currency": "PKR",
                "description": "; ".join(specs) if specs else None,
                "image": card_image(card, page_url),
                "url": href.split("#", 1)[0].split("?", 1)[0],
                "product_id": pid,
                "category": category,
                "brand": detect_brand(title),
                "seller": "Mega.pk",
                "warranty": parse_warranty(text),
                "availability": availability,
            }
        )
        seen.add(pid)
        if len(results) >= limit:
            break

    return results


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/")
def root():
    return {
        "name": "Mega.pk Product Search API",
        "status": "running",
        "endpoints": {
            "search": "/api/search?keyword=iphone&limit=20",
            "fast_search": "/api/search?keyword=iphone&limit=20&details=false",
            "debug": "/api/search?keyword=iphone&debug=true",
            "health": "/health",
            "docs": "/docs",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "platform": "mega.pk"}


@app.get("/api/search")
def search_products(
    keyword: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(20, ge=1, le=100),
    details: bool = Query(True, description="Open each product page for exact price, discount and warranty"),
    debug: bool = Query(False),
):
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(400, "Keyword cannot be empty.")

    raw, final_url, products, attempts = fetch_and_parse(keyword, limit, details)

    if raw is None:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Could not reach a Mega.pk results page with products.",
                "keyword": keyword,
                "attempts": attempts,
                "hint": (
                    "Open mega.pk in a browser, run a search, and copy the "
                    "resulting URL into FALLBACK_SEARCH_URLS."
                ),
            },
        )

    if looks_blocked(raw) and count_product_links(raw) == 0:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Mega.pk returned a verification/block page.",
                "final_url": final_url,
                "attempts": attempts,
            },
        )

    response = {
        "platform": "mega.pk",
        "keyword": keyword,
        "count": len(products),
        "source_url": final_url,
        "products": products,
        "note": "Verify prices on Mega.pk. Missing values are null.",
    }
    if debug or not products:
        soup = BeautifulSoup(raw, "lxml")
        response["diagnostics"] = {
            "attempts": attempts,
            "page_title": clean(soup.title.get_text()) if soup.title else None,
            "product_links_in_html": count_product_links(raw),
        }
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)