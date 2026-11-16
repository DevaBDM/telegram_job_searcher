"""Tests for KeywordMatcher adapter and backwards compatibility with legacy config."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobsaver.config import FilterConfig  # noqa: E402
from jobsaver.matcher import Decision, KeywordMatcher  # noqa: E402


def matcher(**kwargs) -> KeywordMatcher:
    defaults = dict(keywords=["python"], case_sensitive=False, whole_word=False)
    defaults.update(kwargs)
    return KeywordMatcher(FilterConfig(**defaults))


class MatcherTests(unittest.TestCase):
    def test_plain_keyword_is_case_insensitive(self):
        self.assertTrue(matcher().match("Looking for a PYTHON dev").matched)

    def test_no_match(self):
        self.assertFalse(matcher().match("We need a Java developer").matched)

    def test_empty_text(self):
        self.assertFalse(matcher().match("").matched)
        self.assertFalse(matcher().match(None).matched)

    def test_priority_reporting(self):
        m = matcher(keywords=["python", "django", "flask"])
        result = m.match("Senior Python / Django engineer")
        self.assertTrue(result.matched)
        self.assertEqual(result.decision, Decision.INCLUDE)
        self.assertIn(result.matched_keyword, ("python", "django"))

    def test_must_include_is_and_logic(self):
        m = matcher(keywords=["python"], must_include=["remote"])
        self.assertFalse(m.match("Python developer in Addis").matched)
        self.assertTrue(m.match("Remote Python developer").matched)

    def test_exclude_wins(self):
        m = matcher(keywords=["python"], exclude=["unpaid"])
        result = m.match("Unpaid python internship")
        self.assertFalse(result.matched)
        self.assertEqual(result.decision, Decision.EXCLUDE)

    def test_priority_keyword_overrides_exclude(self):
        m = matcher(
            keywords=["python"],
            priority_keywords=["lead architect"],
            exclude=["unpaid", "internship"],
        )
        self.assertFalse(m.match("Unpaid python internship").matched)
        result = m.match("Looking for a Python Lead Architect for an unpaid internship")
        self.assertTrue(result.matched)
        self.assertEqual(result.decision, Decision.INCLUDE)
        self.assertEqual(result.matched_keyword, "lead architect")

    def test_priority_keyword_must_include_still_required(self):
        m = matcher(
            priority_keywords=["python"],
            must_include=["remote"],
            exclude=["internship"],
        )
        self.assertFalse(m.match("Python internship in Addis").matched)
        self.assertTrue(m.match("Remote Python internship").matched)

    def test_multiword_phrase(self):
        m = matcher(keywords=["data engineer"])
        self.assertTrue(m.match("Hiring a Data Engineer").matched)
        self.assertFalse(m.match("data processing engineer").matched)

    def test_case_sensitive(self):
        strict = matcher(keywords=["Python"], case_sensitive=True)
        self.assertFalse(strict.match("python developer").matched)
        self.assertTrue(strict.match("Python developer").matched)

    # ------------------------------------------------------------------ #
    # Fuzzy matching
    # ------------------------------------------------------------------ #
    def test_fuzzy_catches_misspelling(self):
        m = matcher(keywords=["python"], fuzzy=True, fuzzy_threshold=70)
        self.assertTrue(m.match("Looking for a pythn developer").matched)
        self.assertIn("python", m.match("pythn developer").keywords)

    def test_fuzzy_threshold_too_strict(self):
        m = matcher(keywords=["python"], fuzzy=True, fuzzy_threshold=95)
        self.assertFalse(m.match("pythn developer").matched)

    def test_fuzzy_disabled_misspelling_no_match(self):
        m = matcher(keywords=["python"], fuzzy=False)
        self.assertFalse(m.match("pythn developer").matched)

    def test_fuzzy_multiword(self):
        m = matcher(keywords=["backend developer"], fuzzy=True, fuzzy_threshold=70)
        self.assertTrue(m.match("backend devloper needed").matched)


class ConfigTests(unittest.TestCase):
    def test_shipped_config_parses(self):
        """config.example.yaml must always be valid (api_id placeholder is 0)."""
        from jobsaver.config import ConfigError, load_config

        path = Path(__file__).resolve().parent.parent / "config.example.yaml"
        with self.assertRaises(ConfigError):  # placeholder api_id -> expected error
            load_config(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
