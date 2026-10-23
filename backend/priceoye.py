# priceoye.py
# PriceOye scraper: FastAPI + Playwright + BeautifulSoup
# Version: 6.3.1  (review extraction fix: author / rating / text / images)
#
# Install:
#   pip install fastapi uvicorn playwright beautifulsoup4
#   playwright install chromium
# Run:
#   python priceoye.py

import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://priceoye.pk"
NAVIGATION_TIMEOUT = 45000
MAX_SEARCH_PAGES = 20
MAX_CONCURRENT_PRODUCTS = 4
MAX_REVIEWS = 100

app = FastAPI(title="PriceOye Product Scraper", version="6.3.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

# ----------------------------- General helpers -----------------------------
def clean_text(value):
    if value is None:
        return None
    value = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def clean_url(url):
    if not url:
        return None
    url = urljoin(BASE_URL, str(url).strip())
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("priceoye.pk", "www.priceoye.pk"):
        return None
    return urlunparse(("https", "priceoye.pk", parsed.path.rstrip("/") or "/", "", "", ""))


def absolute_image_url(value, page_url=BASE_URL):
    if not value:
        return None
    value = str(value).strip()
    if value.startswith(("data:", "blob:", "javascript:")):
        return None
    if value.startswith("//"):
        value = "https:" + value
    else:
        value = urljoin(page_url, value)
    return value if urlparse(value).scheme in ("http", "https") else None


def price_to_integer(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            value = re.sub(r"(?i)\bPKR\b|\bRs\.?\b|₨", "", value).strip()
            if not re.fullmatch(r"\d+(?:\.\d+)?", value):
                return None
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            return None
        return int(number.quantize(Decimal("1"), rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, TypeError):
        return None


def number_value(value):
    if value is None or isinstance(value, bool):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    try:
        return float(match.group()) if match else None
    except (ValueError, TypeError):
        return None


def integer_count(value):
    value = number_value(value)
    return int(value) if value is not None else None


def calculate_discount(current, original):
    if current is None or original is None or original <= current or original <= 0:
        return None
    return f"{round((original - current) / original * 100)}% OFF"


def format_rating_stars(rating):
    if rating is None:
        return None
    try:
        rating = max(0, min(5, int(round(float(rating)))))
    except (TypeError, ValueError):
        return None
    return "★" * rating + "☆" * (5 - rating)


def empty_product(url, title=None):
    return {"platform": "priceoye", "title": clean_text(title), "price": None,
            "original_price": None, "discount": None, "currency": "PKR",
            "description": None, "image": None, "url": clean_url(url), "sku": None,
            "brand": None, "availability": None, "rating": None,
            "review_count": None, "reviews": []}

# ----------------------------- JSON extraction -----------------------------
def walk_json(value):
    out = []
    if isinstance(value, dict):
        out.append(value)
        for child in value.values():
            out.extend(walk_json(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(walk_json(child))
    return out


def parse_json_script(script):
    raw = script.string or script.get_text(strip=True)
    if not raw or len(raw) > 5_000_000:
        return []
    try:
        return walk_json(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return []


def extract_json_objects(soup, embedded=False):
    selectors = (["script#__NEXT_DATA__", "script[type='application/json']",
                  "script[id*='state' i]", "script[id*='product' i]", "script[id*='review' i]"]
                 if embedded else ['script[type="application/ld+json"]'])
    out = []
    for script in soup.select(",".join(selectors)):
        out.extend(parse_json_script(script))
    return out


def find_product_schema(soup):
    for obj in extract_json_objects(soup):
        types = obj.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(str(t).lower() == "product" for t in types):
            return obj
    return {}


def get_offer(schema):
    offer = schema.get("offers")
    if isinstance(offer, list):
        return offer[0] if offer else {}
    return offer if isinstance(offer, dict) else {}


def schema_price(schema):
    offer = get_offer(schema)
    for value in (offer.get("price"), offer.get("lowPrice"), offer.get("priceSpecification")):
        if isinstance(value, dict):
            value = value.get("price")
        result = price_to_integer(value)
        if result is not None:
            return result
    return None


def schema_brand(schema):
    brand = schema.get("brand")
    if isinstance(brand, dict):
        return clean_text(brand.get("name"))
    return clean_text(brand) if isinstance(brand, str) else None


def schema_rating(schema):
    aggregate = schema.get("aggregateRating", {})
    if not isinstance(aggregate, dict):
        return None, None
    rating = number_value(aggregate.get("ratingValue"))
    count = integer_count(aggregate.get("reviewCount") or aggregate.get("ratingCount"))
    if rating is not None and not 0 <= rating <= 5:
        rating = None
    return rating, count

# --------------------------------- Prices ----------------------------------
def meta_price(soup):
    for selector in ('meta[property="product:price:amount"]', 'meta[property="og:price:amount"]', 'meta[itemprop="price"]'):
        node = soup.select_one(selector)
        if node:
            value = price_to_integer(node.get("content"))
            if value is not None:
                return value
    return None


def extract_price(soup):
    value = schema_price(find_product_schema(soup)) or meta_price(soup)
    if value is not None:
        return value
    selectors = ['[itemprop="price"]', '[data-price]', '.product-price', '.price-box .price',
                 "[class*='current-price']", "[class*='sale-price']", "[class*='selling-price']",
                 "[class*='productPrice']", "[class*='price-now']"]
    for selector in selectors:
        for node in soup.select(selector):
            value = price_to_integer(node.get("content") or node.get("data-price") or node.get_text(" ", strip=True))
            if value is not None:
                return value
    return None


def extract_price_pair(text):
    if not text:
        return None, None
    match = re.search(r"(?:Rs\.?|PKR|₨)\s*([\d,]+)(?:\s+(?:Rs\.?|PKR|₨)\s*([\d,]+))?", text, re.I)
    if not match:
        return None, None
    current, original = price_to_integer(match.group(1)), price_to_integer(match.group(2))
    if current is not None and original is not None and original <= current:
        original = None
    return current, original


def extract_original_price(soup, current=None):
    selectors = ['meta[property="product:price:regular_amount"]', "[class*='old-price']",
                 "[class*='original-price']", "[class*='regular-price']", "[class*='list-price']",
                 "[class*='strike-price']", "[class*='previous-price']", ".price-old", ".old-price", "del", "s"]
    for selector in selectors:
        for node in soup.select(selector):
            value = price_to_integer(node.get("content") or node.get("data-price") or node.get_text(" ", strip=True))
            if value is not None and (current is None or value > current):
                return value
    value = price_to_integer(get_offer(find_product_schema(soup)).get("highPrice"))
    return value if value is not None and current is not None and value > current else None


def extract_discount(soup, text=""):
    for node in soup.select("[class*='discount'], [class*='off-percent'], [class*='sale-badge'], [class*='offer-badge']"):
        value = clean_text(node.get_text(" ", strip=True))
        match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*OFF\b", value or "", re.I)
        if match:
            return f"{match.group(1)}% OFF"
    match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*OFF\b", text or "", re.I)
    return f"{match.group(1)}% OFF" if match else None

# ---------------------------- Ratings / reviews ----------------------------
def extract_overall_rating(soup, text=""):
    rating = count = None
    for selector in ('[itemprop="ratingValue"]', '[data-rating]', "[class*='average-rating']",
                     "[class*='rating-value']", "[class*='product-rating']", "[class*='ratingValue']"):
        for node in soup.select(selector):
            candidate = number_value(node.get("content") or node.get("data-rating") or node.get("aria-label") or node.get_text(" ", strip=True))
            if candidate is not None and 0 <= candidate <= 5:
                rating = candidate
                break
        if rating is not None:
            break
    for selector in ('[itemprop="reviewCount"]', '[itemprop="ratingCount"]', "[class*='review-count']",
                     "[class*='reviews-count']", "[class*='rating-count']", "[class*='total-reviews']"):
        for node in soup.select(selector):
            raw = clean_text(node.get("content") or node.get_text(" ", strip=True)) or ""
            match = re.search(r"([\d,]+)\s*(?:reviews?|ratings?)\b", raw, re.I)
            if match:
                count = integer_count(match.group(1))
                break
        if count is not None:
            break
    match = re.search(r"(?<!\d)([0-5](?:\.\d+)?)\s*\|\s*([\d,]+)\s+Reviews?\b", text or "", re.I)
    if match:
        rating = rating if rating is not None else number_value(match.group(1))
        count = count if count is not None else integer_count(match.group(2))
    return rating, count


def extract_review_images(node, page_url):
    images, seen = [], set()
    excluded = ("avatar", "profile", "logo", "placeholder", "star", "rating", "verified", "payment")
    for image in node.select("img, [style*='background-image']"):
        candidates = [image.get("data-original"), image.get("data-src"), image.get("data-lazy-src"),
                      image.get("data-large-image"), image.get("src")]
        style_match = re.search(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)", image.get("style", ""), re.I)
        if style_match:
            candidates.append(style_match.group(1))
        for candidate in candidates:
            url = absolute_image_url(candidate, page_url)
            if not url or any(word in url.lower() for word in excluded):
                continue
            path = urlparse(url).path.lower()
            # Customer photos on PriceOye are commonly under /review/.
            if "/review/" not in path and not path.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
                continue
            if path.endswith((".svg", ".gif")):
                continue
            if url not in seen:
                seen.add(url)
                images.append(url)
    for link in node.select("a[href]"):
        url = absolute_image_url(link.get("href"), page_url)
        if not url or any(word in url.lower() for word in excluded):
            continue
        path = urlparse(url).path.lower()
        if "/review/" in path or path.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
            if url not in seen:
                seen.add(url)
                images.append(url)
    return images[:10]


def normalize_author(value):
    value = clean_text(value)
    if not value:
        return None
    parts = value.split()
    # PriceOye shows avatar initials before the name: "UB Usman Butt", "SY Syeda", "RA Raza"
    if len(parts) >= 2 and re.fullmatch(r"[A-Z]{1,3}", parts[0]):
        rest = parts[1:]
        initials = "".join(w[0] for w in rest if w).upper()
        first_two = rest[0][:2].upper()
        if initials.startswith(parts[0]) or first_two == parts[0]:
            value = " ".join(rest)
    return value.strip() or None


def review_key(review):
    author = (review.get("author") or "").casefold()
    date = (review.get("date") or "").casefold()
    if author and date:
        return (author, date)
    return (author, review.get("rating"), date, (review.get("text") or "").casefold())


def merge_reviews(*review_lists):
    merged, positions = [], {}
    for review_list in review_lists:
        for incoming in review_list:
            if not isinstance(incoming, dict):
                continue
            review = dict(incoming)
            review.setdefault("images", [])
            review["author"] = normalize_author(review.get("author"))
            if review.get("rating_stars") is None:
                review["rating_stars"] = format_rating_stars(review.get("rating"))
            key = review_key(review)
            if not any(key):
                continue
            if key in positions:
                existing = merged[positions[key]]
                for image in review.get("images", []):
                    if image and image not in existing.setdefault("images", []):
                        existing["images"].append(image)
                for field in ("author", "rating", "date", "text"):
                    if existing.get(field) is None and review.get(field) is not None:
                        existing[field] = review[field]
                if existing.get("rating_stars") is None:
                    existing["rating_stars"] = format_rating_stars(existing.get("rating"))
                continue
            positions[key] = len(merged)
            merged.append(review)
            if len(merged) >= MAX_REVIEWS:
                return merged
    return merged


def sanitize_reviews(reviews):
    """Drop junk fragments (name/date/heading/seller-reply used as text) and tidy fields."""
    date_re = re.compile(r"\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}|\d{4}-\d{2}-\d{2}")
    out = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        author = normalize_author(review.get("author"))
        text = clean_text(review.get("text"))
        date = clean_text(review.get("date"))
        if text:
            low = text.casefold()
            as_name = normalize_author(text)
            if ((author and as_name and as_name.casefold() == author.casefold())
                    or date_re.fullmatch(text)
                    or re.fullmatch(r"\+\d+", text)
                    or low.startswith("reviews of ")
                    or low in ("priceoye.pk", "priceoye")
                    or low.startswith("looking forward to serving you")):
                text = None
        if not author and not text:
            continue
        rating = review.get("rating")
        images = []
        for image in review.get("images") or []:
            if image and image not in images:
                images.append(image)
        out.append({"author": author, "rating": rating,
                    "rating_stars": review.get("rating_stars") or format_rating_stars(rating),
                    "date": date, "text": text, "images": images[:10]})
    return out


def extract_review_record(node, page_url=BASE_URL):
    author_node = node.select_one('[itemprop="author"], [class*="reviewer-name"], [class*="review-author"], [class*="author-name"], [class*="user-name"]')
    rating_node = node.select_one('[itemprop="reviewRating"] [itemprop="ratingValue"], [itemprop="ratingValue"], [data-rating], [class*="review-rating"], [class*="rating-value"], [aria-label*="star" i]')
    date_node = node.select_one("time, [itemprop='datePublished'], [class*='review-date']")
    body_node = node.select_one('[itemprop="reviewBody"], [class*="review-text"], [class*="review-content"], [class*="review-description"], [class*="comment-text"]')
    author = normalize_author(author_node.get_text(" ", strip=True)) if author_node else None
    raw_rating = None
    if rating_node:
        raw_rating = rating_node.get("content") or rating_node.get("data-rating") or rating_node.get("aria-label") or rating_node.get_text(" ", strip=True)
    rating = number_value(raw_rating)
    if rating is not None and not 0 <= rating <= 5:
        rating = None
    if rating is None:
        for image in node.select("img[alt], img[title]"):
            alt = image.get("alt", "") or image.get("title", "")
            match = re.search(r"([0-5](?:\.\d+)?)\s*(?:/|out of)?\s*5", alt, re.I)
            if match:
                rating = number_value(match.group(1))
                break
    date = None
    if date_node:
        date = date_node.get("datetime") or date_node.get("content") or clean_text(date_node.get_text(" ", strip=True))
    text = clean_text(body_node.get_text(" ", strip=True)) if body_node else None
    images = extract_review_images(node, page_url)
    if not text:
        # Fallback is deliberately conservative: remove name/date and UI labels
        # instead of returning the author as the review body.
        lines = [clean_text(x) for x in node.get_text("\n", strip=True).splitlines()]
        lines = [x for x in lines if x and x.casefold() not in {"show more", "load more", "verified purchase"}]
        if author:
            lines = [x for x in lines if normalize_author(x) != author and x != author]
        if date:
            date_string = clean_text(date)
            lines = [x for x in lines if x != date_string]
        lines = [x for x in lines if not re.fullmatch(r"[★☆\s]+", x or "")]
        candidates = [x for x in lines if len(x) >= 2 and len(x) <= 1500]
        text = candidates[-1] if candidates else None
    if not any((author, rating is not None, date, text, images)):
        return None
    return {"author": author, "rating": rating, "rating_stars": format_rating_stars(rating),
            "date": clean_text(date), "text": text, "images": images}


def extract_dom_reviews(soup, page_url=BASE_URL):
    reviews = []
    selectors = ["[itemprop='review']", "[class*='review-item']", "[class*='review-card']",
                 "[class*='customer-review']", "[class*='review-container']", "[data-review-id]", "[class*='review']"]
    for selector in selectors:
        for node in soup.select(selector):
            if len(node.get_text(" ", strip=True)) > 3500:
                continue
            record = extract_review_record(node, page_url)
            if record:
                reviews = merge_reviews(reviews, [record])
            if len(reviews) >= MAX_REVIEWS:
                return reviews
    return reviews


def extract_reviews_from_json(objects, page_url=BASE_URL):
    reviews = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        evidence = any(k in obj for k in ("reviewBody", "review_text", "reviewText", "comment", "reviewer", "reviewRating"))
        author = obj.get("author") or obj.get("reviewer") or obj.get("user") or obj.get("userName") or obj.get("customer_name") or obj.get("customerName")
        if isinstance(author, dict):
            author = author.get("name") or author.get("username") or author.get("displayName")
        body = obj.get("reviewBody") or obj.get("review_text") or obj.get("reviewText") or obj.get("comment") or obj.get("review") or obj.get("text")
        if isinstance(body, dict):
            body = body.get("text") or body.get("content")
        rating_data = obj.get("reviewRating")
        if isinstance(rating_data, dict):
            rating_data = rating_data.get("ratingValue")
        rating = number_value(rating_data or obj.get("ratingValue") or obj.get("rating") or obj.get("stars"))
        if rating is not None and not 0 <= rating <= 5:
            rating = None
        date = obj.get("datePublished") or obj.get("created_at") or obj.get("createdAt") or obj.get("review_date") or obj.get("date")
        if not evidence and not (author or body or date):
            continue
        raw_images = obj.get("images") or obj.get("reviewImages") or obj.get("review_images") or obj.get("attachments") or obj.get("photos") or []
        if isinstance(raw_images, (str, dict)):
            raw_images = [raw_images]
        images = []
        for image in raw_images if isinstance(raw_images, list) else []:
            if isinstance(image, dict):
                image = image.get("url") or image.get("src") or image.get("image") or image.get("original")
            url = absolute_image_url(image, page_url)
            if url and url not in images:
                images.append(url)
        record = {"author": normalize_author(author), "rating": rating, "rating_stars": format_rating_stars(rating),
                  "date": clean_text(date), "text": clean_text(body), "images": images[:10]}
        if any((record["author"], rating is not None, record["date"], record["text"], record["images"])):
            reviews = merge_reviews(reviews, [record])
        if len(reviews) >= MAX_REVIEWS:
            break
    return reviews

# ------------------------- Dedicated reviews page -------------------------
async def extract_rendered_priceoye_reviews(page):
    """Read rendered review cards: one card per review, author / date / stars / text / images separated."""
    script = r"""
    () => {
      const clean = s => (s || '').replace(/\s+/g, ' ').trim();
      const dateRe = /\b\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b/;
      const getImg = img => img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
      const isStar = img => /stars?\.svg|star/i.test(getImg(img) + ' ' + (img.alt || '') + ' ' + (img.title || ''));
      const initialsOnly = s => /^[A-Z]{1,3}$/.test(s);
      const stripInitials = s => {
        const parts = clean(s).split(' ');
        if (parts.length >= 2 && /^[A-Z]{1,3}$/.test(parts[0])) {
          const rest = parts.slice(1);
          const ini = rest.map(w => w[0].toUpperCase()).join('');
          if (ini.startsWith(parts[0]) || rest[0].slice(0, 2).toUpperCase() === parts[0]) return rest.join(' ');
        }
        return clean(s);
      };
      const looksLikeInitialName = s => stripInitials(s) !== clean(s);
      const isDateLine = l => { const m = l.match(dateRe); return !!m && l.length <= m[0].length + 25; };
      const junk = l =>
        /^[★☆\s]+$/.test(l) ||
        /^(show more|load more|see more|read more|verified purchase|verified buyer|customer reviews?|reviews?|helpful|report)$/i.test(l) ||
        /^\+\d+$/.test(l) || /^reviews of /i.test(l) || /^\d+(\.\d+)?$/.test(l) ||
        /^\d+\s*(stars?|ratings?|reviews?)$/i.test(l);

      const ratingFor = card => {
        const els = [card, ...card.querySelectorAll('[data-rating],[data-score],[aria-label],[title],[itemprop="ratingValue"]')];
        for (const el of els) {
          const raw = [el.getAttribute('data-rating'), el.getAttribute('data-score'), el.getAttribute('content'),
                       el.getAttribute('aria-label'), el.getAttribute('title')].filter(Boolean).join(' ');
          const m = raw.match(/(?:rating|rated|stars?)\s*[:=]?\s*([0-5](?:\.\d+)?)/i) || raw.match(/\b([0-5](?:\.\d+)?)\s*(?:\/\s*5|out of 5)\b/i);
          if (m && Number(m[1]) >= 0 && Number(m[1]) <= 5) return Number(m[1]);
        }
        // star bar drawn with a percentage width (e.g. width: 80% => 4 stars)
        for (const el of card.querySelectorAll('[style*="width"]')) {
          const m = (el.getAttribute('style') || '').match(/width\s*:\s*(\d+(?:\.\d+)?)%/i);
          const ctx = ((el.className || '') + ' ' + ((el.parentElement && el.parentElement.className) || '')).toString().toLowerCase();
          if (m && /star|rating/.test(ctx)) {
            const v = Number(m[1]) / 20;
            if (v > 0 && v <= 5) return Math.round(v * 10) / 10;
          }
        }
        // text stars
        const txt = card.innerText || '';
        const filledChars = (txt.match(/★/g) || []).length;
        const emptyChars = (txt.match(/☆/g) || []).length;
        if (filledChars + emptyChars >= 1 && filledChars + emptyChars <= 5) return filledChars;
        // star images
        const stars = [...card.querySelectorAll('img')].filter(isStar);
        if (stars.length) {
          let filled = 0, known = 0;
          for (const img of stars) {
            const s = (getImg(img) + ' ' + (img.className || '') + ' ' + (img.alt || '') + ' ' + (img.title || '') + ' ' +
                       (img.parentElement ? (img.parentElement.className || '') : '')).toString().toLowerCase();
            const isEmpty = /empty|outline|blank|gr[ae]y|inactive|unfilled|disabled|\boff\b|unselected|border/.test(s);
            const isFilled = !isEmpty && /fill|full|active|yellow|gold|orange|solid|checked|selected|\bon\b/.test(s);
            if (isEmpty) known++;
            if (isFilled) { known++; filled++; }
          }
          if (known > 0) return Math.min(5, filled);
          // No filled/empty marker in the DOM: assume the number of star images shown is the rating.
          if (stars.length <= 5) return stars.length;
        }
        return null;
      };

      const customerImages = card => {
        const out = [], seen = new Set();
        const add = raw => {
          if (!raw) return;
          try {
            const u = new URL(raw, location.href), p = u.pathname.toLowerCase(), s = u.href.toLowerCase();
            if (!/^https?:$/.test(u.protocol) || /stars?\.svg|avatar|profile|logo|placeholder|verified|payment|rating/.test(s)) return;
            if (/\.(svg|gif)(?:$|\?)/.test(p)) return;
            if (!p.includes('/review/') && !/\.(jpg|jpeg|png|webp|avif)(?:$|\?)/.test(p)) return;
            if (!seen.has(u.href)) { seen.add(u.href); out.push(u.href); }
          } catch (_) {}
        };
        for (const img of card.querySelectorAll('img')) {
          if (isStar(img)) continue;
          add(img.getAttribute('data-large-image')); add(img.getAttribute('data-original'));
          add(img.getAttribute('data-src')); add(img.getAttribute('data-lazy-src')); add(getImg(img));
        }
        for (const a of card.querySelectorAll('a[href]')) add(a.href);
        return out.slice(0, 10);
      };

      // Innermost elements that contain a review date.
      const dateEls = [...document.querySelectorAll('body *')].filter(el => {
        if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(el.tagName)) return false;
        const t = clean(el.textContent);
        if (!t || t.length > 60 || !dateRe.test(t)) return false;
        return ![...el.children].some(c => dateRe.test(clean(c.textContent)));
      });
      const countDates = el => dateEls.filter(d => el.contains(d)).length;

      const results = [], seen = new Set();
      for (const dateEl of dateEls) {
        // Climb to the largest ancestor that still contains exactly ONE review date.
        let card = dateEl.parentElement || dateEl, el = card;
        while (el && el !== document.body && el !== document.documentElement) {
          const t = clean(el.innerText || el.textContent);
          if (t.length > 1800 || countDates(el) !== 1) break;
          if (/Reviews of |Write a review|Customer Reviews|Rating Breakdown/i.test(t)) break;
          card = el; el = el.parentElement;
        }
        let lines = (card.innerText || card.textContent || '').split(/\n+/).map(clean).filter(Boolean);
        // Seller reply ("Priceoye.pk" ...) is not part of the customer's review.
        const sellerIdx = lines.findIndex(l => /^priceoye(\.pk)?$/i.test(l));
        if (sellerIdx > 0) lines = lines.slice(0, sellerIdx);
        lines = lines.filter(l => !/^priceoye(\.pk)?$/i.test(l) && !/^looking forward to serving you/i.test(l) && !junk(l));

        const dateIndex = lines.findIndex(isDateLine);
        const date = dateIndex >= 0 ? lines[dateIndex].match(dateRe)[0] : (clean(dateEl.textContent).match(dateRe) || [null])[0];

        // ---- author (order independent) ----
        let author = null;
        const authorNode = card.querySelector('[itemprop="author"], [class*="reviewer-name"], [class*="review-author"], [class*="author-name"], [class*="user-name"], [class*="customer-name"]');
        if (authorNode) {
          const s = clean(authorNode.innerText || authorNode.textContent);
          if (s && s.length < 100 && !dateRe.test(s)) author = stripInitials(s);
        }
        if (!author) {
          const i = lines.findIndex(looksLikeInitialName);
          if (i >= 0) author = stripInitials(lines[i]);
          else {
            const j = lines.findIndex(initialsOnly);
            if (j >= 0 && lines[j + 1] && !isDateLine(lines[j + 1])) author = lines[j + 1];
          }
        }
        if (!author && dateIndex >= 0) {
          for (const k of [dateIndex - 1, dateIndex + 1]) {
            const l = lines[k];
            if (l && l.length <= 40 && !isDateLine(l) && !initialsOnly(l) && /^[A-Za-z][A-Za-z'’. -]+$/.test(l)) { author = stripInitials(l); break; }
          }
        }

        // ---- text ----
        const authorLow = author ? author.toLowerCase() : null;
        const textLines = lines.filter(l =>
          !isDateLine(l) && !initialsOnly(l) &&
          !(authorLow && stripInitials(l).toLowerCase() === authorLow));
        let reviewText = textLines.join(' ').trim();
        if (reviewText.length > 1500) reviewText = reviewText.slice(0, 1500);
        if (!reviewText) reviewText = null;

        const images = customerImages(card);
        const rating = ratingFor(card);
        if (!author && !reviewText) continue;
        const item = { author, rating, date, text: reviewText, images };
        const key = [(author || '').toLowerCase(), date || '', (reviewText || '').toLowerCase()].join('|');
        if (!seen.has(key)) { seen.add(key); results.push(item); }
      }
      return results;
    }
    """
    try:
        data = await page.evaluate(script)
        reviews = []
        for item in data or []:
            rating = number_value(item.get("rating"))
            if rating is not None and not 0 <= rating <= 5:
                rating = None
            images = []
            for image in item.get("images", []):
                image = absolute_image_url(image, page.url)
                if image and image not in images:
                    images.append(image)
            record = {"author": normalize_author(item.get("author")), "rating": rating,
                      "rating_stars": format_rating_stars(rating), "date": clean_text(item.get("date")),
                      "text": clean_text(item.get("text")), "images": images[:10]}
            if record["author"] or record["text"]:
                reviews.append(record)
        return reviews
    except Exception as exc:
        print(f"[PriceOye rendered reviews] Extraction failed: {exc}")
        return []


async def scrape_dedicated_reviews(browser, product_url):
    product_url = clean_url(product_url)
    if not product_url:
        return []
    path = urlparse(product_url).path.rstrip("/")
    reviews_url = product_url if path.lower().endswith("/reviews") else f"{BASE_URL}{path}/reviews"
    context = await browser.new_context(locale="en-PK", viewport={"width": 1365, "height": 900}, device_scale_factor=1)
    try:
        page = await context.new_page()
        response = await page.goto(reviews_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
        if response and response.status >= 400:
            print(f"[PriceOye reviews] HTTP {response.status}: {reviews_url}")
            return []
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(1500)
        reviews, seen_markers = [], set()
        for _ in range(12):
            reviews = merge_reviews(reviews, await extract_rendered_priceoye_reviews(page))
            try:
                body_len = len(await page.locator("body").inner_text())
            except Exception:
                body_len = 0
            marker = (len(reviews), body_len)
            if marker in seen_markers or len(reviews) >= MAX_REVIEWS:
                break
            seen_markers.add(marker)
            clicked = False
            for selector in ["button:has-text('Show More')", "button:has-text('Load More')",
                             "a:has-text('Show More')", "a:has-text('Load More')", "button:has-text('See More')",
                             "a:has-text('See More')", "button:has-text('Next')", "a[rel='next']", "[aria-label*='next' i]"]:
                try:
                    loc = page.locator(selector).first
                    if await loc.count() and await loc.is_visible(timeout=300) and await loc.is_enabled():
                        await loc.scroll_into_view_if_needed(timeout=1500)
                        await loc.click(timeout=2500)
                        await page.wait_for_timeout(1000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(900)
                except Exception:
                    pass
                reviews = merge_reviews(reviews, await extract_rendered_priceoye_reviews(page))
                break
        # Old DOM/JSON extraction is only a fallback now (it produced the fragment records).
        if not reviews:
            soup = BeautifulSoup(await page.content(), "html.parser")
            objects = extract_json_objects(soup, embedded=True) + extract_json_objects(soup)
            reviews = merge_reviews(extract_dom_reviews(soup, page.url), extract_reviews_from_json(objects, page.url))
        reviews = sanitize_reviews(reviews)
        print(f"[PriceOye reviews] URL: {reviews_url} | Reviews extracted: {len(reviews)}")
        return reviews[:MAX_REVIEWS]
    except Exception as exc:
        print(f"[PriceOye reviews] Error for {reviews_url}: {exc}")
        return []
    finally:
        await context.close()

# --------------------------- Product data helpers --------------------------
def extract_image(soup, page_url, schema=None):
    if schema:
        value = schema.get("image")
        if isinstance(value, list): value = value[0] if value else None
        if isinstance(value, dict): value = value.get("url") or value.get("contentUrl")
        result = absolute_image_url(value, page_url)
        if result: return result
    for selector in ('meta[property="og:image"]', 'meta[name="twitter:image"]', 'meta[itemprop="image"]',
                     "[class*='gallery'] img", "[class*='product-image'] img", "[class*='productImage'] img", "main img"):
        for node in soup.select(selector):
            result = absolute_image_url(node.get("content") or node.get("data-zoom-image") or node.get("data-large-image") or node.get("data-src") or node.get("data-original") or node.get("src"), page_url)
            if result: return result
    return None


async def get_description(page, soup, schema):
    selectors = ['[itemprop="description"]', "#description", ".product-description", ".product__description",
                 ".product-detail-description", "[class*='product-description']"]
    for selector in selectors:
        try:
            nodes = page.locator(selector)
            for i in range(min(await nodes.count(), 5)):
                node = nodes.nth(i)
                if await node.is_visible(timeout=300):
                    text = clean_text(await node.inner_text(timeout=1500))
                    if text and len(text) > 20: return text
        except Exception:
            continue
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            if text and len(text) > 20: return text
    return clean_text(schema.get("description"))

# ------------------------------ Search logic -------------------------------
def is_product_url(url):
    path = urlparse(url).path.strip("/").lower()
    excluded = {"shop", "products", "search", "cart", "checkout", "login", "register", "account", "about", "contact",
                "privacy-policy", "terms-and-conditions", "mobiles", "smart-watches", "wireless-earbuds", "tablets", "laptops", "accessories"}
    if not path or path in excluded or path.startswith(("blog/", "pages/", "collections/", "brands/", "category/")):
        return False
    return True


def extract_search_products(html, page_url, keyword=None):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    normalized = clean_text(keyword).casefold() if keyword else None
    for selector in ("div.productBox", ".product-box", ".product-card", ".product-item", "[class*='productBox']", "[class*='product-card']", "[data-product-id]"):
        for card in soup.select(selector):
            link = card.select_one("a[href]")
            if not link: continue
            url = clean_url(urljoin(page_url, link.get("href", "")))
            if not url or not is_product_url(url): continue
            title_node = card.select_one("h1, h2, h3, .p-title, .product-title, [class*='product-title'], [class*='p-title']")
            title = clean_text(title_node.get_text(" ", strip=True) if title_node else link.get("title") or link.get_text(" ", strip=True))
            if not title:
                img_node = card.select_one("img[alt]")
                title = clean_text(img_node.get("alt")) if img_node else None
            if not title or (normalized and normalized not in title.casefold()): continue
            price, original = extract_price(card), None
            original = extract_original_price(card, price)
            current_text, old_text = extract_price_pair(card.get_text(" ", strip=True))
            if price is None: price = current_text
            if original is None and old_text and price and old_text > price: original = old_text
            img = card.select_one("img")
            image = absolute_image_url(img.get("data-src") or img.get("data-original") or img.get("src"), page_url) if img else None
            card_text = card.get_text(" ", strip=True)
            found[url] = {"url": url, "title": title, "price": price, "original_price": original,
                          "discount": extract_discount(card, card_text) or calculate_discount(price, original), "image": image}
    return list(found.values())


async def submit_site_search(page, keyword):
    for selector in ('input[type="search"]', 'input[name="search"]', 'input[name="q"]', 'input[name="query"]',
                     'input[placeholder*="search" i]', 'input[aria-label*="search" i]'):
        try:
            field = page.locator(selector).first
            if not await field.is_visible(timeout=800): continue
            await field.fill(keyword, timeout=3000)
            await field.press("Enter", timeout=3000)
            try: await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightTimeoutError: pass
            await page.wait_for_timeout(1000)
            return True
        except Exception:
            continue
    return False


def add_page_parameter(url, number):
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(number)]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query, doseq=True), ""))


async def scrape_search_pages(browser, keyword, limit):
    context = await browser.new_context(locale="en-PK", viewport={"width": 1365, "height": 900})
    page, found, search_url = await context.new_page(), {}, None
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
        await submit_site_search(page, keyword)
        if page.url.rstrip("/") != BASE_URL: search_url = page.url
        if search_url:
            for item in extract_search_products(await page.content(), page.url, keyword): found[item["url"]] = item
        if not found:
            for candidate in [f"{BASE_URL}/search?{urlencode({'q': keyword})}", f"{BASE_URL}/search?{urlencode({'query': keyword})}", f"{BASE_URL}/products?{urlencode({'q': keyword})}"]:
                try:
                    await page.goto(candidate, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                    items = extract_search_products(await page.content(), page.url, keyword)
                    if items:
                        search_url = page.url
                        for item in items: found[item["url"]] = item
                        break
                except Exception: continue
        if not search_url: return list(found.values())[:limit]
        for number in range(2, MAX_SEARCH_PAGES + 1):
            if len(found) >= limit: break
            try:
                await page.goto(add_page_parameter(search_url, number), wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                items = extract_search_products(await page.content(), page.url, keyword)
            except Exception: break
            added = 0
            for item in items:
                if item["url"] not in found: found[item["url"]] = item; added += 1
                if len(found) >= limit: break
            if not added: break
        return list(found.values())[:limit]
    finally:
        await context.close()

# ---------------------------- Product scraping -----------------------------
async def open_reviews_section(page):
    labels = re.compile(r"^(reviews?|customer reviews?|ratings? ?(&|and)? ?reviews?)(\s*\(?\d+\)?)?$", re.I)
    for selector in ("a", "button", "[role='tab']", "[role='button']"):
        try:
            locator = page.locator(selector)
            for index in range(min(await locator.count(), 150)):
                element = locator.nth(index)
                try:
                    if not await element.is_visible(timeout=250): continue
                    text = clean_text(await element.inner_text(timeout=500)) or clean_text(await element.get_attribute("aria-label"))
                    if text and labels.fullmatch(text.strip()):
                        await element.click(timeout=2500); await page.wait_for_timeout(900); break
                except Exception: continue
        except Exception: continue
    for selector in ("#reviews", "#review", "[id*='review' i]", "[class*='reviews-section']", "[class*='review-section']"):
        try:
            element = page.locator(selector).first
            if await element.count() and await element.is_visible(timeout=300):
                await element.scroll_into_view_if_needed(timeout=2000); await page.wait_for_timeout(700); break
        except Exception: continue
    for _ in range(6):
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)"); await page.wait_for_timeout(350)
        except Exception: break
    try: await page.wait_for_load_state("networkidle", timeout=2500)
    except PlaywrightTimeoutError: pass
    await page.wait_for_timeout(500)


async def scrape_product(browser, item, semaphore):
    async with semaphore:
        url = clean_url(item.get("url"))
        if not url: return None
        context = await browser.new_context(locale="en-PK", viewport={"width": 1365, "height": 900})
        page = await context.new_page()
        product = empty_product(url, item.get("title"))
        captured_json = []
        async def capture_response(response):
            try:
                if "json" not in response.headers.get("content-type", "").lower(): return
                low = response.url.lower()
                if any(x in low for x in ("analytics", "google", "facebook", "pixel", "tracking", "hotjar")): return
                if response.status < 200 or response.status >= 300 or len(captured_json) >= 100: return
                captured_json.extend(walk_json(await response.json()))
            except Exception: pass
        page.on("response", lambda response: asyncio.create_task(capture_response(response)))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            try: await page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError: pass
            await page.wait_for_timeout(700)
            soup = BeautifulSoup(await page.content(), "html.parser")
            schema = find_product_schema(soup)
            try: body_text = await page.locator("body").inner_text(timeout=5000)
            except Exception: body_text = soup.get_text(" ", strip=True)
            h1 = soup.select_one("h1")
            product["title"] = clean_text(schema.get("name") or (h1.get_text(" ", strip=True) if h1 else None) or item.get("title"))
            current = schema_price(schema) or meta_price(soup) or extract_price(soup)
            visible_current, visible_original = extract_price_pair(body_text)
            if current is None: current = visible_current or item.get("price")
            product["price"] = current
            original = extract_original_price(soup, current)
            if original is None and visible_original is not None and current is not None and visible_original > current: original = visible_original
            if original is None: original = item.get("original_price")
            if original is not None and current is not None and original <= current: original = None
            product["original_price"] = original
            product["discount"] = extract_discount(soup, body_text) or calculate_discount(current, original) or item.get("discount")
            product["image"] = extract_image(soup, url, schema) or item.get("image")
            product["sku"] = clean_text(schema.get("sku") or schema.get("productID"))
            product["brand"] = schema_brand(schema)
            availability = get_offer(schema).get("availability")
            if availability: product["availability"] = str(availability).rsplit("/", 1)[-1].replace("_", " ")
            if not product["availability"]:
                node = soup.select_one('[itemprop="availability"]')
                if node:
                    raw = node.get("href") or node.get("content") or node.get_text(" ", strip=True)
                    product["availability"] = str(raw).rsplit("/", 1)[-1].replace("_", " ")
            rating, count = schema_rating(schema)
            page_rating, page_count = extract_overall_rating(soup, body_text)
            product["rating"] = rating if rating is not None else page_rating
            product["review_count"] = count if count is not None else page_count
            product["description"] = await get_description(page, soup, schema)
            await open_reviews_section(page)
            updated_soup = BeautifulSoup(await page.content(), "html.parser")
            reviews = await scrape_dedicated_reviews(browser, url)
            if not reviews:
                reviews = merge_reviews(extract_dom_reviews(updated_soup, page.url),
                                        extract_reviews_from_json(captured_json, page.url))
            product["reviews"] = sanitize_reviews(reviews)[:MAX_REVIEWS]
            try: updated_text = await page.locator("body").inner_text(timeout=3000)
            except Exception: updated_text = updated_soup.get_text(" ", strip=True)
            new_rating, new_count = extract_overall_rating(updated_soup, updated_text)
            if product["rating"] is None: product["rating"] = new_rating
            if product["review_count"] is None: product["review_count"] = new_count
            if product["review_count"] is None and product["reviews"]: product["review_count"] = len(product["reviews"])
            product["url"], product["currency"] = url, "PKR"
        except Exception as exc:
            product["_error"] = str(exc)
        finally:
            await context.close()
        return product

# --------------------------------- API -------------------------------------
@app.get("/")
async def home():
    return {"name": "PriceOye Product Scraper", "version": "6.3.1", "status": "running",
            "endpoints": {"search": "/api/search?keyword=iphone&limit=20",
                          "product": "/api/product?url=https://priceoye.pk/mobiles/apple/apple-iphone-17-pro"}}


@app.get("/api/search")
async def search_products(keyword: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)):
    keyword = clean_text(keyword)
    if not keyword: raise HTTPException(400, "Keyword cannot be empty.")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            items = await scrape_search_pages(browser, keyword, limit)
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_PRODUCTS)
            results = await asyncio.gather(*[scrape_product(browser, item, semaphore) for item in items], return_exceptions=True)
            products = []
            for index, result in enumerate(results):
                if isinstance(result, Exception) or not result:
                    item = items[index]; fallback = empty_product(item.get("url"), item.get("title"))
                    for key in ("price", "original_price", "discount", "image"): fallback[key] = item.get(key)
                    products.append(fallback)
                else:
                    result.pop("_error", None); products.append(result)
            return {"keyword": keyword, "requested_limit": limit, "count": len(products), "products": products}
        finally:
            await browser.close()


@app.get("/api/product")
async def get_product(url: str = Query(None), handle: str = Query(None)):
    target = url or (urljoin(BASE_URL, handle.strip("/")) if handle else None)
    if not target: raise HTTPException(400, "Provide either url or handle.")
    target = clean_url(target)
    if not target: raise HTTPException(400, "Invalid URL. Use a PriceOye product URL.")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            product = await scrape_product(browser, {"url": target}, asyncio.Semaphore(1))
            if not product: raise HTTPException(404, "Could not scrape this product.")
            product.pop("_error", None)
            return product
        finally:
            await browser.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, workers=1)