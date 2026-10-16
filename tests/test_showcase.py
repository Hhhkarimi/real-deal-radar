import datetime as dt
import hashlib
import io
import json
import pathlib
import tempfile
import unittest
from collections import Counter
from unittest.mock import patch

from dealradar.core import History,UTC
from dealradar.showcase import select_showcase,item_due,item_key,mark_item
from dealradar.telegram import publish,visible_length
from test_engine import offer
import dataclasses

SETTINGS=dict(max_items=25,discount_items=15,bestseller_items=10,max_per_type=2)


def rows(prefix,count):
    result=[]
    for i in range(count):
        c=offer(product_key=f'{prefix}:{i}',category=f'{prefix} نوع {i//2}',
                image_url=f'https://images.example/{prefix}/{i}.jpg')
        row=dict(candidate=dataclasses.asdict(c),status='unverified',reasons=[],source_rank=i+1,
                 ranking_scope='سراسری',price_comparison=dict(confidence='none',comparable_shop_count=0))
        if prefix=='best':row['list_kind']='bestseller'
        result.append(row)
    return result


class ShowcaseTests(unittest.TestCase):
    def test_twenty_five_items_with_quotas_variety_and_no_overlap(self):
        discounted,best=rows('discount',40),rows('best',30)
        best.insert(0,dict(best[0],candidate=discounted[0]['candidate']))
        selected=select_showcase(discounted,best,SETTINGS)
        self.assertEqual(len(selected),25)
        self.assertEqual(Counter(r['showcase_section'] for r in selected),{'discount':15,'bestseller':10})
        self.assertEqual(len({r['candidate']['product_key'] for r in selected}),25)
        self.assertLessEqual(max(Counter(r['candidate']['category'] for r in selected).values()),2)

    def test_no_fake_fill_when_categories_or_new_items_are_limited(self):
        data=rows('discount',30)
        for row in data:row['candidate']['category']='همه از یک نوع'
        self.assertEqual(len(select_showcase(data,[],SETTINGS)),2)
        self.assertEqual(select_showcase(data,[],SETTINGS,due=lambda c:False),[])

    def test_confirmed_price_memory_is_per_channel_and_threshold_is_cumulative(self):
        with tempfile.TemporaryDirectory() as directory:
            h=History(pathlib.Path(directory)/'h.sqlite')
            c=rows('discount',1)[0]['candidate']
            self.assertTrue(item_due(h,'channel',c))
            mark_item(h,'channel',item_key(c),c['price_toman'],'2026-10-16T00:00:00+00:00')
            self.assertFalse(item_due(h,'channel',c))
            self.assertFalse(item_due(h,'channel',dict(c,price_toman=728)))
            self.assertTrue(item_due(h,'channel',dict(c,price_toman=735)))
            self.assertTrue(item_due(h,'another-channel',c))
            h.close()

    def test_one_summary_twenty_five_photos_four_lines_and_history(self):
        selected=select_showcase(rows('discount',40),rows('best',30),SETTINGS)
        report=dict(showcase=True,showcase_items=selected,generated_at=dt.datetime.now(UTC).isoformat(),
                    interval_hours=24,candidate_count=100,deals=[],advertised_offers=selected[:15],bestsellers=selected[15:],errors=[])
        calls=[]
        def opener(req,**kwargs):
            calls.append((req.full_url,json.loads(req.data)))
            return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
        with tempfile.TemporaryDirectory() as directory,patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'@test'}):
            h=History(pathlib.Path(directory)/'h.sqlite')
            config=dict(interval_hours=24,timezone='Asia/Tehran',telegram=dict(enabled=True,manual_run_id='1-1'))
            self.assertEqual(publish(report,h,config,opener=opener,sleep=lambda _:None),[])
            self.assertEqual(len(calls),26)
            for url,body in calls[1:]:
                self.assertTrue(url.endswith('/sendPhoto'))
                self.assertEqual(len(body['caption'].splitlines()),4)
                self.assertLessEqual(visible_length(body['caption']),500)
            self.assertEqual(h.db.execute('SELECT COUNT(*) FROM showcase_items').fetchone()[0],25)
            key=hashlib.sha256(b'@test').hexdigest()
            self.assertFalse(item_due(h,key,selected[0]['candidate']))
            h.close()

    def test_failed_item_is_not_marked_sent(self):
        selected=select_showcase(rows('discount',1),[],SETTINGS)
        report=dict(showcase=True,showcase_items=selected,generated_at=dt.datetime.now(UTC).isoformat(),
                    interval_hours=24,candidate_count=1,deals=[],advertised_offers=selected,bestsellers=[],errors=[])
        def opener(req,**kwargs):
            if req.full_url.endswith('/sendPhoto'):raise TimeoutError()
            return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
        with tempfile.TemporaryDirectory() as directory,patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'@test'}):
            h=History(pathlib.Path(directory)/'h.sqlite')
            publish(report,h,dict(interval_hours=24,telegram={'enabled':True}),opener=opener,sleep=lambda _:None)
            key=hashlib.sha256(b'@test').hexdigest()
            self.assertTrue(item_due(h,key,selected[0]['candidate']))
            h.close()


if __name__=='__main__':unittest.main()
