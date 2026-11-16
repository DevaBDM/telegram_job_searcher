"""Tests for the Priority Substring Matching System (Aho-Corasick automaton)
as specified in the Technical Specification.
"""

from __future__ import annotations

import unittest
from jobsaver.matcher import Decision, MatchResult, PriorityMatcher


class PrioritySubstringMatchingSpecTests(unittest.TestCase):
    """Directly tests all requirements and walkthrough cases from the spec."""

    def test_reference_walkthrough(self):
        rules = [
            "critical bug",    # 0 (INCLUDE)
            "!bug",            # 1 (EXCLUDE)
            "!duplicate",      # 2 (EXCLUDE)
            "security issue",  # 3 (INCLUDE)
            "!minor",          # 4 (EXCLUDE)
            "issue",           # 5 (INCLUDE)
        ]
        matcher = PriorityMatcher(rules=rules)

        # 1. "Critical bug on login" -> 0, 1 -> Index 0 wins (INCLUDE)
        res1 = matcher.evaluate("Critical bug on login")
        self.assertEqual(res1.decision, Decision.INCLUDE)
        self.assertEqual(res1.matched_rule_index, 0)
        self.assertEqual(res1.matched_keyword, "critical bug")
        self.assertTrue(res1.matched)

        # 2. "Found a minor bug" -> 1, 4 -> Index 1 wins (EXCLUDE)
        res2 = matcher.evaluate("Found a minor bug")
        self.assertEqual(res2.decision, Decision.EXCLUDE)
        self.assertEqual(res2.matched_rule_index, 1)
        self.assertEqual(res2.matched_keyword, "!bug")
        self.assertFalse(res2.matched)

        # 3. "Minor security issue" -> 3, 4, 5 -> Index 3 wins (INCLUDE)
        res3 = matcher.evaluate("Minor security issue")
        self.assertEqual(res3.decision, Decision.INCLUDE)
        self.assertEqual(res3.matched_rule_index, 3)
        self.assertEqual(res3.matched_keyword, "security issue")
        self.assertTrue(res3.matched)

        # 4. "Minor layout issue" -> 4, 5 -> Index 4 wins (EXCLUDE)
        res4 = matcher.evaluate("Minor layout issue")
        self.assertEqual(res4.decision, Decision.EXCLUDE)
        self.assertEqual(res4.matched_rule_index, 4)
        self.assertEqual(res4.matched_keyword, "!minor")
        self.assertFalse(res4.matched)

        # 5. "Update user profile" -> None -> UNMATCHED
        res5 = matcher.evaluate("Update user profile")
        self.assertEqual(res5.decision, Decision.UNMATCHED)
        self.assertIsNone(res5.matched_rule_index)
        self.assertIsNone(res5.matched_keyword)
        self.assertFalse(res5.matched)

    def test_duplicate_keywords_lowest_index_takes_precedence(self):
        rules = [
            "important",
            "!spam",        # index 1
            "python",       # index 2
            "spam",         # index 3 (duplicate of !spam -> index 1 takes permanent precedence)
        ]
        matcher = PriorityMatcher(rules=rules)
        res = matcher.evaluate("Here is some spam message")
        self.assertEqual(res.decision, Decision.EXCLUDE)
        self.assertEqual(res.matched_rule_index, 1)
        self.assertEqual(res.matched_keyword, "!spam")

    def test_empty_strings_and_bare_prefix_ignored(self):
        rules = ["", "!", "   ", "!  ", "valid keyword"]
        matcher = PriorityMatcher(rules=rules)
        res = matcher.evaluate("This has a valid keyword")
        self.assertEqual(res.decision, Decision.INCLUDE)
        self.assertEqual(res.matched_rule_index, 4)

    def test_empty_rule_list_returns_unmatched(self):
        matcher = PriorityMatcher(rules=[])
        res = matcher.evaluate("Any text")
        self.assertEqual(res.decision, Decision.UNMATCHED)
        self.assertIsNone(res.matched_rule_index)

    def test_empty_input_text_returns_unmatched(self):
        matcher = PriorityMatcher(rules=["python", "!spam"])
        self.assertEqual(matcher.evaluate("").decision, Decision.UNMATCHED)
        self.assertEqual(matcher.evaluate(None).decision, Decision.UNMATCHED)
        self.assertEqual(matcher.evaluate("   ").decision, Decision.UNMATCHED)

    def test_case_sensitivity(self):
        matcher_insensitive = PriorityMatcher(rules=["Python"], case_sensitive=False)
        self.assertEqual(matcher_insensitive.evaluate("python dev").decision, Decision.INCLUDE)

        matcher_sensitive = PriorityMatcher(rules=["Python"], case_sensitive=True)
        self.assertEqual(matcher_sensitive.evaluate("python dev").decision, Decision.UNMATCHED)
        self.assertEqual(matcher_sensitive.evaluate("Python dev").decision, Decision.INCLUDE)

    def test_must_include_and_logic_preservation(self):
        # must_include must all be present
        # Note: !internship is index 0, python is index 1 so !internship overrides python
        matcher = PriorityMatcher(
            rules=["!internship", "python"],
            must_include=["addis ababa", "remote"]
        )
        # Missing "remote"
        self.assertEqual(matcher.evaluate("Python dev in addis ababa").decision, Decision.UNMATCHED)
        # Both present -> "python" matches (INCLUDE)
        self.assertEqual(matcher.evaluate("Remote Python dev in addis ababa").decision, Decision.INCLUDE)
        # Both present but "!internship" at index 0 beats "python" at index 1 -> EXCLUDE
        self.assertEqual(matcher.evaluate("Remote Python internship in addis ababa").decision, Decision.EXCLUDE)

    def test_fuzzy_matching_preservation(self):
        matcher = PriorityMatcher(
            rules=["python developer"],
            fuzzy=True,
            fuzzy_threshold=70
        )
        res = matcher.evaluate("Looking for a pythn devloper")
        self.assertEqual(res.decision, Decision.INCLUDE)
        self.assertEqual(res.matched_keyword, "python developer")

    def test_collection_operations(self):
        rules = ["!internship", "python"]
        matcher = PriorityMatcher(rules=rules)

        texts = [
            "Senior python engineer",
            "Unpaid python internship",
            "Cooking recipes",
        ]

        self.assertEqual(matcher.filter_included(texts), ["Senior python engineer"])
        self.assertEqual(matcher.filter_excluded(texts), ["Unpaid python internship"])
        self.assertEqual(matcher.filter_unmatched(texts), ["Cooking recipes"])

        partitioned = matcher.partition_items(texts)
        self.assertEqual(partitioned[Decision.INCLUDE], ["Senior python engineer"])
        self.assertEqual(partitioned[Decision.EXCLUDE], ["Unpaid python internship"])
        self.assertEqual(partitioned[Decision.UNMATCHED], ["Cooking recipes"])


if __name__ == "__main__":
    unittest.main()
