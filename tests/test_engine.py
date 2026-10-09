import dataclasses
import datetime as dt
import json
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from dealradar.core import Offer, UTC, History, evaluate, load_config
from dealradar.sources import merchant_quote
from dealradar.telegram import publish
from dealradar.cli import worker, run_once

NOW = dt.datetime(2026, 10, 9, 9, tzinfo=UTC)
C = dict(min_market_sellers=2, max_evidence_age_hours=6, min_saving_percent=15, min_advertised_discount=20)


def offer(**changes):
    row = dict(product_key="dkp:123", title="هدفون", url="https://dk.example/p/123", seller_id="dk",
               price_toman=700, shipping_toman=None, variant_key="black", warranty_key="18m",
               observed_at=NOW.isoformat(), in_stock=True, category="هدفون", supermarket=False,
               discount_percent=50)
    row.update(changes)
    return Offer.parse(row)


def quotes():
    return [offer(seller_id=f"shop{i}", url=f"https://shop{i}.example/p", price_toman=p)
            for i, p in enumerate([1000, 1100])]


class EngineTests(unittest.TestCase):
    def test_compare_against_lowest_not_average(self):
        row = evaluate(offer(), quotes(), C, NOW)
        self.assertEqual((row["status"], row["saving_percent"], row["market_low_toman"]), ("verified", 30, 1000))

    def test_duplicate_seller_is_not_independent(self):
        market = [dataclasses.replace(q, seller_id="same") for q in quotes()]
        self.assertEqual(evaluate(offer(), market, C, NOW)["status"], "unverified")

    def test_variant_and_warranty_must_match(self):
        for field in ["variant_key", "warranty_key", "product_key"]:
            market = [dataclasses.replace(q, **{field: "different"}) for q in quotes()]
            self.assertEqual(evaluate(offer(), market, C, NOW)["status"], "unverified")

    def test_stale_future_expired_out_of_stock_evidence(self):
        for change in [dict(observed_at=(NOW-dt.timedelta(hours=7)).isoformat()),
                       dict(observed_at=(NOW+dt.timedelta(minutes=1)).isoformat()),
                       dict(expires_at=NOW.isoformat()), dict(in_stock=False)]:
            self.assertEqual(evaluate(offer(), [dataclasses.replace(q, **change) for q in quotes()], C, NOW)["status"], "unverified")

    def test_supermarket_is_excluded(self):
        self.assertEqual(evaluate(offer(supermarket=True), quotes(), C, NOW)["status"], "excluded")

    def test_shipping_can_reverse_a_deal(self):
        market = [dataclasses.replace(q, shipping_toman=0) for q in quotes()]
        row = evaluate(offer(shipping_toman=400), market, C, NOW)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["comparison_basis"], "delivered")

    def test_unknown_shipping_never_claims_delivered(self):
        row = evaluate(offer(), quotes(), C, NOW)
        self.assertEqual(row["comparison_basis"], "item_only")

    def test_big_advertised_discount_is_not_sufficient(self):
        self.assertEqual(evaluate(offer(price_toman=1200, discount_percent=90), quotes(), C, NOW)["status"], "rejected")

    def test_invalid_price_and_naive_timestamp_rejected(self):
        for kwargs in [dict(price_toman=float("nan")), dict(price_toman=-10),
                       dict(observed_at="2026-10-09T09:00:00"), dict(url="javascript:alert(1)")]:
            with self.assertRaises(ValueError):
                offer(**kwargs)

    def test_history_and_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(pathlib.Path(directory)/"prices.sqlite")
            row = evaluate(offer(), quotes(), C, NOW)
            self.assertIsNone(history.record(offer()))
            newer = offer(observed_at=(NOW+dt.timedelta(hours=1)).isoformat(), price_toman=600)
            self.assertEqual(history.record(newer), 700)
            self.assertTrue(history.due(row, NOW, 24))
            history.mark(row, NOW)
            self.assertFalse(history.due(row, NOW+dt.timedelta(hours=2), 24))
            self.assertTrue(history.due(row, NOW+dt.timedelta(hours=25), 24))
            history.close()

    def test_merchant_sku_currency_and_stock(self):
        mapping = dataclasses.asdict(offer()) | {"expected_sku": "ABC"}
        document = {'@type':'Product','sku':'ABC','offers':{'@type':'Offer','price':'10000','priceCurrency':'IRR','availability':'https://schema.org/InStock'}}
        page = '<script type="application/ld+json">'+json.dumps(document)+'</script>'
        q = merchant_quote(mapping, page, NOW.isoformat())
        self.assertEqual(q.price_toman, 1000)
        self.assertTrue(q.in_stock)
        with self.assertRaises(ValueError):
            merchant_quote(mapping | {"expected_sku": "WRONG"}, page, NOW.isoformat())

    def test_config_rejects_zero_nan_and_single_seller(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/"c.toml"
            for text in ["interval_hours=0", "interval_hours=nan", "min_market_sellers=1"]:
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load_config(path)
            path.write_text("interval_hours=0.5")
            self.assertEqual(load_config(path)["interval_hours"], 0.5)

    def test_interval_reload_without_restart(self):
        class Stop:
            ticks = 0
            def is_set(self): return self.ticks >= 3
            def wait(self, _):
                self.ticks += 1
                path.write_text("interval_hours=0.01")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/"c.toml"
            path.write_text("interval_hours=6")
            times = iter([0, 40, 80])
            runs = []
            worker(path, stop=Stop(), clock=lambda: next(times), runner=lambda c: runs.append(c["interval_hours"]))
            self.assertEqual(runs, [6, 0.01, 0.01])

    def test_run_produces_reports_and_escapes_html(self):
        with tempfile.TemporaryDirectory() as directory:
            config = C | dict(_base=pathlib.Path(directory),interval_hours=2,max_items=20,timezone="Asia/Tehran",telegram={})
            candidate = offer(title='<script>alert("x")</script>')
            with patch('dealradar.cli.collect', return_value=([candidate], quotes(), [])), patch('dealradar.cli.dt') as mock_dt:
                mock_dt.datetime.now.return_value = NOW
                report = run_once(config, demo=True)
            self.assertEqual(len(report["deals"]), 1)
            page=(pathlib.Path(directory)/'output/index.html').read_text()
            self.assertIn('&lt;script&gt;', page)
            self.assertNotIn('<script>alert', page)
            self.assertTrue((pathlib.Path(directory)/'output/latest.json').exists())

    def test_fabricated_feed_cannot_be_published_via_run(self):
        with tempfile.TemporaryDirectory() as directory:
            config = C | dict(_base=pathlib.Path(directory),interval_hours=6,max_items=20,timezone="Asia/Tehran",telegram={'enabled':True})
            candidate = offer(product_key='demo:123')
            market = [dataclasses.replace(q, product_key='demo:123') for q in quotes()]
            with patch('dealradar.cli.collect',return_value=([candidate],market,[])), patch('dealradar.cli.publish') as sender, patch('dealradar.cli.dt') as mock_dt:
                mock_dt.datetime.now.return_value=NOW
                row=run_once(config)
            self.assertTrue(row['demo'])
            sender.assert_not_called()



if __name__ == '__main__':
    unittest.main()
