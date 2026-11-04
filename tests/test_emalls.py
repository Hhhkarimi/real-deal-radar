import dataclasses
import datetime as dt
import gzip
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from dealradar.core import UTC
from dealradar.emalls import read_search,listed_comparison,enrich_emalls
from dealradar.cli import run_once
from dealradar.telegram import compact_photo
from test_engine import offer,C

class EmallsTests(unittest.TestCase):
    def candidate(self):
        return offer(title='هدفون انکر مدل R50i A3949',brand_name='انکر',observed_at=dt.datetime.now(UTC).isoformat())
    def test_gzip_search_uses_observed_readonly_endpoint(self):
        response=io.BytesIO(gzip.compress(json.dumps({'lstsearchresualt':[]}).encode()))
        response.url='https://emalls.ir/swservice/site_search'
        with patch('dealradar.emalls.HTTP.open',return_value=response) as op:
            self.assertEqual(read_search('R50i'),[])
        req=op.call_args.args[0]
        self.assertEqual(req.get_method(),'POST')
        self.assertIn(b'findstr=R50i',req.data)
    def test_reference_price_is_never_claimed_as_equivalent_variant(self):
        items=[dict(title='هدفون انکر R50i',price='۲,۰۰۰,۰۰۰',offcount=12,link='/product~id~123'),
               dict(title='کاور هدفون انکر R50i',price='۵۰,۰۰۰',offcount=1,link='/case~id~456')]
        result=listed_comparison(self.candidate(),items,dt.datetime.now(UTC))
        self.assertEqual(result['listed_low_toman'],2000000)
        self.assertEqual(result['confidence'],'partial')
        self.assertEqual(result['comparable_shop_count'],0)
        self.assertNotIn('saving_percent',result)
        caption=compact_photo(dict(candidate=dataclasses.asdict(self.candidate()),price_comparison=result))['caption']
        self.assertIn('ایمالز',caption)
        self.assertNotIn('ترب',caption)
    def test_source_failure_does_not_abort_candidate_collection(self):
        rows=[dict(candidate=dataclasses.asdict(self.candidate()))]
        def fail(query):raise TimeoutError()
        cfg={'emalls':{'enabled':True}}
        enrich_emalls(rows,cfg,reader=fail)
        self.assertEqual(cfg['_market_coverage']['provider_status'],'unavailable')
        self.assertEqual(len(rows),1)
    def test_hunter_routes_to_digikala_fallback_without_torob(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=dict(C,_base=pathlib.Path(directory),max_items=25,interval_hours=.25,
                     agent={'enabled':True,'market_source':'emalls','digest_hours':12},
                     showcase={'enabled':True,'max_items':25,'discount_items':25,'bestseller_items':0,'max_per_type':2,'always_recommend':True},
                     telegram={'enabled':False})
            def collect(policy,now):
                self.assertFalse(policy['torob']['enabled'])
                self.assertFalse(policy['bestsellers']['enabled'])
                return [dataclasses.replace(self.candidate(),observed_at=now.isoformat())],[],[]
            with patch('dealradar.cli.collect',side_effect=collect),patch('dealradar.emalls.enrich_emalls'),patch('dealradar.cli.enrich_prices') as torob:
                report=run_once(cfg,send=False)
            torob.assert_not_called()
            self.assertEqual(report['market_source'],'emalls')
            self.assertEqual(len(report['showcase_items']),1)
            self.assertFalse(report.get('agent',False))
    def test_low_advertised_discount_cannot_make_fallback_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=dict(C,_base=pathlib.Path(directory),max_items=25,interval_hours=12,
                     showcase={'enabled':True,'max_items':25,'discount_items':25,'bestseller_items':0,'max_per_type':2,'always_recommend':True})
            product=dataclasses.replace(self.candidate(),discount_percent=5)
            with patch('dealradar.cli.collect',return_value=([product],[],[])):
                report=run_once(cfg,send=False)
            self.assertEqual(len(report['showcase_items']),1)
            self.assertTrue(report['showcase_items'][0]['fallback_recommendation'])

if __name__=='__main__':unittest.main()
