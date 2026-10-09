import datetime as dt
import html
import json
import pathlib
from zoneinfo import ZoneInfo

REASONS = {
    "unknown_warranty": "گارانتی نامشخص است",
    "supermarket": "کالای سوپرمارکتی",
    "candidate_unavailable_or_stale": "قیمت قدیمی، ناموجود یا پیشنهاد منقضی",
    "advertised_discount_below_threshold": "درصد تخفیف درج‌شده کمتر از حد تنظیم‌شده",
    "insufficient_independent_sellers": "کمتر از دو فروشندهٔ مستقل با مدل و گارانتی یکسان",
    "invalid_market_price": "قیمت بازار نامعتبر",
    "market_saving_below_threshold": "صرفه‌جویی نسبت به بازار کمتر از حد تنظیم‌شده",
}


def fa_money(value):
    return f"{value:,}".translate(str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬"))


def write_reports(report, destination, timezone):
    destination = pathlib.Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    e = lambda value: html.escape(str(value), quote=True)
    when = dt.datetime.fromisoformat(report["generated_at"]).astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
    rows = report["deals"]
    advertised = report.get("advertised_offers", [])
    heading = "گزارش تخفیف‌های دیجی‌کالا" if advertised else "گزارش تخفیف واقعی"
    cards = []
    markdown = ["# " + heading + (" — دادهٔ نمایشی" if report["demo"] else ""),
                f"زمان بررسی: {when} ({timezone})", f"فاصلهٔ بررسی: {report['interval_hours']} ساعت", ""]
    for row in rows:
        c = row["candidate"]
        basis = "قیمت با ارسال" if row["comparison_basis"] == "delivered" else "قیمت کالا؛ هزینهٔ ارسال نامشخص و محاسبه نشده"
        evidence = "".join(f'<li><a href="{e(q["url"])}" rel="noopener noreferrer">{e(q["seller_id"])}</a> · {fa_money(q["price_toman"])} تومان · {e(q["observed_at"])}</li>' for q in row["evidence"])
        low = row.get("previous_30d_low_toman")
        history = "تاریخچه کافی جمع‌آوری نشده" if low is None else f"کمترین قیمت ثبت‌شدهٔ قبلی در ۳۰ روز: {fa_money(low)} تومان"
        cards.append(f'''<article><p class="saving">{row['saving_percent']:.1f}٪ زیر کمترین قیمت معتبر پیدا‌شده</p>
<h2><a href="{e(c['url'])}" rel="noopener noreferrer">{e(c['title'])}</a></h2>
<p class="price">{fa_money(c['price_toman'])} <small>تومان</small></p>
<p>قیمت مقایسه: {fa_money(row['market_low_toman'])} · صرفه‌جویی: {fa_money(row['saving_toman'])} تومان</p>
<p>{e(basis)}</p><p>فروشنده: {e(c['seller_id'])} · گارانتی: {e(c['warranty_key'])}</p>
<p>نسخه/رنگ: {e(c['variant_key'])}</p><p>{e(history)}</p>
<details><summary>شواهد قیمت ({len(row['evidence'])} فروشنده)</summary><ul>{evidence}</ul>
<p>شناسهٔ محصول: {e(c['product_key'])} · تخفیف درج‌شده: {c['discount_percent']}٪</p></details></article>''')
        markdown.extend([f"## {c['title']}", f"قیمت: {fa_money(c['price_toman'])} تومان · صرفه‌جویی بازار: {row['saving_percent']}٪",
                         basis, f"خرید: {c['url']}", f"گارانتی: {c['warranty_key']}", history])
        markdown.extend(f"- {q['seller_id']}: {fa_money(q['price_toman'])} تومان — {q['url']} — {q['observed_at']}" for q in row["evidence"])
        markdown.append("")
    empty = '<p>پیشنهاد واجد شرایط پیدا نشد. منابع قیمت یا حداقل صرفه‌جویی را در config.toml بررسی کنید.</p>'
    if advertised:
        cards.append('<h2>تخفیف اعلامی دیجی‌کالا — کمترین قیمت بازار تأیید نشده</h2>')
        markdown.extend(['## تخفیف اعلامی دیجی‌کالا', 'کمترین قیمت بازار برای این موارد تأیید نشده است؛ درصدها اعلام دیجی‌کالا هستند.', ''])
        for row in advertised:
            c = row['candidate']
            cards.append(f'<article><h2><a href="{e(c["url"])}" rel="noopener noreferrer">{e(c["title"])}</a></h2><p class="price">{fa_money(c["price_toman"])} تومان · تخفیف اعلامی {c["discount_percent"]:g}٪</p><p>⚠️ کمترین قیمت بازار تأیید نشده</p><p>رنگ: {e(c.get("color_name") or "بدون رنگ مشخص")} · سایز: {e(c.get("size_name") or "بدون سایز مشخص")} · گارانتی: {e(c["warranty_key"])}</p></article>')
            markdown.extend([f'### {c["title"]}', f'قیمت: {fa_money(c["price_toman"])} تومان · تخفیف اعلامی: {c["discount_percent"]:g}٪', f'خرید: {c["url"]}', '⚠️ کمترین قیمت بازار تأیید نشده', ''])
    audit = "".join(f"<tr><td>{e(r['candidate']['title'])}</td><td>{e('؛ '.join(REASONS.get(reason, reason) for reason in r['reasons']))}</td></tr>" for r in report["audit"])
    errors = "".join(f"<li>{e(error)}</li>" for error in report["errors"])
    coverage = ""
    if report.get("market_coverage"):
        coverage = f'<p>جست‌وجوی بازار برای {report["market_coverage"]["checked"]} کالا · <a href="market-discovery.json">جزئیات کشف بازار و دلایل رد فروشنده‌ها</a></p>'
    banner = '<p class="warning">این گزارش با دادهٔ ساختگی برای نمایش ساخته شده و پیشنهاد خرید نیست.</p>' if report["demo"] else ""
    page = f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>گزارش تخفیف واقعی</title>
<style>body{{margin:0;background:#f8fafc;color:#0f172a;font-family:Tahoma,Arial,sans-serif;line-height:1.9}}main{{max-width:960px;margin:auto;padding:32px 20px}}h1,h2{{text-wrap:balance}}h1{{font-size:28px}}h2{{font-size:19px}}p{{text-wrap:pretty}}a{{color:#047857}}a:focus-visible,summary:focus-visible{{outline:3px solid #047857;outline-offset:4px}}article{{background:white;border:1px solid #e2e8f0;border-radius:8px;padding:24px;margin:20px 0}}.price{{font-size:26px;font-weight:bold;font-variant-numeric:tabular-nums}}small{{font-size:14px}}.saving{{color:#047857;font-weight:bold}}.warning{{background:#fef3c7;padding:16px;color:#78350f}}summary{{cursor:pointer;padding:8px 0}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #e2e8f0;text-align:right;padding:12px}}.table-wrap{{overflow:auto}}footer{{margin-top:30px;color:#475569}}@media(max-width:600px){{main{{padding:20px 12px}}article{{padding:16px}}}}</style></head><body><main>
{banner}<h1>{e(heading)}</h1><p>بررسی: {e(when)} ({e(timezone)}) · فاصلهٔ بررسی: {report['interval_hours']} ساعت</p>
<p>{len(rows)} پیشنهاد تأییدشده و {len(advertised)} تخفیف اعلامی از {report['candidate_count']} کالا · سقف گزارش {report['max_items']} کالا</p>
{coverage}{''.join(cards) or empty}<details><summary>موارد تأییدنشده و حذف‌شده</summary><div class="table-wrap"><table><thead><tr><th>کالا</th><th>دلیل</th></tr></thead><tbody>{audit}</tbody></table></div></details>
<details {'open' if errors else ''}><summary>وضعیت دریافت داده ({len(report['errors'])} خطا)</summary><ul>{errors}</ul></details>
<footer>قیمت‌ها تضمین ارزان‌ترین بودن در کل بازار نیستند. مدل و گارانتی فقط بین منابع پیکربندی‌شده مقایسه شده‌اند. پیش از خرید، قیمت سبد و موجودی را بررسی کنید.</footer></main></body></html>'''
    payloads = {"latest.json": json.dumps(report, ensure_ascii=False, indent=2), "index.html": page, "latest.md": "\n".join(markdown)}
    for name, content in payloads.items():
        temporary = destination / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination / name)
