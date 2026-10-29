import dataclasses
import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from dealradar.core import History, Offer, UTC, evaluate, load_config
from dealradar.hunter import HuntMemory, qualify, record_price, run_once, select, settings
from dealradar.hunter_delivery import message, publish
from dealradar.telegram import visible_length

NOW = dt.datetime(2026, 10, 29, 10, tzinfo=UTC)
CONFIG = dict(interval_hours=.25, min_advertised_discount=20, min_saving_percent=15,
              min_market_sellers=3, max_evidence_age_hours=6,
              agent={"enabled": True}, timezone="Asia/Tehran")


def offer(**changes):
    values = dict(product_key="dkp:1", title="هدفون مدل ABC123", url="https://www.digikala.com/product/dkp-1/",
                  seller_id="dk:1", price_toman=700000, shipping_toman=None, variant_key="black",
                  warranty_key="18m", observed_at=NOW.isoformat(), in_stock=True, category="هدفون",
                  supermarket=False, discount_percent=0)
    return Offer.parse(dict(values, **changes))


def row(**changes):
    c = offer(**changes)
    market = [dataclasses.replace(c, seller_id=f"shop:{i}", url=f"https://shop{i}.example/p", price_toman=1000000 + i*10000)
              for i in range(3)]
    return evaluate(c, market, dict(CONFIG, min_advertised_discount=0), NOW)


def deal(**changes):
    return qualify(row(**changes), CONFIG, NOW)[0]


class HunterTests(unittest.TestCase):
    def setUp(self):
        self.history = History(":memory:")
        self.memory = HuntMemory(self.history, "channel")

    def tearDown(self):
        self.history.close()

    def test_real_saving_without_a_discount_sticker_is_a_flash_deal(self):
        r = deal()
        self.assertEqual(r["hunt_tier"], "flash")
        self.assertEqual(r["saving_percent"], 30)

    def test_large_sticker_discount_with_no_market_advantage_is_rejected(self):
        self.assertIsNone(deal(price_toman=1100000, discount_percent=90))

    def test_unconfirmed_partial_and_stale_market_evidence_stay_silent(self):
        for comparison in [{"confidence": "none"}, {"confidence": "partial"},
                           {"confidence": "high", "fetched_at": (NOW-dt.timedelta(minutes=21)).isoformat()}]:
            with self.subTest(comparison=comparison):
                r = row(); r["price_comparison"] = comparison
                self.assertIsNone(qualify(r, CONFIG, NOW)[0])

    def test_mcp_evidence_is_recomputed_and_requires_distinct_sellers_and_domains(self):
        r = row()
        r["price_comparison"] = dict(confidence="high", fetched_at=NOW.isoformat(), saving_percent=99,
            evidence=[dict(shop_id=i, url=f"https://shop{i}.example/p", price_toman=1000000) for i in range(3)])
        self.assertEqual(qualify(r, CONFIG, NOW)[0]["saving_percent"], 30)
        r["price_comparison"]["evidence"][2]["shop_id"] = 0
        self.assertIsNone(qualify(r, CONFIG, NOW)[0])
        for q in r["price_comparison"]["evidence"]:
            q["url"] = "https://one-shop.example/p"
        self.assertIsNone(qualify(r, CONFIG, NOW)[0])

    def test_invalid_stock_expiry_price_and_unknown_warranty_never_qualify(self):
        for changes in [dict(in_stock=False), dict(supermarket=True), dict(price_toman=0),
                        dict(expires_at=NOW.isoformat()), dict(warranty_key="unknown")]:
            with self.subTest(changes=changes):
                self.assertIsNone(deal(**changes))

    def test_digikala_domain_alias_cannot_count_as_an_independent_market_seller(self):
        r = row()
        r["evidence"][0]["url"] = "https://digikala.com/product/dkp-1/"
        self.assertIsNone(qualify(r, CONFIG, NOW)[0])

    def test_percentage_and_absolute_saving_both_matter(self):
        self.assertEqual(deal(price_toman=800000)["hunt_tier"], "digest")
        r = row(price_toman=700)
        for q in r["evidence"]:
            q["price_toman"] = 1000
        self.assertIsNone(qualify(r, CONFIG, NOW)[0])

    def test_digest_is_bounded_and_varied_without_filler(self):
        rows = [deal(product_key=f"dkp:{i}", price_toman=800000, category=f"type:{i%3}") for i in range(10)]
        chosen = select(rows, self.memory, CONFIG, NOW)
        self.assertEqual(len(chosen["digest"]), 3)
        self.assertEqual(len({r["candidate"]["category"] for r in chosen["digest"]}), 3)
        self.assertEqual(select([], self.memory, CONFIG, NOW), dict(flash=[], digest=[]))

    def test_flash_bypasses_digest_clock_but_obeys_daily_limit(self):
        self.memory.confirm([deal(product_key="old")], NOW, "digest", 1)
        chosen = select([deal()], self.memory, CONFIG, NOW + dt.timedelta(minutes=15))
        self.assertEqual(len(chosen["flash"]), 1)
        self.assertFalse(chosen["digest"])
        for i in range(4):
            self.memory.confirm([deal(product_key=f"oldflash:{i}")], NOW, "flash", 2+i)
        self.assertFalse(select([deal()], self.memory, CONFIG, NOW)["flash"])

    def test_changed_color_and_one_percent_price_change_do_not_repeat_a_product(self):
        self.memory.confirm([deal()], NOW, "flash", 1)
        cfg = settings(CONFIG)
        for changes in [dict(variant_key="white"), dict(price_toman=699000), dict(price_toman=710000)]:
            self.assertFalse(self.memory.due(deal(**changes), NOW+dt.timedelta(days=1), cfg))
        self.assertTrue(self.memory.due(deal(), NOW+dt.timedelta(days=7), cfg))
        self.assertTrue(self.memory.due(deal(price_toman=600000), NOW+dt.timedelta(hours=6), cfg))
        self.assertFalse(self.memory.due(deal(price_toman=600000), NOW+dt.timedelta(hours=1), cfg))

    def test_delivery_dedup_survives_restart_and_is_scoped_to_channel(self):
        self.memory.confirm([deal()], NOW, "digest", 1)
        restarted = HuntMemory(self.history, "channel")
        self.assertFalse(restarted.due(deal(), NOW+dt.timedelta(hours=12), settings(CONFIG)))
        self.assertTrue(HuntMemory(self.history, "another").due(deal(), NOW, settings(CONFIG)))

    def test_ambiguous_attempts_reserve_the_flash_budget_and_digest_clock(self):
        self.memory.start([deal()], NOW, "flash")
        self.assertEqual(self.memory.flash_count(NOW, "Asia/Tehran"), 1)
        self.memory.start([deal(product_key="dkp:2")], NOW, "digest")
        self.assertFalse(self.memory.digest_due(NOW+dt.timedelta(hours=1), 12))
        self.assertTrue(self.memory.digest_due(NOW+dt.timedelta(hours=12), 12))

    def test_digest_reopens_after_twelve_hours_without_repeating_flashes(self):
        self.memory.confirm([deal()], NOW, "flash", 1)
        self.memory.confirm([deal(product_key="dkp:2")], NOW, "digest", 2)
        fresh = deal(product_key="dkp:3", price_toman=800000)
        self.assertFalse(select([fresh], self.memory, CONFIG, NOW+dt.timedelta(hours=11))["digest"])
        self.assertEqual(len(select([fresh], self.memory, CONFIG, NOW+dt.timedelta(hours=12))["digest"]), 1)

    def test_lookup_budget_rotates_and_prioritizes_new_products_and_price_drops(self):
        first = row(); first["price_comparison"] = {"reason": "no_matching_model"}
        self.memory.checked([first], NOW)
        second = row(product_key="dkp:2")
        self.assertIs(self.memory.prioritize([first, second])[0], second)
        second["price_comparison"] = {"reason": "no_matching_model"}
        self.memory.checked([second], NOW+dt.timedelta(minutes=1))
        self.assertIs(self.memory.prioritize([second, first])[0], first)
        second["candidate"]["price_toman"] = 600000
        self.assertIs(self.memory.prioritize([first, second])[0], second)

    def test_unchanged_prices_are_recorded_daily_instead_of_every_poll(self):
        record_price(self.history, offer())
        record_price(self.history, offer(observed_at=(NOW+dt.timedelta(minutes=15)).isoformat()))
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 1)
        record_price(self.history, offer(price_toman=650000, observed_at=(NOW+dt.timedelta(minutes=30)).isoformat()))
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 2)

    def test_invalid_configuration_is_rejected(self):
        for changes in [dict(digest_items=True), dict(digest_items=99), dict(digest_hours=0),
                        dict(repeat_days=float("nan")), dict(min_comparable_shops=2),
                        dict(flash_saving_percent=10), dict(flash_saving_toman=1)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings(dict(CONFIG, agent=changes))

    def test_default_config_enables_small_frequent_hunts(self):
        config = load_config(pathlib.Path(__file__).parents[1]/"config.toml")
        self.assertEqual(config["interval_hours"], .25)
        self.assertTrue(config["agent"]["enabled"])
        self.assertFalse(config["bestsellers"]["enabled"])

    def test_empty_run_never_invokes_telegram(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(CONFIG, _base=pathlib.Path(directory))
            with patch("dealradar.hunter.collect", return_value=([], [], ["Torob unavailable"])), \
                 patch("dealradar.hunter_delivery.urllib.request.urlopen") as network:
                report = run_once(config)
            self.assertEqual(report["telegram_delivery"]["reason"], "no_new_deals")
            network.assert_not_called()

    def test_revalidation_drops_expired_and_worsened_offers_before_delivery(self):
        now = dt.datetime.now(UTC)
        c = offer(observed_at=now.isoformat())
        market = [dataclasses.replace(c, price_toman=1000000, seller_id=f"shop:{i}", url=f"https://shop{i}.example/p") for i in range(3)]
        for renewed in [dataclasses.replace(c, in_stock=False), dataclasses.replace(c, price_toman=1100000)]:
            with self.subTest(renewed=renewed), tempfile.TemporaryDirectory() as directory:
                config = dict(CONFIG, _base=pathlib.Path(directory), telegram={"enabled": True})
                with patch("dealradar.hunter.collect", return_value=([c], market, [])), \
                     patch("dealradar.hunter.recheck", return_value=dict(row(), candidate=dataclasses.asdict(renewed),
                           evidence=[dataclasses.asdict(q) for q in market])):
                    report = run_once(config, send=False)
                self.assertFalse(report["deals"])

    def test_candidate_feeds_run_through_the_cli_and_save_a_verified_preview(self):
        from dealradar.cli import run_once as cli_run
        now = dt.datetime.now(UTC)
        c = offer(observed_at=now.isoformat())
        quotes = [dataclasses.replace(c, seller_id=f"shop:{i}", url=f"https://shop{i}.example/p", price_toman=1000000)
                  for i in range(3)]
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            (base/"candidates.json").write_text(json.dumps([dataclasses.asdict(c)]), encoding="utf-8")
            (base/"market.json").write_text(json.dumps([dataclasses.asdict(q) for q in quotes]), encoding="utf-8")
            config = dict(CONFIG, _base=base, feeds=dict(candidates=["candidates.json"], market=["market.json"]))
            report = cli_run(config, send=False)
            self.assertEqual(len(report["flash_deals"]), 1)
            self.assertEqual(report["telegram_delivery"]["status"], "preview")
            self.assertFalse(report["advertised_offers"])
            self.assertEqual(json.loads((base/"output/latest.json").read_text(encoding="utf-8"))["deals"][0]["saving_percent"], 30)
            history = History(base/"data/history.sqlite3")
            self.assertFalse(history.db.execute("SELECT 1 FROM hunt_deliveries").fetchone())
            history.close()

    def test_price_increase_downgrades_a_flash_and_updates_its_comparison(self):
        r = row()
        r["candidate"]["price_toman"] = 800000
        r["price_comparison"] = dict(confidence="high", fetched_at=NOW.isoformat(), saving_percent=30,
            evidence=[dict(shop_id=i, url=f"https://shop{i}.example/p", price_toman=1000000) for i in range(3)])
        chosen = qualify(r, CONFIG, NOW)[0]
        self.assertEqual(chosen["hunt_tier"], "digest")
        self.assertEqual(chosen["price_comparison"]["saving_percent"], 20)

    def test_old_showcase_receipt_is_respected_during_migration(self):
        from dealradar.showcase import item_key, mark_item
        mark_item(self.history, "channel", item_key(deal()["candidate"]), 700000, NOW.isoformat())
        self.assertFalse(self.memory.due(deal(), NOW+dt.timedelta(days=1), settings(CONFIG)))

    def test_message_is_bounded_and_discloses_scope_and_shipping(self):
        rows = [deal(product_key=str(i), title="<عنوان> "*100, warranty_key="<گارانتی> "*100) for i in range(5)]
        # A long warranty still matches the same concrete comparison evidence.
        text = message(rows, "digest")
        self.assertLess(visible_length(text), 4000)
        self.assertIn("&lt;", text)
        self.assertIn("هزینهٔ ارسال", text)
        self.assertIn("منابع بررسی‌شده", text)


class HunterDeliveryTests(unittest.TestCase):
    setUp = HunterTests.setUp
    tearDown = HunterTests.tearDown

    def report(self):
        return dict(flash_deals=[deal()], digest_deals=[])

    def test_confirmed_send_is_remembered_and_does_not_need_a_summary_post(self):
        calls = []
        def opener(req, timeout):
            calls.append(json.loads(req.data))
            return io.BytesIO(b'{"ok":true,"result":{"message_id":123}}')
        with patch.dict("os.environ", TELEGRAM_BOT_TOKEN="secret", TELEGRAM_CHAT_ID="channel"):
            report = self.report()
            self.assertEqual(publish(report, self.memory, dict(CONFIG, telegram={"enabled": True}), opener=opener), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["telegram_delivery"]["sent"], 1)
        self.assertEqual(self.memory.flash_count(dt.datetime.now(UTC), "Asia/Tehran"), 1)

    def test_timeout_is_not_retried_and_quarantines_the_product(self):
        def opener(req, timeout):
            raise TimeoutError("secret-bearing transport message")
        with patch.dict("os.environ", TELEGRAM_BOT_TOKEN="secret", TELEGRAM_CHAT_ID="channel"):
            report = self.report()
            errors = publish(report, self.memory, dict(CONFIG, telegram={"enabled": True}), opener=opener)
        self.assertNotIn("secret", " ".join(errors))
        self.assertFalse(self.memory.due(deal(), dt.datetime.now(UTC), settings(CONFIG)))
        self.assertFalse(self.history.db.execute("SELECT 1 FROM hunt_deliveries").fetchone())

    def test_explicit_429_can_retry_and_success_is_confirmed_once(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "limited", {}, io.BytesIO(b'{"parameters":{"retry_after":0}}'))
            return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        with patch.dict("os.environ", TELEGRAM_BOT_TOKEN="secret", TELEGRAM_CHAT_ID="channel"):
            errors = publish(self.report(), self.memory, dict(CONFIG, telegram={"enabled": True}), opener=opener, sleep=lambda _:None)
        self.assertFalse(errors)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM hunt_deliveries").fetchone()[0], 1)

    def test_invalid_retry_delay_is_reported_as_failed_delivery(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, "limited", {}, io.BytesIO(b'{"parameters":{"retry_after":999}}'))
        with patch.dict("os.environ", TELEGRAM_BOT_TOKEN="secret", TELEGRAM_CHAT_ID="channel"):
            report = self.report()
            errors = publish(report, self.memory, dict(CONFIG, telegram={"enabled": True}), opener=opener)
        self.assertTrue(errors)
        self.assertEqual(report["telegram_delivery"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
