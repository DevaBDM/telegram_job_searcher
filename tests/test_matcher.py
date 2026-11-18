"""Tests for the ordered rule matcher (substring, whole-word, regex, fuzzy)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobsaver.config import ConfigError, FilterConfig  # noqa: E402
from jobsaver.matcher import Decision, KeywordMatcher  # noqa: E402


def matcher(rules=("python",), **kwargs) -> KeywordMatcher:
    return KeywordMatcher(FilterConfig(rules=list(rules), **kwargs))


class MatcherTests(unittest.TestCase):
    def test_plain_rule_is_case_insensitive(self):
        self.assertTrue(matcher().match("Looking for a PYTHON dev").matched)

    def test_no_match(self):
        self.assertFalse(matcher().match("We need a Java developer").matched)

    def test_empty_text(self):
        self.assertFalse(matcher().match("").matched)
        self.assertFalse(matcher().match(None).matched)

    def test_priority_reporting(self):
        m = matcher(["python", "django", "flask"])
        result = m.match("Senior Python / Django engineer")
        self.assertTrue(result.matched)
        self.assertEqual(result.decision, Decision.INCLUDE)
        self.assertEqual(result.matched_keyword, "python")
        self.assertEqual(result.matched_rule_index, 0)

    def test_must_include_is_and_logic(self):
        m = matcher(["python"], must_include=["remote"])
        self.assertFalse(m.match("Python developer in Addis").matched)
        self.assertTrue(m.match("Remote Python developer").matched)

    def test_exclude_wins_when_it_comes_first(self):
        m = matcher(["!unpaid", "python"])
        result = m.match("Unpaid python internship")
        self.assertFalse(result.matched)
        self.assertEqual(result.decision, Decision.EXCLUDE)
        self.assertEqual(result.excluded_by, "!unpaid")

    def test_priority_rule_overrides_exclude(self):
        m = matcher(["lead architect", "!unpaid", "!internship", "python"])
        self.assertFalse(m.match("Unpaid python internship").matched)
        result = m.match("Looking for a Python Lead Architect for an unpaid internship")
        self.assertTrue(result.matched)
        self.assertEqual(result.decision, Decision.INCLUDE)
        self.assertEqual(result.matched_keyword, "lead architect")

    def test_priority_rule_must_include_still_required(self):
        m = matcher(["python", "!internship"], must_include=["remote"])
        self.assertFalse(m.match("Python internship in Addis").matched)
        self.assertTrue(m.match("Remote Python internship").matched)

    def test_multiword_phrase(self):
        m = matcher(["data engineer"])
        self.assertTrue(m.match("Hiring a Data Engineer").matched)
        self.assertFalse(m.match("data processing engineer").matched)

    def test_case_sensitive(self):
        strict = matcher(["Python"], case_sensitive=True)
        self.assertFalse(strict.match("python developer").matched)
        self.assertTrue(strict.match("Python developer").matched)

    def test_duplicate_first_occurrence_wins(self):
        m = matcher(["python", "!python"])
        self.assertTrue(m.match("python developer").matched)

    # ------------------------------------------------------------------ #
    # Whole-word rules (no fuzzy)
    # ------------------------------------------------------------------ #
    def test_word_rule_matches_standalone_word(self):
        m = matcher(["w:IT"])
        self.assertTrue(m.match("IT support role").matched)
        self.assertTrue(m.match("Hiring an it officer").matched)

    def test_word_rule_ignores_substrings(self):
        m = matcher(["w:IT"])
        for text in (
            "We offer a competitive position with great benefits",
            "Edit documents and quality assurance",
            "This unit has no training",
        ):
            self.assertFalse(m.match(text).matched, text)

    def test_word_rule_multiword_uses_boundaries(self):
        m = matcher(["w:IT Officer"])
        self.assertTrue(m.match("Hiring an IT Officer now").matched)
        self.assertFalse(m.match("Hiring IT Officers now").matched)

    def test_word_rule_handles_punctuation_target(self):
        m = matcher(["w:C++"])
        self.assertTrue(m.match("C++ developer wanted").matched)
        self.assertTrue(m.match("We use C++, Python and Go").matched)

    def test_word_rule_is_never_fuzzy(self):
        m = matcher(["w:python"], fuzzy=True, fuzzy_threshold=70)
        self.assertFalse(m.match("Looking for a pythn developer").matched)
        self.assertTrue(m.match("Looking for a python developer").matched)

    def test_word_exclusion(self):
        m = matcher(["!w:IT", "python"])
        self.assertFalse(m.match("IT python role").matched)
        self.assertTrue(m.match("Python role with great benefits").matched)

    # ------------------------------------------------------------------ #
    # Regex rules (no fuzzy)
    # ------------------------------------------------------------------ #
    def test_regex_rule(self):
        m = matcher([r"re:\b(?:api|rest)\b"])
        self.assertTrue(m.match("REST and API services").matched)
        self.assertFalse(m.match("rapid application development").matched)

    def test_regex_exclusion(self):
        m = matcher([r"!re:\bintern(ship)?\b", "python"])
        self.assertFalse(m.match("python intern").matched)
        self.assertFalse(m.match("python internship").matched)
        self.assertTrue(m.match("python engineer").matched)

    def test_regex_is_never_fuzzy(self):
        m = matcher([r"re:\bpython\b"], fuzzy=True, fuzzy_threshold=70)
        self.assertFalse(m.match("pythn developer").matched)

    def test_invalid_regex_raises(self):
        with self.assertRaises(ConfigError):
            matcher([r"re:(unclosed"])

    # ------------------------------------------------------------------ #
    # Fuzzy matching (plain substring rules only)
    # ------------------------------------------------------------------ #
    def test_fuzzy_catches_misspelling(self):
        m = matcher(["python"], fuzzy=True, fuzzy_threshold=70)
        self.assertTrue(m.match("Looking for a pythn developer").matched)
        self.assertIn("python", m.match("pythn developer").keywords)

    def test_fuzzy_threshold_too_strict(self):
        m = matcher(["python"], fuzzy=True, fuzzy_threshold=95)
        self.assertFalse(m.match("pythn developer").matched)

    def test_fuzzy_disabled_misspelling_no_match(self):
        m = matcher(["python"], fuzzy=False)
        self.assertFalse(m.match("pythn developer").matched)

    def test_fuzzy_multiword(self):
        m = matcher(["backend developer"], fuzzy=True, fuzzy_threshold=70)
        self.assertTrue(m.match("backend devloper needed").matched)


class ConfigTests(unittest.TestCase):
    def test_shipped_config_parses(self):
        """config.example.yaml must always be valid (api_id placeholder is 0)."""
        from jobsaver.config import load_config

        path = Path(__file__).resolve().parent.parent / "config.example.yaml"
        with self.assertRaises(ConfigError):  # placeholder api_id -> expected error
            load_config(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
