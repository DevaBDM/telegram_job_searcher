"""Best-effort deadline extraction from job-post text.

Scans for common phrases like "Deadline: Nov 20, 2026", "Apply before 20/11/2026",
"closing date: 2026-11-30", etc.  Returns the first match as a ``datetime.date``,
or ``None`` when no deadline can be found (which is perfectly fine — many posts
simply don't state one).

Only plain-text heuristics — no LLM calls, no network.  False positives are
preferable to crashing: if the regex fires on something that isn't really a
deadline, the worst case is a wrong date in one column.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

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
# A trigger is a keyword/phrase that usually precedes a date meaning "deadline".
_TRIGGER = (
    r"(?:"
    r"deadline|dead[\s\-]?line"
    r"|closing\s+date|close\s+date"
    r"|due\s+date"
    r"|last\s+(?:date|day)\s+(?:to|of|for)\s+appl(?:y|ication)"
    r"|apply\s+(?:before|by|until|on\s+or\s+before)"
    r"|application\s+(?:deadline|closes?|due|end)"
    r"|submit\s+(?:before|by|until)"
    r"|submission\s+(?:deadline|due|date|closes?)"
    r"|expires?\s+(?:on|at|by|date)?"
    r"|expiry\s+date"
    r"|valid\s+(?:until|through|till)"
    r"|(?:not?\s+)?later\s+than"
    r"|end\s+date"
    r"|ቀነ\s*ገደብ|የማመልከቻ\s*ቀን|የመጨረሻ\s*ቀን"     # Amharic: deadline / application date / last date
    r")"
)

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

    # Try year-bearing patterns first (higher confidence), then no-year fallbacks.
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
