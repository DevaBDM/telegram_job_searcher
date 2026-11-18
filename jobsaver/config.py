"""Configuration loading and validation for the Telegram job saver."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    """Raised when the config file is missing something required."""


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

VALID_MODES = {"auto", "forward", "copy"}

# Accepted date/time layouts, tried in order (ISO-8601 is tried first).
DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class TelegramConfig:
    api_id: int
    api_hash: str
    phone: str = ""
    session: str = "jobsaver"
    session_dir: str = "sessions"

    @property
    def session_path(self) -> Path:
        directory = Path(self.session_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        return directory / self.session


@dataclass
class FilterConfig:
    """Ordered matching rules. Index 0 has the highest priority.

    Rule forms (all live in ``rules``)::

        "Software Developer"      substring include
        "!Marketing Management"   substring exclude
        "w:IT"                    whole-word include (no fuzzy)
        "!w:IT"                   whole-word exclude (no fuzzy)
        "re:\\b(?:api|rest)\\b"     regular expression include (no fuzzy)
        "!re:\\bintern\\b"          regular expression exclude (no fuzzy)
    """

    rules: List[str] = field(default_factory=list)
    must_include: List[str] = field(default_factory=list)
    exclude_symbol: str = "!"
    case_sensitive: bool = False
    fuzzy: bool = False          # approximate matching for plain substring rules
    fuzzy_threshold: int = 70    # 0-100, lower = more lenient (only when fuzzy is true)


@dataclass
class SavingConfig:
    mode: str = "auto"
    destination: str = "me"
    include_link: bool = True
    append_note: bool = False
    header: str = "🎯 {channel} · {date}"


DEFAULT_HEADER = SavingConfig.header


@dataclass
class RuntimeConfig:
    poll_interval_seconds: int = 300
    initial_scan_limit: int = 200
    max_messages_per_poll: int = 100
    max_age_days: int = 14
    dry_run: bool = False
    replace_saved: bool = False  # re-send messages already in saved_messages
    save_to_telegram: bool = True


@dataclass
class ScanConfig:
    """Which slice of history to read, and what to do with read state."""

    from_date: Optional[datetime] = None      # None -> "resume" or "new posts only"
    to_date: Optional[datetime] = None        # None -> now
    resume: bool = True                       # continue from the stored checkpoint
    mark_as_read: bool = False                # False -> read without marking as read
    timezone_name: str = "UTC"                # how bare dates are interpreted
    tz: tzinfo = field(default_factory=lambda: timezone.utc)
    max_history_messages: int = 2000          # safety cap per chat per pass


@dataclass
class AppConfig:
    telegram: TelegramConfig
    sources: List[str]
    filters: FilterConfig
    saving: SavingConfig
    runtime: RuntimeConfig
    database: str = "jobsaver.sqlite3"
    log_file: str = "logs/jobsaver.log"
    scan: ScanConfig = field(default_factory=ScanConfig)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _expand_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` with the value of environment variable VAR."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _section(data: dict, name: str) -> dict:
    value = data.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' must be a mapping (key: value pairs).")
    return value


def _as_list(value: Any, field_name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ConfigError(f"'{field_name}' must be a list of strings.")
    result: List[str] = []
    for item in value:
        if isinstance(item, (int, float)):
            item = str(item)
        if not isinstance(item, str):
            raise ConfigError(f"'{field_name}' contains a non-string entry: {item!r}")
        if item.strip():
            result.append(item.strip())
    return result


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int, field_name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{field_name}' must be an integer, got {value!r}.") from None


def parse_timezone(name: str) -> tzinfo:
    """Resolve an IANA timezone name, with a built-in UTC fallback.

    Windows ships without the IANA database, so non-UTC zones need the
    ``tzdata`` package (listed in requirements.txt).
    """
    text = str(name).strip() or "UTC"
    if text.upper() in {"UTC", "GMT", "Z"}:
        return timezone.utc
    try:
        return ZoneInfo(text)
    except ZoneInfoNotFoundError:
        raise ConfigError(
            f"Unknown timezone {text!r}. Use an IANA name such as 'UTC', "
            "'Africa/Addis_Ababa' or 'Europe/Berlin'."
        ) from None
    except Exception:  # noqa: BLE001 - e.g. ModuleNotFoundError: tzdata on Windows
        raise ConfigError(
            f"Timezone {text!r} needs the 'tzdata' package, which is not installed.\n"
            "  pip install tzdata\n"
            "or set  scan.timezone: \"UTC\"  in config.yaml."
        ) from None


def parse_when(value: Any, tz: tzinfo, field_name: str) -> Optional[datetime]:
    """Parse a config/CLI date into a timezone-aware datetime.

    Accepts ISO-8601 (``2026-11-14``, ``2026-11-14 15:34:45``,
    ``2026-11-14T15:34:45+03:00``), several common layouts, and the
    keywords ``now`` / ``today`` / ``yesterday``.
    Naive values are interpreted in ``tz``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)

    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "~"}:
        return None

    lowered = text.lower()
    now = datetime.now(tz)
    if lowered in {"now", "today"}:
        return now
    if lowered == "yesterday":
        return now - timedelta(days=1)

    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ConfigError(
            f"'{field_name}' is not a valid date/time: {text!r}. "
            "Try '2026-11-14', '2026-11-14 15:34:45' or '2026-11-14T15:34:45+03:00'."
        )

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
def load_config(path: str | Path = "config.yaml") -> AppConfig:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            "Copy 'config.example.yaml' to 'config.yaml' and fill it in."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        try:
            raw = yaml.safe_load(handle) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Could not parse {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Top level of the config file must be a mapping.")

    data = _expand_env(raw)

    # Relative paths in the config are resolved against the config file itself,
    # so the program behaves the same no matter which directory it is started from.
    base_dir = config_path.parent.resolve()

    def resolve_path(value: str) -> str:
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else base_dir / path)

    # --- telegram -------------------------------------------------------- #
    tg = _section(data, "telegram")
    api_id = _as_int(tg.get("api_id"), 0, "telegram.api_id")
    api_hash = str(tg.get("api_hash") or "").strip()
    phone = str(tg.get("phone") or "").strip()

    problems = []
    if not api_id:
        problems.append("telegram.api_id is missing (get it from https://my.telegram.org)")
    if not api_hash or api_hash.upper() == "TODO":
        problems.append("telegram.api_hash is missing (get it from https://my.telegram.org)")
    if problems:
        raise ConfigError("Invalid telegram settings:\n  - " + "\n  - ".join(problems))

    telegram = TelegramConfig(
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        session=str(tg.get("session") or "jobsaver"),
        session_dir=resolve_path(str(tg.get("session_dir") or "sessions")),
    )

    # --- sources --------------------------------------------------------- #
    sources = _as_list(data.get("sources"), "sources")
    if not sources:
        raise ConfigError(
            "No 'sources' configured. Add at least one channel/group "
            "(@username, -100… id or t.me link)."
        )

    # --- filters --------------------------------------------------------- #
    raw_filters = _section(data, "filters")
    removed = [
        key
        for key in ("keywords", "priority_keywords", "exclude", "whole_word")
        if key in raw_filters
    ]
    if removed:
        raise ConfigError(
            "These filters.* keys were removed: " + ", ".join(removed) + ".\n"
            "Everything now lives in 'filters.rules' (index 0 = highest priority):\n"
            "  - plain entry              → substring match\n"
            "  - '!entry'                 → exclusion\n"
            "  - 'w:entry' / '!w:entry'   → whole-word match, no fuzzy\n"
            "  - 're:...' / '!re:...'     → regular expression, no fuzzy\n"
            "Move those entries into 'filters.rules' and delete the old keys."
        )

    filters = FilterConfig(
        rules=_as_list(raw_filters.get("rules"), "filters.rules"),
        must_include=_as_list(raw_filters.get("must_include"), "filters.must_include"),
        exclude_symbol=str(raw_filters.get("exclude_symbol") or "!"),
        case_sensitive=_as_bool(raw_filters.get("case_sensitive"), False),
        fuzzy=_as_bool(raw_filters.get("fuzzy"), False),
        fuzzy_threshold=_as_int(
            raw_filters.get("fuzzy_threshold"), 70, "filters.fuzzy_threshold"
        ),
    )
    if filters.fuzzy and not (0 <= filters.fuzzy_threshold <= 100):
        raise ConfigError(
            "filters.fuzzy_threshold must be between 0 and 100, "
            f"got {filters.fuzzy_threshold!r}."
        )
    if not filters.rules:
        raise ConfigError(
            "No 'filters.rules' configured — nothing would ever match. "
            "Add at least one rule."
        )
    if not filters.exclude_symbol:
        raise ConfigError("filters.exclude_symbol must not be empty.")

    # --- saving ---------------------------------------------------------- #
    raw_saving = _section(data, "saving")
    mode = str(raw_saving.get("mode") or "auto").strip().lower()
    if mode not in VALID_MODES:
        raise ConfigError(
            f"saving.mode must be one of {sorted(VALID_MODES)}, got {mode!r}."
        )
    saving = SavingConfig(
        mode=mode,
        destination=str(raw_saving.get("destination") or "me").strip(),
        include_link=_as_bool(raw_saving.get("include_link"), True),
        append_note=_as_bool(raw_saving.get("append_note"), False),
        header=(
            DEFAULT_HEADER
            if raw_saving.get("header") is None
            else str(raw_saving.get("header"))
        ),
    )

    # --- scan window ----------------------------------------------------- #
    raw_scan = _section(data, "scan")
    tz_name = str(raw_scan.get("timezone") or "UTC").strip()
    tz = parse_timezone(tz_name)

    scan = ScanConfig(
        from_date=parse_when(raw_scan.get("from_date"), tz, "scan.from_date"),
        to_date=parse_when(raw_scan.get("to_date"), tz, "scan.to_date"),
        resume=_as_bool(raw_scan.get("resume"), True),
        mark_as_read=_as_bool(raw_scan.get("mark_as_read"), False),
        timezone_name=tz_name,
        tz=tz,
        max_history_messages=_as_int(
            raw_scan.get("max_history_messages"), 2000, "scan.max_history_messages"
        ),
    )
    if scan.max_history_messages < 1:
        scan.max_history_messages = 1
    if scan.from_date and scan.to_date and scan.from_date > scan.to_date:
        raise ConfigError(
            "scan.from_date is later than scan.to_date "
            f"({scan.from_date.isoformat()} > {scan.to_date.isoformat()})."
        )

    # --- runtime --------------------------------------------------------- #
    raw_runtime = _section(data, "runtime")
    runtime = RuntimeConfig(
        poll_interval_seconds=_as_int(
            raw_runtime.get("poll_interval_seconds"), 300, "runtime.poll_interval_seconds"
        ),
        initial_scan_limit=_as_int(
            raw_runtime.get("initial_scan_limit"), 200, "runtime.initial_scan_limit"
        ),
        max_messages_per_poll=_as_int(
            raw_runtime.get("max_messages_per_poll"), 100, "runtime.max_messages_per_poll"
        ),
        max_age_days=_as_int(raw_runtime.get("max_age_days"), 14, "runtime.max_age_days"),
        dry_run=_as_bool(raw_runtime.get("dry_run"), False),
        save_to_telegram=_as_bool(raw_runtime.get("save_to_telegram"), True),
    )
    if runtime.poll_interval_seconds < 5:
        runtime.poll_interval_seconds = 5
    if runtime.initial_scan_limit < 0:
        runtime.initial_scan_limit = 0
    if runtime.max_messages_per_poll < 1:
        runtime.max_messages_per_poll = 1

    storage = _section(data, "storage")
    database = resolve_path(str(storage.get("database") or "jobsaver.sqlite3"))
    log_file = resolve_path(str(storage.get("log_file") or "logs/jobsaver.log"))

    return AppConfig(
        telegram=telegram,
        sources=sources,
        filters=filters,
        saving=saving,
        runtime=runtime,
        database=database,
        log_file=log_file,
        scan=scan,
    )
