"""Tests for the comment-preserving config editor (no Telegram, no network)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from jobsaver.configfile import (  # noqa: E402
    ConfigFileError,
    read_list,
    write_list,
)

SAMPLE = """\
# Header comment that must survive
telegram:
  api_id: 123
  api_hash: "abc"

# Channels & groups to watch
sources:
  - "@first_channel"      # trailing note
  # - "@a_commented_example"
  - "-1001234567890"

filters:
  case_sensitive: false

  # Ordered rules, index 0 wins.
  rules:
    - python
    - "backend developer"

  # AND logic
  must_include: []
"""


class TempConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.yaml"

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8", newline="")

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8", newline="")

    def parsed(self) -> dict:
        return yaml.safe_load(self.read())


class ReadListTests(TempConfigTest):
    def test_reads_a_top_level_list(self):
        self.write(SAMPLE)
        self.assertEqual(read_list(self.path, "sources"), ["@first_channel", "-1001234567890"])

    def test_reads_a_nested_list(self):
        self.write(SAMPLE)
        self.assertEqual(
            read_list(self.path, "rules", "filters"), ["python", "backend developer"]
        )

    def test_missing_key_is_empty(self):
        self.write(SAMPLE)
        self.assertEqual(read_list(self.path, "nope", "filters"), [])

    def test_empty_list(self):
        self.write(SAMPLE)
        self.assertEqual(read_list(self.path, "must_include", "filters"), [])

    def test_invalid_yaml_is_reported(self):
        self.write("a: [unclosed\n")
        with self.assertRaises(ConfigFileError):
            read_list(self.path, "sources")


class WriteListTests(TempConfigTest):
    def test_replaces_only_the_list_items(self):
        self.write(SAMPLE)
        write_list(self.path, "sources", ["@hahujobs", "-1001212256595"])

        text = self.read()
        self.assertIn("# Header comment that must survive", text)
        self.assertIn("# Ordered rules, index 0 wins.", text)
        self.assertIn("# AND logic", text)
        self.assertIn("api_hash: \"abc\"", text)
        self.assertEqual(self.parsed()["sources"], ["@hahujobs", "-1001212256595"])
        self.assertEqual(self.parsed()["filters"]["rules"], ["python", "backend developer"])

    def test_nested_list_is_replaced_in_place(self):
        self.write(SAMPLE)
        write_list(self.path, "rules", ["django", "re:\\b(?:api|rest)\\b"], "filters")

        data = self.parsed()
        self.assertEqual(data["filters"]["rules"], ["django", "re:\\b(?:api|rest)\\b"])
        self.assertEqual(data["filters"]["case_sensitive"], False)
        self.assertEqual(data["sources"], ["@first_channel", "-1001234567890"])

    def test_other_sections_keep_their_exact_text(self):
        self.write(SAMPLE)
        before = self.read().split("filters:")[1]
        write_list(self.path, "sources", ["@x"])
        after = self.read().split("filters:")[1]
        self.assertEqual(before, after)

    def test_items_are_quoted_so_yaml_round_trips(self):
        self.write(SAMPLE)
        tricky = ["@user", "-100123", "re:\\b(?:python|django)\\b", "backend developer", "true"]
        write_list(self.path, "sources", tricky)
        self.assertEqual(self.parsed()["sources"], tricky)

    def test_empty_list_is_written(self):
        self.write(SAMPLE)
        write_list(self.path, "sources", [])
        self.assertEqual(self.parsed()["sources"], [])  # [] not null
        self.assertIn("sources: []", self.read())
        self.assertIn("api_hash", self.read())

    def test_missing_nested_key_is_inserted(self):
        text = SAMPLE.replace("  rules:\n    - python\n    - \"backend developer\"\n", "")
        self.write(text)
        write_list(self.path, "rules", ["python"], "filters")
        self.assertEqual(self.parsed()["filters"]["rules"], ["python"])

    def test_inline_flow_list_becomes_a_block(self):
        self.write(SAMPLE.replace(
            'sources:\n  - "@first_channel"      # trailing note\n'
            '  # - "@a_commented_example"\n  - -1001234567890\n',
            'sources: ["@inline", "-100999"]\n',
        ))
        write_list(self.path, "sources", ["@block"])
        self.assertEqual(self.parsed()["sources"], ["@block"])

    def test_crlf_line_endings_are_preserved(self):
        self.write(SAMPLE.replace("\n", "\r\n"))
        write_list(self.path, "sources", ["@windows"])

        raw = self.path.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))  # no lone LF
        self.assertEqual(self.parsed()["sources"], ["@windows"])

    def test_missing_section_is_an_error(self):
        self.write("telegram:\n  api_id: 1\n")
        with self.assertRaises(ConfigFileError):
            write_list(self.path, "rules", ["python"], "fliters")  # typo

    def test_trailing_newline_is_preserved(self):
        self.write(SAMPLE)
        write_list(self.path, "sources", ["@x"])
        self.assertTrue(self.read().endswith("\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
