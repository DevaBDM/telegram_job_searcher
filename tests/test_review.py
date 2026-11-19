"""Tests for the --review Telegram deep links (no Telegram, no network)."""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from jobsaver.store import Store  # noqa: E402
from main import telegram_deep_link  # noqa: E402


class DeepLinkTests(unittest.TestCase):
    def test_public_channel_post(self):
        self.assertEqual(
            telegram_deep_link("https://t.me/hahujobs/164100", -1001364371791, 164100),
            "tg://resolve?domain=hahujobs&post=164100",
        )

    def test_public_link_with_query_string(self):
        self.assertEqual(
            telegram_deep_link("https://t.me/geezjobs_ethiopia/26123?single", -1001478131840, 26123),
            "tg://resolve?domain=geezjobs_ethiopia&post=26123",
        )

    def test_private_channel_post(self):
        self.assertEqual(
            telegram_deep_link("https://t.me/c/1234567890/77", -1001234567890, 77),
            "tg://privatepost?channel=1234567890&post=77",
        )

    def test_missing_link_falls_back_to_private_chat_id(self):
        self.assertEqual(
            telegram_deep_link(None, -1001234567890, 42),
            "tg://privatepost?channel=1234567890&post=42",
        )

    def test_missing_link_falls_back_to_basic_group(self):
        self.assertEqual(
            telegram_deep_link(None, -123456, 42),
            "tg://openmessage?chat_id=-123456&message_id=42",
        )

    def test_unrelated_link_falls_back_to_chat_id(self):
        self.assertEqual(
            telegram_deep_link("https://example.com/job/1", -1001234567890, 5),
            "tg://privatepost?channel=1234567890&post=5",
        )

    def test_no_link_and_no_private_chat(self):
        self.assertIsNone(telegram_deep_link(None, 12345, 1))


class OpenTests(unittest.TestCase):
    @patch.object(main.sys, "platform", "win32")
    @patch("main.os.startfile", create=True)
    def test_uses_startfile_on_windows(self, startfile):
        self.assertTrue(main.open_in_telegram("tg://resolve?domain=x&post=1"))
        startfile.assert_called_once_with("tg://resolve?domain=x&post=1")

    @patch.object(main.sys, "platform", "win32")
    @patch("main.os.startfile", side_effect=OSError("no handler"), create=True)
    def test_reports_failure_when_no_handler(self, _startfile):
        self.assertFalse(main.open_in_telegram("tg://resolve?domain=x&post=1"))


ROWS = [
    {
        "chat_id": -1001,
        "chat_title": "Alpha",
        "message_id": 1,
        "matched": "python, backend developer",
        "deadline": "2026-11-20",
        "message_date": "2026-11-10T09:00:00+00:00",
        "link": "https://t.me/alpha/1",
    },
    {
        "chat_id": -1002,
        "chat_title": "Beta",
        "message_id": 2,
        "matched": "w:IT",
        "deadline": "2026-11-21",
        "message_date": "2026-11-11T09:00:00+00:00",
        "link": "https://t.me/beta/2",
    },
    {
        "chat_id": -1003,
        "chat_title": "Gamma",
        "message_id": 3,
        "matched": None,
        "deadline": None,
        "message_date": "2026-11-12T09:00:00+00:00",
        "link": "https://t.me/gamma/3",
    },
]


def fake_saver(rows, deleted, archived_calls=None):
    class FakeStore:
        def review_jobs(self, null_first=False, archived=False):
            return rows

        def delete_saved(self, chat_id, message_id):
            deleted.append((chat_id, message_id))
            return True

        def archive_saved(self, chat_id, message_id, archived=True):
            (archived_calls if archived_calls is not None else deleted).append(
                ((chat_id, message_id), archived)
            )
            return True

        def count_archived(self):
            return 0

        def close(self):
            return None

    class FakeSaver:
        def __init__(self, _config):
            self.store = FakeStore()

        async def connect(self):
            return True

        async def disconnect(self):
            return None

    return FakeSaver


class ReviewFlowTests(unittest.TestCase):
    def run_review(self, rows, keys, open_result=True, archive=False):
        deleted = []
        archived = []
        with (
            patch("main.JobSaver", fake_saver(rows, deleted, archived)),
            patch("builtins.input", side_effect=keys),
            patch("main.open_in_telegram", return_value=open_result) as opened,
        ):
            result = asyncio.run(main.cmd_review(object(), archive=archive))
        return result, deleted, opened, archived

    def test_x_deletes_current_and_opens_next(self):
        result, deleted, opened, _ = self.run_review(ROWS, ["x", "q"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [(-1001, 1)])
        opened.assert_called_once_with("tg://resolve?domain=beta&post=2")

    def test_x_on_the_last_post_deletes_without_opening(self):
        result, deleted, opened, _ = self.run_review(ROWS[-1:], ["x"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [(-1003, 3)])
        opened.assert_not_called()

    def test_n_skips_and_opens_the_next_post(self):
        result, deleted, opened, archived = self.run_review(ROWS, ["n", "q"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [])
        self.assertEqual(archived, [])
        opened.assert_called_once_with("tg://resolve?domain=beta&post=2")

    def test_n_on_the_last_post_does_nothing(self):
        result, deleted, opened, _ = self.run_review(ROWS[-1:], ["n"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [])
        opened.assert_not_called()

    def test_gt_is_a_plain_next(self):
        _result, deleted, opened, _ = self.run_review(ROWS, [">", "q"])

        self.assertEqual(deleted, [])
        opened.assert_not_called()

    def test_z_archives_the_post_and_opens_the_next(self):
        result, deleted, opened, archived = self.run_review(ROWS, ["z", "q"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [])  # archived, not deleted
        self.assertEqual(archived, [((-1001, 1), True)])
        opened.assert_called_once_with("tg://resolve?domain=beta&post=2")

    def test_z_in_the_archive_restores_the_post(self):
        _result, deleted, opened, archived = self.run_review(
            ROWS, ["z", "q"], archive=True
        )

        self.assertEqual(deleted, [])
        self.assertEqual(archived, [((-1001, 1), False)])
        opened.assert_called_once_with("tg://resolve?domain=beta&post=2")

    def test_d_only_deletes_and_does_not_open_the_next(self):
        _result, deleted, opened, _ = self.run_review(ROWS, ["d", "q"])

        self.assertEqual(deleted, [(-1001, 1)])
        opened.assert_not_called()

    def test_enter_opens_the_current_post(self):
        _result, deleted, opened, _ = self.run_review(ROWS, ["", "q"])

        self.assertEqual(deleted, [])
        opened.assert_called_once_with("tg://resolve?domain=alpha&post=1")


class ReviewLineTests(unittest.TestCase):
    """The post heading shows the rule that matched, not the Telegram message id."""

    def run_review(self, keys):
        out = io.StringIO()
        with (
            patch("main.JobSaver", fake_saver(ROWS, [])),
            patch("builtins.input", side_effect=keys),
            patch("main.open_in_telegram", return_value=True),
            redirect_stdout(out),
        ):
            asyncio.run(main.cmd_review(object()))
        return out.getvalue()

    def test_heading_shows_the_matched_rule(self):
        printed = self.run_review(["n", "n", "q"])

        self.assertIn("matched=python, backend developer", printed)
        self.assertIn("matched=w:IT", printed)

    def test_heading_no_longer_shows_the_message_id(self):
        printed = self.run_review(["n", "n", "q"])

        self.assertNotIn("#1 ", printed)
        self.assertNotIn("#2 ", printed)
        self.assertNotIn("#3 ", printed)

    def test_heading_uses_unknown_when_no_rule_was_recorded(self):
        printed = self.run_review(["n", "n", "q"])

        self.assertIn("matched=unknown", printed)


class ReviewOrderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "review.sqlite3")
        self.addCleanup(self.store.close)

        # (message_id, deadline, posted)
        entries = [
            (1, "2026-11-20", "2026-11-10T09:00:00+00:00"),
            (2, "2026-11-25", "2026-11-11T09:00:00+00:00"),
            (3, None, "2026-11-16T09:00:00+00:00"),
            (4, None, "2026-11-14T09:00:00+00:00"),
            (5, None, "2026-11-15T09:00:00+00:00"),
        ]
        for message_id, deadline, posted in entries:
            self.store.record_saved(
                chat_id=-1001,
                chat_title="Test",
                message_id=message_id,
                message_date=posted,
                matched=["python"],
                mode="local",
                link=f"https://t.me/test/{message_id}",
                deadline=deadline,
            )

    def _ids(self, rows):
        return [row["message_id"] for row in rows]

    def test_default_order_puts_unknown_deadlines_last(self):
        self.assertEqual(self._ids(self.store.review_jobs()), [1, 2, 3, 5, 4])

    def test_null_first_starts_with_unknowns_by_posted_date(self):
        # Unknown deadlines first, oldest post first, then dated posts by deadline.
        self.assertEqual(self._ids(self.store.review_jobs(null_first=True)), [4, 5, 3, 1, 2])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "archive.sqlite3")
        self.addCleanup(self.store.close)

        for message_id in (1, 2, 3):
            self.store.record_saved(
                chat_id=-1001,
                chat_title="Test",
                message_id=message_id,
                message_date="2026-11-10T09:00:00+00:00",
                matched=["python"],
                mode="local",
                link=f"https://t.me/test/{message_id}",
                deadline=None,
            )

    def _ids(self, rows):
        return [row["message_id"] for row in rows]

    def test_archived_posts_leave_the_normal_review(self):
        self.store.archive_saved(-1001, 2)

        self.assertEqual(self._ids(self.store.review_jobs()), [1, 3])
        self.assertEqual(self._ids(self.store.review_jobs(archived=True)), [2])

    def test_archive_keeps_the_row_and_its_data(self):
        self.store.archive_saved(-1001, 2)

        row = self.store.find_saved(-1001, 2)
        self.assertIsNotNone(row)
        self.assertEqual(row["matched"], "python")
        self.assertEqual(row["link"], "https://t.me/test/2")
        self.assertTrue(row["archived_at"])

    def test_restoring_puts_the_post_back(self):
        self.store.archive_saved(-1001, 2)
        self.store.archive_saved(-1001, 2, archived=False)

        self.assertEqual(self._ids(self.store.review_jobs()), [1, 2, 3])
        self.assertEqual(self.store.review_jobs(archived=True), [])
        self.assertIsNone(self.store.find_saved(-1001, 2)["archived_at"])

    def test_archive_view_is_most_recent_first(self):
        self.store.archive_saved(-1001, 1)
        self.store.archive_saved(-1001, 3)

        self.assertEqual(self._ids(self.store.review_jobs(archived=True)), [3, 1])

    def test_count_archived(self):
        self.assertEqual(self.store.count_archived(), 0)
        self.store.archive_saved(-1001, 2)
        self.assertEqual(self.store.count_archived(), 1)

    def test_archiving_an_unknown_post_changes_nothing(self):
        self.assertFalse(self.store.archive_saved(-1001, 99))
        self.assertEqual(self.store.count_archived(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
