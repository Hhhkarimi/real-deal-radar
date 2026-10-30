import dataclasses
import datetime as dt
import json
import pathlib
import tempfile
import unittest

from dealradar.core import UTC
from dealradar.grouping import select_groups, flatten
from dealradar.pricing import compare_listings, combine_comparisons, model_match, enrich_prices, selection_reason, resolve_merchant_url
from unittest.mock import patch
from dealradar.telegram import photo_groups, visible_length
from dealradar.torob_mcp import TorobMCP, MCPError
from test_engine import offer, C


NOW=dt.datetime.now(UTC)


def candidate(**changes):
    return offer(title='هدفون انکر مدل R50i A3949',brand_name='انکر',brand_names=['Anker'],
                 color_name='مشکی',warranty_key='گارانتی 18 ماهه تست',observed_at=NOW.isoformat(),
                 price_toman=700000,**changes)


def market(count=3):
    c=candidate()
    detail=dict(title=c.title,url='https://torob.com/p/test/',condition_text='نو',specifications={'items':[]})
    sellers=dict(price_unit='toman',fetched_at=NOW.isoformat(),loaded_list_complete=True,truncated=False,
                 items=[dict(shop_id=i,shop_name=f'فروشگاه {i}',title=c.title+' مشکی نو',
                             warranty_or_description='گارانتی ۱۸ ماهه تست',price_toman=1000000+i*10000,
                             available=True,shop_type='online',is_ad=False,price_unreliable=False)
                        for i in range(count)])
    return detail,sellers


class PricingTests(unittest.TestCase):
    def test_torob_html_handoff_resolves_without_fetching_the_merchant(self):
        redirect='https://api.torob.com/v4/product-page/redirect/?prk=test'
        with patch('dealradar.sources.fetch_page', return_value=('<a href="https://shop.example/p">خرید</a>',redirect)) as fetch:
            self.assertEqual(resolve_merchant_url(redirect),'https://shop.example/p')
        fetch.assert_called_once_with(redirect,attempts=1)

    def test_ambiguous_redirect_is_not_treated_as_a_verified_shop(self):
        redirect='https://api.torob.com/v4/product-page/redirect/?prk=test'
        with patch('dealradar.sources.fetch_page', return_value=('<a href="https://a.example/p">a</a><a href="https://b.example/p">b</a>',redirect)):
            with self.assertRaises(ValueError):resolve_merchant_url(redirect)

    def test_typographic_hyphen_in_model_does_not_reject_the_same_compact_code(self):
        c=dataclasses.replace(candidate(), title='پاوربانک ریمکس مدل RPP\u2011770', brand_name='ریمکس', brand_names=[])
        self.assertTrue(model_match(c,'پاوربانک ریمکس مدل RPP770'))
        self.assertTrue(model_match(c,'پاوربانک ریمکس مدل RPP-770'))
        self.assertFalse(model_match(c,'پاوربانک ریمکس مدل RPP771'))

    def test_parent_color_catalog_cannot_override_explicit_seller_variant(self):
        detail,sellers=market()
        detail['specifications']['items']=[dict(name='رنگ',value='مشکی، سفید')]
        self.assertEqual(compare_listings(candidate(),detail,sellers,NOW)['comparable_shop_count'],3)

    def test_multiple_parent_listings_do_not_double_count_shops(self):
        first=compare_listings(candidate(),*market(),NOW)
        result=combine_comparisons(candidate(),[first,first])
        self.assertEqual(result['comparable_shop_count'],3)
        self.assertEqual(result['saving_percent'],30)

    def test_three_equivalent_shops_make_a_source_scoped_comparison(self):
        result=compare_listings(candidate(),*market(),NOW)
        self.assertEqual(result['confidence'],'high')
        self.assertEqual(result['comparable_shop_count'],3)
        self.assertEqual(result['market_low_toman'],1000000)
        self.assertEqual(result['saving_toman'],300000)
        self.assertEqual(result['saving_percent'],30)

    def test_repeated_shop_cannot_inflate_confidence(self):
        detail,sellers=market()
        for item in sellers['items']:item['shop_id']=1;item['shop_name']='same'
        result=compare_listings(candidate(),detail,sellers,NOW)
        self.assertEqual(result['confidence'],'partial')
        self.assertEqual(result['comparable_shop_count'],1)

    def test_unknown_stock_ads_bad_prices_and_used_items_are_not_comparable(self):
        for changes in [dict(available=None),dict(available=False),dict(is_ad=True),dict(price_unreliable=True),
                        dict(price_toman=True),dict(price_toman=0),dict(title=candidate().title+' مشکی کارکرده')]:
            detail,sellers=market()
            for item in sellers['items']:item.update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(compare_listings(candidate(),detail,sellers,NOW)['comparable_shop_count'],0)

    def test_warranty_color_condition_and_model_must_be_explicit(self):
        for changes in [dict(warranty_or_description='گارانتی 12 ماهه تست'),dict(warranty_or_description='نامشخص'),
                        dict(title=candidate().title+' سفید نو'),dict(title='هدفون انکر مدل R50i A3959 مشکی نو')]:
            detail,sellers=market()
            for item in sellers['items']:item.update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(compare_listings(candidate(),detail,sellers,NOW)['comparable_shop_count'],0)
        detail,sellers=market();detail['condition_text']=''
        for item in sellers['items']:item['title']=candidate().title+' مشکی'
        self.assertEqual(compare_listings(candidate(),detail,sellers,NOW)['comparable_shop_count'],0)

    def test_partial_reference_prices_never_claim_comparable_saving(self):
        detail,sellers=market()
        for item in sellers['items']:item['warranty_or_description']='نامشخص'
        comparison=compare_listings(candidate(),detail,sellers,NOW)
        self.assertEqual(comparison['confidence'],'partial')
        self.assertEqual(comparison['listed_low_toman'],1000000)
        self.assertNotIn('saving_percent',comparison)
        row=dict(candidate=dataclasses.asdict(candidate()),price_comparison=comparison)
        caption=photo_groups(dict(deals=[],advertised_offers=[row]))[0][0]['caption']
        self.assertIn('نسخهٔ همسان تأیید نشده',caption)
        self.assertNotIn('اختلاف با کمترین پیشنهاد همسان',caption)

    def test_stale_prices_or_wrong_currency_never_calculate_savings(self):
        for changes in [dict(fetched_at=(NOW-dt.timedelta(hours=7)).isoformat()),dict(price_unit='rial')]:
            detail,sellers=market();sellers.update(changes)
            result=compare_listings(candidate(),detail,sellers,NOW)
            self.assertEqual(result['confidence'],'none')
            self.assertNotIn('saving_percent',result)

    def test_exact_model_identifiers_brand_and_editions(self):
        self.assertTrue(model_match(candidate(),'Anker R50i A3949'))
        self.assertFalse(model_match(candidate(),'انکر R50i A3959'))
        self.assertFalse(model_match(candidate(),'برند دیگر R50i A3949'))
        self.assertFalse(model_match(candidate(),'انکر R50i A3949 Pro'))

    def test_ranking_uses_real_saving_instead_of_advertised_discount(self):
        rows=[]
        for i,(ad,saving) in enumerate([(90,10),(30,25),(40,-10)]):
            row=dict(candidate=dataclasses.asdict(candidate()),status='unverified',price_comparison=dict(confidence='high',comparable_shop_count=3,saving_percent=saving,saving_toman=saving*10000))
            row['candidate'].update(product_key=f'dkp:{i}',discount_percent=ad)
            rows.append(row)
        chosen=flatten(select_groups(rows,5,value_ranking=True,category_level='type'))
        self.assertEqual([r['candidate']['discount_percent'] for r in chosen],[30,90])

    def test_caption_explains_comparison_links_and_reason(self):
        row=dict(candidate=dataclasses.asdict(candidate()),status='unverified',price_comparison=compare_listings(candidate(),*market(),NOW))
        row['selection_reason']=selection_reason(row)
        caption=photo_groups(dict(deals=[],advertised_offers=[row]))[0][0]['caption']
        self.assertIn('🟢',caption)
        self.assertIn('3 فروشگاه همسان',caption)
        self.assertIn('دلیل انتخاب',caption)
        self.assertIn('مقایسه در ترب',caption)
        self.assertIn('بدون ارسال',caption)
        self.assertLessEqual(visible_length(caption),1024)

    def test_mcp_calls_seller_tool_and_caches_same_product_rows(self):
        c=candidate();detail,sellers=market()
        detail['id']='id'
        calls=[]
        class Client:
            def __init__(self,config):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,name,args):
                calls.append(name)
                return {'items':[detail]} if name=='search_torob' else detail if name=='product_details' else sellers
        rows=[dict(candidate=dataclasses.asdict(c)),dict(candidate=dataclasses.asdict(c),list_kind='bestseller')]
        config=dict(C,torob={'enabled':True,'backend':'mcp','max_candidates':2})
        enrich_prices(rows,config,client_factory=Client)
        self.assertEqual(calls,['search_torob','product_details','product_sellers'])
        self.assertEqual(rows[0]['price_comparison']['confidence'],'high')
        self.assertEqual(rows[1]['price_comparison']['confidence'],'high')

    def test_mcp_block_stops_without_more_origin_requests(self):
        calls=[]
        class Client:
            def __init__(self,config):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,*args):calls.append(1);raise MCPError('UPSTREAM_BLOCKED',429)
        rows=[dict(candidate=dataclasses.asdict(candidate()))]
        config=dict(C,torob={'enabled':True,'backend':'mcp'})
        enrich_prices(rows,config,client_factory=Client)
        self.assertEqual(len(calls),1)
        self.assertEqual(config['_market_coverage']['provider_status'],'unavailable')
        self.assertEqual(rows[0]['price_comparison']['confidence'],'none')

    def test_real_stdio_handshake_and_tool_errors_without_network(self):
        root=pathlib.Path(__file__).resolve().parents[1]
        if not (root/'vendor/torob-mcp/dist/stdio.js').exists():self.skipTest('Build pinned MCP first')
        with TorobMCP({'_base':root}) as client:
            self.assertEqual(len(client.rpc('tools/list',{})['tools']),10)
            with self.assertRaises(MCPError) as error:
                client.call('product_details',{'product':'invalid-id'})
            self.assertEqual(error.exception.code,'INVALID_PRODUCT')


if __name__=='__main__':unittest.main()
