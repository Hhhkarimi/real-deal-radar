"""A short summary followed by individual captioned product photos."""
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
from .showcase import item_key, mark_item


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


def comparison_caption(row):
    comparison=row.get('price_comparison')
    if comparison is None: return ''
    e=lambda value:html.escape(str(value),quote=True)
    count=comparison.get('comparable_shop_count',0)
    confidence=comparison.get('confidence','none')
    badge={'high':'🟢 تطبیق قوی در دادهٔ ترب','partial':'🟡 مقایسهٔ ناقص','none':'⚪ قیمت بازار تأیید نشده'}[confidence]
    text='\n'+badge
    if count:
        low,high=comparison['market_low_toman'],comparison['market_high_toman']
        text+=f'\nترب: {fa_money(low)} تا {fa_money(high)} تومان · {count} فروشگاه همسان'
        saving=comparison['saving_toman']; percent=abs(comparison['saving_percent'])
        relation='کمتر' if saving>0 else 'بیشتر' if saving<0 else 'برابر'
        text+=f'\nاختلاف با کمترین پیشنهاد همسان: {fa_money(abs(saving))} تومان ({percent:g}٪ {relation})؛ بدون ارسال'
    elif confidence=='partial':
        text+='\nمدل، نسخه، موجودی یا گارانتی برای مقایسهٔ همسان کافی نبود.'
        if comparison.get('listed_low_toman'):
            text+=f'\nقیمت‌های فهرست ترب: {fa_money(comparison["listed_low_toman"])} تا {fa_money(comparison["listed_high_toman"])} تومان؛ نسخهٔ همسان تأیید نشده'
    if comparison.get('fetched_at'):
        try:
            checked=dt.datetime.fromisoformat(comparison['fetched_at'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Tehran'))
            text+=f'\nزمان بررسی ترب: {checked:%m-%d %H:%M}'
        except (ValueError,TypeError): pass
    if comparison.get('loaded_list_complete') is False or comparison.get('truncated'):
        text+=' · پوشش محدود'
    if comparison.get('torob_url'):
        try: text+=f'\n<a href="{e(safe_url(comparison["torob_url"]))}">مقایسه در ترب</a>'
        except ValueError: pass
    return text


def compact_photo(row):
    c=row['candidate'];e=lambda value:html.escape(str(value),quote=True)
    title=' '.join(c['title'].split());title=title[:65]+('…' if len(title)>65 else '')
    icon='🏆' if row.get('showcase_section')=='bestseller' else '💸'
    lines=[f'{icon} <b>{e(title)}</b>',f'💰 {fa_money(c["price_toman"])} تومان · تخفیف اعلامی {c.get("discount_percent",0):g}٪']
    p=row.get('price_comparison',{});count=p.get('comparable_shop_count',0)
    confidence=p.get('confidence','none')
    if count:
        saving=p['saving_percent'];relation='کمتر' if saving>0 else 'بیشتر' if saving<0 else 'برابر'
        lines.append(f'📊 تربِ همسان: {fa_money(p["market_low_toman"])} تومان · {abs(saving):g}٪ {relation}')
    else:lines.append('🟡 قیمت همسان تأیید نشد' if confidence=='partial' else '⚪ قیمت بازار دریافت نشد')
    badge={'high':'🟢 تطبیق ترب','partial':'🟡 ناقص','none':'⚪ بدون تأیید'}.get(confidence,'⚪ بدون تأیید')
    links=f'<a href="{e(safe_url(c["url"]))}">خرید</a>'
    if p.get('torob_url'):
        try:links+=f' | <a href="{e(safe_url(p["torob_url"]))}">ترب</a>'
        except ValueError:pass
    lines.append(badge+' · '+links)
    return dict(type='photo',media=safe_url(c['image_url']) if c.get('image_url') else None,
                caption='\n'.join(lines),parse_mode='HTML',item_key=item_key(c),price_toman=c['price_toman'])


def photo_groups(report):
    """Each photo keeps its own product link, price and confidence caption."""
    photos = []
    if report.get('showcase'):
        photos=[compact_photo(row) for row in report['showcase_items']]
        return [photos[i:i+10] for i in range(0,len(photos),10)]
    entries = [(r, True) for r in report['deals']] + [(r, False) for r in report.get('advertised_offers', [])]
    discount_count = len(entries)
    entries += [(r, False) for r in report.get('bestsellers', [])]
    if report.get('grouped'):
        entries = [(r, r.get('status') == 'verified') for g in report['discount_groups'] for r in g['items']]
        entries += [(r, False) for g in report['bestseller_groups'] for r in g['items']]
    for index, (row, verified) in enumerate(entries, 1):
        c = row['candidate']
        e = lambda value: html.escape(str(value), quote=True)
        title_limit=140 if 'price_comparison' in row else 240
        title = c['title'][:title_limit] + ('…' if len(c['title']) > title_limit else '')
        caption = f'{index}. <a href="{e(safe_url(c["url"]))}">{e(title)}</a>\n💰 {fa_money(c["price_toman"])} تومان'
        bestseller = row.get('list_kind') == 'bestseller'
        if report.get('grouped'):
            label = '🏆' if bestseller else '💸'
            caption = f'{label} {e(row["report_category"])} · {row["category_position"]}\n' + caption.split('. ', 1)[1]
        elif bestseller:
            caption = f'🏆 پرفروش {index-discount_count}\n' + caption.split('. ', 1)[1]
        if c.get('discount_percent') is not None:
            caption += f' · تخفیف اعلامی {c["discount_percent"]:g}٪'
        if row.get('price_comparison') is not None:
            caption+=comparison_caption(row)
            reason=row.get('selection_reason','')[:140]
            if reason: caption+='\nدلیل انتخاب: '+e(reason)
            if bestseller: caption+='\nپرفروش بودن تضمین کمترین قیمت نیست.'
        elif bestseller:
            caption += f'\nجایگاه {row["source_rank"]} در فهرست {e(row["ranking_scope"])}'
            caption += '\n⚠️ پرفروش بودن به معنی کمترین قیمت بازار نیست.'
        elif verified:
            caption += f'\n✅ {row["saving_percent"]:.1f}٪ زیر قیمت منابع بررسی‌شده'
            if row['comparison_basis'] == 'item_only':
                caption += '؛ بدون هزینهٔ ارسال'
        else:
            caption += '\n⚠️ کمترین قیمت بازار تأیید نشده است.'
        photos.append(dict(type='photo', media=safe_url(c['image_url']) if c.get('image_url') else None, caption=caption, parse_mode='HTML',
                           category=row.get('report_category'), list_kind='bestseller' if bestseller else 'discount'))
    return [photos[i:i+10] for i in range(0, len(photos), 10)]


def publish_photos(report, token, channel, settings, *, opener, sleep, history=None):
    if not settings.get('photos_enabled', True):
        report['telegram_photos'] = dict(status='disabled', sent=0)
        return
    groups = [[photo] for group in photo_groups(report) for photo in group]
    planned = sum(map(len, groups))
    count = len(report['deals']) + len(report.get('advertised_offers', [])) + len(report.get('bestsellers', []))
    if report.get('showcase'):
        pass
    elif report.get('grouped'):
        plan, previous = [], None
        for group in groups:
            photo = group[0]
            key = (photo['list_kind'], photo['category'])
            if key != previous:
                section = '🏆 پرفروش‌ها' if key[0] == 'bestseller' else '💸 تخفیف‌ها'
                category = html.escape(str(key[1]), quote=True)
                criterion = 'جایگاه در فهرست پرفروش‌های دیجی‌کالا' if key[0] == 'bestseller' else ('صرفه‌جویی قابل مشاهده در پیشنهادهای همسان ترب؛ موارد بدون شاهد با برچسب جدا' if report.get('value_ranking') else 'درصد تخفیف اعلامی دیجی‌کالا')
                heading = f'<b>{section} · {category}</b>\nتا {report["items_per_category"]} کالای برتر بر اساس {criterion}.'
                plan.append([dict(media=None, caption=heading, section_header=True)])
                previous = key
            plan.append(group)
        groups = plan
    elif report.get('bestsellers_enabled') or report.get('bestsellers'):
        n = len(report.get('bestsellers', []))
        heading = f'🏆 <b>گزارش کالاهای پرفروش</b>\n{n} کالای موجود و غیرسوپرمارکتی از فهرست پرفروش‌های سراسری و دسته‌های دیجی‌کالا.\nرتبهٔ هر کالا مربوط به فهرست ذکرشده در کپشن است؛ پرفروش بودن تضمین ارزان‌ترین بودن نیست.'
        groups.insert(len(report['deals']) + len(report.get('advertised_offers', [])), [dict(media=None, caption=heading, section_header=True)])
    delivery = dict(status='sent' if planned else 'no_images', planned=planned, sent=0,
                    missing_images=sum(not g[0]['media'] and not g[0].get('section_header') for g in groups), failed_groups=0, photos_sent=0)
    report['telegram_photos'] = delivery
    for group_index, group in enumerate(groups):
        # One self-contained message per product, never an album.
        method = 'sendPhoto' if group[0]['media'] else 'sendMessage'
        body = dict(chat_id=channel, disable_notification=True)
        p = group[0]
        if p['media']:
            body.update(photo=p['media'], caption=p['caption'], parse_mode='HTML')
        else:
            body.update(text=p['caption'], parse_mode='HTML', link_preview_options={'is_disabled': True})
        confirmed = False
        rejected = False
        sleep(3.1)
        for attempt in range(3):
            try:
                req = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}',
                    data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
                with opener(req, timeout=35) as response:
                    answer = json.load(response)
                result = answer.get('result')
                messages = result if isinstance(result, list) else [result]
                confirmed = bool(answer.get('ok') and len(messages) == len(group) and
                                 all(isinstance(m, dict) and m.get('message_id') for m in messages))
                rejected = not answer.get('ok') and answer.get('error_code') == 400
                break
            except urllib.error.HTTPError as error:
                rejected = error.code == 400
                if error.code == 429 and attempt < 2:
                    try:
                        delay = json.load(error).get('parameters', {}).get('retry_after', 5)
                        if isinstance(delay, (int, float)) and not isinstance(delay, bool) and 0 <= delay <= 30:
                            sleep(delay)
                            continue
                    except (ValueError, TypeError):
                        pass
                break
            except Exception:
                # A timeout may occur after acceptance: never resend blindly.
                break
        if confirmed:
            delivery['sent'] += 0 if p.get('section_header') else len(group)
            delivery['photos_sent'] += bool(p['media'])
            if history is not None and p.get('item_key'):
                mark_item(history,hashlib.sha256(channel.encode()).hexdigest(),p['item_key'],p['price_toman'],report['generated_at'])
        elif rejected and p['media']:
            # A rejected image does not remove the product's caption/link.
            groups[group_index+1:group_index+1] = [[dict(p, media=None)]]
            delivery['missing_images'] += 1
        else:
            delivery['failed_groups'] += 1
    if delivery['failed_groups'] or delivery['missing_images']:
        delivery['status'] = 'partial'
    print('Telegram photos: ' + json.dumps(delivery))


def summary(report, config):
    now = dt.datetime.fromisoformat(report['generated_at']).astimezone(ZoneInfo(config.get('timezone', 'Asia/Tehran')))
    verified = len(report['deals'])
    advertised = len(report.get('advertised_offers', []))
    if report.get('showcase'):
        count=len(report['showcase_items']);best=len(report.get('bestsellers',[]))
        if not count:
            if report.get('errors'):
                return f'🛍 <b>ویترین روزانه</b> · {now:%Y-%m-%d}\nدریافت داده کامل نشد؛ کالای جدید قابل ارائه پیدا نشد.'
            return f'🛍 <b>ویترین روزانه</b> · {now:%Y-%m-%d}\nکالای جدید یا تغییر مهم قیمت برای معرفی پیدا نشد.\nبررسی بعدی: نوبت روزانهٔ بعد.'
        counts={level:sum(r.get('price_comparison',{}).get('confidence','none')==level for r in report['showcase_items']) for level in ['high','partial','none']}
        return (f'🛍 <b>ویترین روزانهٔ {count} انتخاب</b> · {now:%Y-%m-%d}\n'
                f'💸 {count-best} تخفیف · 🏆 {best} پرفروش\n'
                f'اطمینان ترب: 🟢 {counts["high"]} · 🟡 {counts["partial"]} · ⚪ {counts["none"]}\n'
                'مقایسه محدود به منابع بررسی‌شده و بدون ارسال است؛ قیمت سبد را بررسی کنید.')
    text = f'🔎 <b>گزارش تخفیف‌های دیجی‌کالا</b>\n🕒 {now:%Y-%m-%d %H:%M}\nبررسی هر {report["interval_hours"]} ساعت · {report["candidate_count"]} کالای بررسی‌شده\n'
    text += f'📦 {verified + advertised} کالا · ✅ {verified} دارای شواهد بازار · ⚠️ {advertised} تخفیف اعلامی\n'
    if report.get('bestsellers_enabled') or report.get('bestsellers'):
        text += f'🏆 بخش دوم: {len(report.get("bestsellers", []))} کالای پرفروش از فهرست سراسری و دسته‌های دیجی‌کالا\n'
    if report.get('grouped'):
        text += f'📂 در هر دسته تا {report["items_per_category"]} کالا\n'
        for key, label in [('discount_groups', 'تخفیف‌ها'), ('bestseller_groups', 'پرفروش‌ها')]:
            names = '، '.join(html.escape(str(g['category'])[:60]) for g in report[key][:12])
            text += f'{label}: {names or "دادهٔ کافی دریافت نشد"}\n'
    if report.get('value_ranking'):
        rows=report['deals']+report.get('advertised_offers',[])+report.get('bestsellers',[])
        counts={level:sum(r.get('price_comparison',{}).get('confidence','none')==level for r in rows) for level in ['high','partial','none']}
        text+=f'\nاطمینان مقایسهٔ ترب: 🟢 {counts["high"]} تطبیق قوی · 🟡 {counts["partial"]} ناقص · ⚪ {counts["none"]} بدون تأیید\n'
    if verified + advertised:
        text += '\nمشخصات و لینک خرید هر کالا در پیام تصویری جداگانهٔ بعدی می‌آید.\n'
    else:
        text += '\nکالای پرتخفیف واجد شرایط پیدا نشد.\n'
    text += '\n⚠️ برای تخفیف‌های اعلامی، کمترین قیمت بازار تأیید نشده است. قیمت سبد، موجودی و گارانتی را پیش از خرید بررسی کنید.'
    if report.get('errors'):
        text += '\nدریافت بعضی منابع ناموفق بود؛ پوشش بازار کامل نیست.'
    if report.get('demo'):
        text += '\nدادهٔ ساختگی؛ پیشنهاد خرید نیست.'
    return text


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
    payload = json.dumps({"chat_id": channel, "text": summary(report, config) if settings.get("photos_enabled", True) else digest(report, config), "parse_mode": "HTML",
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
            # Keep confirmed text delivery even if a photo URL is inaccessible.
            # The successful invocation stays deduplicated after a restart.
            publish_photos(report, token, channel, settings, opener=opener, sleep=sleep, history=history)
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
