"""End-to-end scan test with a fake Telegram client (no account, no network).

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.tl import types as tg_types  # noqa: E402

from jobsaver.config import (  # noqa: E402
    AppConfig,
    FilterConfig,
    RuntimeConfig,
    SavingConfig,
    ScanConfig,
    TelegramConfig,
)
from jobsaver.runner import JobSaver  # noqa: E402

NOW = datetime.now(timezone.utc)


def make_channel(chat_id: int = 1234567890, username: str | None = "python_jobs") -> Any:
    return tg_types.Channel(
        id=chat_id,
        title="Python Jobs",
        photo=None,
        date=NOW,
        creator=False,
        left=False,
        broadcast=True,
        verified=False,
        megagroup=False,
        restricted=False,
        signatures=False,
        min=False,
        scam=False,
        has_link=False,
        has_geo=False,
        slowmode_enabled=False,
        access_hash=1,
        username=username,
        restriction_reason=None,
        admin_rights=None,
        banned_rights=None,
        default_banned_rights=None,
        participants_count=None,
    )


def make_message(msg_id: int, text: str, *, age_days: int = 0, action: Any = None) -> Any:
    return SimpleNamespace(
        id=msg_id,
        text=text,
        date=NOW - timedelta(days=age_days),
        chat_id=-1001234567890,
        action=action,
        media=None,
    )


def at(moment: datetime):
    """Message factory pinned to an exact moment (UTC, whole seconds)."""
    def build(msg_id: int, text: str, action: Any = None) -> Any:
        return SimpleNamespace(
            id=msg_id,
            text=text,
            date=moment,
            chat_id=-1001234567890,
            action=action,
            media=None,
        )
    return build


class FakeClient:
    """Mimics the slices of the Telethon API used by JobSaver."""

    def __init__(self, messages: List[Any], *, fail_forward: bool = False):
        self.messages = sorted(messages, key=lambda m: m.id)
        self.fail_forward = fail_forward
        self.forwarded: List[tuple] = []
        self.sent: List[tuple] = []
        self.get_calls: List[dict] = []
        self.iter_calls: List[dict] = []
        self.read_acks: List[int] = []

    async def get_messages(self, entity, limit=None, min_id=0, reverse=False, **kwargs):
        self.get_calls.append(
            {"limit": limit, "min_id": min_id, "reverse": reverse,
             "offset_date": kwargs.get("offset_date")}
        )
        pool = [m for m in self.messages if m.id > min_id]
        offset_date = kwargs.get("offset_date")
        if offset_date is not None and not reverse:
            pool = [m for m in pool if m.date <= offset_date]
        if reverse:
            return pool[:limit] if limit else pool
        pool = sorted(pool, key=lambda m: m.id, reverse=True)  # newest first
        return pool[:limit] if limit else pool

    async def iter_messages(self, entity, offset_date=None, limit=None, min_id=0,
                            reverse=False, **kwargs):
        self.iter_calls.append(
            {"offset_date": offset_date, "limit": limit, "min_id": min_id, "reverse": reverse}
        )
        pool = [m for m in self.messages if m.id > min_id]
        pool.sort(key=lambda m: m.id, reverse=not reverse)  # ascending when reverse
        if limit:
            pool = pool[:limit]
        for message in pool:
            yield message

    async def forward_messages(self, destination, message):
        if self.fail_forward:
            raise RuntimeError("protected content")
        self.forwarded.append((destination, message.id))

    async def send_message(self, destination, text, link_preview=False):
        self.sent.append((destination, text))

    async def send_read_acknowledge(self, entity, max_id=0, **kwargs):
        self.read_acks.append(max_id)


def build_saver(tmpdir: str, **overrides) -> JobSaver:
    saving = overrides.pop("saving", SavingConfig())
    runtime = overrides.pop("runtime", RuntimeConfig(initial_scan_limit=50, max_age_days=14))
    filters = overrides.pop(
        "filters", FilterConfig(rules=["!unpaid", "python", "django"])
    )
    scan = overrides.pop("scan", ScanConfig())
    session = overrides.pop("session", "test")
    config = AppConfig(
        telegram=TelegramConfig(
            api_id=1, api_hash="0" * 32, phone="+10000000000",
            session=session, session_dir=f"{tmpdir}/sessions",
        ),
        sources=["@python_jobs"],
        filters=filters,
        saving=saving,
        runtime=runtime,
        database=f"{tmpdir}/test.sqlite3",
        log_file=f"{tmpdir}/log.txt",
        scan=scan,
    )
    return JobSaver(config)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.channel = make_channel()
        self._real_clients: List[Any] = []
        self._savers: List[JobSaver] = []

    def tearDown(self):
        for saver in self._savers:
            try:
                saver.store.close()
            except Exception:  # noqa: BLE001
                pass
        # The real TelegramClient holds a lock on its session file on Windows.
        for client in self._real_clients:
            try:
                client.session.close()
            except Exception:  # noqa: BLE001
                pass
        self._tmp.cleanup()

    def make_saver(self, **overrides) -> JobSaver:
        saver = build_saver(self._tmp.name, **overrides)
        self._real_clients.append(saver.client)
        self._savers.append(saver)
        return saver

    def test_matches_are_saved_and_others_skipped(self):
        saver = self.make_saver()
        saver.client = FakeClient([
            make_message(1, "We need a Java developer"),            # no match
            make_message(2, "Hiring: Senior Python Engineer"),      # match
            make_message(3, "Unpaid python internship"),            # excluded
            make_message(4, "Django + Python backend role"),        # match
            make_message(5, "Join our channel!", action=object()),  # service message
            make_message(6, "Old python job", age_days=40),         # too old
        ])

        saved = asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saved, 2)
        self.assertEqual([m for _, m in saver.client.forwarded], [2, 4])
        # the cursor advances past every message, including non-matching ones
        self.assertEqual(saver.store.get_last_message_id(-1001234567890), 6)
        self.assertEqual(saver.store.count_saved(), 2)

    def test_first_run_reads_history_newest_limited(self):
        saver = self.make_saver(runtime=RuntimeConfig(initial_scan_limit=2))
        saver.client = FakeClient([
            make_message(1, "python job 1"),
            make_message(2, "python job 2"),
            make_message(3, "python job 3"),
        ])

        saved = asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saved, 2)                         # only the 2 newest
        self.assertEqual([m for _, m in saver.client.forwarded], [2, 3])
        self.assertEqual(saver.client.get_calls[0]["min_id"], 0)
        self.assertFalse(saver.client.get_calls[0]["reverse"])

    def test_second_scan_saves_nothing(self):
        # resume=False -> the message-id cursor is used for the incremental path
        saver = self.make_saver(scan=ScanConfig(resume=False))
        saver.client = FakeClient([make_message(1, "Hiring python dev")])
        self.assertEqual(asyncio.run(saver.scan_chat(self.channel)), 1)
        self.assertEqual(asyncio.run(saver.scan_chat(self.channel)), 0)
        self.assertEqual(len(saver.client.forwarded), 1)
        # the second poll asks only for newer messages
        self.assertEqual(saver.client.get_calls[-1]["min_id"], 1)
        self.assertTrue(saver.client.get_calls[-1]["reverse"])

    def test_second_scan_with_resume_saves_nothing(self):
        saver = self.make_saver()
        saver.client = FakeClient([make_message(1, "Hiring python dev")])
        self.assertEqual(asyncio.run(saver.scan_chat(self.channel)), 1)

        self.assertEqual(asyncio.run(saver.scan_chat(self.channel)), 0)
        self.assertEqual(len(saver.client.forwarded), 1)
        self.assertTrue(saver.client.iter_calls)  # timestamp window, not the id cursor

    def test_forward_blocked_falls_back_to_copy(self):
        saver = self.make_saver()
        saver.client = FakeClient([make_message(7, "Python developer, $120k")], fail_forward=True)

        saved = asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saved, 1)
        self.assertEqual(saver.client.forwarded, [])
        self.assertEqual(len(saver.client.sent), 1)
        destination, text = saver.client.sent[0]
        self.assertEqual(destination, "me")
        self.assertIn("Python developer, $120k", text)
        self.assertIn("t.me/python_jobs/7", text)
        self.assertIn("Python Jobs", text)  # header rendered

    def test_dry_run_saves_nothing_and_does_not_advance_state(self):
        saver = self.make_saver(runtime=RuntimeConfig(dry_run=True))
        saver.client = FakeClient([make_message(9, "python job")])

        self.assertEqual(asyncio.run(saver.scan_chat(self.channel)), 0)
        self.assertEqual(saver.client.forwarded, [])
        self.assertEqual(saver.client.sent, [])
        self.assertEqual(saver.store.get_last_message_id(-1001234567890), 0)
        self.assertIsNone(saver.store.get_checkpoint(-1001234567890))
        self.assertEqual(saver.store.count_saved(), 0)

    def test_dry_run_keeps_an_existing_checkpoint(self):
        checkpoint = datetime(2026, 11, 10, tzinfo=timezone.utc)
        saver = self.make_saver(runtime=RuntimeConfig(dry_run=True))
        saver.store.set_checkpoint(
            -1001234567890, title="Python Jobs", scanned_until=checkpoint, last_message_id=5
        )
        saver.client = FakeClient([make_message(9, "python job")])

        asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saver.store.get_last_message_id(-1001234567890), 5)
        self.assertEqual(saver.store.get_checkpoint(-1001234567890), checkpoint)

    def test_dry_run_prints_an_aligned_table(self):
        saver = self.make_saver(runtime=RuntimeConfig(dry_run=True))
        saver.client = FakeClient(
            [make_message(9, "python job"), make_message(10, "python + django role")]
        )

        out = io.StringIO()
        with redirect_stdout(out):
            asyncio.run(saver.scan_chat(self.channel))

        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        # header, its underline, then one row per match
        self.assertEqual(len(lines), 4)
        self.assertIn("CHANNEL", lines[0])
        self.assertIn("MATCHED RULE", lines[0])
        self.assertTrue(lines[1].lstrip("- ").strip() == "")

        rows = lines[2:]
        self.assertIn("https://t.me/python_jobs/9", rows[0])
        self.assertIn("https://t.me/python_jobs/10", rows[1])
        # the rule column starts at the same offset in every row
        self.assertEqual(
            [row.index("python") for row in rows],
            [rows[0].index("python")] * 2,
        )
        self.assertNotIn("#9", "".join(rows))
        self.assertNotIn("[DRY-RUN]", "".join(rows))

    def test_dry_run_numbers_the_rows(self):
        saver = self.make_saver(runtime=RuntimeConfig(dry_run=True))
        saver.client = FakeClient(
            [make_message(9, "python job"), make_message(10, "python job")]
        )

        out = io.StringIO()
        with redirect_stdout(out):
            asyncio.run(saver.scan_chat(self.channel))

        rows = [
            line
            for line in out.getvalue().splitlines()
            if line.strip().startswith(("1", "2"))
        ]
        self.assertEqual([row.strip()[0] for row in rows], ["1", "2"])

    def test_append_note_mode(self):
        saver = self.make_saver(saving=SavingConfig(append_note=True))
        saver.client = FakeClient([make_message(3, "python + django role")])

        asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(len(saver.client.sent), 1)
        # Only the winning (highest-priority) keyword is reported.
        self.assertIn("matched: python", saver.client.sent[0][1])

    def test_cursor_never_moves_backwards(self):
        saver = self.make_saver()
        saver.store.set_last_message_id(-1001234567890, 100, "Python Jobs")
        saver.client = FakeClient([make_message(101, "python role")])

        asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saver.store.get_last_message_id(-1001234567890), 101)

    # ------------------------------------------------------------------ #
    # Date windows / checkpoints / read state
    # ------------------------------------------------------------------ #
    def test_date_window_only_reads_the_requested_range(self):
        nov = lambda d, h=0: datetime(2026, 11, d, h, 0, 0, tzinfo=timezone.utc)  # noqa: E731
        saver = self.make_saver(
            scan=ScanConfig(
                from_date=nov(5),
                to_date=nov(10),
                resume=False,
            )
        )
        saver.client = FakeClient([
            at(nov(1))(1, "python job before the window"),
            at(nov(5))(2, "python job at the start"),
            at(nov(8))(3, "java job inside"),           # in range, no match
            at(nov(10))(4, "python job at the end"),
            at(nov(15))(5, "python job after the window"),
        ])

        saved = asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saved, 2)
        self.assertEqual([m for _, m in saver.client.forwarded], [2, 4])
        self.assertEqual(saver.store.get_checkpoint(-1001234567890), nov(10))

    def test_window_parameters_sent_to_telegram(self):
        start = datetime(2026, 11, 5, tzinfo=timezone.utc)
        end = datetime(2026, 11, 10, tzinfo=timezone.utc)
        saver = self.make_saver(scan=ScanConfig(from_date=start, to_date=end, resume=False))
        saver.client = FakeClient([at(start)(1, "python job")])

        asyncio.run(saver.scan_chat(self.channel))

        # 1) anchor lookup: newest message at/just after the window start
        self.assertEqual(saver.client.get_calls[0]["offset_date"], start + timedelta(seconds=1))
        # 2) forward pagination from that anchor
        call = saver.client.iter_calls[-1]
        self.assertTrue(call["reverse"])
        self.assertEqual(call["min_id"], 0)
        self.assertEqual(call["limit"], 2000)

    def test_checkpoint_is_stored_after_a_scan(self):
        moment = datetime(2026, 11, 14, 15, 34, 45, tzinfo=timezone.utc)
        saver = self.make_saver()
        saver.client = FakeClient([at(moment)(11, "python job")])

        asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saver.store.get_checkpoint(-1001234567890), moment)
        self.assertEqual(saver.store.get_last_message_id(-1001234567890), 11)

    def test_large_window_is_consumed_in_batches_without_gaps(self):
        """A window bigger than the per-pass cap must never skip a post."""
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        messages = [
            at(datetime(2026, 11, day, 12, 0, 0, tzinfo=timezone.utc))(day, f"python job {day}")
            for day in range(1, 31)
        ]
        saver = self.make_saver(
            scan=ScanConfig(from_date=start, resume=False, max_history_messages=10),
            runtime=RuntimeConfig(max_age_days=0),
        )
        saver.client = FakeClient(messages)

        total = 0
        for _ in range(5):
            total += asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(total, 30)
        forwarded = [m for _, m in saver.client.forwarded]
        self.assertEqual(len(forwarded), 30)
        self.assertEqual(len(set(forwarded)), 30)          # nothing twice
        self.assertEqual(saver.store.count_saved(), 30)
        self.assertEqual(
            saver.store.get_checkpoint(-1001234567890), messages[-1].date
        )

    def test_resume_continues_from_checkpoint_without_duplicates(self):
        nov = lambda d, h=0, m=0: datetime(2026, 11, d, h, m, 0, tzinfo=timezone.utc)  # noqa: E731

        # --- first run: three posts, stops at Nov 3 10:00 -----------------
        first = self.make_saver(runtime=RuntimeConfig(initial_scan_limit=50, max_age_days=0))
        first.client = FakeClient([
            at(nov(1))(1, "python job 1"),
            at(nov(2))(2, "python job 2"),
            at(nov(3, 10))(3, "python job 3"),
        ])
        self.assertEqual(asyncio.run(first.scan_chat(self.channel)), 3)
        self.assertEqual(first.store.get_checkpoint(-1001234567890), nov(3, 10))
        first.store.close()

        # --- restart: resume must pick up exactly where it stopped --------
        second = self.make_saver(
            session="test2", runtime=RuntimeConfig(initial_scan_limit=50, max_age_days=0)
        )
        second.client = FakeClient([
            at(nov(1))(1, "python job 1"),          # already seen
            at(nov(2))(2, "python job 2"),          # already seen
            at(nov(3, 10))(3, "python job 3"),      # already seen (same second)
            at(nov(4))(4, "python job 4"),          # new
            at(nov(14, 15, 34))(5, "python job 5"),  # new
        ])

        start, end, mode = second.window_for(-1001234567890)
        self.assertEqual(mode, "resume")
        self.assertEqual(start, nov(3, 10) - timedelta(seconds=1))
        self.assertIsNone(end)

        saved = asyncio.run(second.scan_chat(self.channel))

        self.assertEqual(saved, 2)
        self.assertEqual([m for _, m in second.client.forwarded], [4, 5])
        self.assertEqual(second.store.count_saved(), 5)
        self.assertEqual(second.store.get_checkpoint(-1001234567890), nov(14, 15, 34))

    def test_explicit_from_date_overrides_the_checkpoint(self):
        nov = lambda d, h=0: datetime(2026, 11, d, h, 0, 0, tzinfo=timezone.utc)  # noqa: E731
        saver = self.make_saver()
        saver.store.set_checkpoint(
            -1001234567890, title="Python Jobs", scanned_until=nov(14, 12), last_message_id=50
        )

        # nothing configured -> resume from the checkpoint
        start, _, mode = saver.window_for(-1001234567890)
        self.assertEqual(mode, "resume")
        self.assertEqual(start, nov(14, 12) - timedelta(seconds=1))

        # explicit from_date -> that date wins
        saver.cfg.scan.from_date = nov(1)
        start, _, mode = saver.window_for(-1001234567890)
        self.assertEqual(mode, "range")
        self.assertEqual(start, nov(1))

    def test_resume_disabled_falls_back_to_message_cursor(self):
        saver = self.make_saver(scan=ScanConfig(resume=False))
        saver.store.set_checkpoint(
            -1001234567890,
            title="Python Jobs",
            scanned_until=datetime(2026, 11, 14, tzinfo=timezone.utc),
            last_message_id=50,
        )
        saver.client = FakeClient([make_message(51, "python job")])

        _, _, mode = saver.window_for(-1001234567890)
        self.assertEqual(mode, "incremental")

        asyncio.run(saver.scan_chat(self.channel))
        self.assertEqual(saver.client.get_calls[-1]["min_id"], 50)

    def test_messages_stay_unread_by_default(self):
        saver = self.make_saver()
        saver.client = FakeClient([make_message(3, "python job")])

        asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saver.client.read_acks, [])

    def test_mark_as_read_when_enabled(self):
        saver = self.make_saver(scan=ScanConfig(mark_as_read=True))
        saver.client = FakeClient([
            make_message(3, "python job"),
            make_message(4, "java job"),  # not saved, but scanned
        ])

        asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saver.client.read_acks, [4])

    def test_dry_run_never_marks_read(self):
        saver = self.make_saver(
            scan=ScanConfig(mark_as_read=True), runtime=RuntimeConfig(dry_run=True)
        )
        saver.client = FakeClient([make_message(3, "python job")])

        asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saver.client.read_acks, [])

    def test_reset_checkpoints_keeps_saved_history(self):
        saver = self.make_saver()
        saver.client = FakeClient([make_message(3, "python job")])
        asyncio.run(saver.scan_chat(self.channel))
        self.assertEqual(saver.store.count_saved(), 1)

        cleared = saver.store.reset_checkpoints()

        self.assertEqual(cleared, 1)
        self.assertIsNone(saver.store.get_checkpoint(-1001234567890))
        self.assertEqual(saver.store.get_last_message_id(-1001234567890), 0)
        self.assertEqual(saver.store.count_saved(), 1)  # history kept

    def test_describe_scan_reports_the_configuration(self):
        saver = self.make_saver(
            session="describe",
            scan=ScanConfig(
                from_date=datetime(2026, 11, 1, tzinfo=timezone.utc),
                to_date=datetime(2026, 11, 10, tzinfo=timezone.utc),
                mark_as_read=True,
            ),
        )
        text = saver.describe_scan()
        self.assertIn("from 2026-11-01", text)
        self.assertIn("to 2026-11-10", text)
        self.assertIn("mark as read", text)
        self.assertTrue(saver.window_complete())  # end date already passed

    def test_describe_scan_defaults(self):
        saver = self.make_saver(session="describe2")
        text = saver.describe_scan()
        self.assertIn("checkpoint", text)
        self.assertIn("to now", text)
        self.assertIn("leave unread", text)
        self.assertFalse(saver.window_complete())

    def test_future_end_date_keeps_polling(self):
        saver = self.make_saver(
            session="describe3",
            scan=ScanConfig(to_date=datetime(2030, 1, 1, tzinfo=timezone.utc)),
        )
        self.assertFalse(saver.window_complete())

    def test_failed_save_is_retried_next_cycle(self):
        # mode=forward -> a blocked forward raises instead of falling back to copy
        saver = self.make_saver(saving=SavingConfig(mode="forward"))
        saver.client = FakeClient([make_message(5, "python role")], fail_forward=True)

        self.assertEqual(asyncio.run(saver.scan_chat(self.channel)), 0)
        # cursor stopped just before the failing post, so nothing is lost
        self.assertEqual(saver.store.get_last_message_id(-1001234567890), 4)
        self.assertEqual(saver.store.count_saved(), 0)

    def test_broken_post_is_skipped_after_three_attempts(self):
        saver = self.make_saver(saving=SavingConfig(mode="forward"))
        saver.client = FakeClient([make_message(5, "python role")], fail_forward=True)

        for _ in range(3):
            asyncio.run(saver.scan_chat(self.channel))

        self.assertEqual(saver.store.get_last_message_id(-1001234567890), 5)
        self.assertEqual(saver._failed_saves, {})

    def test_source_reference_normalisation(self):
        normalize = JobSaver._normalize_ref
        self.assertEqual(normalize("@python_jobs"), "@python_jobs")
        self.assertEqual(normalize("https://t.me/python_jobs"), "python_jobs")
        self.assertEqual(normalize("https://t.me/python_jobs/1234"), "python_jobs")
        self.assertEqual(normalize("t.me/python_jobs/"), "python_jobs")
        self.assertEqual(normalize("https://t.me/+AbCdEf123"), "+AbCdEf123")
        self.assertEqual(normalize("-1001234567890"), "-1001234567890")


if __name__ == "__main__":
    unittest.main(verbosity=2)
