"""Tests for `--sources` / `--rules` list management (no Telegram, no network)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from jobsaver.config import ConfigError  # noqa: E402
from jobsaver.configfile import read_list  # noqa: E402
from main import (  # noqa: E402
    _parse_list_action,
    apply_list_action,
    build_parser,
    cmd_manage_lists,
)

SAMPLE = """\
telegram:
  api_id: 123
  api_hash: "abc"

# Channels to watch
sources:
  - "@old_channel"

filters:
  # ordered rules
  rules:
    - python
    - django
"""


class ActionParsingTests(unittest.TestCase):
    def test_no_values_means_list(self):
        self.assertEqual(_parse_list_action([], "sources"), ("list", []))
        self.assertEqual(_parse_list_action(None, "sources"), ("list", []))

    def test_add_with_values(self):
        self.assertEqual(
            _parse_list_action(["add", "@a", "-1001"], "sources"), ("add", ["@a", "-1001"])
        )

    def test_action_is_case_insensitive(self):
        self.assertEqual(_parse_list_action(["SET", "@a"], "sources"), ("set", ["@a"]))

    def test_blank_values_are_dropped(self):
        self.assertEqual(_parse_list_action(["add", " ", "@a"], "sources"), ("add", ["@a"]))

    def test_unknown_action_explains_the_choices(self):
        with self.assertRaises(ConfigError) as caught:
            _parse_list_action(["append", "@a"], "sources")
        message = str(caught.exception)
        for word in ("add", "set", "remove", "list"):
            self.assertIn(word, message)


class ApplyActionTests(unittest.TestCase):
    def test_list_changes_nothing(self):
        self.assertEqual(apply_list_action(["a"], "list", [], "x"), ["a"])

    def test_add_appends_and_skips_duplicates(self):
        result = apply_list_action(["@a", "@b"], "add", ["@B", "@c"], "Sources")
        self.assertEqual(result, ["@a", "@b", "@c"])

    def test_set_replaces_everything(self):
        self.assertEqual(apply_list_action(["@a"], "set", ["@x", "@y"], "Sources"), ["@x", "@y"])

    def test_set_keeps_the_order_and_dedupes(self):
        self.assertEqual(
            apply_list_action([], "set", ["@b", "@a", "@B"], "Sources"), ["@b", "@a"]
        )

    def test_remove_is_case_insensitive(self):
        self.assertEqual(
            apply_list_action(["@A", "@b"], "remove", ["@a"], "Sources"), ["@b"]
        )

    def test_remove_of_missing_entry_is_a_no_op(self):
        self.assertEqual(apply_list_action(["@a"], "remove", ["@zzz"], "Sources"), ["@a"])

    def test_add_without_values_is_an_error(self):
        with self.assertRaises(ConfigError):
            apply_list_action([], "add", [], "Sources")

    def test_remove_without_values_is_an_error(self):
        with self.assertRaises(ConfigError):
            apply_list_action(["@a"], "remove", [], "Sources")


class ManageCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.yaml"
        self.path.write_text(SAMPLE, encoding="utf-8", newline="")

    def run_cli(self, *argv: str) -> bool:
        args = build_parser().parse_args(["-c", str(self.path), *argv])
        return cmd_manage_lists(str(self.path), args)

    def data(self) -> dict:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))

    def test_list_does_not_modify_the_file(self):
        before = self.path.read_bytes()
        self.assertFalse(self.run_cli("--sources"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_add_writes_the_file(self):
        self.assertTrue(self.run_cli("--sources", "add", "@hahujobs", "-1001212256595"))
        self.assertEqual(self.data()["sources"], ["@old_channel", "@hahujobs", "-1001212256595"])

    def test_set_replaces_the_file(self):
        self.assertTrue(self.run_cli("--sources", "set", "@only"))
        self.assertEqual(self.data()["sources"], ["@only"])

    def test_remove_writes_the_file(self):
        self.assertTrue(self.run_cli("--sources", "remove", "@old_channel"))
        self.assertEqual(self.data()["sources"], [])

    def test_adding_a_duplicate_leaves_the_file_untouched(self):
        before = self.path.read_bytes()
        self.assertFalse(self.run_cli("--sources", "add", "@OLD_CHANNEL"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_rules_are_managed_too(self):
        self.assertTrue(self.run_cli("--rules", "add", "backend developer"))
        self.assertEqual(
            self.data()["filters"]["rules"], ["python", "django", "backend developer"]
        )

    def test_both_lists_in_one_command(self):
        self.assertTrue(
            self.run_cli("--sources", "add", "@new", "--rules", "set", "python")
        )
        data = self.data()
        self.assertEqual(data["sources"], ["@old_channel", "@new"])
        self.assertEqual(data["filters"]["rules"], ["python"])

    def test_comments_survive(self):
        self.run_cli("--sources", "set", "@x")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# Channels to watch", text)
        self.assertIn("# ordered rules", text)

    def test_read_list_sees_the_write(self):
        self.run_cli("--rules", "add", "fastapi")
        self.assertEqual(
            read_list(self.path, "rules", "filters"), ["python", "django", "fastapi"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
