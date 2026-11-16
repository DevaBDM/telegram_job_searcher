#!/usr/bin/env python3
"""Command line entry point.

Usage
-----
    python main.py --login                     # log in once, create the session file
    python main.py --chats                     # list your channels/groups with their ids
    python main.py                             # run forever, polling for new posts
    python main.py --once                      # single pass, then exit

    # scan a specific date range (overrides the stored checkpoint)
    python main.py --from "2026-11-01" --to "2026-11-16 23:59" --once
    python main.py --from "2026-11-10 08:00" --mark-read --once

    # resume from the last stored checkpoint (default behaviour)
    python main.py --once
    python main.py --reset-checkpoint --once   # forget progress, start over
    python main.py --checkpoint                # show per-chat progress
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from jobsaver.config import AppConfig, ConfigError, load_config, parse_when
from jobsaver.console import configure_output, safe_print
from jobsaver.runner import JobSaver
from jobsaver.store import Store, from_db


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(config: AppConfig, verbose: bool) -> None:
    log_path = Path(config.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
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
            safe_print(
                f"  {row['saved_at']}  {row['chat_title']} #{row['message_id']} "
                f"[{row['matched']}] {row['link'] or ''}"
            )


async def cmd_run(config: AppConfig, once: bool) -> int:
    saver = JobSaver(config)
    await saver.run(once=once)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Read job channels/groups, match keywords, save matches to Saved Messages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "date formats: '2026-11-14', '2026-11-14 15:34:45', "
            "'2026-11-14T15:34:45+03:00', 'now', 'yesterday'\n\n"
            "examples:\n"
            "  python main.py --login\n"
            "  python main.py --chats\n"
            "  python main.py --from 2026-11-01 --to 2026-11-16 --once\n"
            "  python main.py --from '2026-11-14 15:34:45' --mark-read --once\n"
            "  python main.py --checkpoint\n"
            "  python main.py --reset-checkpoint --once\n"
        ),
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="config file (default: config.yaml)")
    parser.add_argument("--login", action="store_true", help="authenticate and exit")
    parser.add_argument("--chats", action="store_true", help="list your channels/groups with ids and exit")
    parser.add_argument("--stats", action="store_true", help="show local stats and exit")
    parser.add_argument("--checkpoint", action="store_true", help="show per-chat scan progress and exit")
    parser.add_argument("--once", action="store_true", help="single scan pass, then exit")
    parser.add_argument("--dry-run", action="store_true", help="print matches without saving them")

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

    read_state = parser.add_mutually_exclusive_group()
    read_state.add_argument(
        "--mark-read", dest="mark_as_read", action="store_true", default=None,
        help="mark scanned messages as read",
    )
    read_state.add_argument(
        "--keep-unread", dest="mark_as_read", action="store_false", default=None,
        help="read without marking as read (default)",
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


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = build_parser().parse_args(argv)

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
        if not any((args.once, args.stats, args.checkpoint)):
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
