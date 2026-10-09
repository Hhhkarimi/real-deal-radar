import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from dealradar.core import History
from dealradar.telegram import digest, visible_length, publish


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
