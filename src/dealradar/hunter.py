"""Stateful deal hunting: verify, recheck, select, then remember confirmed delivery."""
import dataclasses
import datetime as dt
import hashlib
import math
import os
from collections import Counter
from urllib.parse import urlsplit

from .core import Offer, UTC, evaluate, fresh, identity, safe_url, timestamp
from .pricing import enrich_prices, resolve_comparison_urls
from .report import write_reports
from .sources import collect, dk_details, json_feed


DEFAULTS = dict(
    enabled=False, digest_hours=12, digest_items=3, flash_items=2,
    flash_daily_limit=4, max_per_category=1, repeat_days=7,
    repeat_price_drop_percent=10, repeat_min_hours=6,
    digest_saving_percent=15, digest_saving_toman=100_000,
    flash_saving_percent=25, flash_saving_toman=250_000,
    min_comparable_shops=3, max_market_age_minutes=20,
)


def settings(config):
    values = dict(DEFAULTS, **config.get("agent", {}))
    if not isinstance(values["enabled"], bool):
        raise ValueError("agent.enabled must be boolean")
    integer_ranges = dict(digest_items=(1, 5), flash_items=(1, 5), flash_daily_limit=(1, 20),
                          max_per_category=(1, 5), min_comparable_shops=(3, 100))
    ranges = dict(digest_hours=(1, 168), repeat_days=(1, 90), repeat_min_hours=(1, 168),
                  repeat_price_drop_percent=(1, 100), digest_saving_percent=(1, 99),
                  flash_saving_percent=(1, 99), digest_saving_toman=(1, 10**12),
                  flash_saving_toman=(1, 10**12), max_market_age_minutes=(1, 60), **integer_ranges)
    for key, (low, high) in ranges.items():
        value = values[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high
                or key in integer_ranges and not isinstance(value, int)):
            raise ValueError(f"agent.{key} must be within {low}..{high}")
    for suffix in ("percent", "toman"):
        if values[f"flash_saving_{suffix}"] < values[f"digest_saving_{suffix}"]:
            raise ValueError("Flash thresholds must be at least as strict as digest thresholds")
    return values


def channel_key(config):
    channel = os.environ.get(config.get("telegram", {}).get("chat_id_env", "TELEGRAM_CHAT_ID"), "preview")
    return hashlib.sha256(channel.encode()).hexdigest()


class HuntMemory:
    def __init__(self, history, channel):
        self.db, self.channel = history.db, channel
        self.db.execute("CREATE TABLE IF NOT EXISTS hunt_checks (key TEXT PRIMARY KEY, checked TEXT, price INTEGER)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS hunt_deliveries (
            channel TEXT, product TEXT, price INTEGER, sent_at TEXT, kind TEXT, message_id INTEGER)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS hunt_attempts (
            channel TEXT, product TEXT, attempted_at TEXT, kind TEXT, PRIMARY KEY(channel,product))""")
        self.db.commit()

    def prioritize(self, rows):
        # Rotate the market lookup budget; an unchanged first page cannot starve
        # the rest of the catalog on every poll.
        checked = {key: (when, price) for key, when, price in self.db.execute("SELECT key, checked, price FROM hunt_checks")}
        def rank(row):
            c = row["candidate"]
            last = checked.get(c["product_key"])
            # New products and actual price drops get the next lookup first.
            return (0 if last is None or c["price_toman"] < last[1] else 1,
                    last[0] if last else "", -c.get("discount_percent", 0))
        return sorted(rows, key=rank)

    def checked(self, rows, now):
        for row in rows:
            if row.get("price_comparison", {}).get("reason") not in (None, "not_checked"):
                self.db.execute("INSERT OR REPLACE INTO hunt_checks VALUES (?,?,?)",
                                (row["candidate"]["product_key"], now.isoformat(), row["candidate"]["price_toman"]))
        self.db.commit()

    def due(self, row, now, cfg):
        candidate = row["candidate"]
        pending = self.db.execute("SELECT attempted_at FROM hunt_attempts WHERE channel=? AND product=?",
                                  (self.channel, candidate["product_key"])).fetchone()
        # A timeout may have reached Telegram. Quarantine that product rather
        # than blindly posting it again on the next scan.
        if pending and (now - timestamp(pending[0])).total_seconds() < cfg["repeat_days"] * 86400:
            return False
        last = self.db.execute("""SELECT price, sent_at FROM hunt_deliveries
            WHERE channel=? AND product=? ORDER BY sent_at DESC LIMIT 1""",
                               (self.channel, candidate["product_key"])).fetchone()
        if last is None and self.db.execute("SELECT 1 FROM sqlite_master WHERE name='showcase_items'").fetchone():
            from .showcase import item_key
            last = self.db.execute("SELECT price, sent_at FROM showcase_items WHERE channel=? AND item=?",
                                   (self.channel, item_key(candidate))).fetchone()
        if last is None:
            return True
        old_price, sent_at = last
        elapsed = (now - timestamp(sent_at)).total_seconds()
        improved = candidate["price_toman"] <= old_price * (1 - cfg["repeat_price_drop_percent"] / 100)
        return elapsed >= cfg["repeat_days"] * 86400 or (
            row["hunt_tier"] == "flash" and improved and elapsed >= cfg["repeat_min_hours"] * 3600)

    def digest_due(self, now, hours):
        last = self.db.execute("SELECT MAX(sent_at) FROM hunt_deliveries WHERE channel=? AND kind='digest'",
                               (self.channel,)).fetchone()[0]
        pending = self.db.execute("SELECT MAX(attempted_at) FROM hunt_attempts WHERE channel=? AND kind='digest'",
                                  (self.channel,)).fetchone()[0]
        last = max(filter(None, [last, pending]), default=None)
        return last is None or (now - timestamp(last)).total_seconds() >= hours * 3600

    def flash_count(self, now, timezone):
        from zoneinfo import ZoneInfo
        today = now.astimezone(ZoneInfo(timezone)).date()
        confirmed = self.db.execute("SELECT sent_at FROM hunt_deliveries WHERE channel=? AND kind='flash'", (self.channel,)).fetchall()
        pending = self.db.execute("SELECT attempted_at FROM hunt_attempts WHERE channel=? AND kind='flash'", (self.channel,)).fetchall()
        return sum(timestamp(row[0]).astimezone(ZoneInfo(timezone)).date() == today for row in confirmed + pending)

    def start(self, rows, now, kind):
        self.db.executemany("INSERT OR REPLACE INTO hunt_attempts VALUES (?,?,?,?)",
                            [(self.channel, r["candidate"]["product_key"], now.isoformat(), kind) for r in rows])
        self.db.commit()

    def clear_attempt(self, rows):
        self.db.executemany("DELETE FROM hunt_attempts WHERE channel=? AND product=?",
                            [(self.channel, r["candidate"]["product_key"]) for r in rows])
        self.db.commit()

    def confirm(self, rows, now, kind, message_id):
        with self.db:
            for row in rows:
                c = row["candidate"]
                self.db.execute("INSERT INTO hunt_deliveries VALUES (?,?,?,?,?,?)",
                                (self.channel, c["product_key"], c["price_toman"], now.isoformat(), kind, message_id))
                self.db.execute("DELETE FROM hunt_attempts WHERE channel=? AND product=?",
                                (self.channel, c["product_key"]))


def qualify(row, config, now):
    """Return a report-compatible verified deal, or a reason for staying quiet."""
    cfg = settings(config)
    candidate = Offer.parse(row["candidate"])
    if (candidate.supermarket or not candidate.in_stock or candidate.price_toman <= 0
            or not fresh(candidate, now, cfg["max_market_age_minutes"] / 60)):
        return None, "candidate_unavailable_or_stale"
    if candidate.warranty_key.strip().casefold() in {"unknown", "نامشخص"}:
        return None, "unknown_warranty"
    comparison = row.get("price_comparison", {})
    quotes = []
    if comparison:
        if comparison.get("confidence") != "high":
            return None, "market_comparison_unconfirmed"
        try:
            age = (now - timestamp(comparison["fetched_at"])).total_seconds()
            if not 0 <= age <= cfg["max_market_age_minutes"] * 60:
                raise ValueError("stale")
            for quote in comparison.get("evidence", []):
                quotes.append(Offer.parse(dict(
                    row["candidate"], seller_id="torob:" + str(quote["shop_id"]),
                    url=safe_url(quote["url"]), price_toman=quote["price_toman"], shipping_toman=None,
                    observed_at=comparison["fetched_at"], discount_percent=0,
                )))
        except (KeyError, TypeError, ValueError):
            return None, "invalid_market_evidence"
    else:
        try:
            quotes = [Offer.parse(q) for q in row.get("evidence", [])]
        except (KeyError, TypeError, ValueError):
            return None, "invalid_market_evidence"
    own_host = urlsplit(candidate.url).hostname.removeprefix("www.")
    quotes = [q for q in quotes if urlsplit(q.url).hostname.removeprefix("www.") != own_host]
    policy = dict(config, min_advertised_discount=0, min_saving_percent=cfg["digest_saving_percent"],
                  min_market_sellers=cfg["min_comparable_shops"],
                  max_evidence_age_hours=cfg["max_market_age_minutes"] / 60)
    verified = evaluate(candidate, quotes, policy, now)
    if verified["status"] != "verified":
        return None, ",".join(verified["reasons"])
    if verified["saving_toman"] < cfg["digest_saving_toman"]:
        return None, "saving_amount_too_small"
    flash = (verified["saving_toman"] / verified["market_low_toman"] * 100 >= cfg["flash_saving_percent"]
             and verified["saving_toman"] >= cfg["flash_saving_toman"])
    verified.update(hunt_tier="flash" if flash else "digest",
                    previous_30d_low_toman=row.get("previous_30d_low_toman"))
    if comparison:
        verified["price_comparison"] = dict(comparison, **{key: verified[key] for key in
            ("market_low_toman", "saving_toman", "saving_percent", "comparison_basis")})
    return verified, None


def select(rows, memory, config, now):
    cfg = settings(config)
    eligible = sorted((r for r in rows if memory.due(r, now, cfg)),
                      key=lambda r: (-r["saving_percent"], -r["saving_toman"]))
    flash_limit = min(cfg["flash_items"], max(0, cfg["flash_daily_limit"] -
                      memory.flash_count(now, config.get("timezone", "Asia/Tehran"))))
    selected, seen = {"flash": [], "digest": []}, set()
    for kind, limit in [("flash", flash_limit), ("digest", cfg["digest_items"])]:
        if kind == "digest" and not memory.digest_due(now, cfg["digest_hours"]):
            continue
        categories = Counter()
        for row in eligible:
            c = row["candidate"]
            if len(selected[kind]) >= limit:
                break
            if c["product_key"] in seen or (kind == "flash" and row["hunt_tier"] != "flash"):
                continue
            if categories[c["category"]] >= cfg["max_per_category"]:
                continue
            selected[kind].append(row)
            seen.add(c["product_key"])
            categories[c["category"]] += 1
    return selected


def recheck(row, config, now):
    candidate = Offer.parse(row["candidate"])
    if candidate.product_id:
        renewed = dk_details(candidate.product_id, now)
    else:
        offers = [o for location in config.get("feeds", {}).get("candidates", [])
                  for o in json_feed(location, config["_base"])]
        matches = [o for o in offers if identity(o) == identity(candidate) and o.seller_id == candidate.seller_id]
        if not matches:
            raise ValueError("Candidate disappeared")
        renewed = max(matches, key=lambda o: timestamp(o.observed_at))
    if identity(renewed) != identity(candidate):
        raise ValueError("Selected variant changed")
    refreshed = dict(row, candidate=dataclasses.asdict(renewed))
    if not row.get("price_comparison"):
        from .market import equivalent_quote
        from .sources import fetch_page
        evidence = []
        for quote in row.get("evidence", []):
            if quote["seller_id"].startswith("merchant:"):
                html, url = fetch_page(quote["url"], attempts=1)
                from urllib.parse import urlsplit
                if urlsplit(url).hostname.removeprefix("www.") != quote["seller_id"].removeprefix("merchant:"):
                    raise ValueError("Merchant domain changed")
                quote = dataclasses.asdict(equivalent_quote(renewed, html, url, quote["seller_id"], now))
            evidence.append(quote)
        refreshed["evidence"] = evidence
    return refreshed


def run_once(config, *, demo=False, send=True):
    from .core import History
    from .hunter_delivery import message, publish
    cfg = settings(config)
    now = dt.datetime.now(UTC)
    candidates, market, errors = collect(config, now)
    demo = demo or any(o.product_key.startswith("demo:") for o in candidates + market)
    history = History(config["_base"] / config.get("database", "data/history.sqlite3"))
    try:
        memory = HuntMemory(history, channel_key(config))
        policy = dict(config, min_advertised_discount=0, min_saving_percent=cfg["digest_saving_percent"],
                      min_market_sellers=cfg["min_comparable_shops"])
        rows = []
        for candidate in candidates:
            row = evaluate(candidate, market, policy, now)
            row["previous_30d_low_toman"] = record_price(history, candidate)
            rows.append(row)
        rows = memory.prioritize(rows)
        config.pop("_market_coverage", None)
        if not demo:
            enrichment = dict(config, min_advertised_discount=0, _hunt_order=True)
            enrich_prices([r for r in rows if r["status"] != "verified"], enrichment)
            if enrichment.get("_market_coverage"):
                config["_market_coverage"] = enrichment["_market_coverage"]
        resolve_comparison_urls(rows, cfg['min_comparable_shops'],
                                saving_percent=cfg['digest_saving_percent'], saving_toman=cfg['digest_saving_toman'])
        accepted, audit = [], []
        for row in rows:
            deal, reason = qualify(row, config, dt.datetime.now(UTC))
            if deal:
                accepted.append(deal)
            else:
                audit.append(dict(row, hunt_rejection=reason))
        memory.checked(rows, dt.datetime.now(UTC))
        # Keep a bounded observation window instead of growing every poll.
        cutoff = (now - dt.timedelta(days=30)).isoformat()
        history.db.execute("DELETE FROM prices WHERE seen<?", (cutoff,))
        history.db.commit()
        planned = select(accepted, memory, config, dt.datetime.now(UTC))
        # Only the bounded outgoing shortlist needs a second candidate fetch.
        checked = []
        for row in planned["flash"] + planned["digest"]:
            if demo:
                checked.append(row)
                continue
            try:
                renewed = recheck(row, config, dt.datetime.now(UTC))
                deal, reason = qualify(renewed, config, dt.datetime.now(UTC))
                if deal:
                    checked.append(deal)
                else:
                    audit.append(dict(renewed, hunt_rejection=reason))
            except Exception:
                errors.append("Candidate revalidation failed: " + row["candidate"]["product_key"])
        planned = select(checked, memory, config, dt.datetime.now(UTC))
        report = dict(generated_at=dt.datetime.now(UTC).isoformat(), interval_hours=config["interval_hours"],
                      max_items=cfg["flash_items"] + cfg["digest_items"], candidate_count=len(candidates),
                      demo=demo, agent=True, flash_deals=planned["flash"], digest_deals=planned["digest"],
                      deals=planned["flash"] + planned["digest"], audit=audit,
                      advertised_offers=[], bestsellers=[], bestsellers_enabled=False,
                      verified_not_shown=len(accepted) - len(checked), errors=errors)
        report['verification_summary'] = dict(
            eligible_candidates=sum(not c.supermarket and c.in_stock and c.price_toman > 0 for c in candidates),
            comparable_products=sum(r.get('price_comparison', {}).get('comparable_shop_count', 0) > 0 for r in rows),
            rejection_counts=dict(Counter(r['hunt_rejection'] for r in audit)),
            comparison_reasons=dict(Counter(r.get('price_comparison', {}).get('reason', 'direct_feed') for r in rows)))
        if config.get("_market_coverage"):
            report["market_coverage"] = config["_market_coverage"]
            if report["market_coverage"].get("provider_status") == "unavailable":
                errors.append("Torob MCP price source unavailable; no unverified fallback will be posted")
        if not demo and send:
            errors.extend(publish(report, memory, config))
        else:
            report["telegram_delivery"] = {"status": "demo" if demo else "preview"}
        destination = config["_base"] / config.get("output_dir", "output")
        write_reports(report, destination, config.get("timezone", "Asia/Tehran"))
        preview = "\n\n".join(message(planned[kind], kind) for kind in ("flash", "digest") if planned[kind])
        (destination / "channel-post.html").write_text(
            '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>شکار تخفیف</title>'
            '<body style="font-family:Tahoma;white-space:pre-wrap;line-height:1.8">' +
            (preview or "فرصت خرید تازه و تأییدشده‌ای پیدا نشد؛ پیامی ارسال نمی‌شود.") + '</body></html>', encoding="utf-8")
        return report
    finally:
        history.close()


def record_price(history, candidate):
    """Record changes and a daily baseline, avoiding hundreds of identical samples."""
    import json
    key = json.dumps([*identity(candidate), candidate.seller_id], ensure_ascii=False)
    last = history.db.execute("SELECT seen, price FROM prices WHERE key=? ORDER BY seen DESC LIMIT 1", (key,)).fetchone()
    if last is None or last[1] != candidate.price_toman or timestamp(last[0]).date() != timestamp(candidate.observed_at).date():
        return history.record(candidate)
    cutoff = (timestamp(candidate.observed_at) - dt.timedelta(days=30)).isoformat()
    return history.db.execute("SELECT MIN(price) FROM prices WHERE key=? AND seen>=? AND seen<?",
                              (key, cutoff, candidate.observed_at)).fetchone()[0]
