import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('actions_run',Path(__file__).resolve().parents[1]/'scripts/actions_run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ActionsTests(unittest.TestCase):
    def test_agent_market_outage_fails_visibly_without_publishing_unverified_items(self):
        report = dict(agent=True, deals=[], flash_deals=[], digest_deals=[], errors=['Torob unavailable'],
                      candidate_count=1, market_coverage={'provider_status':'unavailable'})
        with patch.dict('os.environ', {'RADAR_DRY_RUN':'true'}, clear=True), \
             patch.object(runner, 'load_config', return_value={}), \
             patch.object(runner, 'run_once', return_value=report), \
             self.assertRaisesRegex(SystemExit, 'Market verification unavailable'):
            runner.main()

    def test_manual_dispatch_has_its_own_delivery_identifier(self):
        c, dry = runner.configure({}, {'RADAR_DRY_RUN':'false', 'TELEGRAM_BOT_TOKEN':'test', 'TELEGRAM_CHAT_ID':'test', 'DEALRADAR_MANUAL_RUN_ID':'123-2'})
        self.assertFalse(dry)
        self.assertEqual(c['telegram']['manual_run_id'], '123-2')
        c, _ = runner.configure({}, {'RADAR_DRY_RUN':'true'})
        self.assertNotIn('manual_run_id', c['telegram'])

    def test_real_publish_requires_secrets(self):
        with self.assertRaises(ValueError):
            runner.configure({}, {})

    def test_preview_does_not_require_credentials(self):
        c, dry = runner.configure({}, {'RADAR_DRY_RUN':'true'})
        self.assertTrue(dry)
        self.assertEqual(c['database'],'data/history.sqlite3')

    def test_feed_override_disables_experimental_digikala_adapter(self):
        c, _ = runner.configure({}, {'RADAR_DRY_RUN':'true','CANDIDATE_FEED_URL':'https://feed.example/candidates','MARKET_FEED_URL':'https://feed.example/market'})
        self.assertFalse(c['digikala']['enabled'])
        self.assertEqual(c['feeds']['market'],['https://feed.example/market'])

    def test_non_https_feed_rejected(self):
        with self.assertRaises(ValueError):
            runner.configure({}, {'RADAR_DRY_RUN':'true','MARKET_FEED_URL':'http://feed.example/market'})


if __name__ == '__main__':
    unittest.main()
