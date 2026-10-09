"""Torob discovers shops; only fresh, equivalent merchant offers are evidence.

An aggregator's cheapest price is never promoted to verified evidence. Ambiguous
names, variants, warranties, AggregateOffer and inaccessible shops fail closed.
"""
import dataclasses
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


def search_query(candidate):
    model = re.search(r"مدل\s+([^\s،,]+)", candidate.title)
    if model and any(c.isdigit() for c in model[1]):
        return (candidate.brand_name + " " + model[1]).strip(), normalized(model[1])
    return candidate.title, normalized(candidate.title)


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
    matches = [p for document in parser.documents for p in products(document)
               if normalized(p.get("name", "")) == normalized(candidate.title)]
    if len(matches) != 1:
        raise ValueError("exact_product_name_missing_or_ambiguous")
    product = matches[0]
    props = product.get("additionalProperty", [])
    if isinstance(props, dict):
        props = [props]
    attrs = {normalized(p.get("name", "")): p.get("value", "") for p in props if isinstance(p, dict)}
    def attribute(*names):
        for name in names:
            if normalized(name) in attrs:
                return attrs[normalized(name)]
        return ""
    # Attribute evidence must belong to this Product, not a footer, another
    # product's reviews, a search snippet or an unfiltered list of variants.
    color = product.get("color") or attribute("رنگ", "color")
    size = product.get("size") or attribute("سایز", "اندازه", "size")
    if normalized(color) != normalized(candidate.color_name):
        raise ValueError("color_mismatch_or_missing")
    if normalized(size) != normalized(candidate.size_name):
        raise ValueError("size_mismatch_or_missing")
    warranty = attribute("گارانتی", "ضمانت", "warranty")
    if candidate.warranty_key == "نامشخص" or normalized(warranty) != normalized(candidate.warranty_key):
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
    selected = [c for c in candidates if not c.supermarket and c.in_stock and
                c.discount_percent >= config["min_advertised_discount"]][:limit]
    for candidate in selected:
        if time.monotonic() >= deadline:
            break
        record = dict(product_key=candidate.product_key, title=candidate.title, matches=[], failures=[])
        audit.append(record)
        try:
            query, required = search_query(candidate)
            record["query"] = query
            search = json.loads(fetch("https://api.torob.com/v4/base-product/search/?" + urlencode({"q": query, "page": 0})))
            seen = set()
            rows = []
            for item in search["results"]:
                name = normalized(item.get("name1", "") + " " + item.get("name2", ""))
                if " " + required + " " not in " " + name + " ":
                    continue
                if candidate.brand_name and " " + normalized(candidate.brand_name) + " " not in " " + name + " ":
                    continue
                key = item.get("random_key")
                if key and key not in seen:
                    rows.append(item)
                    seen.add(key)
            for item in rows[:result_limit]:
                if time.monotonic() >= deadline:
                    break
                time.sleep(delay)
                key = item["random_key"]
                detail = json.loads(fetch("https://api.torob.com/v4/base-product/details/?" + urlencode({"prk": key})))
                sellers = detail.get("products_info", {}).get("result", [])
                match = dict(title=detail.get("name1"), url="https://torob.com/p/" + key + "/",
                             aggregator_price_toman=detail.get("price"), verified_sellers=[], failures=[])
                record["matches"].append(match)
                eligible = [s for s in sellers if s.get("availability") is True and not s.get("is_price_unreliable") and not s.get("installment")]
                eligible.sort(key=lambda s: s.get("price", float("inf")))
                for seller in eligible[:shop_limit]:
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
            errors.append("Torob discovery failed: " + candidate.product_key + ": " + type(error).__name__)
    destination = config["_base"] / config.get("output_dir", "output")
    destination.mkdir(parents=True, exist_ok=True)
    summary = dict(checked=len(audit), budget_exhausted=time.monotonic() >= deadline,
                   matched_direct_quotes=len(quotes), provider="torob_and_direct_merchants")
    config["_market_coverage"] = summary
    (destination / "market-discovery.json").write_text(json.dumps(dict(generated_at=now.isoformat(), **summary,
        eligible=len([c for c in candidates if not c.supermarket and c.in_stock and c.discount_percent >= config["min_advertised_discount"]]),
        records=audit), ensure_ascii=False, indent=2), encoding="utf-8")
    return quotes, errors
