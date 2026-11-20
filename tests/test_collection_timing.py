import datetime as dt
import pathlib
import tempfile
import time
import io
import json
import unittest
from unittest.mock import patch

from dealradar.cli import run_once
from dealradar.hunter import run_once as hunt_once
from dealradar.core import UTC
from test_engine import C, offer
from dealradar.sources import collect, fetch_page
from test_live_sources import product


class CollectionTimingTests(unittest.TestCase):
    def test_collection_deadline_retains_completed_products_and_stops_new_requests(self):
        elapsed=[0]
        listing={'data':{'widgets':[{'type':'vertical_product_listing','data':{'pager':{'total_pages':1},
            'widgets':[{'type':'product','data':{'id':123}},{'type':'product','data':{'id':124}}]}}]}}
        def get(url,**kwargs):
            if '/amazing/' in url:return json.dumps(listing)
            elapsed[0]=3
            return json.dumps({'data':{'product':product()}})
        with patch('dealradar.sources.fetch',side_effect=get) as fetch, \
             patch('dealradar.sources.time.monotonic',side_effect=lambda:elapsed[0]), \
             patch('dealradar.sources.time.sleep'):
            candidates,_,errors=collect({'_base':pathlib.Path('.'),'digikala':{
                'enabled':True,'pages':6,'max_duration_seconds':2}},dt.datetime.now(UTC))
        self.assertEqual(len(candidates),1)
        self.assertEqual(fetch.call_count,2)
        self.assertTrue(any('time budget exhausted' in e for e in errors))

    def test_slow_stream_cannot_extend_the_http_deadline_indefinitely(self):
        elapsed=[0]
        class Response(io.BytesIO):
            url='https://example.com/p'
            def read1(self,size):
                elapsed[0]=3
                return super().read1(size)
        with patch('dealradar.sources.HTTP.open',return_value=Response(b'body')), \
             patch('dealradar.sources.time.monotonic',side_effect=lambda:elapsed[0]):
            with self.assertRaisesRegex(RuntimeError,'time budget exceeded'):
                fetch_page('https://example.com/p',deadline=2)

    def collect_after_start(self, *args):
        time.sleep(.01)
        now=dt.datetime.now(UTC)
        c=offer(observed_at=now.isoformat(), price_toman=700000)
        market=[offer(observed_at=now.isoformat(),price_toman=1000000,seller_id=f'shop:{i}',
                      url=f'https://shop{i}.example/p') for i in range(3)]
        return [c],market,[]

    def test_newly_hydrated_offer_is_not_rejected_as_future_dated(self):
        def collect(*args):
            candidates,_,errors=self.collect_after_start()
            return candidates,[],errors
        with tempfile.TemporaryDirectory() as directory:
            config=dict(C,_base=pathlib.Path(directory),max_items=5,interval_hours=12)
            with patch('dealradar.cli.collect',side_effect=collect):
                report=run_once(config,send=False)
            self.assertEqual(len(report['advertised_offers']),1)

    def test_hunter_preserves_market_evidence_collected_after_cycle_start(self):
        with tempfile.TemporaryDirectory() as directory:
            config=dict(C,_base=pathlib.Path(directory),max_items=5,interval_hours=.25,agent={'enabled':True})
            with patch('dealradar.hunter.collect',side_effect=self.collect_after_start), \
                 patch('dealradar.hunter.recheck',side_effect=lambda row,*args:row):
                report=hunt_once(config,send=False)
            self.assertEqual(len(report['flash_deals']),1)
