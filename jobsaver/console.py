"""Console helpers.

Windows consoles and redirected stdout often default to a legacy code page
(cp1252, cp437…), and writing "🎯" or "✓" to them raises UnicodeEncodeError.
Every user-facing line goes through here so output can never crash the run.
"""

from __future__ import annotations

import sys


def configure_output() -> None:
    """Force UTF-8 on stdout/stderr, with replacement instead of exceptions."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - some streams refuse reconfiguration
            pass


def safe_print(*args, **kwargs) -> None:
    """``print`` that degrades to ASCII instead of raising on odd consoles."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        separator = kwargs.get("sep")
        separator = " " if separator is None else separator
        text = separator.join(str(arg) for arg in args)
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            sys.stdout.write(text.encode(encoding, "replace").decode(encoding, "replace") + "\n")
        except Exception:  # noqa: BLE001 - last resort, never crash the app
            pass
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
