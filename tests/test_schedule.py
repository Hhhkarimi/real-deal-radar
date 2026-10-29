import datetime as dt
import pathlib
import re
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from dealradar.cli import run_once, worker
from dealradar.hunter_delivery import publish
from dealradar.schedule import monitoring_allowed

CONFIG = dict(interval_hours=.25, timezone="Asia/Tehran", monitoring_start_hour=8, monitoring_end_hour=22,
              agent={"enabled": True})


def cron_field(field, value):
    for part in field.split(","):
        if part.startswith("*/") and value % int(part[2:]) == 0:
            return True
        if "-" in part:
            low, high = map(int, part.split("-"))
            if low <= value <= high:
                return True
        elif part.isdigit() and int(part) == value:
            return True
    return False


class ScheduleTests(unittest.TestCase):
    def test_tehran_open_and_close_boundaries(self):
        for utc_time, expected in [("04:29:59", False), ("04:30:00", True),
                                   ("18:29:59", True), ("18:30:00", False),
                                   ("20:30:00", False), ("00:00:00", False)]:
            with self.subTest(time=utc_time):
                now = dt.datetime.fromisoformat("2026-10-30T" + utc_time + "+00:00")
                self.assertEqual(monitoring_allowed(CONFIG, now), expected)

    def test_actual_workflow_crons_cover_only_the_daytime_quarter_hours(self):
        path = pathlib.Path(__file__).parents[1]/".github/workflows/channel.yml"
        expressions = re.findall(r"cron: '([^']+)'", path.read_text(encoding="utf-8"))
        self.assertEqual(len(expressions), 3)
        for date in ("2026-10-30", "2027-01-01", "2027-07-01"):
            fired = []
            midnight = dt.datetime.fromisoformat(date + "T00:00:00+00:00")
            for offset in range(1440):
                now = midnight + dt.timedelta(minutes=offset)
                matches = sum(cron_field(expr.split()[0], now.minute) and cron_field(expr.split()[1], now.hour)
                              for expr in expressions)
                local = now.astimezone(ZoneInfo("Asia/Tehran"))
                expected = 8 <= local.hour < 22 and local.minute % 15 == 0
                self.assertEqual(matches, int(expected), f"Unexpected schedule at {local}")
                if matches:
                    fired.append(local)
            self.assertEqual(len(fired), 56)
            self.assertEqual(fired[0].strftime("%H:%M"), "08:00")
            self.assertEqual(fired[-1].strftime("%H:%M"), "21:45")

    def test_late_run_is_skipped_before_source_access(self):
        with patch("dealradar.cli.monitoring_allowed", return_value=False), \
             patch("dealradar.cli.collect") as collect, \
             patch("dealradar.hunter.run_once") as hunt:
            report = run_once(CONFIG)
        collect.assert_not_called()
        hunt.assert_not_called()
        self.assertEqual(report["telegram_delivery"]["reason"], "outside_monitoring_hours")
        self.assertFalse(report["errors"])

    def test_worker_pauses_at_night_and_resumes_without_consuming_an_interval(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        runner = Mock()
        with patch("dealradar.cli.load_config", return_value=CONFIG), \
             patch("dealradar.cli.monitoring_allowed", side_effect=[False, True]):
            worker("config.toml", stop=stop, clock=iter([0, 10]).__next__, runner=runner)
        runner.assert_called_once_with(CONFIG)

    def test_a_cycle_that_finishes_at_night_cannot_publish(self):
        memory = Mock()
        report = {"flash_deals": [{"candidate": {}}], "digest_deals": []}
        with patch("dealradar.hunter_delivery.monitoring_allowed", return_value=False):
            self.assertEqual(publish(report, memory, CONFIG), [])
        memory.start.assert_not_called()
        self.assertEqual(report["telegram_delivery"]["reason"], "outside_monitoring_hours")

    def test_legacy_configs_without_hours_remain_unrestricted(self):
        now = dt.datetime(2026, 10, 30, 0, tzinfo=dt.timezone.utc)
        self.assertTrue(monitoring_allowed({}, now))

    def test_invalid_hours_and_naive_times_are_rejected(self):
        for start, end in [(True, 22), (22, 8), (8, 25), (-1, 22)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                monitoring_allowed(dict(CONFIG, monitoring_start_hour=start, monitoring_end_hour=end))
        with self.assertRaises(ValueError):
            monitoring_allowed(CONFIG, dt.datetime(2026, 10, 30))


if __name__ == "__main__":
    unittest.main()
