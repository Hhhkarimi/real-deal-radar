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
    heading = f"🔎 <b>گزارش تخفیف واقعی</b>\n🕒 {now:%Y-%m-%d %H:%M} · {e(config.get('timezone', 'Asia/Tehran'))}\n"
    heading += f"بررسی هر {report['interval_hours']} ساعت · {report['candidate_count']} کالای بررسی‌شده\n"
    if report.get("market_coverage"):
        heading += f"جست‌وجوی بازار برای {report['market_coverage']['checked']} کالا\n"
    if report.get("demo"):
        heading += "⚠️ <b>دادهٔ ساختگی؛ پیشنهاد خرید نیست.</b>\n"
    footer = "\nقیمت‌ها نسبت به منابع بررسی‌شده‌اند؛ قیمت سبد، موجودی و گارانتی را پیش از خرید بررسی کنید."
    if any(row["comparison_basis"] == "item_only" for row in report["deals"]):
        footer += "\nهزینهٔ ارسال در مقایسه‌های علامت‌دار (*) محاسبه نشده است."
    if report.get("errors"):
        footer += "\n⚠️ دریافت بعضی منابع ناموفق بود؛ پوشش بازار کامل نیست."
    if report.get("market_coverage", {}).get("budget_exhausted"):
        footer += "\nبررسی بازار به سقف زمان این نوبت رسید."
    report_url = config.get("telegram", {}).get("report_url")
    if report_url:
        footer += f'\n<a href="{e(safe_url(report_url))}">گزارش کامل و شواهد قیمت</a>'
    footer += "\n#تخفیف_واقعی"
    blocks = []
    for index, row in enumerate(report["deals"], 1):
        c = row["candidate"]
        title = c["title"][:80] + ("…" if len(c["title"]) > 80 else "")
        marker = " *" if row["comparison_basis"] == "item_only" else ""
        block = f'\n{index}. <a href="{e(safe_url(c["url"]))}">{e(title)}</a>\n'
        price = c['price_toman'] + (c.get('shipping_toman') or 0) if row['comparison_basis'] == 'delivered' else c['price_toman']
        basis = " با ارسال" if row['comparison_basis'] == 'delivered' else ""
        block += f"💰 {fa_money(price)} تومان{basis} · {row['saving_percent']:.1f}٪ زیر بازار{marker}\n"
        block += f"قیمت مقایسه: {fa_money(row['market_low_toman'])} تومان\n"
        omitted = len(report["deals"]) - len(blocks) - 1
        remaining = f"\n{omitted} پیشنهاد دیگر در گزارش کامل.\n" if omitted else ""
        if visible_length(heading + "".join(blocks) + block + remaining + footer) > 3900:
            break
        blocks.append(block)
    if not report["deals"]:
        if report.get("errors") and not report["candidate_count"]:
            heading += "\n⚠️ دریافت داده ناموفق بود؛ امکان ارزیابی پیشنهادها وجود نداشت.\n"
        else:
            heading += "\nدر این نوبت پیشنهاد دارای شواهد کافی و صرفه‌جویی مطلوب پیدا نشد.\n"
    omitted = len(report["deals"]) - len(blocks)
    note = f"\n{omitted} پیشنهاد دیگر در گزارش کامل.\n" if omitted else ""
    return heading + "".join(blocks) + note + footer


def publish(report, history, config, *, opener=urllib.request.urlopen, sleep=time.sleep):
    settings = config.get("telegram", {})
    if not settings.get("enabled", False):
        return []
    token = os.environ.get(settings.get("token_env", "TELEGRAM_BOT_TOKEN"))
    channel = os.environ.get(settings.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    if not token or not channel:
        return ["Telegram channel post not sent: missing environment credentials"]
    now = dt.datetime.fromisoformat(report["generated_at"])
    slot = int(now.timestamp() // (config["interval_hours"] * 3600))
    # The same interval is not posted again after a worker/container restart.
    channel_key = hashlib.sha256(channel.encode()).hexdigest()
    key = f"{channel_key}:{config['interval_hours']}:{slot}"
    history.db.execute("CREATE TABLE IF NOT EXISTS channel_posts (key TEXT PRIMARY KEY, sent_at TEXT, message_id INTEGER)")
    history.db.commit()
    if history.db.execute("SELECT 1 FROM channel_posts WHERE key=?", (key,)).fetchone():
        return []
    last = history.db.execute("SELECT MAX(sent_at) FROM channel_posts WHERE key LIKE ?", (channel_key + ":%",)).fetchone()[0]
    slot_mode = os.environ.get("DEALRADAR_SCHEDULE_MODE") == "slots"
    if not slot_mode and last and (now - dt.datetime.fromisoformat(last)).total_seconds() < config["interval_hours"] * 3600:
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
                return ["Telegram did not confirm channel delivery; no successful post recorded"]
            history.db.execute("INSERT INTO channel_posts VALUES (?,?,?)", (key, now.isoformat(), answer["result"]["message_id"]))
            history.db.commit()
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
    return ["Telegram channel delivery failed or was not confirmed; check channel permissions and worker logs"]
