"""HTTP adapters. Digikala's public web endpoint is unofficial and may change."""
import datetime as dt
import json
import http.cookiejar
import pathlib
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit, quote

from .core import Offer, UTC, safe_url


HTTP = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def fetch_page(url, *, attempts=3):
    safe_url(url)
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(quote(url, safe=":/?=&%+@;,#"), headers={"User-Agent": "RealDealRadar/0.4 (+price-research)", "Accept": "application/json,text/html"})
            with HTTP.open(req, timeout=20) as response:
                safe_url(response.url)
                data = response.read(5_000_001)
                if len(data) > 5_000_000:
                    raise ValueError("Response exceeds 5 MB")
                return data.decode("utf-8"), response.url
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise RuntimeError(f"HTTP {error.code} from {urlsplit(url).hostname}") from None
            retry = error.headers.get("Retry-After", "")
            time.sleep(min(30, int(retry)) if retry.isdigit() else 2 ** (attempt + 1))
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise RuntimeError(f"Network failure from {urlsplit(url).hostname}") from None
            time.sleep(2 ** (attempt + 1))


def fetch(url, *, attempts=3):
    return fetch_page(url, attempts=attempts)[0]


def json_feed(location, base):
    if str(location).startswith("https://"):
        raw = fetch(location)
    else:
        raw = (base / location).read_text(encoding="utf-8")
    rows = json.loads(raw)
    if isinstance(rows, dict):
        rows = rows["offers"]
    if not isinstance(rows, list):
        raise ValueError("Feed must contain an offers array")
    return [Offer.parse(row) for row in rows]


class StructuredData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.buffer = []
        self.documents = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.active = True
            self.buffer = []

    def handle_data(self, data):
        if self.active:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.active = False
            self.documents.append(json.loads("".join(self.buffer)))


def products(value):
    if isinstance(value, list):
        for child in value:
            yield from products(child)
    elif isinstance(value, dict):
        kind = value.get("@type", [])
        if kind == "Product" or isinstance(kind, list) and "Product" in kind:
            yield value
        if "@graph" in value:
            yield from products(value["@graph"])


def merchant_quote(mapping, html, observed_at):
    # A manually reviewed SKU binding prevents fuzzy title matches.
    parser = StructuredData()
    parser.feed(html)
    matches = [p for document in parser.documents for p in products(document)
               if str(p.get("sku", "")) == str(mapping["expected_sku"])]
    if len(matches) != 1:
        raise ValueError("Merchant Product SKU absent or ambiguous")
    offers = matches[0].get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    if len(offers) != 1 or offers[0].get("@type") != "Offer":
        raise ValueError("Require one concrete Offer; aggregate prices are not accepted")
    q = offers[0]
    currency = q.get("priceCurrency")
    if currency not in ("IRR", "IRT"):
        raise ValueError("Merchant priceCurrency must be IRR or IRT")
    price = float(str(q["price"]).replace(",", "")) / (10 if currency == "IRR" else 1)
    row = {k: mapping[k] for k in ["product_key", "title", "url", "seller_id", "variant_key", "warranty_key", "category", "supermarket"]}
    row.update(price_toman=price, shipping_toman=mapping.get("shipping_toman"),
               observed_at=observed_at, in_stock=str(q.get("availability", "")).rsplit("/", 1)[-1] == "InStock")
    if q.get("priceValidUntil"):
        row["expires_at"] = q["priceValidUntil"] if "T" in q["priceValidUntil"] else q["priceValidUntil"] + "T23:59:59+03:30"
    return Offer.parse(row)


def dk_card(product, now):
    variant = product.get("default_variant") or {}
    price = variant.get("price") or {}
    seller = variant.get("seller") or {}
    color = variant.get("color") or {}
    size = variant.get("size") or {}
    warranty = variant.get("warranty") or {}
    category = product.get("category") or {}
    layer = product.get("data_layer") or {}
    taxonomy = [layer.get(f"item_category{i}", "") for i in range(2, 6)]
    allowed_roots = {"کالای دیجیتال", "لوازم خانگی برقی", "مد و پوشاک", "خانه و آشپزخانه", "ورزش و سفر", "خودرو و موتورسیکلت", "ابزار آلات و تجهیزات", "کتاب، لوازم تحریر و هنر"}
    excluded = any(any(term in value for term in ("سوپرمارکت", "مواد غذایی", "خوراکی", "مکمل", "شوینده")) for value in taxonomy)
    supermarket = category.get("is_supermarket")
    if not isinstance(supermarket, bool):
        supermarket = excluded or taxonomy[0] not in allowed_roots
    variant_key = f"color:{color.get('id', 'none')}|size:{size.get('id', 'none')}"
    path = (product.get("url") or {}).get("uri")
    if not path:
        raise ValueError("Digikala product URL missing")
    row = dict(product_key=f"dkp:{product['id']}", product_id=product["id"],
               title=product["title_fa"], url="https://www.digikala.com" + path,
               seller_id=f"digikala-seller:{seller.get('id', 'unknown')}",
               price_toman=price["selling_price"] / 10, shipping_toman=None,
               variant_key=variant_key, warranty_key=warranty.get("title_fa") or warranty.get("title") or "نامشخص",
               observed_at=now.isoformat(), in_stock=product.get("status") == "marketable" and variant.get("status", "marketable") == "marketable" and price.get("order_limit", 1) > 0 and not price.get("is_locked_for_digiplus", False),
               category=category.get("title_fa") or next((v for v in reversed(taxonomy) if v), "نامشخص"),
               # Missing classification is excluded conservatively.
               supermarket=supermarket,
               discount_percent=price.get("discount_percent", 0))
    row.update(color_name=color.get("title", ""), size_name=size.get("title", ""), brand_name=layer.get("brand", ""))
    if price.get("timer", 0) > 0:
        row["expires_at"] = (now + dt.timedelta(seconds=price["timer"])).isoformat()
    return Offer.parse(row)


def dk_details(product_id, now):
    data = json.loads(fetch(f"https://api.digikala.com/v2/product/{int(product_id)}/"))
    return dk_card(data["data"]["product"], now)


def collect(config, now):
    candidates, market, errors = [], [], []
    feeds = config.get("feeds", {})
    for kind, target in [("candidates", candidates), ("market", market)]:
        for index, location in enumerate(feeds.get(kind, [])):
            try:
                target.extend(json_feed(location, config["_base"]))
            except Exception as error:
                errors.append(f"{kind} feed {index + 1}: {type(error).__name__}: {error}")
    for index, mapping in enumerate(config.get("market_pages", [])):
        try:
            market.append(merchant_quote(mapping, fetch(mapping["url"]), now.isoformat()))
        except Exception as error:
            errors.append(f"merchant page {index + 1}: {type(error).__name__}: {error}")
    dk = config.get("digikala", {})
    if dk.get("enabled", False):
        pages = int(dk.get("pages", 3))
        if not 1 <= pages <= 20:
            raise ValueError("Digikala pages must be 1..20")
        for page in range(1, pages + 1):
            try:
                data = json.loads(fetch(f"https://api.digikala.com/v1/incredible-offers/products/?page={page}&sort=4"))
                rows = data["data"].get("products")
                if not isinstance(rows, list):
                    raise ValueError("Digikala response schema changed")
                for row in rows:
                    try:
                        candidates.append(dk_card(row, now))
                    except (KeyError, ValueError, TypeError):
                        errors.append(f"Digikala product {row.get('id', '?')}: incomplete offer")
            except Exception as error:
                errors.append(f"Digikala page {page}: {type(error).__name__}: {error}")
            if page < pages:
                time.sleep(max(1, float(dk.get("request_delay_seconds", 3))))
    unique = {}
    for offer in candidates:
        key = (offer.product_key, offer.variant_key, offer.warranty_key)
        if key not in unique or offer.observed_at > unique[key].observed_at:
            unique[key] = offer
    candidates = list(unique.values())
    if config.get("torob", {}).get("enabled", False):
        from .market import collect_market
        quotes, notices = collect_market(candidates, config, now)
        market.extend(quotes)
        errors.extend(notices)
    return candidates, market, errors
