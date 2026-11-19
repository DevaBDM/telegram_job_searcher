#!/usr/bin/env python3
"""Command line entry point.

Usage
-----
    python main.py --login                     # log in once, create the session file
    python main.py --chats                     # list your channels/groups with their ids
    python main.py                             # run forever, polling for new posts
    python main.py --once                      # single pass, then exit

    # manage the lists in config.yaml from the terminal
    python main.py --sources                   # show the watched channels
    python main.py --sources add "@hahujobs" -1001212256595
    python main.py --sources set "@hahujobs"   # replace the whole list
    python main.py --sources remove "@old"
    python main.py --rules                     # show the ordered matching rules
    python main.py --rules add "backend developer"
    python main.py --rules add "w:IT" "!internship"

    # scan a specific date range (overrides the stored checkpoint)
    python main.py --from "2026-11-01" --to "2026-11-16 23:59" --once
    python main.py --from "2026-11-10 08:00" --mark-read --once

    # resume from the last stored checkpoint (default behaviour)
    python main.py --once
    python main.py --reset-checkpoint --once   # forget progress, start over
    python main.py --checkpoint                # show per-chat progress

    # review saved posts in deadline order and open them in Telegram Desktop
    python main.py --review
    python main.py --review --null-first

    # inside --review:  o/Enter=open  a=apply  n=next  >=skip+open
    #                   x=delete+open  d=delete  q=quit
    #                   j=note only  l=letter only  s=show text  p=paste a post
    # a asks for the FULL posting first (the Telegram post is only a teaser:
    # open its link, copy the listing, paste it, then a line with only  EOF),
    # then asks "tailor?" and "record as a(applied) r(researching) n(nothing)?"
    # The agents run in this same window and the review prompt comes back on
    # its own - there is no second window to close.

    # review -> tailor -> job note (the review prompt accepts a/l/j/s/p)
    python main.py --tailor 42 --posting-file full-posting.txt   # full listing
    python main.py --tailor 42 --posting-file - --letter-only    # from stdin
    python main.py --tailor 42 --from-telegram                   # short text
    python main.py --note 42 --posting-file - --note-mode applied
    python main.py --pipeline-check            # agent paths + model server status
    python main.py --pipeline-check            # agent paths + model server status
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jobsaver.config import AppConfig, ConfigError, load_config, parse_when
from jobsaver.configfile import ConfigFileError, read_list, write_list
from jobsaver.console import configure_output, safe_print
from jobsaver import pipeline
from jobsaver.runner import JobSaver
from jobsaver.store import Store, from_db


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class _DropMtProtoNoise(logging.Filter):
    """Hide Telethon's dropped-update warnings on the console.

    While --review waits at the prompt the asyncio loop is blocked, so updates
    queue up. When it resumes, Telethon discards any that are older than a few
    minutes ("Server sent a very old message ... ignoring"). Nothing the scanner
    needs is lost — history is read with get_messages, not with updates — but
    the line looks like an error. It still goes to logs/jobsaver.log.
    """

    _IGNORED_PREFIXES = (
        "Server sent a very old message",
        "Server sent a very new message",
        "Server resent the older message",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("telethon"):
            return True
        message = record.getMessage()
        return not message.startswith(self._IGNORED_PREFIXES)


def setup_logging(config: AppConfig, verbose: bool) -> None:
    log_path = Path(config.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    console.addFilter(_DropMtProtoNoise())
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    logging.getLogger("telethon").setLevel(logging.WARNING)


def _fmt_moment(value: str | None, config: AppConfig) -> str:
    moment = from_db(value)
    if moment is None:
        return "-"
    return moment.astimezone(config.scan.tz).strftime("%Y-%m-%d %H:%M:%S %Z")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _display_name(me: Any) -> str:
    if me is None:
        return "unknown user"
    name = " ".join(part for part in (me.first_name, me.last_name) if part)
    return f"{name} (@{me.username})" if me.username else (name or str(me.id))


async def cmd_login(config: AppConfig) -> None:
    saver = JobSaver(config)
    try:
        await saver.client.connect()
        if await saver.client.is_user_authorized():
            safe_print(
                f"Already logged in as {_display_name(await saver.client.get_me())} - "
                "reusing the existing session."
            )
        else:
            safe_print(f"Logging in with {config.telegram.phone or 'the phone in config.yaml'}...")
            safe_print(
                "Telegram will send a login code to your Telegram app; type it below.\n"
                "If you use two-step verification you will also be asked for your password.\n"
            )
            await saver.ensure_authorized()
            safe_print(f"✓ Logged in as {_display_name(await saver.client.get_me())}")
        safe_print(f"Session saved to: {config.telegram.session_path}.session")
        safe_print("You can now run `python main.py`.")
    finally:
        await saver.disconnect()
        saver.store.close()


async def cmd_chats(config: AppConfig) -> None:
    saver = JobSaver(config)
    try:
        await saver.client.connect()
        await saver.ensure_authorized()

        rows = []
        async for dialog in saver.client.iter_dialogs():
            if dialog.is_user:
                continue
            entity = dialog.entity
            username = getattr(entity, "username", None)
            kind = "group" if getattr(entity, "megagroup", False) or dialog.is_group else "channel"
            rows.append((dialog.id, kind, (username or "-"), dialog.name or ""))

        rows.sort(key=lambda r: (r[1], r[3].lower()))
        safe_print(f"{'ID':>22}  {'TYPE':<8} {'USERNAME':<28} TITLE")
        safe_print("-" * 100)
        for chat_id, kind, username, name in rows:
            safe_print(f"{chat_id:>22}  {kind:<8} {username:<28} {name}")
        safe_print("-" * 100)
        safe_print(f"{len(rows)} channel(s)/group(s). Copy the ID into config.yaml -> sources.")
    finally:
        await saver.disconnect()
        saver.store.close()


def cmd_checkpoints(config: AppConfig) -> None:
    with Store(config.database) as store:
        rows = store.list_checkpoints()
        if not rows:
            safe_print("No scan progress stored yet.")
            safe_print("The first run will start from `scan.from_date`, or from recent history.")
            return
        safe_print(f"Scan progress (times shown in {config.scan.timezone_name}):")
        safe_print(f"{'CHAT ID':>22}  {'SCANNED UP TO':<28} {'LAST MSG':>10}  TITLE")
        safe_print("-" * 100)
        for row in rows:
            safe_print(
                f"{row['chat_id']:>22}  {_fmt_moment(row['last_scanned_date'], config):<28} "
                f"{row['last_message_id']:>10}  {row['title'] or ''}"
            )
        safe_print("-" * 100)
        safe_print("Next run resumes from these timestamps (unless --from is given).")


def cmd_stats(config: AppConfig) -> None:
    with Store(config.database) as store:
        safe_print(f"Database: {store.path}")
        safe_print(f"Total saved: {store.count_saved()}")
        rows = store.recent_saved(limit=10)
        if not rows:
            safe_print("Nothing saved yet.")
            return
        safe_print("\nLast 10:")
        for row in rows:
            deadline_info = f" deadline={row['deadline']}" if row['deadline'] else ""
            safe_print(
                f"  {row['saved_at']}  {row['chat_title']} #{row['message_id']} "
                f"[{row['matched']}]{deadline_info} {row['link'] or ''}"
            )


async def cmd_run(config: AppConfig, once: bool) -> int:
    saver = JobSaver(config)
    await saver.run(once=once)
    return 0


_TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}


def telegram_deep_link(link: str | None, chat_id: int, message_id: int) -> str | None:
    """Turn a stored t.me link (or chat/message pair) into a ``tg://`` deep link.

    Public channel post:  ``tg://resolve?domain=<username>&post=<message_id>``
    Private channel post: ``tg://privatepost?channel=<internal_id>&post=<message_id>``

    Telegram Desktop opens these straight at the post.  ``None`` means we could
    not build a link (no stored link and no usable chat id).
    """
    if link:
        parsed = urlparse(link)
        if parsed.netloc.lower() in _TELEGRAM_HOSTS:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 3 and parts[0].lower() == "c":
                return f"tg://privatepost?channel={parts[1]}&post={parts[2]}"
            if len(parts) >= 2:
                return f"tg://resolve?domain={parts[0]}&post={parts[1]}"

    # Fall back to the stored (chat_id, message_id) when there is no link.
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"tg://privatepost?channel={raw[4:]}&post={message_id}"
    if raw.startswith("-"):
        return f"tg://openmessage?chat_id={raw}&message_id={message_id}"
    return None


def open_in_telegram(url: str) -> bool:
    """Hand a ``tg://`` URL to the installed Telegram desktop client."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            subprocess.Popen(
                ["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        return True
    except OSError:
        return False


def _open_review_post(deep_link: str | None, link: str | None) -> None:
    """Open one review item in Telegram, with a browser fallback."""
    if not deep_link:
        safe_print("  ! no Telegram link for this post")
        return
    if open_in_telegram(deep_link):
        safe_print("  ↗ opened in Telegram — come back here and press n/d/q")
    elif link and webbrowser.open(link):
        safe_print("  ↗ Telegram app not found — opened the link in the browser")
    else:
        safe_print(f"  ! could not open it; copy this link: {link or deep_link}")


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read a column from a sqlite3.Row (or a plain dict) without raising."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


async def _fetch_post_text(saver: JobSaver, row: Any) -> str | None:
    """The original text of one saved post, straight from Telegram."""
    safe_print(f"  fetching #{row['message_id']} from {row['chat_title']} ...")
    try:
        entity = await saver.resolve(str(row["chat_id"]))
        message = await saver.client.get_messages(entity, ids=int(row["message_id"]))
    except Exception as exc:  # noqa: BLE001 - network/permission problems are reported, not raised
        safe_print(f"  ! could not fetch the post: {exc}")
        return None
    if not message:
        safe_print("  ! the post is no longer available in Telegram")
        return None
    body = (getattr(message, "text", None) or getattr(message, "message", "") or "").strip()
    if not body:
        safe_print("  ! the post has no text (media-only post)")
        return None
    return body


def _looks_like_posting(raw: str) -> bool:
    """A pasted posting at the review prompt, as opposed to a mistyped command."""
    text = raw.strip()
    return len(text) >= 40 or "\n" in text


def _read_pasted(first: str = "", hint: str = "") -> str:
    safe_print(
        "  "
        + (hint or "paste the posting, then a line containing only  EOF")
    )
    lines = [first] if first else []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() in {"EOF", ":END", "###END", "---END---"}:
            break
        lines.append(line)
    return "\n".join(lines).strip("\n")


def _confirm(prompt: str, *, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except EOFError:
        return False
    if not answer:
        return default
    return answer in {"y", "yes"}


def _choose_note_mode(default: str = "research") -> str:
    safe_print("")
    safe_print("  Record it in the job notes?")
    safe_print("    a = applied      (letter copied into the note, status: applied)")
    safe_print("    r = researching  (description filled, letter block empty)")
    safe_print("    n = no record    (leave the vault alone)")
    try:
        answer = input(f"  choice [{default[0]}] ").strip().lower()
    except EOFError:
        return "skip"
    if not answer:
        return default
    if answer in {"a", "applied", "y", "yes"}:
        return "applied"
    if answer in {"n", "no", "none", "s", "skip"}:
        return "skip"
    return "research"


def _posting_from_file(source: str) -> str:
    """Read the full posting from a file, or from stdin for ``-``."""
    if source == "-":
        try:
            return sys.stdin.buffer.read().decode("utf-8", "replace")
        except AttributeError:
            return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8", errors="replace")


async def cmd_tailor(
    config: AppConfig,
    saved_id: int,
    *,
    letter_only: bool = False,
    note_mode: str = "research",
    posting_file: str | None = None,
    from_telegram: bool = False,
) -> int:
    """One saved post -> tailor -> optionally a job note. No prompts except the note choice."""
    body: str | None = _posting_from_file(posting_file).strip() if posting_file else None
    if not body and not from_telegram:
        # Ask for the text before touching Telegram: the saved post is only a teaser.
        safe_print("The saved Telegram post is usually only the short version of the listing.")
        safe_print("Pass the full text you copied from the link:")
        safe_print(f'  ... main.py --tailor {saved_id} --posting-file full-posting.txt')
        safe_print(f'  ... main.py --tailor {saved_id} --posting-file -     (pipe it in)')
        safe_print(f'  ... main.py --tailor {saved_id} --from-telegram      (use the short text)')
        return 2

    saver = JobSaver(config)
    try:
        await saver.connect()
        row = saver.store.get_saved(saved_id)
        if row is None:
            safe_print(f"No saved post with database id {saved_id}. Run --review to see the ids.")
            return 2

        if not body:
            body = await _fetch_post_text(saver, row)
        if not body:
            safe_print("The Telegram text could not be read; paste the listing with --posting-file.")
            return 2

        if not Path(pipeline.python_executable()).exists():
            safe_print(f"Could not find the Python interpreter for the agents: {pipeline.python_executable()}")
            return 2
        missing = pipeline.missing_paths()
        if missing:
            safe_print("The agent folders were not found:")
            for label, path in missing:
                safe_print(f"  - {label}: {path}")
            return 2

        ok, detail = pipeline.server_reachable()
        if not ok:
            safe_print(f"[!!] llama-server is not reachable at {detail}")
            safe_print("     start it first, e.g.:  llama-server -m models/<model>.gguf -c 32768 --port 1234 --jinja")
            return 2

        path = pipeline.write_posting(
            body,
            link=_row_value(row, "link"),
            channel=_row_value(row, "chat_title"),
            posted=_row_value(row, "message_date"),
        )
        safe_print(f"posting: {pipeline.rel(path)} ({len(body)} chars)")

        key = pipeline.state_key(row)
        outcome = pipeline.tailor(path, letter_only=letter_only)
        pipeline.record(key, posting=pipeline.rel(path), tailored_at=pipeline.now_iso())
        if not outcome.ok:
            return 2 if outcome.exit_code == 2 else 1

        if note_mode == "none":
            return 0

        applied = note_mode == "applied"
        note_out = pipeline.note(path, applied=applied)
        if note_out.ok:
            notes = [
                item
                for item in note_out.changed
                if item.startswith("25 - Opportunities Pipeline/Jobs & Internships/")
            ]
            pipeline.record(
                key,
                note=notes[0] if notes else "",
                applied=applied,
                note_at=pipeline.now_iso(),
            )
        return 0 if note_out.ok else 1
    finally:
        await saver.disconnect()
        saver.store.close()


async def cmd_note(
    config: AppConfig,
    saved_id: int,
    *,
    applied: bool = False,
    posting_file: str | None = None,
    from_telegram: bool = False,
) -> int:
    """One saved post -> a job note, without running agent_tailer."""
    body: str | None = _posting_from_file(posting_file).strip() if posting_file else None
    if not body and not from_telegram:
        safe_print("The saved Telegram post is usually only the short version of the listing.")
        safe_print("Pass the full text you copied from the link:")
        safe_print(f'  ... main.py --note {saved_id} --posting-file full-posting.txt')
        safe_print(f'  ... main.py --note {saved_id} --posting-file -     (pipe it in)')
        safe_print(f'  ... main.py --note {saved_id} --from-telegram      (use the short text)')
        return 2

    saver = JobSaver(config)
    try:
        await saver.connect()
        row = saver.store.get_saved(saved_id)
        if row is None:
            safe_print(f"No saved post with database id {saved_id}. Run --review to see the ids.")
            return 2

        if not body:
            body = await _fetch_post_text(saver, row)
        if not body:
            safe_print("The Telegram text could not be read; paste the listing with --posting-file.")
            return 2

        ok, detail = pipeline.server_reachable()
        if not ok:
            safe_print(f"[!!] llama-server is not reachable at {detail}")
            return 2

        path = pipeline.write_posting(
            body,
            link=_row_value(row, "link"),
            channel=_row_value(row, "chat_title"),
            posted=_row_value(row, "message_date"),
        )
        safe_print(f"posting: {pipeline.rel(path)} ({len(body)} chars)")
        outcome = pipeline.note(path, applied=applied)
        if outcome.ok:
            notes = [
                item
                for item in outcome.changed
                if item.startswith("25 - Opportunities Pipeline/Jobs & Internships/")
            ]
            pipeline.record(
                pipeline.state_key(row),
                note=notes[0] if notes else "",
                applied=applied,
                note_at=pipeline.now_iso(),
            )
        return 0 if outcome.ok else 1
    finally:
        await saver.disconnect()
        saver.store.close()


async def cmd_review(config: AppConfig, null_first: bool = False) -> int:
    saver = JobSaver(config)
    try:
        await saver.connect()
        rows = saver.store.review_jobs(null_first=null_first)
        if not rows:
            safe_print("Nothing to review — no saved posts yet.")
            return 0
        if null_first:
            safe_print(
                f"Reviewing {len(rows)} saved post(s) — posts without a deadline "
                "first (oldest posted first)."
            )
        else:
            safe_print(f"Reviewing {len(rows)} saved post(s) in deadline order.")
        safe_print("Press Enter (or 'o') to open the current post in Telegram.")

        # ------------------------------------------------------------- helpers
        def stage(body: str, row: Any | None) -> tuple[Path, str | None]:
            """Write the posting to the shared inbox and return (path, state key)."""
            path = pipeline.write_posting(
                body,
                link=_row_value(row, "link"),
                channel=_row_value(row, "chat_title"),
                posted=_row_value(row, "message_date"),
            )
            safe_print(f"  posting saved: {pipeline.rel(path)} ({len(body)} chars)")
            return path, (pipeline.state_key(row) if row is not None else None)

        def prompt_note(body_path: Path, key: str | None, *, tailored: bool) -> None:
            mode = _choose_note_mode("research")
            if mode == "skip":
                return
            if mode == "applied" and not tailored:
                if not _confirm(
                    "  no letter was tailored for this post: the note would copy the current "
                    "master letter (from the last tailored job). Copy it anyway?",
                    default=False,
                ):
                    mode = "research"
            outcome = pipeline.note(body_path, applied=(mode == "applied"))
            if outcome.ok and key:
                notes = [
                    item
                    for item in outcome.changed
                    if item.startswith("25 - Opportunities Pipeline/Jobs & Internships/")
                ]
                pipeline.record(
                    key,
                    note=notes[0] if notes else "",
                    applied=(mode == "applied"),
                    note_at=pipeline.now_iso(),
                )

        async def obtain_posting(row: Any) -> str | None:
            """The full listing. Telegram usually holds only the short teaser."""
            link = _row_value(row, "link")
            teaser = await _fetch_post_text(saver, row)
            if teaser:
                safe_print(f"  telegram: {pipeline.first_line(teaser)}")
            safe_print(f"  link:     {link or '(no stored link)'}")

            body = _read_pasted(
                "",
                hint=(
                    "paste the FULL posting (open the link), then a line containing only  EOF\n"
                    "  just type EOF to use the Telegram text instead"
                ),
            )
            if body.strip():
                return body
            if teaser:
                safe_print("  ! nothing pasted - using the Telegram text (usually the short version)")
                return teaser
            safe_print("  ! nothing was pasted and the Telegram text is unavailable")
            return None

        async def apply_row(row: Any, *, letter_only: bool = False) -> None:
            """a: get the full posting, offer tailoring, then offer to record it."""
            body = await obtain_posting(row)
            if body is None:
                return
            safe_print(f"  posting: {pipeline.first_line(body)}  ({len(body)} chars)")

            if letter_only:
                want_tailor = _confirm("  tailor the cover letter for this post? (no CV)", default=True)
            else:
                want_tailor = _confirm("  tailor the CV + cover letter for this post?", default=True)

            path, key = stage(body, row)
            if not want_tailor:
                prompt_note(path, key, tailored=False)
                return

            outcome = pipeline.tailor(path, letter_only=letter_only)
            if key:
                pipeline.record(key, posting=pipeline.rel(path), tailored_at=pipeline.now_iso())
            if not outcome.ok:
                safe_print("  ! tailoring did not produce a usable result; nothing was recorded")
                return
            if key:
                pipeline.record(key, artifacts=outcome.changed)
            prompt_note(path, key, tailored=True)

        async def note_row(row: Any) -> None:
            """j: record the post without running agent_tailer."""
            body = await obtain_posting(row)
            if body is None:
                return
            safe_print(f"  posting: {pipeline.first_line(body)}  ({len(body)} chars)")
            path, key = stage(body, row)
            prompt_note(path, key, tailored=False)

        def paste_row(first: str = "") -> None:
            body = _read_pasted(first)
            if not body.strip():
                safe_print("  ! empty posting, ignored")
                return
            safe_print(f"  {pipeline.first_line(body)}  ({len(body)} chars)")
            want_tailor = _confirm("  tailor the CV + cover letter for this post?", default=True)
            path, _key = stage(body, None)
            if not want_tailor:
                prompt_note(path, None, tailored=False)
                return
            outcome = pipeline.tailor(path)
            if not outcome.ok:
                safe_print("  ! tailoring did not produce a usable result; nothing was recorded")
                return
            prompt_note(path, None, tailored=True)

        def show_row(row: Any, body: str | None) -> None:
            if body is None:
                safe_print("  ! no text available; press o to open the post in Telegram")
                return
            safe_print("")
            safe_print("-" * 72)
            for line in body.splitlines()[:60]:
                safe_print("  " + line)
            if len(body.splitlines()) > 60:
                safe_print("  … (truncated)")
            safe_print("-" * 72)

        # ---------------------------------------------------------------- loop
        open_next = False
        for index, row in enumerate(rows, 1):
            link = row["link"]
            deep_link = telegram_deep_link(link, row["chat_id"], row["message_id"])
            posted = (row["message_date"] or "")[:10] or "unknown"
            matched = (_row_value(row, "matched") or "").strip() or "unknown"
            mark = pipeline.badge(pipeline.entry_for(pipeline.state_key(row)))
            safe_print(
                f"\n[{index}/{len(rows)}] id={_row_value(row, 'id', '?')} {row['chat_title']} "
                f"matched={matched} deadline={row['deadline'] or 'unknown'} "
                f"posted={posted}{mark}"
            )
            safe_print(f"  {link or deep_link or '(no link available)'}")

            if open_next:
                open_next = False
                _open_review_post(deep_link, link)

            while True:
                try:
                    raw = input(
                        "o/Enter=open  n=next  >=skip+open  x=delete+open  d=delete  q=quit\n"
                        "a=apply  j=note only  l=letter only  s=show text  p=paste a post\n"
                        "a/l/j ask for the FULL posting: open the link, paste it, then a line with  EOF\n"
                        "review> "
                    )
                except EOFError:
                    return 0
                choice = raw.strip().lower()

                if choice in ("", "o", "open"):
                    _open_review_post(deep_link, link)
                    continue
                if choice == "n":
                    break
                if choice in (">", "n+o", "next-open"):
                    # Skip this post untouched, but carry on in Telegram.
                    open_next = True
                    break
                if choice in ("x", "do", "d+o"):
                    saver.store.delete_saved(row["chat_id"], row["message_id"])
                    safe_print("  deleted from the review list")
                    open_next = True
                    break
                if choice == "d":
                    saver.store.delete_saved(row["chat_id"], row["message_id"])
                    safe_print("  deleted from the review list")
                    break
                if choice == "q":
                    return 0
                if choice in ("a", "apply", "t", "tailor"):
                    await apply_row(row)
                    continue
                if choice in ("l", "letter"):
                    await apply_row(row, letter_only=True)
                    continue
                if choice == "j":
                    await note_row(row)
                    continue
                if choice in ("s", "show"):
                    show_row(row, await _fetch_post_text(saver, row))
                    continue
                if choice in ("p", "paste"):
                    paste_row()
                    continue
                if _looks_like_posting(raw):
                    # Telegram pastes usually arrive as free text, not as "p" first.
                    paste_row(raw)
                    continue
                safe_print("  ? enter o/Enter, a, n, >, x, d, q, j, l, s or p")
    finally:
        await saver.disconnect()
        saver.store.close()
    return 0



# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Read job channels/groups, match ordered rules, save matches to Saved Messages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "date formats: '2026-11-14', '2026-11-14 15:34:45', "
            "'2026-11-14T15:34:45+03:00', 'now', 'yesterday'\n\n"
            "examples:\n"
            "  python main.py --login\n"
            "  python main.py --chats\n"
            "  python main.py --sources add \"@hahujobs\" -1001212256595\n"
            "  python main.py --sources set \"@hahujobs\" \"@geezjobs_ethiopia\"\n"
            "  python main.py --sources list\n"
            "  python main.py --rules                    # list the ordered rules\n"
            "  python main.py --rules add \"w:IT\" \"!internship\"\n"
            "  python main.py --rules set \"Software\" \"!Marketing\"\n"
            "  python main.py --from 2026-11-01 --to 2026-11-16 --once\n"
            "  python main.py --from '2026-11-14 15:34:45' --mark-read --once\n"
            "  python main.py --from 2026-11-01 --replace-saved --once   # re-send already-saved posts\n"
            "  python main.py --review          # open saved posts in deadline order\n"
            "  python main.py --review --null-first   # start with missing deadlines\n"
            "  python main.py --tailor 42 --posting-file full-posting.txt\n"
            "                                     # tailor one saved post (full text you pasted)\n"
            "  python main.py --note 42 --posting-file - --note-mode applied\n"
            "  python main.py --pipeline-check  # agent paths + model server status\n"
            "  python main.py --checkpoint\n"
            "  python main.py --reset-checkpoint --once\n"
        ),
    )
    parser.add_argument(
        "-c", "--config",
        default=str(Path(__file__).resolve().parent / "config.yaml"),
        help="config file (default: config.yaml next to main.py)",
    )
    parser.add_argument("--login", action="store_true", help="authenticate and exit")
    parser.add_argument("--chats", action="store_true", help="list your channels/groups with ids and exit")
    parser.add_argument("--stats", action="store_true", help="show local stats and exit")
    parser.add_argument("--checkpoint", action="store_true", help="show per-chat scan progress and exit")
    parser.add_argument("--once", action="store_true", help="single scan pass, then exit")
    parser.add_argument("--dry-run", action="store_true", help="print matches without saving them")
    parser.add_argument("--no-save", dest="save_to_telegram", action="store_false", default=None, help="record matches locally without sending them to Saved Messages")
    parser.add_argument("--save", dest="save_to_telegram", action="store_true", help="send matches to Saved Messages (default)")
    parser.add_argument("--review", action="store_true", help="review saved posts in deadline order; Enter/o opens the post, > skips to the next one, x deletes it, a tailors it, j writes the note")
    parser.add_argument("--null-first", action="store_true", help="with --review: start with posts that have no detected deadline (oldest posted first)")

    agents = parser.add_argument_group(
        "hand saved posts to the local agents (agent_tailer / agent_jobnote)"
    )
    agents.add_argument(
        "--tailor", type=int, metavar="ID", default=None,
        help="tailor one saved post (the id= shown by --review), then offer the job note",
    )
    agents.add_argument(
        "--note", type=int, metavar="ID", default=None,
        help="write the job note for one saved post, without tailoring",
    )
    agents.add_argument(
        "--letter-only", action="store_true",
        help="with --tailor: cover letter only (no CV, no PDF)",
    )
    agents.add_argument(
        "--note-mode", choices=("applied", "research", "none"), default="research",
        help="with --tailor/--note: applied note (letter copied) or research-only (default)",
    )
    agents.add_argument(
        "--posting-file", metavar="PATH", default=None,
        help="with --tailor/--note: the full posting text (the saved Telegram post is "
             "usually only the short version); '-' reads stdin",
    )
    agents.add_argument(
        "--from-telegram", action="store_true",
        help="with --tailor/--note: use the saved Telegram text even if it is the short version",
    )
    agents.add_argument(
        "--pipeline-check", action="store_true",
        help="show the agent paths, the shared inbox and the model server status, then exit",
    )

    window = parser.add_argument_group("scan window")
    window.add_argument(
        "--from", dest="from_date", metavar="WHEN",
        help="start of the window, e.g. '2026-11-01' (overrides the stored checkpoint)",
    )
    window.add_argument(
        "--to", dest="to_date", metavar="WHEN",
        help="end of the window, inclusive (default: now)",
    )
    window.add_argument(
        "--reset-checkpoint", action="store_true",
        help="forget stored scan progress and start over (saved history is kept)",
    )
    window.add_argument(
        "--replace-saved", dest="replace_saved", action="store_true", default=None,
        help="re-send and re-record messages already in Saved Messages (default: skip them)",
    )

    read_state = parser.add_mutually_exclusive_group()
    read_state.add_argument(
        "--mark-read", dest="mark_as_read", action="store_true", default=None,
        help="mark scanned messages as read",
    )
    read_state.add_argument(
        "--keep-unread", dest="mark_as_read", action="store_false", default=None,
        help="read without marking as read (default)",
    )

    lists = parser.add_argument_group(
        "manage the config lists (writes config.yaml, keeps its comments)"
    )
    lists.add_argument(
        "--sources", nargs="*", metavar="ACTION|REF", default=None,
        help="channels to watch: 'list' (default) | add REF... | set REF... | remove REF...",
    )
    lists.add_argument(
        "--rules", nargs="*", metavar="ACTION|RULE", default=None,
        help="filters.rules: 'list' (default) | add RULE... | set RULE... | remove RULE...",
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def apply_cli_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    """CLI values win over config.yaml."""
    tz = config.scan.tz

    if args.from_date:
        config.scan.from_date = parse_when(args.from_date, tz, "--from")
    if args.to_date:
        config.scan.to_date = parse_when(args.to_date, tz, "--to")
    if (
        config.scan.from_date
        and config.scan.to_date
        and config.scan.from_date > config.scan.to_date
    ):
        raise ConfigError(
            "--from is later than --to "
            f"({config.scan.from_date.isoformat()} > {config.scan.to_date.isoformat()})."
        )

    if args.mark_as_read is not None:
        config.scan.mark_as_read = args.mark_as_read
    if args.dry_run:
        config.runtime.dry_run = True
    if args.replace_saved is not None:
        config.runtime.replace_saved = args.replace_saved
    if args.save_to_telegram is not None:
        config.runtime.save_to_telegram = args.save_to_telegram


# --------------------------------------------------------------------------- #
# Managing sources / rules from the terminal
# --------------------------------------------------------------------------- #
LIST_ACTIONS = ("list", "add", "set", "remove")


def _parse_list_action(values: list[str] | None, flag: str) -> tuple[str, list[str]]:
    """Split ``["add", "@a", "-100…"]`` into an action and its values."""
    items = [value.strip() for value in (values or []) if value and value.strip()]
    if not items:
        return "list", []
    action = items[0].lower()
    if action not in LIST_ACTIONS:
        raise ConfigError(
            f"Unknown action {items[0]!r} for --{flag}.\n"
            f"  --{flag}                      show the current list\n"
            f"  --{flag} add VALUE ...        append (duplicates are ignored)\n"
            f"  --{flag} set VALUE ...        replace the whole list\n"
            f"  --{flag} remove VALUE ...     drop an exact entry"
        )
    return action, items[1:]


def _dedupe(values: list[str]) -> tuple[list[str], list[str]]:
    """Case-insensitive dedupe that keeps the first spelling and the order."""
    seen: set[str] = set()
    unique: list[str] = []
    duplicates: list[str] = []
    for value in values:
        key = value.strip().lower()
        if key in seen:
            duplicates.append(value)
            continue
        seen.add(key)
        unique.append(value)
    return unique, duplicates


def _show_list(title: str, values: list[str]) -> None:
    safe_print(f"{title} ({len(values)}):")
    if not values:
        safe_print("  (empty)")
        return
    width = len(str(len(values)))
    for index, value in enumerate(values, 1):
        safe_print(f"  {index:>{width}}. {value}")


def apply_list_action(
    current: list[str], action: str, items: list[str], title: str
) -> list[str]:
    """Return the new list for one action, printing what happened."""
    if action == "list":
        return current

    if action == "set":
        if not items:
            raise ConfigError("'set' needs at least one value.")
        new_values, duplicates = _dedupe(items)
        for value in duplicates:
            safe_print(f"  ! duplicate ignored: {value}")
        safe_print(f"{title}: replaced {len(current)} → {len(new_values)}.")
        return new_values

    if action == "add":
        if not items:
            raise ConfigError("'add' needs at least one value.")
        merged, duplicates = _dedupe(current + items)
        for value in duplicates:
            safe_print(f"  ! already there, skipped: {value}")
        added = len(merged) - len(current)
        safe_print(f"{title}: added {added} (now {len(merged)}).")
        return merged

    # remove
    if not items:
        raise ConfigError("'remove' needs at least one value.")
    wanted = {value.lower() for value in items}
    existing = {value.lower() for value in current}
    for value in items:
        if value.lower() not in existing:
            safe_print(f"  ! not in the list, ignored: {value}")
    kept = [value for value in current if value.lower() not in wanted]
    safe_print(f"{title}: removed {len(current) - len(kept)} (now {len(kept)}).")
    return kept


def cmd_manage_lists(config_path: str, args: argparse.Namespace) -> bool:
    """Apply --sources / --rules actions. Returns True if anything changed."""
    jobs = []
    if args.sources is not None:
        action, items = _parse_list_action(args.sources, "sources")
        jobs.append(("sources", None, action, items))
    if args.rules is not None:
        action, items = _parse_list_action(args.rules, "rules")
        jobs.append(("rules", "filters", action, items))

    changed = False
    for key, section, action, items in jobs:
        if action == "list":
            _show_list(f"{key.capitalize()} in {config_path}", read_list(config_path, key, section))
            continue

        current = read_list(config_path, key, section)
        updated = apply_list_action(current, action, items, key.capitalize())
        if updated != current:
            write_list(config_path, key, updated, section)
            changed = True
        _show_list(f"{key.capitalize()} in {config_path}", updated)

    if changed:
        safe_print("")
        safe_print(f"Saved to {Path(config_path).resolve()}")
    return changed



def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = build_parser().parse_args(argv)

    if args.null_first and not args.review:
        print("--null-first requires --review.", file=sys.stderr)
        return 2

    # --sources / --rules edit config.yaml directly, so they run before the
    # file is validated (you may be building the lists from an empty state).
    if args.sources is not None or args.rules is not None:
        try:
            cmd_manage_lists(args.config, args)
        except (ConfigError, ConfigFileError) as exc:
            print(f"Configuration error:\n{exc}", file=sys.stderr)
            return 2
        if not any((args.once, args.dry_run, args.stats, args.checkpoint, args.chats, args.review,
                    args.tailor is not None, args.note is not None, args.pipeline_check)):
            return 0

    try:
        config = load_config(args.config)
        apply_cli_overrides(config, args)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2

    setup_logging(config, args.verbose)

    if args.reset_checkpoint:
        with Store(config.database) as store:
            cleared = store.reset_checkpoints()
        safe_print(
            f"Cleared scan progress for {cleared} chat(s). "
            "Saved-message history kept (nothing will be duplicated)."
        )
        if not any((args.once, args.stats, args.checkpoint, args.review)):
            return 0

    try:
        if args.login:
            asyncio.run(cmd_login(config))
        elif args.chats:
            asyncio.run(cmd_chats(config))
        elif args.checkpoint:
            cmd_checkpoints(config)
        elif args.stats:
            cmd_stats(config)
        elif args.pipeline_check:
            pipeline.describe_health()
        elif args.tailor is not None:
            return asyncio.run(
                cmd_tailor(
                    config,
                    args.tailor,
                    letter_only=args.letter_only,
                    note_mode="none" if args.note_mode == "none" else args.note_mode,
                    posting_file=args.posting_file,
                    from_telegram=args.from_telegram,
                )
            )
        elif args.note is not None:
            return asyncio.run(
                cmd_note(
                    config,
                    args.note,
                    applied=(args.note_mode == "applied"),
                    posting_file=args.posting_file,
                    from_telegram=args.from_telegram,
                )
            )
        elif args.review:
            asyncio.run(cmd_review(config, null_first=args.null_first))
        else:
            return asyncio.run(cmd_run(config, once=args.once))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
