from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import pathlib
import sqlite3
import tomllib
from urllib.parse import urlsplit

UTC = dt.timezone.utc


def timestamp(value: str) -> dt.datetime:
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result.astimezone(UTC)


def money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Prices must be non-negative numeric Toman values")
    return round(value)


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("Product/evidence URLs must be HTTPS")
    return value


@dataclasses.dataclass(frozen=True)
class Offer:
    product_key: str
    title: str
    url: str
    seller_id: str
    price_toman: int
    shipping_toman: int | None
    variant_key: str
    warranty_key: str
    observed_at: str
    in_stock: bool
    category: str
    supermarket: bool
    discount_percent: float = 0
    expires_at: str | None = None
    product_id: int | None = None
    color_name: str = ""
    size_name: str = ""
    brand_name: str = ""

    @classmethod
    def parse(cls, row: dict) -> "Offer":
        required = ["product_key", "title", "url", "seller_id", "price_toman", "shipping_toman",
                    "variant_key", "warranty_key", "observed_at", "in_stock", "category", "supermarket"]
        if any(key not in row for key in required):
            raise ValueError("Missing offer fields: " + ", ".join(k for k in required if k not in row))
        for key in ["product_key", "seller_id", "variant_key", "warranty_key", "category", "title"]:
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
        if not isinstance(row["in_stock"], bool) or not isinstance(row["supermarket"], bool):
            raise ValueError("in_stock and supermarket must be boolean")
        timestamp(row["observed_at"])
        if row.get("expires_at"):
            timestamp(row["expires_at"])
        discount = row.get("discount_percent", 0)
        if isinstance(discount, bool) or not isinstance(discount, (int, float)) or not 0 <= discount <= 100:
            raise ValueError("discount_percent must be between 0 and 100")
        values = {f.name: row[f.name] for f in dataclasses.fields(cls) if f.name in row}
        values["url"] = safe_url(row["url"])
        values["price_toman"] = money(row["price_toman"])
        values["shipping_toman"] = None if row["shipping_toman"] is None else money(row["shipping_toman"])
        return cls(**values)


def load_config(path: pathlib.Path, override: float | None = None) -> dict:
    with path.open("rb") as stream:
        c = tomllib.load(stream)
    c.setdefault("interval_hours", 6)
    if override is not None:
        c["interval_hours"] = override
    for key, default in [("max_items", 20), ("min_saving_percent", 15), ("min_advertised_discount", 20),
                         ("min_market_sellers", 2), ("max_evidence_age_hours", 6),
                         ("notification_cooldown_hours", 24)]:
        c.setdefault(key, default)
    for key in ["interval_hours", "max_evidence_age_hours", "notification_cooldown_hours"]:
        value = c[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a finite number greater than zero")
    for key, low, high in [("min_market_sellers", 2, 100), ("max_items", 1, 100),
                            ("min_saving_percent", 0, 100), ("min_advertised_discount", 0, 100)]:
        value = c[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
    if not isinstance(c["min_market_sellers"], int) or not isinstance(c["max_items"], int):
        raise ValueError("max_items and min_market_sellers must be integers")
    c["_base"] = path.resolve().parent
    return c


def fresh(offer: Offer, now: dt.datetime, hours: float) -> bool:
    age = (now - timestamp(offer.observed_at)).total_seconds()
    return 0 <= age <= hours * 3600 and (not offer.expires_at or timestamp(offer.expires_at) > now)


def identity(offer: Offer):
    return offer.product_key, offer.variant_key, offer.warranty_key


def evaluate(candidate: Offer, market: list[Offer], config: dict, now: dt.datetime) -> dict:
    result = {"candidate": dataclasses.asdict(candidate), "status": "unverified", "reasons": [], "evidence": []}
    def reject(reason):
        result["reasons"].append(reason)
        return result
    if candidate.supermarket:
        result["status"] = "excluded"
        return reject("supermarket")
    if not candidate.in_stock or not fresh(candidate, now, config["max_evidence_age_hours"]):
        return reject("candidate_unavailable_or_stale")
    if candidate.discount_percent < config["min_advertised_discount"]:
        return reject("advertised_discount_below_threshold")
    if candidate.warranty_key.strip().casefold() in {"نامشخص", "unknown"}:
        return reject("unknown_warranty")
    # One seller operating multiple domains still counts as one independent seller.
    by_seller = {}
    candidate_host = urlsplit(candidate.url).hostname
    for quote in market:
        if identity(quote) != identity(candidate) or quote.supermarket or not quote.in_stock:
            continue
        if quote.seller_id == candidate.seller_id or urlsplit(quote.url).hostname == candidate_host:
            continue
        if not fresh(quote, now, config["max_evidence_age_hours"]):
            continue
        old = by_seller.get(quote.seller_id)
        if old is None or timestamp(quote.observed_at) > timestamp(old.observed_at):
            by_seller[quote.seller_id] = quote
    quotes = list(by_seller.values())
    result["evidence"] = [dataclasses.asdict(q) for q in quotes]
    hosts = {urlsplit(q.url).hostname.removeprefix("www.") for q in quotes}
    if min(len(quotes), len(hosts)) < config["min_market_sellers"]:
        return reject("insufficient_independent_sellers")
    # Never assume missing shipping is zero. Unknown shipping switches ALL prices
    # to an explicitly labelled item-only comparison.
    delivered = candidate.shipping_toman is not None and all(q.shipping_toman is not None for q in quotes)
    def cost(q):
        return q.price_toman + (q.shipping_toman if delivered else 0)
    market_low = min(cost(q) for q in quotes)
    if market_low <= 0:
        return reject("invalid_market_price")
    saving = market_low - cost(candidate)
    percent = saving / market_low * 100
    result.update(market_low_toman=market_low, saving_toman=saving, saving_percent=round(percent, 2),
                  comparison_basis="delivered" if delivered else "item_only")
    if saving <= 0 or percent < config["min_saving_percent"]:
        result["status"] = "rejected"
        return reject("market_saving_below_threshold")
    result["status"] = "verified"
    return result


class History:
    def __init__(self, path):
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS prices (key TEXT, seen TEXT, price INTEGER, PRIMARY KEY(key, seen))")
        self.db.execute("CREATE TABLE IF NOT EXISTS notices (key TEXT PRIMARY KEY, seen TEXT)")

    def record(self, offer: Offer):
        key = json.dumps([*identity(offer), offer.seller_id], ensure_ascii=False)
        cutoff = (timestamp(offer.observed_at) - dt.timedelta(days=30)).isoformat()
        previous = self.db.execute("SELECT MIN(price) FROM prices WHERE key=? AND seen>=? AND seen<?",
                                   (key, cutoff, offer.observed_at)).fetchone()[0]
        self.db.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?)", (key, offer.observed_at, offer.price_toman))
        self.db.commit()
        return previous

    def notice_key(self, row):
        c = row["candidate"]
        raw = json.dumps([c["product_key"], c["variant_key"], c["warranty_key"], c["price_toman"]])
        return hashlib.sha256(raw.encode()).hexdigest()

    def due(self, row, now, hours):
        seen = self.db.execute("SELECT seen FROM notices WHERE key=?", (self.notice_key(row),)).fetchone()
        return seen is None or (now - timestamp(seen[0])).total_seconds() >= hours * 3600

    def mark(self, row, now):
        self.db.execute("INSERT OR REPLACE INTO notices VALUES (?,?)", (self.notice_key(row), now.isoformat()))
        self.db.commit()

    def close(self):
        self.db.close()
