"""Telegram Job Saver — watch job channels, match ordered rules, save to Saved Messages."""

from .console import configure_output

__version__ = "1.0.0"

# Make stdout/stderr UTF-8 before anything prints anything.
configure_output()
