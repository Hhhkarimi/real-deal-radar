import argparse
import datetime as dt
import logging
import pathlib
import signal
import threading
import time

from .core import History, UTC, evaluate, identity, load_config, advertised_shortlist
from .report import write_reports
from .sources import collect, dk_details
from .telegram import digest, publish

LOG = logging.getLogger("dealradar")


def run_once(config, *, demo=False, send=True):
    now = dt.datetime.now(UTC)
    candidates, market, errors = collect(config, now)
    # Fabricated fixtures must remain labelled and must never be published,
    # even when somebody runs the demo config with `run` or `preview`.
    demo = demo or any(o.product_key.startswith("demo:") for o in candidates + market)
    history = History(config["_base"] / config.get("database", "data/history.sqlite3"))
    try:
        results = []
        for candidate in candidates:
            row = evaluate(candidate, market, config, now)
            if row["status"] == "verified" and not demo:
                try:
                    if candidate.product_id:
                        renewed = dk_details(candidate.product_id, dt.datetime.now(UTC))
                        if identity(renewed) != identity(candidate):
                            raise ValueError("Default variant changed during revalidation")
                    else:
                        # Re-read configured candidate feeds, rather than trusting the shortlist.
                        from .sources import json_feed
                        refreshed = []
                        for location in config.get("feeds", {}).get("candidates", []):
                            refreshed.extend(json_feed(location, config["_base"]))
                        matching = [o for o in refreshed if identity(o) == identity(candidate) and o.seller_id == candidate.seller_id]
                        if not matching:
                            raise ValueError("Candidate disappeared during revalidation")
                        renewed = max(matching, key=lambda o: o.observed_at)
                    candidate = renewed
                    # Fresh direct merchant evidence is checked again immediately
                    # before a successful shortlist can become a channel post.
                    from .market import equivalent_quote
                    from .sources import fetch_page
                    refreshed_market = []
                    for quote in market:
                        if identity(quote) == identity(candidate) and quote.seller_id.startswith("merchant:"):
                            try:
                                html, destination = fetch_page(quote.url, attempts=1)
                                from urllib.parse import urlsplit
                                if urlsplit(destination).hostname.removeprefix("www.") != quote.seller_id.removeprefix("merchant:"):
                                    raise ValueError("Merchant domain changed")
                                quote = equivalent_quote(candidate, html, destination, quote.seller_id, dt.datetime.now(UTC))
                            except Exception:
                                errors.append("Merchant revalidation failed: " + quote.seller_id)
                                continue
                        refreshed_market.append(quote)
                    market = refreshed_market
                    row = evaluate(candidate, market, config, dt.datetime.now(UTC))
                except Exception:
                    row["status"] = "unverified"
                    row["reasons"] = ["candidate_unavailable_or_stale"]
                    errors.append("Candidate revalidation failed: " + candidate.product_key)
            row["previous_30d_low_toman"] = history.record(candidate)
            results.append(row)
        verified = sorted((r for r in results if r["status"] == "verified"), key=lambda r: (-r["saving_percent"], -r["saving_toman"]))
        deals = verified[:config["max_items"]]
        advertised = advertised_shortlist(results, config, dt.datetime.now(UTC), config["max_items"] - len(deals))
        report = dict(generated_at=dt.datetime.now(UTC).isoformat(), interval_hours=config["interval_hours"],
                      max_items=config["max_items"], candidate_count=len(candidates), demo=demo,
                      deals=deals, audit=[r for r in results if r["status"] != "verified"],
                      advertised_offers=advertised,
                      verified_not_shown=max(0, len(verified) - len(deals)), errors=errors)
        if config.get("_market_coverage"):
            report["market_coverage"] = config["_market_coverage"]
        if not demo and send:
            report["errors"].extend(publish(report, history, config))
        else:
            report["telegram_delivery"] = {"status": "demo" if demo else "preview"}
        write_reports(report, config["_base"] / config.get("output_dir", "output"), config.get("timezone", "Asia/Tehran"))
        destination = config["_base"] / config.get("output_dir", "output")
        post = digest(report, config)
        (destination / "channel-post.html").write_text(
            '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>پیش‌نمایش پست کانال</title><body style="font-family:Tahoma;max-width:700px;margin:32px auto;padding:16px;white-space:pre-wrap;line-height:1.8">' + post + '</body></html>', encoding="utf-8")
        LOG.info("%d candidates; %d verified deals; %d source/delivery errors", len(candidates), len(deals), len(errors))
        return report
    finally:
        history.close()


def worker(path, override=None, stop=None, clock=time.monotonic, runner=run_once):
    stop = stop or threading.Event()
    last_start = None
    while not stop.is_set():
        try:
            config = load_config(path, override)
            now = clock()
            if last_start is None or now - last_start >= config["interval_hours"] * 3600:
                last_start = now
                runner(config)
        except Exception as error:
            LOG.error("Worker cycle failed: %s", type(error).__name__)
        # Reload every 10 seconds; shortening/lengthening the interval takes effect
        # relative to the previous run's START, without restarting the worker.
        stop.wait(10)


def main():
    parser = argparse.ArgumentParser(description="Persian evidence-backed deal reports")
    parser.add_argument("command", choices=["run", "watch", "demo", "check", "preview"])
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("config.toml"))
    parser.add_argument("--interval-hours", type=float, help="Override interval; supports fractional hours")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config, args.interval_hours)
        if args.command == "check":
            from zoneinfo import ZoneInfo
            ZoneInfo(config.get("timezone", "Asia/Tehran"))
            print(f"Config valid. Interval: {config['interval_hours']} hours")
        elif args.command == "watch":
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            worker(args.config, args.interval_hours, stop)
        else:
            report = run_once(config, demo=args.command == "demo", send=args.command != "preview")
            if report["errors"] and not report["candidate_count"]:
                raise SystemExit(2)
    except (ValueError, FileNotFoundError, KeyError, TypeError) as error:
        parser.exit(2, f"Configuration/data error: {error}\n")


if __name__ == "__main__":
    main()
