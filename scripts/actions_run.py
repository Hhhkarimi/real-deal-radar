"""GitHub Actions one-shot runner; never prints secret-valued configuration."""
import os
import json
import re
from collections import Counter
from pathlib import Path

from dealradar.cli import run_once
from dealradar.core import load_config, safe_url


def configure(config, env):
    dry = env.get("RADAR_DRY_RUN", "false") == "true"
    if not dry and not all(env.get(key) for key in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]):
        raise ValueError("Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in repository Actions secrets")
    config["database"] = "data/history.sqlite3"
    config["output_dir"] = "output"
    config.setdefault("telegram", {})["enabled"] = True
    manual_id = env.get("DEALRADAR_MANUAL_RUN_ID", "")
    if manual_id:
        if not re.fullmatch(r"[0-9]+-[0-9]+", manual_id):
            raise ValueError("Invalid manual workflow run identifier")
        config["telegram"]["manual_run_id"] = manual_id
    for env_key, kind in [("MARKET_FEED_URL", "market"), ("CANDIDATE_FEED_URL", "candidates")]:
        if env.get(env_key):
            config.setdefault("feeds", {})[kind] = [safe_url(env[env_key])]
            if kind == "market":
                config.setdefault("torob", {})["enabled"] = False
            if kind == "candidates":
                config.setdefault("digikala", {})["enabled"] = False
    return config, dry


def main():
    try:
        config, dry = configure(load_config(Path("config.toml")), os.environ)
    except ValueError:
        raise SystemExit("Configuration invalid: check repository secrets and HTTPS feed URLs") from None
    row = run_once(config, send=not dry)
    print(f"Generated report: {len(row['deals'])} verified deals; {len(row['errors'])} errors; preview={dry}")
    print(f"Advertised offers without market verification: {len(row.get('advertised_offers', []))}")
    print("Telegram delivery: " + json.dumps(row.get("telegram_delivery", {"status": "preview" if dry else "unknown"})))
    print("Market coverage: " + json.dumps(row.get("market_coverage", {}), ensure_ascii=False))
    source_failures = Counter()
    for error in row["errors"]:
        provider = next((p for p in ["Digikala", "Torob", "Telegram"] if error.startswith(p)), "other")
        status = re.search(r"HTTP (\d{3})", error)
        source_failures[provider + ("_http_" + status[1] if status else "_failed")] += 1
    print("Source failure counts: " + json.dumps(dict(source_failures)))
    # Fail visibly on missing data or unconfirmed Telegram delivery. Reports and
    # history are saved by the always() workflow steps regardless of this exit.
    if any(error.startswith("Telegram") for error in row["errors"]):
        raise SystemExit("Telegram delivery failed; inspect report status")
    if row["errors"] and not row["candidate_count"]:
        raise SystemExit("Data collection failed; inspect report status")


if __name__ == "__main__":
    main()
