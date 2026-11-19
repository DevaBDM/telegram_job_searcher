"""Console noise filter (Telethon drops queued updates while --review waits)."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


def record(name: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.WARNING, "test", 1, message, None, None)


class MtProtoNoiseFilterTests(unittest.TestCase):
    def setUp(self):
        self.noise = main._DropMtProtoNoise()

    def test_drops_the_ignored_update_warnings(self):
        for message in (
            "Server sent a very old message with ID 7709611932686182401, ignoring (see FAQ for details)",
            "Server sent a very new message with ID 42, ignoring (see FAQ for details)",
            "Server resent the older message 7, ignoring",
        ):
            with self.subTest(message=message):
                self.assertFalse(
                    self.noise.filter(record("telethon.network.mtprotostate", message))
                )

    def test_keeps_every_other_telethon_message(self):
        self.assertTrue(
            self.noise.filter(
                record("telethon.client.updates", "Got difference for channel 123")
            )
        )

    def test_keeps_our_own_messages(self):
        self.assertTrue(self.noise.filter(record("jobsaver", "cycle done: 0 saved")))

    def test_only_console_is_filtered(self):
        """The file log keeps the warning: it is useful when a clock is wrong."""
        logger = logging.getLogger("telethon.network.mtprotostate")
        handler = logging.StreamHandler()
        handler.addFilter(self.noise)

        with self.subTest("filter attached to the console handler only"):
            self.assertEqual(
                [type(f).__name__ for f in handler.filters], ["_DropMtProtoNoise"]
            )
        logger.propagate = True
        self.assertEqual(len(logger.filters), 0)


if __name__ == "__main__":
    unittest.main()
