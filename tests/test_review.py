"""Tests for the --review Telegram deep links (no Telegram, no network)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
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
        "deadline": "2026-11-20",
        "message_date": "2026-11-10T09:00:00+00:00",
        "link": "https://t.me/alpha/1",
    },
    {
        "chat_id": -1002,
        "chat_title": "Beta",
        "message_id": 2,
        "deadline": "2026-11-21",
        "message_date": "2026-11-11T09:00:00+00:00",
        "link": "https://t.me/beta/2",
    },
    {
        "chat_id": -1003,
        "chat_title": "Gamma",
        "message_id": 3,
        "deadline": None,
        "message_date": "2026-11-12T09:00:00+00:00",
        "link": "https://t.me/gamma/3",
    },
]


def fake_saver(rows, deleted):
    class FakeStore:
        def review_jobs(self, null_first=False):
            return rows

        def delete_saved(self, chat_id, message_id):
            deleted.append((chat_id, message_id))
            return True

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
    def run_review(self, rows, keys, open_result=True):
        deleted = []
        with (
            patch("main.JobSaver", fake_saver(rows, deleted)),
            patch("builtins.input", side_effect=keys),
            patch("main.open_in_telegram", return_value=open_result) as opened,
        ):
            result = asyncio.run(main.cmd_review(object()))
        return result, deleted, opened

    def test_x_deletes_current_and_opens_next(self):
        result, deleted, opened = self.run_review(ROWS, ["x", "q"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [(-1001, 1)])
        opened.assert_called_once_with("tg://resolve?domain=beta&post=2")

    def test_x_on_the_last_post_deletes_without_opening(self):
        result, deleted, opened = self.run_review(ROWS[-1:], ["x"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [(-1003, 3)])
        opened.assert_not_called()

    def test_skip_and_open_keeps_the_post_and_opens_the_next(self):
        result, deleted, opened = self.run_review(ROWS, [">", "q"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [])  # nothing removed from the review list
        opened.assert_called_once_with("tg://resolve?domain=beta&post=2")

    def test_skip_and_open_on_the_last_post_deletes_nothing(self):
        result, deleted, opened = self.run_review(ROWS[-1:], [">"])

        self.assertEqual(result, 0)
        self.assertEqual(deleted, [])
        opened.assert_not_called()

    def test_d_only_deletes_and_does_not_open_the_next(self):
        _result, deleted, opened = self.run_review(ROWS, ["d", "q"])

        self.assertEqual(deleted, [(-1001, 1)])
        opened.assert_not_called()

    def test_enter_opens_the_current_post(self):
        _result, deleted, opened = self.run_review(ROWS, ["", "q"])

        self.assertEqual(deleted, [])
        opened.assert_called_once_with("tg://resolve?domain=alpha&post=1")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
