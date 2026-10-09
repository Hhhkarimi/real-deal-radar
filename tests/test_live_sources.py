import dataclasses
import datetime as dt
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from dealradar.core import Offer, UTC
from dealradar.market import equivalent_quote, normalized
from dealradar.sources import dk_card, fetch

NOW = dt.datetime(2026, 10, 9, 9, 0, tzinfo=UTC)


def product():
    return {'id':123, 'title_fa':'هاب هیسکا مدل HR-53', 'url':{'uri':'/product/dkp-123/'},
            'status':'marketable','data_layer':{'item_category2':'کالای دیجیتال','item_category3':'USB هاب'},
            'default_variant':{'status':'marketable','color':{'id':2,'title':'سفید'},'seller':{'id':1},
             'warranty':{'title_fa':'گارانتی 18 ماهه تست'},'price':{'selling_price':10000000,'order_limit':2,'timer':600,'discount_percent':30}}}


def merchant(**overrides):
    p={'@type':'Product','name':'هاب هیسکا مدل HR-53','color':'سفید',
       'additionalProperty':[{'name':'گارانتی','value':'گارانتی 18 ماهه تست'}],
       'offers':{'@type':'Offer','priceCurrency':'IRR','price':15000000,'availability':'https://schema.org/InStock'}}
    p.update(overrides)
    return '<script type="application/ld+json">'+json.dumps(p)+'</script>'


class LiveSources(unittest.TestCase):
    def test_standard_cookie_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if 'session=ok' not in self.headers.get('Cookie',''):
                    self.send_response(307);self.send_header('Set-Cookie','session=ok; Path=/');self.send_header('Location',self.path);self.end_headers()
                else:
                    self.send_response(200);self.end_headers();self.wfile.write(b'{"status":200}')
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch('dealradar.sources.safe_url',lambda url:url):
                text=fetch(f'http://127.0.0.1:{server.server_port}/')
            self.assertEqual(json.loads(text)['status'],200)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_live_taxonomy_and_warranty_fields(self):
        c=dk_card(product(),NOW)
        self.assertFalse(c.supermarket)
        self.assertEqual(c.warranty_key,'گارانتی 18 ماهه تست')
        self.assertEqual(c.color_name,'سفید')
        self.assertEqual(c.expires_at,'2026-10-09T09:10:00+00:00')
        p=product();p['data_layer']={}
        self.assertTrue(dk_card(p,NOW).supermarket)
        p=product();p['data_layer']['item_category2']='مواد غذایی'
        self.assertTrue(dk_card(p,NOW).supermarket)

    def test_locked_or_unavailable_variant_is_not_stock(self):
        for modification in [{'order_limit':0},{'is_locked_for_digiplus':True}]:
            p=product();p['default_variant']['price'].update(modification)
            self.assertFalse(dk_card(p,NOW).in_stock)

    def test_exact_direct_offer_currency(self):
        c=dk_card(product(),NOW)
        q=equivalent_quote(c,merchant(),'https://shop.example/p','merchant:shop.example',NOW)
        self.assertEqual(q.price_toman,1500000)
        self.assertEqual(q.variant_key,c.variant_key)
        self.assertIsNone(q.shipping_toman)

    def test_ambiguous_variant_warranty_and_model_fail_closed(self):
        c=dk_card(product(),NOW)
        for args in [dict(color='مشکی'),dict(additionalProperty=[]),dict(name='هاب هیسکا مدل HR-53 Pro'),
                     dict(offers={'@type':'AggregateOffer','lowPrice':1,'priceCurrency':'IRT'}),
                     dict(offers={'@type':'Offer','price':1,'availability':'https://schema.org/OutOfStock','priceCurrency':'IRT'})]:
            with self.assertRaises(ValueError):
                equivalent_quote(c,merchant(**args),'https://shop.example/p','merchant:shop.example',NOW)

    def test_text_normalization_preserves_model_numbers(self):
        self.assertEqual(normalized('هيسكا HR-۵۳'),normalized('هیسکا HR-53'))
        self.assertNotEqual(normalized('HR-53'),normalized('HR-530'))

    def test_torob_explicit_shop_link_and_ambiguity(self):
        from dealradar.market import shop_page
        with patch('dealradar.market.fetch_page',side_effect=[('<a href="https://shop.example/p">اینجا</a>','https://api.torob.com/v4/product-page/redirect/'),('product','https://shop.example/p')]):
            self.assertEqual(shop_page('https://api.torob.com/v4/product-page/redirect/'),('product','https://shop.example/p'))
        with patch('dealradar.market.fetch_page',return_value=('<a href="https://one.example/p">a</a><a href="https://two.example/p">b</a>','https://api.torob.com/v4/product-page/redirect/')):
            with self.assertRaises(ValueError):shop_page('https://api.torob.com/v4/product-page/redirect/')

    def test_two_direct_merchants_can_verify_saving(self):
        import pathlib
        import tempfile
        from dealradar.market import collect_market
        from dealradar.core import evaluate
        c=dk_card(product(),NOW)
        search={'results':[{'random_key':'abc','name1':c.title,'name2':''}]}
        detail={'name1':c.title,'price':1500000,'products_info':{'result':[
            {'availability':True,'shop_name':s,'page_url':'https://api.torob.com/v4/product-page/redirect/?shop='+s,'price':1500000}
            for s in ['one','two']]}}
        with tempfile.TemporaryDirectory() as directory, patch('dealradar.market.fetch',side_effect=[json.dumps(search),json.dumps(detail)]), patch('dealradar.market.shop_page',side_effect=[(merchant(),'https://one.example/p'),(merchant(),'https://two.example/p')]),patch('dealradar.market.time.sleep'):
            config={'_base':pathlib.Path(directory),'min_advertised_discount':20,'min_market_sellers':2,'min_saving_percent':15,'max_evidence_age_hours':6,'torob':{'max_candidates':1,'search_results':1,'shops_per_product':2}}
            quotes,errors=collect_market([c],config,NOW)
            self.assertEqual(errors,[])
            self.assertEqual(len(quotes),2)
            row=evaluate(c,quotes,config,NOW)
            self.assertEqual(row['status'],'verified')
            self.assertEqual(row['saving_toman'],500000)

    def test_one_domain_or_unknown_warranty_cannot_verify(self):
        from dealradar.core import evaluate
        c=dk_card(product(),NOW)
        q=equivalent_quote(c,merchant(),'https://one.example/p','seller-one',NOW)
        other=dataclasses.replace(q,seller_id='seller-two')
        config={'min_market_sellers':2,'min_saving_percent':15,'min_advertised_discount':20,'max_evidence_age_hours':6}
        self.assertEqual(evaluate(c,[q,other],config,NOW)['reasons'],['insufficient_independent_sellers'])
        unknown=dataclasses.replace(c,warranty_key='نامشخص')
        self.assertEqual(evaluate(unknown,[q],config,NOW)['reasons'],['unknown_warranty'])

    def test_manufacturer_model_allows_different_title(self):
        c=dataclasses.replace(dk_card(product(),NOW),brand_name='هیسکا')
        html=merchant(name='هاب USB-C هیسکا HR53',mpn='HR-53',brand={'name':'هیسکا'})
        q=equivalent_quote(c,html,'https://shop.example/p','merchant:shop.example',NOW)
        self.assertEqual(q.price_toman,1500000)
        q=equivalent_quote(c,merchant(name='هاب USB-C هیسکا HR53'),'https://shop.example/p','merchant:shop.example',NOW)
        self.assertEqual(q.price_toman,1500000)
        for changes in [dict(mpn='HR-530'),dict(brand={'name':'برند دیگر'}),dict(name='هاب هیسکا HR53 Pro'),dict(name='هاب هیسکا HR53 بسته 2 عددی')]:
            args=dict(name='هاب USB-C هیسکا HR53',mpn='HR-53',brand={'name':'هیسکا'});args.update(changes)
            with self.assertRaises(ValueError):
                equivalent_quote(c,merchant(**args),'https://shop.example/p','merchant:shop.example',NOW)

    def test_scoped_product_table_supplies_warranty(self):
        c=dk_card(product(),NOW)
        table='<table class="woocommerce-product-attributes shop_attributes"><tr><th>گارانتی</th><td>گارانتی 18 ماهه تست</td></tr></table>'
        q=equivalent_quote(c,merchant(additionalProperty=[])+table,'https://shop.example/p','merchant:shop.example',NOW)
        self.assertEqual(q.warranty_key,c.warranty_key)
        with self.assertRaises(ValueError):
            equivalent_quote(c,merchant(additionalProperty=[])+'<footer>گارانتی 18 ماهه تست</footer>','https://shop.example/p','merchant:shop.example',NOW)

    def test_model_suffix_and_conflicting_identifier_rejected(self):
        c=dataclasses.replace(dk_card(product(),NOW),brand_name='ریولینک',title='هدفون ریولینک مدل RV-25 ANC')
        with self.assertRaises(ValueError):
            equivalent_quote(c,merchant(name='هدفون ریولینک RV25 L',brand={'name':'ریولینک'}),'https://shop.example/p','merchant:shop.example',NOW)
        c=dataclasses.replace(dk_card(product(),NOW),brand_name='هیسکا')
        with self.assertRaises(ValueError):
            equivalent_quote(c,merchant(mpn='HR-530'),'https://shop.example/p','merchant:shop.example',NOW)
