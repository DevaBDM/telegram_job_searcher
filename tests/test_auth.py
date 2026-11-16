"""Authentication flow tests: session reuse and headless safety (no Telegram)."""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock  # type: ignore
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobsaver.config import (  # noqa: E402
    AppConfig,
    ConfigError,
    FilterConfig,
    RuntimeConfig,
    SavingConfig,
    ScanConfig,
    TelegramConfig,
)
from jobsaver.runner import JobSaver, _stdin_is_interactive  # noqa: E402


class FakeAuthClient:
    """Only the pieces ensure_authorized() touches."""

    def __init__(self, authorized: bool):
        self.authorized = authorized
        self.start_calls = []

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def start(self, phone=None) -> None:
        self.start_calls.append(phone)


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class AuthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.saver = JobSaver(
            AppConfig(
                telegram=TelegramConfig(
                    api_id=1,
                    api_hash="0" * 32,
                    phone="+10000000000",
                    session="auth",
                    session_dir=f"{self._tmp.name}/sessions",
                ),
                sources=["@python_jobs"],
                filters=FilterConfig(keywords=["python"]),
                saving=SavingConfig(),
                runtime=RuntimeConfig(),
                database=f"{self._tmp.name}/auth.sqlite3",
                log_file=f"{self._tmp.name}/log.txt",
                scan=ScanConfig(),
            )
        )
        self.addCleanup(self.saver.store.close)
        self.addCleanup(self.saver.client.session.close)

    def test_existing_session_is_reused_silently(self):
        fake = FakeAuthClient(authorized=True)
        self.saver.client = fake

        asyncio.run(self.saver.ensure_authorized())

        self.assertEqual(fake.start_calls, [])  # no prompting

    def test_headless_run_without_session_fails_with_instructions(self):
        fake = FakeAuthClient(authorized=False)
        self.saver.client = fake

        with mock.patch("sys.stdin", io.StringIO("")):
            with self.assertRaises(ConfigError) as caught:
                asyncio.run(self.saver.ensure_authorized())

        message = str(caught.exception)
        self.assertIn("--login", message)
        self.assertIn("sessions", message)
        self.assertEqual(fake.start_calls, [])  # never blocks on input()

    def test_interactive_run_without_session_starts_login(self):
        fake = FakeAuthClient(authorized=False)
        self.saver.client = fake

        with mock.patch("sys.stdin", TtyStringIO()):
            asyncio.run(self.saver.ensure_authorized())

        self.assertEqual(fake.start_calls, ["+10000000000"])

    def test_stdin_detection_handles_missing_stream(self):
        with mock.patch("sys.stdin", None):
            self.assertFalse(_stdin_is_interactive())
        with mock.patch("sys.stdin", io.StringIO("")):
            self.assertFalse(_stdin_is_interactive())
        with mock.patch("sys.stdin", TtyStringIO()):
            self.assertTrue(_stdin_is_interactive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
