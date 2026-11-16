"""Best-effort deadline extraction from job-post text.

Scans for common phrases like "Deadline: Nov 20, 2026", "Apply before 20/11/2026",
"closing date: 2026-11-30", etc.  Returns the first match as a ``datetime.date``,
or ``None`` when no deadline can be found (which is perfectly fine — many posts
simply don't state one).

Two matching strategies:

1. **Regex** — a compiled alternation of all known trigger phrases.  Fast and
   precise; handles the correctly-spelled and common-misspelling cases.
2. **Fuzzy** — when ``rapidfuzz`` is installed, every line of the post is
   checked against the trigger phrases with ``partial_ratio``.  This catches
   typos like "Deadlin", "Closin Date", "Aply Before", etc.  The threshold
   defaults to 70 (same as the keyword matcher).

Only plain-text heuristics — no LLM calls, no network.  False positives are
preferable to crashing: if a heuristic fires on something that isn't really a
deadline, the worst case is a wrong date in one column.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

# ── month names (English + a few Amharic transliterations) ────────────────
_MONTHS: dict[str, int] = {}
_EN_MONTHS = [
    ("january", 1), ("february", 2), ("march", 3), ("april", 4),
    ("may", 5), ("june", 6), ("july", 7), ("august", 8),
    ("september", 9), ("october", 10), ("november", 11), ("december", 12),
]
for _name, _num in _EN_MONTHS:
    _MONTHS[_name] = _num
    _MONTHS[_name[:3]] = _num        # jan, feb, …

# Common ordinal suffixes so "20th" parses as 20
_ORDINAL = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)\b", re.IGNORECASE)

# ── trigger phrases ───────────────────────────────────────────────────────
# Canonical forms used for both regex building and fuzzy matching.
DEADLINE_KEYWORDS: List[str] = [
    "Application Close",
    "Application Closed",
    "Application Closes",
    "Apply Before",
    "Apply By",
    "Apply No Later Than",
    "Apply Until",
    "Close Date",
    "Closing Date",
    "Closing On",
    "Cut-Off Date",
    "Cutoff Date",
    "Deadline",
    "Due Date",
    "End Date",
    "Expiration Date",
    "Expires",
    "Final Date",
    "Last Date",
    "Last Day To Apply",
    "No Later Than",
    "Open Until",
    "Submit Before",
    "Submit By",
    "Valid Until",
    # Amharic
    "ቀነ ገደብ",
    "የማመልከቻ ቀን",
    "የመጨረሻ ቀን",
]

# Build the regex alternation from the canonical list.  Multi-word phrases
# have their spaces relaxed to ``\s+``, and hyphens become ``[\s\-]?``.
def _phrase_to_pattern(phrase: str) -> str:
    """Turn a human-readable phrase into a regex fragment."""
    # Escape regex-special chars except space and hyphen (handled below)
    escaped = re.escape(phrase).replace(r"\ ", " ").replace(r"\-", "-")
    # Relax whitespace and hyphens
    escaped = re.sub(r"[\s]+", r"\\s+", escaped)
    escaped = escaped.replace("-", r"[\\s\\-]?")
    return escaped

_TRIGGER = (
    r"(?:" +
    "|".join(_phrase_to_pattern(kw) for kw in DEADLINE_KEYWORDS) +
    r")"
)

# ── Fuzzy matching infrastructure ─────────────────────────────────────────
FUZZY_THRESHOLD = 70  # 0-100, same default as the keyword matcher

# Lowercase versions for fuzzy comparison
_KEYWORDS_LOWER = [kw.lower() for kw in DEADLINE_KEYWORDS]

def _load_fuzz() -> Any:
    """Import rapidfuzz.fuzz if available, else return None."""
    try:
        from rapidfuzz import fuzz as _fuzz_mod  # noqa: WPS433
        return _fuzz_mod
    except ImportError:
        return None

_fuzz = _load_fuzz()

# ── date patterns ─────────────────────────────────────────────────────────
# After removing ordinal suffixes the date part is expected in one of these:

# 2026-11-20  or  2026/11/20
_ISO = r"(?P<y1>\d{4})[\-/](?P<m1>\d{1,2})[\-/](?P<d1>\d{1,2})"

# 20-11-2026  or  20/11/2026  (DD-MM-YYYY)
_DMY_NUM = r"(?P<d2>\d{1,2})[\-/](?P<m2>\d{1,2})[\-/](?P<y2>\d{4})"

# Nov 20, 2026  or  November 20 2026
_MONTH_NAME = (
    r"(?P<mn3>[A-Za-z]+)\.?\s+(?P<d3>\d{1,2})\s*,?\s*(?P<y3>\d{4})"
)

# 20 Nov 2026  or  20 November, 2026
_DAY_MONTH_NAME = (
    r"(?P<d4>\d{1,2})\s+(?P<mn4>[A-Za-z]+)\.?\s*,?\s*(?P<y4>\d{4})"
)

# Nov 20  or  November 20  (no year → assume current/next occurrence)
_MONTH_DAY_NOYEAR = r"(?P<mn5>[A-Za-z]+)\.?\s+(?P<d5>\d{1,2})"

# 20 Nov  (no year)
_DAY_MONTH_NOYEAR = r"(?P<d6>\d{1,2})\s+(?P<mn6>[A-Za-z]+)"

# Year-bearing patterns tried first (higher priority), then no-year fallbacks.
_DATE_GROUPS_WITH_YEAR = [_ISO, _DMY_NUM, _MONTH_NAME, _DAY_MONTH_NAME]
_DATE_GROUPS_NO_YEAR = [_MONTH_DAY_NOYEAR, _DAY_MONTH_NOYEAR]

# Build one compiled regex per date variant, all sharing the same trigger prefix.
_TRIGGER_PREFIX = _TRIGGER + r"[\s:;\-–—]*" + r"(?:.{0,40}?\s)?"

_PATTERNS_WITH_YEAR = [
    re.compile(_TRIGGER_PREFIX + r"(?:" + dp + r")", re.IGNORECASE | re.UNICODE)
    for dp in _DATE_GROUPS_WITH_YEAR
]
_PATTERNS_NO_YEAR = [
    re.compile(_TRIGGER_PREFIX + r"(?:" + dp + r")", re.IGNORECASE | re.UNICODE)
    for dp in _DATE_GROUPS_NO_YEAR
]

# Standalone date patterns (no trigger) — used after fuzzy confirms a trigger
# is present on a nearby line.
_ALL_DATE_ALTS = "|".join(_DATE_GROUPS_WITH_YEAR + _DATE_GROUPS_NO_YEAR)
_DATE_ONLY = re.compile(_ALL_DATE_ALTS, re.IGNORECASE)


def _month_from_name(name: str) -> Optional[int]:
    return _MONTHS.get(name.lower().rstrip("."))


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except (ValueError, OverflowError):
        return None


def _infer_year(month: int, day: int) -> Optional[date]:
    """Pick the nearest future occurrence when the year is missing."""
    today = date.today()
    candidate = _safe_date(today.year, month, day)
    if candidate is None:
        return None
    if candidate < today - timedelta(days=60):
        # Probably means next year (a deadline 2 months in the past is unlikely)
        candidate = _safe_date(today.year + 1, month, day)
    return candidate


def _extract_from_match(m: re.Match[str]) -> Optional[date]:
    """Try each named-group family in the match."""
    g = m.groupdict()

    # Family 1: YYYY-MM-DD
    if g.get("y1"):
        return _safe_date(int(g["y1"]), int(g["m1"]), int(g["d1"]))

    # Family 2: DD-MM-YYYY
    if g.get("d2"):
        return _safe_date(int(g["y2"]), int(g["m2"]), int(g["d2"]))

    # Family 3: MonthName DD, YYYY
    if g.get("mn3"):
        month = _month_from_name(g["mn3"])
        if month:
            return _safe_date(int(g["y3"]), month, int(g["d3"]))

    # Family 4: DD MonthName YYYY
    if g.get("mn4"):
        month = _month_from_name(g["mn4"])
        if month:
            return _safe_date(int(g["y4"]), month, int(g["d4"]))

    # Family 5: MonthName DD (no year)
    if g.get("mn5"):
        month = _month_from_name(g["mn5"])
        if month:
            return _infer_year(month, int(g["d5"]))

    # Family 6: DD MonthName (no year)
    if g.get("mn6"):
        month = _month_from_name(g["mn6"])
        if month:
            return _infer_year(month, int(g["d6"]))

    return None


# ── Regex-based extraction (exact trigger match) ─────────────────────────
def _extract_regex(cleaned: str) -> Optional[date]:
    """Try every trigger+date regex pattern against the full text."""
    for pattern in _PATTERNS_WITH_YEAR:
        for m in pattern.finditer(cleaned):
            result = _extract_from_match(m)
            if result is not None:
                return result

    for pattern in _PATTERNS_NO_YEAR:
        for m in pattern.finditer(cleaned):
            result = _extract_from_match(m)
            if result is not None:
                return result

    return None


# ── Fuzzy-based extraction (misspelled triggers) ─────────────────────────
def _extract_fuzzy(cleaned: str, threshold: int = FUZZY_THRESHOLD) -> Optional[date]:
    """Use rapidfuzz to find misspelled trigger phrases, then look for a date nearby.

    Strategy: split the text into lines, score each line against every keyword.
    When a line scores above the threshold, scan that line (and the next) for a
    date pattern.  This keeps false positives low — the fuzzy hit and the date
    must be close together.
    """
    if _fuzz is None:
        return None

    lines = cleaned.splitlines()
    for i, line in enumerate(lines):
        line_lower = line.lower()
        # Check if any deadline keyword fuzzy-matches this line
        hit = False
        for kw_lower in _KEYWORDS_LOWER:
            score = _fuzz.partial_ratio(kw_lower, line_lower)
            if score >= threshold:
                hit = True
                break

        if not hit:
            continue

        # A trigger was fuzzy-matched on this line.  Look for a date on this
        # line and, if the date might wrap, on the next line too.
        search_zone = line
        if i + 1 < len(lines):
            search_zone += " " + lines[i + 1]

        # Try year-bearing date patterns first
        for dp_re in (_DATE_GROUPS_WITH_YEAR + _DATE_GROUPS_NO_YEAR):
            for m in re.finditer(dp_re, search_zone, re.IGNORECASE):
                result = _extract_from_match(m)
                if result is not None:
                    return result

    return None


def extract_deadline(text: str | None) -> Optional[date]:
    """Return the first deadline date found in *text*, or ``None``.

    >>> extract_deadline("Deadline: November 20, 2026")
    datetime.date(2026, 11, 20)
    >>> extract_deadline("No deadline here") is None
    True
    """
    if not text:
        return None

    # Normalise ordinal suffixes so "20th" becomes "20"
    cleaned = _ORDINAL.sub(r"\1", text)

    # 1) Try exact regex triggers first (fast, high confidence).
    result = _extract_regex(cleaned)
    if result is not None:
        return result

    # 2) Fall back to fuzzy trigger matching (catches misspellings).
    return _extract_fuzzy(cleaned)
