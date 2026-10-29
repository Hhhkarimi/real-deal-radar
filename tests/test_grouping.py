import dataclasses
import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from dealradar.cli import run_once
from dealradar.core import UTC, load_config
from dealradar.grouping import select_groups, flatten
from dealradar.telegram import publish
from test_engine import C, offer


def rows_for_categories(count=5, categories=6):
    now = dt.datetime.now(UTC).isoformat()
    return [dict(candidate=dataclasses.asdict(offer(product_key=f'dkp:{g}-{i}', observed_at=now,
                  category='نوع جزئی', category_group=f'دسته {g}', discount_percent=20+i,
                  image_url=f'https://images.example/{g}-{i}.jpg')),
                 status='unverified', reasons=['insufficient_independent_sellers'])
            for g in range(categories) for i in range(count)]


class GroupingTests(unittest.TestCase):
    def test_five_per_category_without_a_global_twenty_item_cutoff(self):
        groups = select_groups(rows_for_categories(count=9), 5)
        self.assertEqual(len(groups), 6)
        self.assertEqual(len(flatten(groups)), 30)
        for group in groups:
            self.assertEqual([r['candidate']['discount_percent'] for r in group['items']], [28,27,26,25,24])
            self.assertEqual([r['category_position'] for r in group['items']], [1,2,3,4,5])

    def test_bestsellers_rank_by_source_not_discount(self):
        rows = rows_for_categories(count=8, categories=1)
        for i, row in enumerate(rows):
            row.update(list_kind='bestseller', source_rank=i+1, source_category_id=12, ranking_scope='دسته 0')
        result = flatten(select_groups(rows, 5, bestsellers=True))
        self.assertEqual([r['source_rank'] for r in result], [1,2,3,4,5])

    def test_short_categories_duplicates_and_invalid_products(self):
        rows = rows_for_categories(count=2, categories=1)
        rows += [rows[0],dict(rows[0],candidate=dict(rows[0]['candidate'],product_key='grocery',supermarket=True))]
        result = flatten(select_groups(rows, 5))
        self.assertEqual(len(result), 2)
        self.assertEqual(select_groups([], 5), [])
        with self.assertRaises(ValueError):
            select_groups(rows, True)

    def test_run_and_channel_keep_category_headers_and_individual_captions(self):
        candidates = [offer(**{k:v for k,v in row['candidate'].items() if k not in ('product_key',)},
                            product_key=row['candidate']['product_key']) for row in rows_for_categories(count=7, categories=5)]
        bests = rows_for_categories(count=7, categories=2)
        for i,row in enumerate(bests):
            row.update(list_kind='bestseller',source_rank=i+1,source_category_id=1,ranking_scope='دسته')
        with tempfile.TemporaryDirectory() as directory:
            config = dict(C,_base=pathlib.Path(directory),max_items=20,interval_hours=24,
                          grouping={'enabled':True,'items_per_category':5},bestsellers={'enabled':True})
            with patch('dealradar.cli.collect', return_value=(candidates,[],[])), patch('dealradar.cli.collect_bestsellers',return_value=(bests,[])):
                report = run_once(config,send=False)
            self.assertEqual(len(report['advertised_offers']),25)
            self.assertEqual(len(report['bestsellers']),10)
            self.assertEqual(len(report['discount_groups']),5)
            calls=[]
            def opener(req,**kwargs):
                calls.append(json.loads(req.data))
                return io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
            from dealradar.core import History
            h=History(pathlib.Path(directory)/'post.sqlite')
            config['telegram']={'enabled':True,'manual_run_id':'500-1'}
            with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'test'}):
                self.assertEqual(publish(report,h,config,opener=opener,sleep=lambda _:None),[])
            h.close()
            self.assertEqual(len(calls),1+7+35)
            self.assertIn('بررسی هر 24 ساعت', calls[0]['text'])
            self.assertIn('تخفیف‌ها · دسته', calls[1]['text'])
            self.assertIn('دسته 0 · 1', calls[2]['caption'])

    def test_bestseller_collection_does_not_stop_before_other_categories(self):
        from dealradar.sources import collect_bestsellers
        from test_live_sources import product, NOW
        first = product()
        other = product(); other['id'] = 456
        other['data_layer']['item_category2'] = 'مد و پوشاک'
        global_data = {'data':{'products':[first],'categories':[{'code':'apparel','id':8749,'title_fa':'مد و پوشاک'}]}}
        category_data = {'data':{'products':[other]}}
        config = dict(C,grouping={'enabled':True,'items_per_category':5},bestsellers={'enabled':True,'max_items':1,'category_codes':['apparel']})
        with patch('dealradar.sources.fetch',side_effect=[json.dumps(global_data),json.dumps(category_data)]) as get, patch('dealradar.sources.time.sleep'):
            result, errors = collect_bestsellers(config,NOW)
        self.assertEqual(errors,[])
        self.assertEqual(get.call_count,2)
        self.assertEqual(len(select_groups(result,5,bestsellers=True)),2)

    def test_default_schedule_polls_for_deals_every_fifteen_minutes(self):
        root=pathlib.Path(__file__).resolve().parents[1]
        config=load_config(root/'config.toml')
        self.assertEqual(config['interval_hours'],0.25)
        self.assertEqual(config['agent']['digest_hours'],12)
        self.assertEqual(config['grouping']['items_per_category'],5)
        self.assertIn("cron: '7,22,37,52 * * * *'", (root/'.github/workflows/channel.yml').read_text())


if __name__ == '__main__':
    unittest.main()
