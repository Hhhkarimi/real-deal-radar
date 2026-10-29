"""Bounded Telegram messages with per-product delivery receipts."""
import datetime as dt
import html
import json
import os
import time
import urllib.error
import urllib.request

from .core import UTC, safe_url
from .report import fa_money


def message(rows, kind):
    if not rows:
        return ""
    title = "🚨 شکار ویژهٔ خرید" if kind == "flash" else "🎯 گلچین فرصت‌های خرید"
    blocks = ["<b>" + title + "</b>"]
    e = lambda value: html.escape(str(value), quote=True)
    for row in rows:
        c = row["candidate"]
        details = " · ".join(v for v in [c.get("color_name"), c.get("size_name"), c["warranty_key"]] if v)
        blocks.append(
            f'<a href="{e(safe_url(c["url"]))}">{e(c["title"][:100])}</a>\n'
            f'💰 {fa_money(c["price_toman"])} تومان\n'
            f'کمترین قیمت همسانِ بررسی‌شده: {fa_money(row["market_low_toman"])} تومان\n'
            f'صرفه‌جویی: {fa_money(row["saving_toman"])} تومان ({row["saving_percent"]:g}٪)\n'
            f'{e(details[:100])} · {len(row["evidence"])} فروشگاه مستقل')
    blocks.append("مقایسه محدود به منابع بررسی‌شده است؛ ادعای ارزان‌ترین قیمت کل بازار نیست.")
    if any(r["comparison_basis"] == "item_only" for r in rows):
        blocks.append("هزینهٔ ارسال در مقایسه محاسبه نشده است.")
    blocks.append("موجودی، فروشنده، مشخصات و مبلغ نهایی سبد را پیش از خرید بررسی کنید.")
    return "\n\n".join(blocks)


def publish(report, memory, config, *, opener=urllib.request.urlopen, sleep=time.sleep):
    delivery = report["telegram_delivery"] = dict(status="skipped", reason="no_new_deals", sent=0)
    batches = [("flash", [row]) for row in report["flash_deals"]]
    if report["digest_deals"]:
        batches.append(("digest", report["digest_deals"]))
    if not batches:
        return []
    cfg = config.get("telegram", {})
    if not cfg.get("enabled", False):
        delivery.update(status="disabled", reason="telegram_disabled")
        return []
    token = os.environ.get(cfg.get("token_env", "TELEGRAM_BOT_TOKEN"))
    channel = os.environ.get(cfg.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    if not token or not channel:
        delivery.update(status="failed", reason="missing_credentials")
        return ["Telegram channel post not sent: missing environment credentials"]
    errors = []
    for kind, rows in batches:
        text = message(rows, kind)
        from .telegram import visible_length
        if visible_length(text) > 4000:
            errors.append("Telegram message exceeds safe length; reduce agent.digest_items")
            continue
        payload = dict(chat_id=channel, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})
        # Persist before the HTTP request: even a worker crash after acceptance
        # must not immediately repeat the same product.
        memory.start(rows, dt.datetime.now(UTC), kind)
        for attempt in range(3):
            try:
                request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                with opener(request, timeout=25) as response:
                    answer = json.load(response)
                message_id = answer.get("result", {}).get("message_id")
                if not answer.get("ok") or not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                    if answer.get("ok") is False:
                        memory.clear_attempt(rows)
                    raise ValueError("Delivery unconfirmed")
                memory.confirm(rows, dt.datetime.now(UTC), kind, message_id)
                delivery["sent"] += len(rows)
                break
            except urllib.error.HTTPError as error:
                # An explicit rejection can be retried; a timeout cannot.
                memory.clear_attempt(rows)
                if error.code == 429 and attempt < 2:
                    try:
                        delay = json.load(error).get("parameters", {}).get("retry_after", 5)
                        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
                            raise ValueError("Invalid retry delay")
                        sleep(delay)
                        memory.start(rows, dt.datetime.now(UTC), kind)
                        continue
                    except (ValueError, TypeError):
                        pass
                errors.append("Telegram rejected a deal message")
                break
            except Exception:
                errors.append("Telegram deal delivery unconfirmed; inspect report before retrying")
                break
        if errors:
            # Keep the rest of the shortlist available for the next scan.
            break
    delivery.update(status="partial" if errors and delivery["sent"] else "failed" if errors else "sent")
    delivery.pop("reason", None)
    return errors
