"""Date/time parsing and scan-window configuration tests (no Telegram needed)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobsaver.config import (  # noqa: E402
    ConfigError,
    load_config,
    parse_timezone,
    parse_when,
)

UTC = timezone.utc


class ParseWhenTests(unittest.TestCase):
    def test_iso_date(self):
        self.assertEqual(parse_when("2026-11-14", UTC, "x"), datetime(2026, 11, 14, tzinfo=UTC))

    def test_date_and_time_with_space(self):
        self.assertEqual(
            parse_when("2026-11-14 15:34:45", UTC, "x"),
            datetime(2026, 11, 14, 15, 34, 45, tzinfo=UTC),
        )

    def test_iso_t_separator(self):
        self.assertEqual(
            parse_when("2026-11-14T15:34:45", UTC, "x"),
            datetime(2026, 11, 14, 15, 34, 45, tzinfo=UTC),
        )

    def test_explicit_offset_is_kept(self):
        parsed = parse_when("2026-11-14T15:34:45+03:00", UTC, "x")
        self.assertEqual(parsed, datetime(2026, 11, 14, 15, 34, 45, tzinfo=timezone(timedelta(hours=3))))
        self.assertEqual(parsed.astimezone(UTC).hour, 12)

    def test_other_common_layouts(self):
        self.assertEqual(parse_when("14.11.2026", UTC, "x"), datetime(2026, 11, 14, tzinfo=UTC))
        self.assertEqual(
            parse_when("14/11/2026 08:05", UTC, "x"),
            datetime(2026, 11, 14, 8, 5, tzinfo=UTC),
        )

    def test_naive_values_get_the_configured_zone(self):
        parsed = parse_when("2026-11-14 00:00:00", UTC, "x")
        self.assertIsNotNone(parsed.tzinfo)

    def test_keywords(self):
        now = parse_when("now", UTC, "x")
        self.assertIsNotNone(now)
        self.assertLess(abs((datetime.now(UTC) - now).total_seconds()), 5)
        self.assertIsNotNone(parse_when("yesterday", UTC, "x"))

    def test_empty_values_mean_unset(self):
        for value in (None, "", "   ", "null", "None"):
            self.assertIsNone(parse_when(value, UTC, "x"))

    def test_datetime_passthrough(self):
        value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        self.assertEqual(parse_when(value, UTC, "x"), value)

    def test_invalid_value_raises(self):
        with self.assertRaises(ConfigError):
            parse_when("next tuesday", UTC, "scan.from_date")


class TimezoneTests(unittest.TestCase):
    def test_utc_aliases_work_without_tzdata(self):
        for name in ("UTC", "utc", "GMT", "Z", ""):
            self.assertEqual(parse_timezone(name), timezone.utc)

    def test_unknown_zone_raises(self):
        with self.assertRaises(ConfigError):
            parse_timezone("Mars/Olympus_Mons")


SAMPLE = """
telegram:
  api_id: 123456
  api_hash: "0123456789abcdef0123456789abcdef"
sources: ["@python_jobs"]
filters:
  rules: ["python"]
scan:
  from_date: "2026-11-01 08:00:00"
  to_date: "2026-11-16 23:59:59"
  resume: false
  mark_as_read: true
  timezone: "UTC"
  max_history_messages: 500
"""


class ScanConfigTests(unittest.TestCase):
    def _load(self, text: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_scan_section_is_parsed(self):
        config = self._load(SAMPLE)
        self.assertEqual(config.scan.from_date, datetime(2026, 11, 1, 8, 0, tzinfo=UTC))
        self.assertEqual(config.scan.to_date, datetime(2026, 11, 16, 23, 59, 59, tzinfo=UTC))
        self.assertFalse(config.scan.resume)
        self.assertTrue(config.scan.mark_as_read)
        self.assertEqual(config.scan.max_history_messages, 500)
        self.assertEqual(config.scan.timezone_name, "UTC")

    def test_scan_defaults_are_safe(self):
        text = SAMPLE.split("scan:")[0]
        config = self._load(text)
        self.assertIsNone(config.scan.from_date)
        self.assertIsNone(config.scan.to_date)
        self.assertTrue(config.scan.resume)
        self.assertFalse(config.scan.mark_as_read)  # leaves messages unread

    def test_reversed_range_is_rejected(self):
        text = SAMPLE.replace('from_date: "2026-11-01 08:00:00"', 'from_date: "2026-12-01"')
        with self.assertRaises(ConfigError):
            self._load(text)

    def test_bad_date_is_rejected(self):
        text = SAMPLE.replace('from_date: "2026-11-01 08:00:00"', 'from_date: "soon"')
        with self.assertRaises(ConfigError):
            self._load(text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
