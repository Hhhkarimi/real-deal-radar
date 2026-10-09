import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from dealradar.core import History
from dealradar.telegram import digest, visible_length, publish, photo_groups


def report(count=2):
    deals = []
    for i in range(count):
        deals.append(dict(candidate=dict(title='هدفون <جدید> & مناسب خرید ' * 8, url=f'https://shop.example/product/{i}',
                                        price_toman=700000), saving_percent=30, market_low_toman=1000000,
                          comparison_basis='item_only'))
    return dict(generated_at='2026-10-09T09:01:00+00:00', interval_hours=6, candidate_count=80,
                deals=deals, errors=[], demo=False)


C = dict(interval_hours=6, timezone='Asia/Tehran', telegram=dict(enabled=True))


class TelegramTests(unittest.TestCase):
    def test_twenty_photos_send_two_albums_with_product_captions(self):
        row = report(0)
        row['advertised_offers'] = [dict(candidate=dict(title='هدفون <جدید>', url=f'https://shop.example/p/{i}', image_url=f'https://images.example/{i}.jpg', price_toman=700000, discount_percent=40)) for i in range(20)]
        groups = photo_groups(row)
        self.assertEqual(list(map(len, groups)), [10, 10])
        self.assertIn('کمترین قیمت بازار تأیید نشده', groups[0][0]['caption'])
        self.assertIn('https://shop.example/p/0', groups[0][0]['caption'])
        self.assertNotIn('<جدید>', groups[0][0]['caption'])
        self.assertLessEqual(visible_length(groups[0][0]['caption']), 1024)
        calls = []
        def opener(req, **kwargs):
            body = json.loads(req.data); calls.append((req.full_url, body))
            result = [{'message_id': i+1} for i in range(len(body['media']))] if 'media' in body else {'message_id':42}
            return io.BytesIO(json.dumps(dict(ok=True, result=result)).encode())
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'@channel'}):
            history = History(pathlib.Path(directory)/'h.sqlite')
            self.assertEqual(publish(row, history, C, opener=opener, sleep=lambda _:None), [])
            self.assertEqual(len(calls), 3)
            self.assertTrue(calls[1][0].endswith('/sendMediaGroup'))
            self.assertEqual(row['telegram_photos']['sent'], 20)
            self.assertEqual(publish(row, history, C, opener=opener, sleep=lambda _:None), [])
            self.assertEqual(len(calls), 3)
            history.close()

    def test_single_photo_timeout_keeps_text_delivery_and_does_not_retry(self):
        row = report(1)
        row['deals'][0]['candidate']['image_url'] = 'https://images.example/one.jpg'
        calls = []
        def opener(req, **kwargs):
            calls.append(req.full_url)
            if req.full_url.endswith('/sendPhoto'):
                raise TimeoutError('secret')
            return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'@channel'}):
            history = History(pathlib.Path(directory)/'h.sqlite')
            self.assertEqual(publish(row, history, C, opener=opener, sleep=lambda _:None), [])
            self.assertEqual(len(calls), 2)
            self.assertEqual(row['telegram_delivery']['status'], 'sent')
            self.assertEqual(row['telegram_photos']['status'], 'partial')
            self.assertNotIn('secret', json.dumps(row['telegram_photos']))
            history.close()

    def test_explicit_album_rejection_isolates_bad_photo(self):
        row = report(2)
        for i, r in enumerate(row['deals']):
            r['candidate']['image_url'] = f'https://images.example/{i}.jpg'
        calls = []
        def opener(req, **kwargs):
            body = json.loads(req.data); calls.append(body)
            if 'media' in body:
                raise urllib.error.HTTPError('redacted',400,'bad photo',{},io.BytesIO(b'{}'))
            if body.get('photo') == 'https://images.example/0.jpg':
                raise urllib.error.HTTPError('redacted',400,'bad photo',{},io.BytesIO(b'{}'))
            return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'@channel'}):
            history = History(pathlib.Path(directory)/'h.sqlite')
            self.assertEqual(publish(row, history, C, opener=opener, sleep=lambda _:None), [])
            self.assertEqual(len(calls), 4)
            self.assertEqual(row['telegram_photos']['sent'], 1)
            self.assertEqual(row['telegram_photos']['status'], 'partial')
            self.assertEqual(row['telegram_delivery']['status'], 'sent')
            history.close()

    def test_twenty_advertised_offers_have_links_prices_and_clear_uncertainty(self):
        row = report(0)
        row['advertised_offers'] = [dict(candidate=dict(title='هدفون <مدل جدید> ' * 20, url=f'https://shop.example/p/{i}', price_toman=700000, discount_percent=40)) for i in range(20)]
        row['errors'] = ['Torob discovery failed']
        row['market_coverage'] = dict(checked=21, provider_status='unavailable', matched_direct_quotes=0)
        text = digest(row, C)
        self.assertEqual(text.count('<a href='), 20)
        self.assertEqual(text.count('تخفیف اعلامی 40٪'), 20)
        self.assertIn('۷۰۰٬۰۰۰ تومان', text)
        self.assertIn('کمترین قیمت بازار تأیید نشده', text)
        self.assertNotIn('٪ زیر قیمت منابع بازار', text)
        self.assertNotIn('پیدا نشد', text)
        self.assertLessEqual(visible_length(text), 3900)
        self.assertNotIn('<مدل جدید>', text)

    def test_mixed_report_distinguishes_verified_from_advertised(self):
        row = report(1)
        row['advertised_offers'] = [dict(candidate=dict(title='کالای تخفیف‌دار', url='https://shop.example/unverified', price_toman=500000, discount_percent=25))]
        text = digest(row, C)
        self.assertEqual(text.count('<a href='), 2)
        self.assertIn('1. ✅', text)
        self.assertIn('2. ⚠️', text)
        self.assertIn('تخفیف اعلامی 25٪', text)

    def test_manual_runs_send_without_consuming_scheduled_slot(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret', 'TELEGRAM_CHAT_ID':'@channel', 'DEALRADAR_SCHEDULE_MODE':'slots'}):
            history = History(pathlib.Path(directory)/'h.sqlite')
            calls = []
            def opener(*args, **kwargs):
                calls.append(1)
                return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
            manual = dict(C, telegram=dict(enabled=True, manual_run_id='1-1'))
            row = report()
            self.assertEqual(publish(row, history, manual, opener=opener), [])
            self.assertEqual(row['telegram_delivery']['status'], 'sent')
            self.assertEqual(publish(row, history, manual, opener=opener), [])
            self.assertEqual(row['telegram_delivery']['reason'], 'manual_run_already_sent')
            self.assertEqual(publish(row, history, C, opener=opener), [])
            manual['telegram']['manual_run_id'] = '2-1'
            self.assertEqual(publish(row, history, manual, opener=opener), [])
            self.assertEqual(publish(row, history, C, opener=opener), [])
            self.assertEqual(row['telegram_delivery']['reason'], 'slot_already_sent')
            self.assertEqual(len(calls), 3)
            history.close()

    def test_twenty_deals_fit_one_post_and_html_is_escaped(self):
        text = digest(report(20), C)
        self.assertLessEqual(visible_length(text), 4096)
        self.assertEqual(text.count('<a href='), 20)
        self.assertIn('&lt;جدید&gt;', text)
        self.assertNotIn('<جدید>', text)
        self.assertIn('محاسبه نشده', text)

    def test_empty_report_and_outage_have_different_messages(self):
        empty = report(0)
        self.assertIn('پیدا نشد', digest(empty, C))
        empty.update(candidate_count=0, errors=['source timeout'])
        text = digest(empty, C)
        self.assertIn('دریافت داده ناموفق', text)
        self.assertNotIn('source timeout', text)

    def test_one_post_per_interval_after_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret', 'TELEGRAM_CHAT_ID':'@channel'}):
            calls=[]
            def opener(req, **kwargs):
                calls.append(json.loads(req.data))
                return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
            path=pathlib.Path(directory)/'history.sqlite'
            history=History(path)
            self.assertEqual(publish(report(), history, C, opener=opener), [])
            history.close()
            history=History(path)
            self.assertEqual(publish(report(), history, C, opener=opener), [])
            boundary=report(); boundary['generated_at']='2026-10-09T12:01:00+00:00'
            self.assertEqual(publish(boundary, history, C, opener=opener), [])
            later=report();later['generated_at']='2026-10-09T15:01:00+00:00'
            self.assertEqual(publish(later, history, C, opener=opener), [])
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]['chat_id'], '@channel')
            self.assertEqual(calls[0]['parse_mode'], 'HTML')
            history.close()

    def test_schedule_delay_does_not_skip_next_slot(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret', 'TELEGRAM_CHAT_ID':'@channel', 'DEALRADAR_SCHEDULE_MODE':'slots'}):
            history=History(pathlib.Path(directory)/'h.sqlite')
            calls=[]
            def opener(*args, **kwargs):
                calls.append(1)
                return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
            first=report(); first['generated_at']='2026-10-09T06:25:00+00:00'
            second=report(); second['generated_at']='2026-10-09T12:17:00+00:00'
            self.assertEqual(publish(first, history, C, opener=opener), [])
            self.assertEqual(publish(second, history, C, opener=opener), [])
            self.assertEqual(publish(second, history, C, opener=opener), [])
            self.assertEqual(len(calls), 2)
            history.close()

    def test_timeout_no_blind_retry_or_secret_leak(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret', 'TELEGRAM_CHAT_ID':'@channel'}):
            history=History(pathlib.Path(directory)/'h.sqlite')
            calls=[]
            def opener(*args, **kwargs):
                calls.append(1)
                raise TimeoutError('secret')
            errors=publish(report(), history, C, opener=opener)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('secret', str(errors))
            self.assertEqual(history.db.execute('SELECT COUNT(*) FROM channel_posts').fetchone()[0], 0)
            history.close()

    def test_rate_limit_bounded_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'secret', 'TELEGRAM_CHAT_ID':'@channel'}):
            history=History(pathlib.Path(directory)/'h.sqlite')
            calls=[]; waits=[]
            def opener(*args, **kwargs):
                calls.append(1)
                if len(calls)==1:
                    raise urllib.error.HTTPError('redacted',429,'rate limit',{},io.BytesIO(b'{"parameters":{"retry_after":2}}'))
                return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
            self.assertEqual(publish(report(),history,C,opener=opener,sleep=waits.append),[])
            self.assertEqual(waits,[2])
            self.assertEqual(len(calls),2)
            history.close()

    def test_missing_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            history=History(pathlib.Path(directory)/'h.sqlite')
            self.assertIn('missing', publish(report(), history, C)[0])
            history.close()

    def test_preview_demo_is_clearly_labelled(self):
        row=report(); row['demo']=True
        self.assertIn('دادهٔ ساختگی', digest(row,C))


if __name__ == '__main__':
    unittest.main()
