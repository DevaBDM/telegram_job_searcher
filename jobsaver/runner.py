"""Telethon runner: read the configured channels, match, re-send to Saved Messages.

Where scanning starts (per chat, highest priority first)
--------------------------------------------------------
1. ``scan.from_date`` (or ``--from``)  -> that exact moment, ignoring checkpoints.
2. a stored checkpoint + ``scan.resume: true`` -> where the previous run stopped.
3. otherwise -> only posts newer than the last message id already seen.

Scanning always stops at ``scan.to_date`` (or "now" when unset).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from telethon import TelegramClient, errors, utils

from .config import AppConfig, ConfigError
from .console import safe_print
from .deadline import extract_deadline
from .matcher import KeywordMatcher, MatchResult
from .store import Store

log = logging.getLogger("jobsaver")

FLOOD_EXTRA_SLEEP = 5  # seconds added on top of Telegram's own FloodWait value
MAX_SAVE_ATTEMPTS = 3  # give up on a single post after this many failed cycles


def _stdin_is_interactive() -> bool:
    """True when we can safely prompt for a login code."""
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (ValueError, AttributeError, OSError):
        return False


@dataclass
class FetchResult:
    """What was pulled from a chat this cycle, and how the window was chosen."""

    messages: List[Any] = field(default_factory=list)
    mode: str = "incremental"  # "range" | "resume" | "incremental"
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


class JobSaver:
    def __init__(self, config: AppConfig):
        self.cfg = config
        self.matcher = KeywordMatcher(config.filters)
        self.store = Store(config.database)
        self.client = TelegramClient(
            str(config.telegram.session_path),
            config.telegram.api_id,
            config.telegram.api_hash,
            device_model="JobSaver",
            system_version="1.0",
            app_version="1.0",
            flood_sleep_threshold=120,  # auto-sleep for short flood waits
        )
        self._entity_cache: Dict[str, Any] = {}
        self._dialog_index: Optional[Dict[str, Any]] = None
        self._failed_saves: Dict[tuple[int, int], int] = {}
        # Where this process has scanned up to, per chat. An explicit from_date
        # overrides the stored checkpoint once, but successive passes within the
        # same run must keep moving forward.
        self._progress: Dict[int, datetime] = {}

    # ------------------------------------------------------------------ #
    # Connection & authentication
    # ------------------------------------------------------------------ #
    async def ensure_authorized(self) -> None:
        """Make sure a usable session exists, or explain how to create one.

        A Telegram session = a file under ``sessions/`` created once by
        ``python main.py --login``. After that every run reuses it silently.
        """
        if await self.client.is_user_authorized():
            return

        if not _stdin_is_interactive():
            raise ConfigError(
                "Not logged in: no usable session at "
                f"{self.cfg.telegram.session_path}\n"
                "Run `python main.py --login` once in a terminal to authenticate "
                "(Telegram will ask for a code and, if enabled, your 2FA password)."
            )

        phone = self.cfg.telegram.phone or None
        await self.client.start(phone=phone)

    async def connect(self) -> bool:
        await self.client.connect()
        await self.ensure_authorized()

        me = await self.client.get_me()
        if me is None:  # pragma: no cover - defensive
            raise ConfigError("Login failed: no user returned by Telegram.")
        name = " ".join(part for part in (me.first_name, me.last_name) if part) or str(me.id)
        safe_print(f"✓ Logged in as {name} (@{me.username or 'no-username'})")
        return True

    async def disconnect(self) -> None:
        await self.client.disconnect()

    # ------------------------------------------------------------------ #
    # Source resolution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_ref(ref: str) -> str:
        """Accept '@name', '-100…', 'https://t.me/name', 't.me/name/123', 't.me/+invite'."""
        value = ref.strip()
        if "t.me/" in value:
            value = value.split("t.me/", 1)[1]
        value = value.strip().strip("/")
        if value.startswith("+") or value.startswith("joinchat/"):
            return value
        if "/" in value:  # a link to a specific post -> keep just the channel
            value = value.split("/", 1)[0]
        return value

    async def _load_dialogs(self) -> None:
        """Build a lookup table of every chat we can see (needed for raw -100 ids)."""
        log.info("Building chat index from your dialog list (one time)...")
        index: Dict[str, Any] = {}
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            index[str(dialog.id)] = entity
            index[f"-100{entity.id}"] = entity
            if getattr(entity, "username", None):
                index[entity.username.lower()] = entity
        self._dialog_index = index
        log.info("Indexed %d chats.", len(index))

    async def resolve(self, ref: str) -> Any:
        cached = self._entity_cache.get(ref)
        if cached is not None:
            return cached

        normalized = self._normalize_ref(ref)

        # 1) Let Telethon resolve it (works for @username and known ids).
        candidates: List[Any] = [normalized]
        if normalized.lstrip("-").isdigit():
            candidates = [int(normalized)]
        for candidate in candidates:
            try:
                entity = await self.client.get_entity(candidate)
                self._entity_cache[ref] = entity
                return entity
            except (ValueError, errors.RPCError, TypeError):
                pass

        # 2) Fall back to an index of your dialogs.
        if self._dialog_index is None:
            try:
                await self._load_dialogs()
            except Exception as exc:  # noqa: BLE001 - any failure is non fatal here
                log.warning("Could not build dialog index: %s", exc)
                self._dialog_index = {}

        lookup_key = normalized.lstrip("@").lower()
        entity = (self._dialog_index or {}).get(lookup_key)
        if entity is not None:
            self._entity_cache[ref] = entity
            return entity

        raise ConfigError(
            f"Cannot resolve source {ref!r}. Make sure you are a member of that "
            "channel/group, or run `python main.py --chats` to copy its exact id."
        )

    async def resolve_sources(self) -> List[Any]:
        entities: List[Any] = []
        for ref in self.cfg.sources:
            try:
                entities.append(await self.resolve(ref))
            except Exception as exc:  # noqa: BLE001
                log.error("Skipping %s — %s", ref, exc)
        return entities

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def build_link(chat: Any, message_id: int) -> Optional[str]:
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{message_id}"

        # Private channels/supergroups: https://t.me/c/<internal_id>/<message_id>
        internal_id = getattr(chat, "id", None)
        if internal_id is None:
            try:
                peer_id = str(utils.get_peer_id(chat))
            except Exception:  # noqa: BLE001
                return None
            if peer_id.startswith("-100"):
                return f"https://t.me/c/{peer_id[4:]}/{message_id}"
            return None
        return f"https://t.me/c/{internal_id}/{message_id}"

    async def _flood_sleep(self, exc: errors.FloodWaitError) -> None:
        seconds = int(getattr(exc, "seconds", 60)) + FLOOD_EXTRA_SLEEP
        log.warning("Telegram flood limit: sleeping %ds...", seconds)
        await asyncio.sleep(seconds)

    def _too_old(self, date: Optional[datetime]) -> bool:
        days = self.cfg.runtime.max_age_days
        if not days or date is None:
            return False
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date < datetime.now(timezone.utc) - timedelta(days=days)

    def _format_copy(self, chat: Any, message: Any, result: MatchResult, link: Optional[str]) -> str:
        name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(message.chat_id)
        date = message.date.strftime("%Y-%m-%d %H:%M UTC") if message.date else ""
        try:
            header = self.cfg.saving.header.format(
                channel=name, date=date, keywords=", ".join(result.keywords), link=link or ""
            )
        except (KeyError, IndexError):
            header = f"🎯 {name} · {date}"

        parts: List[str] = []
        if header.strip():
            parts.append(header.strip())
        body = (message.text or "").strip()
        if body:
            parts.append(body)
        if self.cfg.saving.include_link and link:
            parts.append(link)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    # Saving
    # ------------------------------------------------------------------ #
    async def save(self, chat: Any, message: Any, result: MatchResult) -> str:
        """Send one matched post to the destination. Returns the mode used."""
        destination = self.cfg.saving.destination
        mode = self.cfg.saving.mode
        link = self.build_link(chat, message.id)

        if mode in ("auto", "forward"):
            forwarded = False
            for attempt in (1, 2):
                try:
                    await self.client.forward_messages(destination, message)
                    forwarded = True
                    break
                except errors.FloodWaitError as exc:
                    if attempt == 2:
                        raise
                    await self._flood_sleep(exc)
                except Exception as exc:  # noqa: BLE001
                    if mode == "forward":
                        raise
                    log.warning(
                        "Forward blocked for message %s (%s) - falling back to copy.",
                        message.id,
                        type(exc).__name__,
                    )
                    break

            if forwarded:
                if self.cfg.saving.append_note:
                    try:
                        await self.client.send_message(
                            destination,
                            "🔎 matched: " + ", ".join(result.keywords),
                            link_preview=False,
                        )
                    except Exception as exc:  # noqa: BLE001 - the post itself is saved
                        log.warning("Could not send the matched-keywords note: %s", exc)
                return "forward"

        text = self._format_copy(chat, message, result, link)
        await self.client.send_message(destination, text, link_preview=False)
        return "copy"

    async def _save_with_retry(self, entity: Any, message: Any, result: MatchResult) -> str:
        try:
            return await self.save(entity, message, result)
        except errors.FloodWaitError as exc:
            await self._flood_sleep(exc)
            return await self.save(entity, message, result)

    # ------------------------------------------------------------------ #
    # Scan window
    # ------------------------------------------------------------------ #
    def window_for(self, chat_id: int) -> tuple[Optional[datetime], Optional[datetime], str]:
        """Return (start, end, mode) for this chat.

        ``start is None`` means "incremental": only messages newer than the
        stored message id are fetched.
        """
        scan = self.cfg.scan
        end = scan.to_date

        if scan.from_date is not None:
            # The explicit range wins over the stored checkpoint, but a window
            # larger than one batch must still advance pass by pass.
            start = scan.from_date
            progressed = self._progress.get(chat_id)
            if progressed is not None and progressed > start:
                start = progressed
            return start, end, "range"

        if scan.resume:
            checkpoint = self.store.get_checkpoint(chat_id)
            if checkpoint is not None:
                # Step back one second so a post sharing the checkpoint's
                # second is not skipped; duplicates are filtered by was_saved().
                return checkpoint - timedelta(seconds=1), end, "resume"

        return None, end, "incremental"

    async def _fetch_incremental(self, entity: Any, last_id: int) -> List[Any]:
        runtime = self.cfg.runtime
        if last_id:
            # oldest first, so a limited batch never leaves a gap
            return list(
                await self.client.get_messages(
                    entity,
                    min_id=last_id,
                    limit=runtime.max_messages_per_poll,
                    reverse=True,
                )
            )
        messages = await self.client.get_messages(entity, limit=runtime.initial_scan_limit)
        return list(reversed(messages))  # process oldest -> newest

    async def _anchor_id(self, entity: Any, start: datetime) -> int:
        """Id of the newest message at/before ``start`` (0 if the chat is younger).

        Anchoring by id (instead of relying on reverse+offset_date semantics)
        keeps the forward pagination below exact and gap-free.
        """
        result = await self.client.get_messages(
            entity, offset_date=start + timedelta(seconds=1), limit=1
        )
        if not result:
            return 0
        newest = result[0] if isinstance(result, (list, tuple)) else result
        return max(int(newest.id) - 1, 0)

    async def _fetch_window(
        self, entity: Any, start: datetime, end: Optional[datetime]
    ) -> List[Any]:
        """Every message with ``start <= date <= end``, oldest first.

        Walks forward from ``start`` (bounded by ``max_history_messages``), so a
        big window is consumed in successive passes without ever skipping a
        post: the checkpoint advances to the newest message of each batch.
        """
        cap = self.cfg.scan.max_history_messages
        min_id = await self._anchor_id(entity, start)
        collected: List[Any] = []
        stopped_at_end = False

        async for message in self.client.iter_messages(
            entity, reverse=True, min_id=min_id, limit=cap
        ):
            if message.date is None:
                continue
            if end is not None and message.date > end:
                stopped_at_end = True
                break
            if message.date < start:  # the anchor sits just before the window
                continue
            collected.append(message)

        if len(collected) >= cap and not stopped_at_end:
            log.warning(
                "History window hit the %d-message cap for %s; the rest is "
                "picked up by the next pass (nothing is skipped). If you use "
                "--once, raise scan.max_history_messages or omit --once so the "
                "window is consumed in batches.",
                cap,
                getattr(entity, "title", entity),
            )

        return collected

    async def fetch_messages(self, entity: Any) -> FetchResult:
        chat_id = utils.get_peer_id(entity)
        start, end, mode = self.window_for(chat_id)
        try:
            if start is None:
                messages = await self._fetch_incremental(
                    entity, self.store.get_last_message_id(chat_id)
                )
            else:
                messages = await self._fetch_window(entity, start, end)
        except errors.FloodWaitError as exc:
            await self._flood_sleep(exc)
            return FetchResult([], mode, start, end)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Could not fetch messages from %s: %s", getattr(entity, "title", entity), exc
            )
            return FetchResult([], mode, start, end)

        log.debug(
            "%s: %d message(s), mode=%s, window=%s..%s",
            getattr(entity, "title", entity),
            len(messages),
            mode,
            start.isoformat() if start else "cursor",
            end.isoformat() if end else "now",
        )
        return FetchResult(messages, mode, start, end)

    # ------------------------------------------------------------------ #
    # Read state
    # ------------------------------------------------------------------ #
    async def mark_read(self, entity: Any, title: str, max_id: int) -> None:
        """Mark everything up to ``max_id`` as read (best effort)."""
        try:
            await self.client.send_read_acknowledge(entity, max_id=max_id)
            log.debug("[%s] marked as read up to #%s", title, max_id)
        except errors.FloodWaitError as exc:
            await self._flood_sleep(exc)
        except Exception as exc:  # noqa: BLE001 - cosmetic, never fail the scan
            log.warning("[%s] could not mark as read: %s", title, exc)

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #
    async def scan_chat(self, entity: Any) -> int:
        chat_id = utils.get_peer_id(entity)
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id)

        fetched = await self.fetch_messages(entity)
        messages = fetched.messages
        if not messages:
            return 0

        saved_count = 0
        highest_id = self.store.get_last_message_id(chat_id)

        for message in messages:
            highest_id = max(highest_id, message.id)

            if getattr(message, "action", None) is not None:  # service message (join, pin…)
                continue
            if fetched.mode == "incremental" and self._too_old(message.date):
                continue

            result = self.matcher.match(message.text)
            if not result.matched:
                if result.excluded_by:
                    log.debug("[%s] #%s excluded by %r", title, message.id, result.excluded_by)
                continue

            # With --replace-saved we intentionally re-send posts that are
            # already in saved_messages; otherwise skip them.
            if not self.cfg.runtime.replace_saved and self.store.was_saved(chat_id, message.id):
                log.debug("[%s] #%s already saved, skipping", title, message.id)
                continue

            if self.cfg.runtime.dry_run:
                safe_print(f"  · [DRY-RUN] {title} #{message.id} → {', '.join(result.keywords)}")
                continue

            key = (chat_id, message.id)
            try:
                used_mode = await self._save_with_retry(entity, message, result)
            except Exception as exc:  # noqa: BLE001
                failures = self._failed_saves.get(key, 0) + 1
                self._failed_saves[key] = failures
                if failures >= MAX_SAVE_ATTEMPTS:
                    # Don't let one broken post block the channel forever.
                    self._failed_saves.pop(key, None)
                    log.error(
                        "[%s] #%s could not be saved after %d attempts, giving up: %s",
                        title, message.id, failures, exc,
                    )
                    continue
                log.error(
                    "[%s] #%s could not be saved (attempt %d), retrying next cycle: %s",
                    title, message.id, failures, exc,
                )
                highest_id = message.id - 1  # keep the cursor here so it is retried
                break
            self._failed_saves.pop(key, None)

            deadline = extract_deadline(message.text)
            deadline_str = deadline.isoformat() if deadline else None

            self.store.record_saved(
                chat_id=chat_id,
                chat_title=title,
                message_id=message.id,
                message_date=message.date.isoformat() if message.date else None,
                matched=result.keywords,
                mode=used_mode,
                link=self.build_link(entity, message.id),
                deadline=deadline_str,
                replace=self.cfg.runtime.replace_saved,
            )
            saved_count += 1
            deadline_tag = f" ⏰ deadline {deadline}" if deadline else ""
            safe_print(f"  ✓ {title} #{message.id} → Saved Messages ({', '.join(result.keywords)}){deadline_tag}")
            log.info(
                "saved chat=%s msg=%s keywords=%s mode=%s",
                title,
                message.id,
                result.keywords,
                used_mode,
            )

        # Advance the checkpoint to the newest message we actually finished.
        finished = [m for m in messages if m.id <= highest_id and m.date]
        scanned_until = max((m.date for m in finished), default=None)

        if scanned_until is not None:
            previous = self._progress.get(chat_id)
            if previous is None or scanned_until > previous:
                self._progress[chat_id] = scanned_until

        if highest_id or scanned_until:
            self.store.set_checkpoint(
                chat_id,
                title=title,
                scanned_until=scanned_until,
                last_message_id=highest_id,
            )

        if self.cfg.scan.mark_as_read and highest_id and not self.cfg.runtime.dry_run:
            await self.mark_read(entity, title, highest_id)

        return saved_count

    async def scan_cycle(self) -> int:
        entities = await self.resolve_sources()
        total = 0
        for entity in entities:
            try:
                total += await self.scan_chat(entity)
            except errors.FloodWaitError as exc:
                await self._flood_sleep(exc)
            except Exception as exc:  # noqa: BLE001
                log.error("Scan failed for %s: %s", getattr(entity, "title", entity), exc)
        return total

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def describe_scan(self) -> str:
        scan = self.cfg.scan
        if scan.from_date is not None:
            start = f"from {scan.from_date:%Y-%m-%d %H:%M:%S %Z}"
        elif scan.resume:
            start = "from each chat's checkpoint"
        else:
            start = "new posts only"
        end = f"to {scan.to_date:%Y-%m-%d %H:%M:%S %Z}" if scan.to_date else "to now"
        read = "mark as read" if scan.mark_as_read else "leave unread"
        return f"{start} {end} · {read}"

    def window_complete(self) -> bool:
        """True when an explicit end date has already passed."""
        to_date = self.cfg.scan.to_date
        if to_date is None:
            return False
        return to_date <= datetime.now(timezone.utc)

    async def run(self, once: bool = False) -> None:
        await self.connect()
        mode = "DRY-RUN" if self.cfg.runtime.dry_run else self.cfg.saving.mode
        safe_print(
            f"Watching {len(self.cfg.sources)} source(s) · saving mode: {mode} · "
            f"destination: {self.cfg.saving.destination}"
        )
        safe_print(f"Scan: {self.describe_scan()}")
        if not once:
            safe_print(f"Polling every {self.cfg.runtime.poll_interval_seconds}s. Press Ctrl+C to stop.\n")
        try:
            while True:
                started = datetime.now()
                saved = await self.scan_cycle()
                elapsed = (datetime.now() - started).total_seconds()
                log.info("cycle done: %d saved in %.1fs", saved, elapsed)
                if once:
                    safe_print(f"Done. {saved} new matching post(s) saved.")
                    break
                if self.window_complete():
                    safe_print(
                        f"Date window complete (end {self.cfg.scan.to_date:%Y-%m-%d %H:%M:%S %Z}). "
                        f"{saved} post(s) saved this pass. Stopping."
                    )
                    break
                if saved == 0:
                    log.debug("nothing new (%.1fs)", elapsed)
                await asyncio.sleep(self.cfg.runtime.poll_interval_seconds)
        finally:
            await self.disconnect()
            self.store.close()
