"""GitHub Actions one-shot runner; never prints secret-valued configuration."""
import os
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
    # Fail visibly on missing data or unconfirmed Telegram delivery. Reports and
    # history are saved by the always() workflow steps regardless of this exit.
    if any(error.startswith("Telegram") for error in row["errors"]):
        raise SystemExit("Telegram delivery failed; inspect report status")
    if row["errors"] and not row["candidate_count"]:
        raise SystemExit("Data collection failed; inspect report status")


if __name__ == "__main__":
    main()
