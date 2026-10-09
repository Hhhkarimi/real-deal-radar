"""One evidence-backed digest per channel and interval, never one message per item."""
import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from .core import safe_url
from .report import fa_money


def visible_length(text):
    # Conservative UTF-16 count: supplementary emoji count as two characters.
    visible = html.unescape(re.sub(r"<[^>]*>", "", text))
    return len(visible.encode("utf-16-le")) // 2


def digest(report, config):
    e = lambda s: html.escape(str(s), quote=True)
    now = dt.datetime.fromisoformat(report["generated_at"]).astimezone(ZoneInfo(config.get("timezone", "Asia/Tehran")))
    advertised = report.get("advertised_offers", [])
    title = "گزارش تخفیف‌های دیجی‌کالا" if advertised else "گزارش تخفیف واقعی"
    heading = f"🔎 <b>{title}</b>\n🕒 {now:%Y-%m-%d %H:%M} · {e(config.get('timezone', 'Asia/Tehran'))}\n"
    heading += f"بررسی هر {report['interval_hours']} ساعت · {report['candidate_count']} کالای بررسی‌شده\n"
    if report.get("market_coverage"):
        heading += f"جست‌وجوی بازار برای {report['market_coverage']['checked']} کالا\n"
    if report.get("demo"):
        heading += "⚠️ <b>دادهٔ ساختگی؛ پیشنهاد خرید نیست.</b>\n"
    footer = "\nقیمت سبد، موجودی، رنگ و گارانتی را پیش از خرید بررسی کنید."
    if report["deals"]:
        footer += " قیمت بازار فقط نسبت به منابع بررسی‌شده است."
    if any(row["comparison_basis"] == "item_only" for row in report["deals"]):
        footer += "\nهزینهٔ ارسال در مقایسه‌های علامت‌دار (*) محاسبه نشده است."
    if advertised:
        footer += "\nدرصد تخفیف موارد ⚠️ اعلام دیجی‌کالاست؛ کمترین قیمت بازار تأیید نشده است."
    if report.get("errors"):
        footer += "\n⚠️ دریافت بعضی منابع ناموفق بود؛ پوشش بازار کامل نیست."
    if report.get("market_coverage", {}).get("provider_status") == "unavailable":
        footer += "\nبررسی بازار به علت عدم پاسخ‌گویی منبع متوقف شد."
    if report.get("market_coverage", {}).get("budget_exhausted"):
        footer += "\nبررسی بازار به سقف زمان این نوبت رسید."
    report_url = config.get("telegram", {}).get("report_url")
    if report_url:
        footer += f'\n<a href="{e(safe_url(report_url))}">گزارش کامل و شواهد قیمت</a>'
    footer += "\n#تخفیف"
    entries = [(row, True) for row in report["deals"]] + [(row, False) for row in advertised]
    if not entries:
        if report.get("errors") and not report["candidate_count"]:
            heading += "\n⚠️ دریافت داده ناموفق بود؛ امکان ارزیابی پیشنهادها وجود نداشت.\n"
        else:
            heading += "\nکالای موجود با تخفیف اعلامی واجد شرایط پیدا نشد.\n"
        return heading + footer
    # Keep up to 20 products in one post by shortening titles before dropping
    # entries. Full titles and variant details remain in the saved report.
    for title_limit in (72, 56, 40, 24):
        blocks = []
        for index, (row, verified) in enumerate(entries, 1):
            c = row["candidate"]
            name = c["title"][:title_limit] + ("…" if len(c["title"]) > title_limit else "")
            badge = "✅" if verified else "⚠️"
            block = f'\n{index}. {badge} <a href="{e(safe_url(c["url"]))}">{e(name)}</a>\n'
            block += f"💰 {fa_money(c['price_toman'])} تومان"
            if c.get("discount_percent") is not None:
                block += f" · تخفیف اعلامی {c['discount_percent']:g}٪"
            if verified:
                marker = " *" if row["comparison_basis"] == "item_only" else ""
                block += f"\n✅ {row['saving_percent']:.1f}٪ زیر قیمت منابع بازار{marker}"
            block += "\n"
            blocks.append(block)
        text = heading + "".join(blocks) + footer
        if visible_length(text) <= 3900:
            return text
    while blocks and visible_length(heading + "".join(blocks) + footer) > 3800:
        blocks.pop()
    omitted = len(entries) - len(blocks)
    note = f"\n{omitted} کالای دیگر در گزارش کامل.\n" if omitted else ""
    return heading + "".join(blocks) + note + footer


def publish(report, history, config, *, opener=urllib.request.urlopen, sleep=time.sleep):
    settings = config.get("telegram", {})
    def status(value, reason=None):
        report["telegram_delivery"] = {"status": value}
        if reason:
            report["telegram_delivery"]["reason"] = reason
    if not settings.get("enabled", False):
        status("disabled")
        return []
    token = os.environ.get(settings.get("token_env", "TELEGRAM_BOT_TOKEN"))
    channel = os.environ.get(settings.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    if not token or not channel:
        status("failed", "missing_credentials")
        return ["Telegram channel post not sent: missing environment credentials"]
    now = dt.datetime.fromisoformat(report["generated_at"])
    slot = int(now.timestamp() // (config["interval_hours"] * 3600))
    # The same interval is not posted again after a worker/container restart.
    channel_key = hashlib.sha256(channel.encode()).hexdigest()
    key = f"{channel_key}:{config['interval_hours']}:{slot}"
    manual_id = settings.get("manual_run_id")
    if manual_id:
        # Manual runs do not consume a scheduled slot. The run/attempt key still
        # prevents re-delivery when the same invocation is resumed.
        key = f"{channel_key}:manual:{manual_id}"
    history.db.execute("CREATE TABLE IF NOT EXISTS channel_posts (key TEXT PRIMARY KEY, sent_at TEXT, message_id INTEGER)")
    history.db.commit()
    if history.db.execute("SELECT 1 FROM channel_posts WHERE key=?", (key,)).fetchone():
        status("skipped", "manual_run_already_sent" if manual_id else "slot_already_sent")
        print("Telegram delivery: skipped; a report was already sent in this interval slot")
        return []
    last = history.db.execute("SELECT MAX(sent_at) FROM channel_posts WHERE key LIKE ? AND key NOT LIKE ?", (channel_key + ":%", channel_key + ":manual:%")).fetchone()[0]
    slot_mode = os.environ.get("DEALRADAR_SCHEDULE_MODE") == "slots"
    if not manual_id and not slot_mode and last and (now - dt.datetime.fromisoformat(last)).total_seconds() < config["interval_hours"] * 3600:
        status("skipped", "interval_not_elapsed")
        print("Telegram delivery: skipped; the posting interval has not elapsed")
        return []
    payload = json.dumps({"chat_id": channel, "text": digest(report, config), "parse_mode": "HTML",
                          "link_preview_options": {"is_disabled": True}}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
                                         headers={"Content-Type": "application/json"})
            with opener(req, timeout=25) as response:
                answer = json.load(response)
            if not answer.get("ok") or not answer.get("result", {}).get("message_id"):
                status("failed", "delivery_unconfirmed")
                return ["Telegram did not confirm channel delivery; no successful post recorded"]
            history.db.execute("INSERT INTO channel_posts VALUES (?,?,?)", (key, now.isoformat(), answer["result"]["message_id"]))
            history.db.commit()
            status("sent")
            print("Telegram delivery: sent and confirmed")
            return []
        except urllib.error.HTTPError as error:
            # Telegram explicitly rejected a 429 request, so bounded retry is safe.
            if error.code == 429 and attempt < 2:
                try:
                    delay = json.load(error).get("parameters", {}).get("retry_after", 5)
                    if not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
                        break
                    sleep(delay)
                    continue
                except (ValueError, TypeError):
                    break
            break
        except Exception:
            # A timeout may happen AFTER Telegram accepted a request. Do not
            # retry blindly within this run, and never expose token-bearing URLs.
            break
    status("failed", "delivery_unconfirmed")
    return ["Telegram channel delivery failed or was not confirmed; check channel permissions and worker logs"]
