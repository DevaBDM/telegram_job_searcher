"""Hand one reviewed post to the two local agents.

This module is a thin bridge, on purpose:

* the Telegram side already knows **which** post is interesting (``--review``);
* ``agent_tailer`` decides **what** the CV and cover letter look like;
* ``agent_jobnote`` decides **what** the vault note looks like.

Nothing here edits a CV, a letter or a note.  It writes the post text to a file
and calls the two one-shot entry points, streaming their output so you can see
the model work.  Both agents enforce their own write sandbox, so a bug here
cannot corrupt the vault.

Layout, derived from this file (no absolute paths are hardcoded)::

    Obsidian Vault/
      15 - Career & Portfolio/agent_tailer/run.py
      25 - Opportunities Pipeline/agent_jobnote/run.py
      25 - Opportunities Pipeline/Ai_parser_telegram_jobs/jobsaver/pipeline.py

Repos can be moved/renamed by setting ``PIPELINE_TAILER_DIR`` and
``PIPELINE_JOBNOTE_DIR`` in the environment.  The interpreter used for the
agents is ``sys.executable`` unless ``PIPELINE_PYTHON`` is set.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .console import safe_print

# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
AGENT_DIR = Path(__file__).resolve().parents[1]          # .../Ai_parser_telegram_jobs
PIPELINE_DIR = AGENT_DIR.parent                          # 25 - Opportunities Pipeline
VAULT_ROOT = PIPELINE_DIR.parent                          # vault root

TAILER_DIR = Path(
    os.environ.get("PIPELINE_TAILER_DIR")
    or VAULT_ROOT / "15 - Career & Portfolio" / "agent_tailer"
)
JOBNOTE_DIR = Path(
    os.environ.get("PIPELINE_JOBNOTE_DIR") or PIPELINE_DIR / "agent_jobnote"
)

WORK_DIR = AGENT_DIR / "_pipeline"
INBOX_DIR = WORK_DIR / "inbox"
STATE_FILE = WORK_DIR / "state.json"

NOTES_DIR = PIPELINE_DIR / "Jobs & Internships"
CAREER_DIR = VAULT_ROOT / "15 - Career & Portfolio"
CV_PDF = CAREER_DIR / "documents" / "pdf" / "CV_Dawit_Berhan.pdf"
CV_TEX = CAREER_DIR / "documents" / "CV" / "CV_Dawit_Berhan.tex"
LETTER = CAREER_DIR / "documents" / "CoverLetters" / "cover_letter_Dawit_Berhan.md"
ARCHIVE_DIR = CAREER_DIR / "documents" / "archive"

DEFAULT_ENDPOINT = "http://127.0.0.1:1234/v1"

_EOF_LINES = {"EOF", ":END", "###END", "---END---"}


# --------------------------------------------------------------------------- #
# Environment / discovery
# --------------------------------------------------------------------------- #
def python_executable() -> str:
    """Interpreter used for the agents (they are stdlib-only, so this is safe)."""
    return os.environ.get("PIPELINE_PYTHON") or sys.executable


def agent_env() -> dict[str, str]:
    """UTF-8 for the children: postings contain em dashes and other non-ASCII."""
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def missing_paths() -> list[tuple[str, Path]]:
    """(label, path) for every piece the bridge needs but cannot find."""
    checks = [
        ("agent_tailer/run.py", TAILER_DIR / "run.py"),
        ("agent_jobnote/run.py", JOBNOTE_DIR / "run.py"),
        ("Jobs & Internships/", NOTES_DIR),
        ("Careers documents/", CAREER_DIR / "documents"),
    ]
    return [(label, path) for label, path in checks if not path.exists()]


def server_reachable(endpoint: str | None = None, timeout: float = 2.0) -> tuple[bool, str]:
    """Probe the OpenAI-compatible server once so we fail fast with a clear message."""
    base = (endpoint or os.environ.get("LLAMA_SERVER_URL") or DEFAULT_ENDPOINT).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/models", timeout=timeout):
            return True, base
    except urllib.error.HTTPError as exc:  # a 401 still means "something is listening"
        return True, f"{base} (HTTP {exc.code})"
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
        return False, f"{base} ({exc})"


# --------------------------------------------------------------------------- #
# Posting files
# --------------------------------------------------------------------------- #
def slugify(text: str, limit: int = 50) -> str:
    raw = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    return (raw or "posting")[:limit].strip("-") or "posting"


def first_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#*-• ").strip()
        if cleaned:
            return cleaned
    return "posting"


def clean_posting(text: str) -> str:
    """Normalise line endings, drop trailing blank lines, keep everything else verbatim."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(text.split("\n")).strip("\n")


def write_posting(
    text: str,
    *,
    link: str | None = None,
    channel: str | None = None,
    posted: str | None = None,
) -> Path:
    """Write one posting into ``_pipeline/inbox/`` and return the file.

    The body is copied verbatim.  Provenance is appended as plain lines (never a
    Markdown heading) so ``agent_jobnote`` can quote the source in the note's
    Links section without breaking its description checks.
    """
    body = clean_posting(text)
    if not body.strip():
        raise ValueError("the posting is empty")

    extras: list[str] = []
    if link:
        extras.append(f"Posting: {link}")
    if channel:
        extras.append(f"Channel: {channel}")
    if posted:
        extras.append(f"Posted: {posted[:10]}")
    if extras:
        body = body.rstrip() + "\n\n" + "\n".join(extras) + "\n"
    else:
        body = body.rstrip() + "\n"

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = f"{stamp}-{slugify(first_line(body))}"
    path = INBOX_DIR / f"{base}.md"
    counter = 2
    while path.exists():
        path = INBOX_DIR / f"{base}-{counter}.md"
        counter += 1
    path.write_text(body, encoding="utf-8")
    return path


def read_posting(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Running the agents
# --------------------------------------------------------------------------- #
@dataclass
class RunOutcome:
    """What one agent run did, in a form the review loop can print and record."""

    agent: str
    command: list[str]
    exit_code: int
    seconds: float
    changed: list[str] = field(default_factory=list)
    started_at: float = 0.0

    @property
    def ok(self) -> bool:
        # exit 1 means "produced, but validation found something to read"
        return self.exit_code in (0, 1)

    def summary(self) -> str:
        state = "ok" if self.exit_code == 0 else (
            "produced with warnings" if self.exit_code == 1 else f"failed (exit {self.exit_code})"
        )
        return f"{self.agent}: {state} in {self.seconds:.0f}s"


def snapshot_artifacts() -> dict[Path, float]:
    """Modification times of everything the agents can produce."""
    seen: dict[Path, float] = {}
    for path in (LETTER, CV_TEX, CV_PDF):
        try:
            seen[path] = path.stat().st_mtime
        except OSError:
            continue
    if NOTES_DIR.is_dir():
        for note in NOTES_DIR.glob("*.md"):
            try:
                seen[note] = note.stat().st_mtime
            except OSError:
                continue
    return seen


def _changed(before: Mapping[Path, float], after: Mapping[Path, float]) -> list[str]:
    """Vault-relative paths of files created or modified, newest first."""
    changed = [
        (path, mtime)
        for path, mtime in after.items()
        if before.get(path) != mtime
    ]
    changed.sort(key=lambda item: item[1], reverse=True)
    return [rel(path) for path, _mtime in changed]


def rel(path: Path) -> str:
    """Path relative to the vault root, or the absolute path when outside it."""
    try:
        return path.relative_to(VAULT_ROOT).as_posix()
    except ValueError:
        return str(path)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _run(
    agent: str,
    command: Sequence[str],
    cwd: Path,
    *,
    title: str,
) -> tuple[int, float, list[str]]:
    """Run one agent, streaming its output. Returns (exit code, seconds, changed files)."""
    before = snapshot_artifacts()
    started = time.monotonic()

    safe_print("")
    safe_print("─" * 72)
    safe_print(f"▶ {title}")
    safe_print(f"  {cwd.name}$ {' '.join(command)}")
    safe_print("─" * 72)

    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=agent_env(),
        )
    except OSError as exc:
        safe_print(f"[!!] could not start {agent}: {exc}")
        return 2, 0.0, []

    assert process.stdout is not None
    for line in process.stdout:
        safe_print(line.rstrip("\n"))
    process.stdout.close()
    code = process.wait()
    seconds = time.monotonic() - started

    after = snapshot_artifacts()
    changed = _changed(before, after)

    safe_print("─" * 72)
    safe_print(f"■ {agent} exited {code} in {seconds:.0f}s")
    for path in changed:
        safe_print(f"  ↳ {path}")
    safe_print("─" * 72)
    return code, seconds, changed


def tailor(
    posting: Path,
    *,
    letter_only: bool = False,
    extra: Sequence[str] = (),
) -> RunOutcome:
    """Run ``agent_tailer/run.py`` on one posting file (documents are its call)."""
    command = [python_executable(), "run.py", "--posting", str(posting)]
    if letter_only:
        command.append("--letter-only")
    command.extend(extra)
    code, seconds, changed = _run(
        "agent_tailer",
        command,
        TAILER_DIR,
        title="agent_tailer — CV + cover letter" if not letter_only else "agent_tailer — cover letter only",
    )
    return RunOutcome("agent_tailer", list(command), code, seconds, changed)


def note(
    posting: Path,
    *,
    applied: bool = False,
    today: str | None = None,
    extra: Sequence[str] = (),
) -> RunOutcome:
    """Run ``agent_jobnote/run.py``.

    ``applied=True`` passes ``--cover-letter``: the agent copies the current
    master letter into the note and records ``status: applied`` with the
    submission date.  Only use it when the letter was actually sent.
    """
    command = [python_executable(), "run.py", "--posting", str(posting)]
    if applied:
        command.append("--cover-letter")
    if today:
        command.extend(["--today", today])
    command.extend(extra)
    code, seconds, changed = _run(
        "agent_jobnote",
        command,
        JOBNOTE_DIR,
        title="agent_jobnote — vault note" + (" (applied)" if applied else ""),
    )
    return RunOutcome("agent_jobnote", list(command), code, seconds, changed)


# --------------------------------------------------------------------------- #
# Small state file: what has already been handed to the agents
# --------------------------------------------------------------------------- #
def state_key(row: Mapping[str, Any]) -> str:
    return f"{row['chat_id']}:{row['message_id']}"


def load_state() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Mapping[str, dict[str, Any]]) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def record(key: str, **fields: Any) -> dict[str, dict[str, Any]]:
    state = load_state()
    entry = dict(state.get(key) or {})
    entry.update(fields)
    entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
    state[key] = entry
    save_state(state)
    return state


def entry_for(key: str) -> dict[str, Any]:
    return load_state().get(key) or {}


def badge(entry: Mapping[str, Any]) -> str:
    """Short marker for the review line."""
    if not entry:
        return ""
    parts: list[str] = []
    if entry.get("note"):
        parts.append("noted")
    if entry.get("applied"):
        parts.append("applied")
    if entry.get("tailored"):
        parts.append("tailored")
    return ("  [" + ", ".join(parts) + "]") if parts else ""


# --------------------------------------------------------------------------- #
# Interactive helpers shared by --review and the one-shot commands
# --------------------------------------------------------------------------- #
def describe_health() -> None:
    safe_print(f"vault        {VAULT_ROOT}")
    safe_print(f"agent_tailer {TAILER_DIR}  {'ok' if (TAILER_DIR / 'run.py').is_file() else 'MISSING'}")
    safe_print(f"agent_jobnote {JOBNOTE_DIR}  {'ok' if (JOBNOTE_DIR / 'run.py').is_file() else 'MISSING'}")
    safe_print(f"python       {python_executable()}")
    safe_print(f"inbox        {INBOX_DIR}")
    safe_print(f"state        {STATE_FILE}  ({len(load_state())} job(s))")
    ok, detail = server_reachable()
    safe_print(("llama-server  " if ok else "llama-server  [!!] ") + detail)


def read_pasted(first_line: str = "") -> str:
    """Read a multi-line posting from stdin until an EOF marker."""
    safe_print("  paste the posting, then a line containing only  EOF")
    lines: list[str] = [first_line] if first_line else []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() in _EOF_LINES:
            break
        lines.append(line)
    return clean_posting("\n".join(lines))


def confirm(prompt: str, *, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def choose_note_mode(default: str = "research") -> str:
    """Ask what to do in the vault. Returns ``applied`` / ``research`` / ``skip``."""
    safe_print("")
    safe_print("  Job note:  [a] applied (letter copied, status=applied)")
    safe_print("             [r] research only (empty letter, status=researching)")
    safe_print("             [s] skip")
    answer = input(f"  choice [{default[0]}] ").strip().lower()
    if not answer:
        return default
    if answer in {"a", "applied", "y", "yes"}:
        return "applied"
    if answer in {"s", "skip", "n", "no"}:
        return "skip"
    return "research"
