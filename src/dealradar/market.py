"""Torob discovers shops; only fresh, equivalent merchant offers are evidence.

An aggregator's cheapest price is never promoted to verified evidence. Ambiguous
names, variants, warranties, AggregateOffer and inaccessible shops fail closed.
"""
import dataclasses
from collections import Counter
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

from .core import Offer, safe_url
from .sources import StructuredData, fetch, fetch_page, products


def normalized(value):
    value = str(value).translate(str.maketrans("يك۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "یک01234567890123456789"))
    return " ".join(re.findall(r"[^\W_]+", value.casefold()))


def color_identity(value):
    name = normalized(value)
    aliases = {"black": "مشکی", "سیاه": "مشکی", "white": "سفید",
               "red": "قرمز", "blue": "آبی", "green": "سبز",
               "silver": "نقره ای", "نقره‌ای": "نقره ای",
               "grey": "خاکستری", "gray": "خاکستری", "gold": "طلایی"}
    return aliases.get(name, name)


def warranty_identity(value):
    text = normalized(value)
    # Normalize prose, never erase the duration or guarantor's name.
    text = re.sub(r"(\d+)\s*ماهه?", r"\1 ماه", text)
    tokens = [t for t in text.split() if t not in {"گارانتی", "ضمانت", "کالا"}]
    return tuple(sorted(tokens))


def failure_code(error, prefix):
    status = re.search(r"HTTP (\d{3})", str(error))
    return prefix + "_http_" + status[1] if status else prefix + "_request_failed"


def search_query(candidate):
    model = re.search(r"مدل\s+([^\s،,]+)", candidate.title)
    if model and any(c.isdigit() for c in model[1]):
        return (candidate.brand_name + " " + model[1]).strip(), normalized(model[1])
    return candidate.title, normalized(candidate.title)


def model_code(title):
    match = re.search(r"مدل\s+([A-Za-z][A-Za-z0-9.-]*[0-9][A-Za-z0-9.-]*)", title)
    return re.sub(r"[^a-z0-9]", "", match[1].lower()) if match else ""


class ProductAttributes(HTMLParser):
    """Read scoped WooCommerce specification tables, never global page text."""
    def __init__(self):
        super().__init__()
        self.active = False
        self.cell = None
        self.key = self.value = ""
        self.attrs = {}

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.active = "woocommerce-product-attributes" in dict(attrs).get("class", "").split()
        if self.active and tag == "tr":
            self.key = self.value = ""
        if self.active and tag in ("th", "td"):
            self.cell = tag

    def handle_data(self, data):
        if self.cell == "th": self.key += data
        if self.cell == "td": self.value += data

    def handle_endtag(self, tag):
        if tag in ("th", "td"): self.cell = None
        if self.active and tag == "tr" and self.key.strip():
            self.attrs[normalized(self.key)] = self.value.strip()
        if tag == "table": self.active = False


def product_identity(candidate, product, attrs):
    code = model_code(candidate.title)
    explicit_mpn = product.get("mpn") or attrs.get(normalized("مدل")) or attrs.get("model")
    if code and explicit_mpn and re.sub(r"[^a-z0-9]", "", str(explicit_mpn).lower()) != code:
        return False
    if normalized(product.get("name", "")) == normalized(candidate.title):
        return True
    # Distinct seller prose is accepted only with a concrete manufacturer/model
    # identifier. Numerical generic models (e.g. clothing 311) fail closed.
    mpn = product.get("mpn") or attrs.get(normalized("مدل")) or attrs.get("model")
    if not code:
        return False
    if mpn:
        if re.sub(r"[^a-z0-9]", "", str(mpn).lower()) != code: return False
    else:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9.-]*", product.get("name", ""))
        if not any(re.sub(r"[^a-z0-9]", "", t.lower()) == code for t in tokens): return False
    brand = product.get("brand")
    if isinstance(brand, dict): brand = brand.get("name")
    brand = brand or attrs.get(normalized("برند"))
    if not candidate.brand_name:
        return False
    if brand:
        if normalized(brand) != normalized(candidate.brand_name): return False
    elif " " + normalized(candidate.brand_name) + " " not in " " + normalized(product.get("name", "")) + " ":
        return False
    # Pack counts, capacity options and edition suffixes can vary within a model.
    for pattern in [r"بسته\s*(\d+)\s*(?:عددی|تایی)", r"(\d+)\s*(?:GB|TB|گیگابایت|ترابایت)", r"\b(pro|plus|max|mini|ultra|anc|nc|se|lte|nfc|4g|5g|l)\b"]:
        a = re.findall(pattern, normalized(candidate.title), flags=re.I)
        b = re.findall(pattern, normalized(product.get("name", "")), flags=re.I)
        if sorted(a) != sorted(b): return False
    return True


class ShopRedirect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_noscript = False
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "noscript":
            self.in_noscript = True
        # Torob's current fallback link is outside the noscript style block.
        # A single explicit HTTPS anchor on this known redirect page is enough;
        # ambiguous pages are rejected and scripts are never executed.
        if tag == "a":
            url = dict(attrs).get("href", "")
            if url.startswith("https://"):
                self.links.append(safe_url(url))

    def handle_endtag(self, tag):
        if tag == "noscript":
            self.in_noscript = False


def shop_page(url):
    html, destination = fetch_page(url, attempts=1)
    if urlsplit(destination).hostname == "api.torob.com":
        parser = ShopRedirect()
        parser.feed(html)
        links = set(parser.links)
        if len(links) != 1:
            raise ValueError("unambiguous_merchant_link_required")
        html, destination = fetch_page(links.pop(), attempts=1)
    return html, destination


def equivalent_quote(candidate, html, url, seller_id, now):
    parser = StructuredData()
    parser.feed(html)
    all_products = [p for document in parser.documents for p in products(document)]
    scoped = ProductAttributes()
    if len(all_products) == 1:
        scoped.feed(html)
    def attributes(p):
        props = p.get("additionalProperty", [])
        if isinstance(props, dict): props = [props]
        return {**scoped.attrs, **{normalized(a.get("name", "")): a.get("value", "") for a in props if isinstance(a, dict)}}
    matches = [p for p in all_products if product_identity(candidate, p, attributes(p))]
    if len(matches) != 1:
        raise ValueError("exact_product_name_missing_or_ambiguous")
    product = matches[0]
    attrs = attributes(product)
    def attribute(*names):
        for name in names:
            if normalized(name) in attrs:
                return attrs[normalized(name)]
        return ""
    # Attribute evidence must belong to this Product, not a footer, another
    # product's reviews, a search snippet or an unfiltered list of variants.
    color = product.get("color") or attribute("رنگ", "color")
    size = product.get("size") or attribute("سایز", "اندازه", "size")
    if color_identity(color) != color_identity(candidate.color_name):
        raise ValueError("color_mismatch_or_missing")
    if normalized(size) != normalized(candidate.size_name):
        raise ValueError("size_mismatch_or_missing")
    warranty = attribute("گارانتی", "ضمانت", "warranty")
    if candidate.warranty_key == "نامشخص" or not warranty_identity(warranty) or warranty_identity(warranty) != warranty_identity(candidate.warranty_key):
        raise ValueError("warranty_mismatch_or_missing")
    offers = product.get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    if len(offers) != 1 or offers[0].get("@type") != "Offer":
        raise ValueError("concrete_single_offer_required")
    offer = offers[0]
    if offer.get("priceCurrency") not in ("IRR", "IRT"):
        raise ValueError("explicit_currency_required")
    if str(offer.get("availability", "")).rsplit("/", 1)[-1] != "InStock":
        raise ValueError("explicit_stock_required")
    if offer.get("itemCondition") and not str(offer["itemCondition"]).endswith("NewCondition"):
        raise ValueError("used_or_refurbished")
    price = float(str(offer["price"]).replace(",", "")) / (10 if offer["priceCurrency"] == "IRR" else 1)
    row = dataclasses.asdict(candidate)
    row.update(url=safe_url(url), seller_id=seller_id, price_toman=price,
               shipping_toman=None, product_id=None, discount_percent=0,
               observed_at=now.isoformat(), expires_at=None)
    if offer.get("priceValidUntil"):
        expiry = offer["priceValidUntil"]
        row["expires_at"] = expiry if "T" in expiry else expiry + "T23:59:59+03:30"
    return Offer.parse(row)


def collect_market(candidates, config, now):
    settings = config["torob"]
    limit = int(settings.get("max_candidates", 20))
    shop_limit = int(settings.get("shops_per_product", 4))
    result_limit = int(settings.get("search_results", 2))
    if not (1 <= limit <= 60 and 1 <= shop_limit <= 8 and 1 <= result_limit <= 3):
        raise ValueError("Torob limits: candidates 1..60, shops 1..8, results 1..3")
    delay = max(1, float(settings.get("request_delay_seconds", 1)))
    deadline = time.monotonic() + min(600, max(30, float(settings.get("max_duration_seconds", 480))))
    audit, quotes, errors = [], [], []
    provider_status = "available"
    eligible_candidates = [c for c in candidates if not c.supermarket and c.in_stock and
                           c.discount_percent >= config["min_advertised_discount"]]
    ranked = sorted(eligible_candidates, key=lambda c: (not bool(model_code(c.title)), -c.discount_percent, c.product_key))
    # Round-robin categories prevents a page of discounted socks consuming the
    # entire market-search allowance before electronics are reached.
    selected = []
    while ranked and len(selected) < limit:
        used = set()
        remaining = []
        for c in ranked:
            if c.category not in used and len(selected) < limit:
                selected.append(c); used.add(c.category)
            else: remaining.append(c)
        ranked = remaining
    for candidate in selected:
        if time.monotonic() >= deadline:
            break
        record = dict(product_key=candidate.product_key, title=candidate.title, matches=[], failures=[])
        audit.append(record)
        try:
            query, required = search_query(candidate)
            record["query"] = query
            # Pace search requests too, including searches with zero matches.
            time.sleep(delay)
            search = json.loads(fetch("https://api.torob.com/v4/base-product/search/?" + urlencode({"q": query, "page": 0}), attempts=1))
            seen = set()
            rows = []
            for item in search["results"]:
                name = normalized(item.get("name1", "") + " " + item.get("name2", ""))
                code = model_code(candidate.title)
                model_match = code and any(re.sub(r"[^a-z0-9]", "", token.lower()) == code for token in re.findall(r"[A-Za-z][A-Za-z0-9.-]*", item.get("name1", "") + " " + item.get("name2", "")))
                if not model_match and " " + required + " " not in " " + name + " ":
                    continue
                if candidate.brand_name and " " + normalized(candidate.brand_name) + " " not in " " + name + " ":
                    continue
                key = item.get("random_key")
                if key and key not in seen:
                    rows.append(item)
                    seen.add(key)
            rows.sort(key=lambda item: bool(item.get("is_adv")))
            for item in rows[:result_limit]:
                if time.monotonic() >= deadline:
                    break
                time.sleep(delay)
                key = item["random_key"]
                detail = json.loads(fetch("https://api.torob.com/v4/base-product/details/?" + urlencode({"prk": key}), attempts=1))
                sellers = detail.get("products_info", {}).get("result", [])
                match = dict(title=detail.get("name1"), url="https://torob.com/p/" + key + "/",
                             aggregator_price_toman=detail.get("price"), verified_sellers=[], failures=[])
                record["matches"].append(match)
                # installment describes optional payment providers, NOT whether
                # the displayed price is a monthly payment. Cash-price evidence
                # still comes from the merchant's concrete Product Offer.
                eligible = [s for s in sellers if s.get("availability") is True and not s.get("is_price_unreliable")]
                eligible.sort(key=lambda s: s.get("price", float("inf")))
                independent = []
                seen_shops = set()
                for seller in eligible:
                    shop = seller.get("shop_id") or seller.get("shop_name") or seller.get("page_url")
                    if shop not in seen_shops:
                        independent.append(seller); seen_shops.add(shop)
                for seller in independent[:shop_limit]:
                    if time.monotonic() >= deadline:
                        break
                    try:
                        time.sleep(delay)
                        url = safe_url(seller["page_url"])
                        if urlsplit(url).hostname != "api.torob.com":
                            raise ValueError("unexpected_shop_redirect_host")
                        html, destination = shop_page(url)
                        host = urlsplit(destination).hostname.removeprefix("www.")
                        if host in ("digikala.com", "torob.com", "api.torob.com"):
                            raise ValueError("independent_merchant_page_required")
                        q = equivalent_quote(candidate, html, destination, "merchant:" + host, now)
                        quotes.append(q)
                        match["verified_sellers"].append(dataclasses.asdict(q))
                    except Exception as error:
                        match["failures"].append(dict(shop=seller.get("shop_name"), reason=str(error)))
        except Exception as error:
            record["failures"].append(str(error))
            code = failure_code(error, "discovery")
            errors.append("Torob discovery failed: " + candidate.product_key + ": " + code)
            if code in {"discovery_http_403", "discovery_http_429", "discovery_http_490"}:
                # Respect refusal/rate limits. Do not hit dozens of remaining
                # products after the provider has rejected this run.
                provider_status = "unavailable"
                break
    destination = config["_base"] / config.get("output_dir", "output")
    destination.mkdir(parents=True, exist_ok=True)
    summary = dict(checked=len(audit), budget_exhausted=time.monotonic() >= deadline,
                   matched_direct_quotes=len(quotes), provider="torob_and_direct_merchants",
                   provider_status=provider_status, selected=len(selected))
    failures = Counter()
    for record in audit:
        for reason in record["failures"]:
            failures[failure_code(reason, "discovery")] += 1
        if not record["matches"] and not record["failures"]: failures["no_exact_market_result"] += 1
        for match in record["matches"]:
            if not match["verified_sellers"] and not match["failures"]: failures["no_eligible_online_seller"] += 1
            for failure in match["failures"]:
                reason = failure["reason"]
                # Only fixed codes reach logs/channel summaries; raw source URLs
                # and exception strings remain in the downloadable audit.
                status = re.search(r"HTTP (\d{3})", reason)
                code = reason if re.fullmatch(r"[a-z_]+", reason) else ("merchant_http_" + status[1] if status else "merchant_request_failed")
                failures[code] += 1
    summary["failure_counts"] = dict(failures)
    config["_market_coverage"] = summary
    (destination / "market-discovery.json").write_text(json.dumps(dict(generated_at=now.isoformat(), **summary,
        eligible=len([c for c in candidates if not c.supermarket and c.in_stock and c.discount_percent >= config["min_advertised_discount"]]),
        records=audit), ensure_ascii=False, indent=2), encoding="utf-8")
    return quotes, errors
