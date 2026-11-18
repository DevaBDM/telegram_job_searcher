"""Tests for the review -> tailor -> job-note bridge (no Telegram, no model)."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from jobsaver import pipeline  # noqa: E402

POSTING = """Senior Backend Developer - Acme PLC

Requirements:
- Python, FastAPI and PostgreSQL
- 3 years of experience

Apply: jobs@acme.example
"""

ROWS = [
    {
        "id": 42,
        "chat_id": -1001,
        "chat_title": "Alpha Jobs",
        "message_id": 7,
        "deadline": "2026-11-20",
        "message_date": "2026-11-16T09:00:00+00:00",
        "link": "https://t.me/alpha/7",
    }
]


class TempWork(unittest.TestCase):
    """Point the bridge's inbox/state at a throw-away directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        patcher = patch.multiple(
            pipeline,
            INBOX_DIR=root / "inbox",
            WORK_DIR=root,
            STATE_FILE=root / "state.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class PostingFileTests(TempWork):
    def test_slugify(self):
        self.assertEqual(
            pipeline.slugify("Senior Full-Stack Engineer — Acme, Inc.!"),
            "senior-full-stack-engineer-acme-inc",
        )
        self.assertEqual(pipeline.slugify("!!!"), "posting")

    def test_clean_posting_normalises_newlines_only(self):
        self.assertEqual(
            pipeline.clean_posting("  Title\r\n\r\nBody\r\n"),
            "  Title\n\nBody",
        )

    def test_write_posting_keeps_the_body_and_appends_provenance(self):
        path = pipeline.write_posting(
            POSTING,
            link="https://t.me/alpha/7",
            channel="Alpha Jobs",
            posted="2026-11-16T09:00:00+00:00",
        )
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("Senior Backend Developer - Acme PLC"))
        self.assertIn("Requirements:", text)
        self.assertIn("Posting: https://t.me/alpha/7", text)
        self.assertIn("Channel: Alpha Jobs", text)
        self.assertIn("Posted: 2026-11-16", text)
        self.assertNotIn("\r", text)

    def test_write_posting_never_overwrites_an_existing_file(self):
        first = pipeline.write_posting(POSTING)
        second = pipeline.write_posting(POSTING)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())

    def test_write_posting_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            pipeline.write_posting("   \n\n ")


class StateTests(TempWork):
    def test_record_merges_and_badges(self):
        key = pipeline.state_key({"chat_id": -1001, "message_id": 7})
        pipeline.record(key, tailored_at="2026-11-17T10:00:00")
        pipeline.record(key, note="Jobs & Internships/Acme.md", applied=True)

        entry = pipeline.entry_for(key)
        self.assertEqual(entry["tailored_at"], "2026-11-17T10:00:00")
        self.assertEqual(entry["note"], "Jobs & Internships/Acme.md")
        self.assertTrue(entry["applied"])
        self.assertIn("noted", pipeline.badge(entry))
        self.assertIn("applied", pipeline.badge(entry))

        stored = json.loads(Path(pipeline.STATE_FILE).read_text(encoding="utf-8"))
        self.assertIn(key, stored)

    def test_missing_state_is_empty(self):
        self.assertEqual(pipeline.load_state(), {})
        self.assertEqual(pipeline.badge({}), "")

    def test_looks_like_posting(self):
        self.assertTrue(main._looks_like_posting(POSTING))
        self.assertFalse(main._looks_like_posting("oo"))
        self.assertFalse(main._looks_like_posting("n"))


# --------------------------------------------------------------------------- #
# The review loop
# --------------------------------------------------------------------------- #
def make_saver():
    class FakeStore:
        def review_jobs(self, null_first=False):
            return ROWS

        def delete_saved(self, chat_id, message_id):
            return True

        def get_saved(self, row_id):
            return ROWS[0] if row_id == 42 else None

        def close(self):
            return None

    class FakeClient:
        async def get_messages(self, entity, ids=None):
            return type("Message", (), {"text": POSTING, "message": POSTING})()

    class FakeSaver:
        def __init__(self, _config):
            self.store = FakeStore()
            self.client = FakeClient()

        async def connect(self):
            return True

        async def disconnect(self):
            return None

        async def resolve(self, _ref):
            return object()

    return FakeSaver


class ReviewPipelineTests(TempWork):
    def run_review(self, keys, note_outcome=None):
        tailor = patch.object(
            pipeline, "tailor", return_value=pipeline.RunOutcome("agent_tailer", [], 0, 1.0, [])
        ).start()
        self.addCleanup(patch.stopall)
        note = patch.object(
            pipeline,
            "note",
            return_value=note_outcome
            or pipeline.RunOutcome("agent_jobnote", [], 0, 1.0, ["25 - Opportunities Pipeline/Jobs & Internships/Acme.md"]),
        ).start()
        record = patch.object(pipeline, "record", return_value={}).start()
        write = patch.object(pipeline, "write_posting", return_value=Path("/tmp/posting.md")).start()
        patcher = (
            patch.object(pipeline, "state_key", return_value="key"),
            patch.object(pipeline, "entry_for", return_value={}),
            patch.object(pipeline, "badge", return_value=""),
            patch.object(main, "JobSaver", make_saver()),
            patch.object(main, "open_in_telegram", return_value=True),
            patch("builtins.input", side_effect=keys),
        )
        for item in patcher:
            item.start()
            self.addCleanup(item.stop)

        result = asyncio.run(main.cmd_review(object()))
        return result, {"tailor": tailor, "note": note, "record": record, "write": write}

    def test_t_tailors_and_skips_the_note_when_asked(self):
        result, calls = self.run_review(["t", "Full pasted posting", "EOF", "", "s", "q"])

        self.assertEqual(result, 0)
        calls["tailor"].assert_called_once()
        self.assertFalse(calls["tailor"].call_args.kwargs.get("letter_only", False))
        calls["write"].assert_called_once()
        calls["note"].assert_not_called()

    def test_a_asks_about_tailoring_then_documents_as_applied(self):
        _result, calls = self.run_review(["a", "Full pasted posting", "EOF", "", "a", "q"])

        calls["tailor"].assert_called_once()
        calls["note"].assert_called_once()
        self.assertTrue(calls["note"].call_args.kwargs["applied"])

    def test_the_pasted_full_posting_wins_over_the_telegram_text(self):
        _result, calls = self.run_review(
            ["a", "Full posting from the link", "second line", "EOF", "n", "s", "q"]
        )

        body = calls["write"].call_args.args[0]
        self.assertIn("Full posting from the link", body)
        self.assertIn("second line", body)
        self.assertNotIn("Acme PLC", body)

    def test_an_empty_paste_falls_back_to_the_telegram_text(self):
        _result, calls = self.run_review(["a", "EOF", "n", "s", "q"])

        self.assertIn("Senior Backend Developer", calls["write"].call_args.args[0])

    def test_a_without_tailoring_documents_as_research(self):
        _result, calls = self.run_review(["a", "Full posted", "EOF", "n", "r", "q"])

        calls["tailor"].assert_not_called()
        calls["note"].assert_called_once()
        self.assertFalse(calls["note"].call_args.kwargs["applied"])

    def test_applied_without_a_fresh_letter_needs_confirmation(self):
        # a -> no tailor -> a -> decline copying the old letter -> research note
        _result, calls = self.run_review(["a", "Full posted", "EOF", "n", "a", "", "q"])

        calls["note"].assert_called_once()
        self.assertFalse(calls["note"].call_args.kwargs["applied"])

    def test_t_then_applied_runs_the_note_with_the_letter(self):
        result, calls = self.run_review(["t", "Full posted", "EOF", "", "a", "q"])

        self.assertEqual(result, 0)
        calls["note"].assert_called_once()
        self.assertTrue(calls["note"].call_args.kwargs["applied"])

    def test_l_runs_the_tailor_in_letter_only_mode(self):
        _result, calls = self.run_review(["l", "Full posted", "EOF", "", "s", "q"])

        calls["tailor"].assert_called_once()
        self.assertTrue(calls["tailor"].call_args.kwargs["letter_only"])

    def test_j_writes_the_note_without_tailoring(self):
        _result, calls = self.run_review(["j", "Full posted", "EOF", "r", "q"])

        calls["tailor"].assert_not_called()
        calls["note"].assert_called_once()
        self.assertFalse(calls["note"].call_args.kwargs["applied"])

    def test_a_pasted_posting_at_the_prompt_starts_the_tailor(self):
        pasted = "Junior Software Developer at Example PLC, Addis Ababa. Apply to hr@example.com."
        _result, calls = self.run_review([pasted, "EOF", "", "s", "q"])

        calls["tailor"].assert_called_once()
        self.assertIn(pasted, str(calls["write"].call_args))

    def test_unknown_short_input_does_not_start_anything(self):
        _result, calls = self.run_review(["oo", "q"])

        calls["tailor"].assert_not_called()
        calls["note"].assert_not_called()

    def test_a_failed_tailor_stops_before_the_note(self):
        failed = pipeline.RunOutcome("agent_tailer", [], 2, 1.0, [])
        with patch.object(pipeline, "tailor", return_value=failed) as tailor:
            self.addCleanup(patch.stopall)
            note = patch.object(pipeline, "note").start()
            patch.object(pipeline, "record", return_value={}).start()
            patch.object(pipeline, "write_posting", return_value=Path("/tmp/posting.md")).start()
            patch.object(pipeline, "state_key", return_value="key").start()
            patch.object(pipeline, "entry_for", return_value={}).start()
            patch.object(pipeline, "badge", return_value="").start()
            patch.object(main, "JobSaver", make_saver()).start()
            patch.object(main, "open_in_telegram", return_value=True).start()
            patch("builtins.input", side_effect=["t", "Full posted", "EOF", "", "q"]).start()

            result = asyncio.run(main.cmd_review(object()))

        self.assertEqual(result, 0)
        tailor.assert_called_once()
        note.assert_not_called()


class AgentInvocationTests(TempWork):
    """The bridge must run each agent's own run.py, in that agent's folder."""

    def stub(self, body: str) -> Path:
        agent = Path(self._tmp.name) / "agent"
        agent.mkdir(exist_ok=True)
        (agent / "run.py").write_text(body, encoding="utf-8")
        return agent

    def test_tailor_passes_the_posting_and_runs_in_the_agent_folder(self):
        agent = self.stub("import sys\nprint('args', sys.argv[1:])\nraise SystemExit(3)\n")
        with patch.multiple(
            pipeline,
            TAILER_DIR=agent,
            python_executable=lambda: sys.executable,
        ):
            outcome = pipeline.tailor(Path("posting.md"), letter_only=True)

        self.assertEqual(outcome.exit_code, 3)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.command[1], "run.py")
        self.assertIn("--posting", outcome.command)
        self.assertEqual(outcome.command[-1], "--letter-only")

    def test_note_passes_cover_letter_and_today(self):
        agent = self.stub("print('note')\n")
        with patch.multiple(
            pipeline,
            JOBNOTE_DIR=agent,
            python_executable=lambda: sys.executable,
        ):
            outcome = pipeline.note(Path("posting.md"), applied=True, today="2026-11-17")

        self.assertEqual(outcome.exit_code, 0)
        self.assertTrue(outcome.ok)
        self.assertIn("--cover-letter", outcome.command)
        self.assertEqual(outcome.command[-2:], ["--today", "2026-11-17"])


class CliFlagTests(unittest.TestCase):
    def test_new_flags_parse(self):
        parser = main.build_parser()
        args = parser.parse_args(["--tailor", "42", "--letter-only", "--note-mode", "applied"])
        self.assertEqual(args.tailor, 42)
        self.assertTrue(args.letter_only)
        self.assertEqual(args.note_mode, "applied")

        args = parser.parse_args(["--note", "7"])
        self.assertEqual(args.note, 7)
        self.assertEqual(args.note_mode, "research")

        args = parser.parse_args(["--pipeline-check"])
        self.assertTrue(args.pipeline_check)

    def test_posting_source_flags_parse(self):
        parser = main.build_parser()
        args = parser.parse_args(["--tailor", "42", "--posting-file", "posting.txt"])
        self.assertEqual(args.posting_file, "posting.txt")
        self.assertFalse(args.from_telegram)

        args = parser.parse_args(["--note", "7", "--posting-file", "-"])
        self.assertEqual(args.posting_file, "-")

        args = parser.parse_args(["--tailor", "42", "--from-telegram"])
        self.assertTrue(args.from_telegram)
        self.assertIsNone(args.posting_file)

    def test_posting_source_flags_default_off(self):
        args = main.build_parser().parse_args(["--review"])
        self.assertIsNone(args.posting_file)
        self.assertFalse(args.from_telegram)


class CliDispatchTests(unittest.TestCase):
    """main() must forward the posting source to the one-shot commands."""

    def dispatch(self, argv):
        with (
            patch.object(main, "load_config", return_value=object()),
            patch.object(main, "apply_cli_overrides"),
            patch.object(main, "setup_logging"),
        ):
            return main.main(argv)

    def test_tailor_receives_the_posting_source_flags(self):
        with patch.object(main, "cmd_tailor", new=AsyncMock(return_value=0)) as tailor:
            code = self.dispatch(
                ["--tailor", "42", "--posting-file", "full.txt", "--from-telegram"]
            )

        self.assertEqual(code, 0)
        kwargs = tailor.call_args.kwargs
        self.assertEqual(kwargs["posting_file"], "full.txt")
        self.assertTrue(kwargs["from_telegram"])

    def test_note_receives_the_posting_source_flags(self):
        with patch.object(main, "cmd_note", new=AsyncMock(return_value=0)) as note:
            code = self.dispatch(
                ["--note", "7", "--posting-file", "-", "--note-mode", "applied"]
            )

        self.assertEqual(code, 0)
        kwargs = note.call_args.kwargs
        self.assertEqual(kwargs["posting_file"], "-")
        self.assertTrue(kwargs["applied"])


class PostingSourceTests(TempWork):
    """The one-shot commands must not touch Telegram before they have the text."""

    def test_posting_from_file_reads_a_file(self):
        path = Path(self._tmp.name) / "posting.txt"
        path.write_text("Full listing\nsecond line\n", encoding="utf-8")

        self.assertEqual(main._posting_from_file(str(path)), "Full listing\nsecond line\n")

    def test_tailor_explains_the_teaser_before_connecting(self):
        boom = patch.object(main, "JobSaver", side_effect=AssertionError("connected too early"))
        with boom:
            code = asyncio.run(main.cmd_tailor(object(), 42))

        self.assertEqual(code, 2)

    def test_note_explains_the_teaser_before_connecting(self):
        boom = patch.object(main, "JobSaver", side_effect=AssertionError("connected too early"))
        with boom:
            code = asyncio.run(main.cmd_note(object(), 7))

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
