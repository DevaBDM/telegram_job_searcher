"""Keyword matching engine.

Rules
-----
* A plain keyword is a substring match:  ``python`` matches "Python developer".
* Prefix a keyword with ``re:`` to use a regular expression: ``re:\\bsenior\\b``
* ``priority_keywords`` -> if ANY matches, the post matches immediately and
  ignores ``exclude`` rules!
* ``keywords``          -> at least ONE must match (OR), but subject to ``exclude``.
* ``must_include``      -> ALL of them must match (AND).
* ``exclude``           -> if ANY matches (and no priority keyword matched), the
  message is dropped.
* ``fuzzy`` / ``fuzzy_threshold`` -> plain keywords also match by approximate
  string matching (rapidfuzz partial_ratio), so misspellings like "pythn"
  still match "python" when the score is above the threshold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Pattern, Tuple

from .config import ConfigError, FilterConfig

REGEX_PREFIX = "re:"


@dataclass(frozen=True)
class MatchResult:
    """Outcome of evaluating one message."""

    matched: bool
    keywords: Tuple[str, ...] = ()
    excluded_by: str | None = None

    def __bool__(self) -> bool:  # allows ``if result:``
        return self.matched


def _compile(raw: str, whole_word: bool, flags: int) -> Pattern[str]:
    """Turn one config entry into a compiled regex."""
    text = raw.strip()
    if text.lower().startswith(REGEX_PREFIX):
        expression = text[len(REGEX_PREFIX):].strip()
        if not expression:
            raise ConfigError(f"Empty regular expression in filters: {raw!r}")
        try:
            return re.compile(expression, flags)
        except re.error as exc:
            raise ConfigError(f"Invalid regular expression {expression!r}: {exc}") from exc

    body = re.escape(text).replace(r"\ ", " ")
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"
    return re.compile(body, flags)


def _compile_all(entries: List[str], whole_word: bool, flags: int) -> List[Tuple[str, Pattern[str]]]:
    return [(entry, _compile(entry, whole_word, flags)) for entry in entries]


class KeywordMatcher:
    """Compiles the filters once and evaluates message text against them."""

    def __init__(self, config: FilterConfig):
        flags = 0 if config.case_sensitive else re.IGNORECASE
        self.case_sensitive = config.case_sensitive
        self.whole_word = config.whole_word
        self.fuzzy_enabled = config.fuzzy
        self.fuzzy_threshold = config.fuzzy_threshold
        self._priority_keywords = _compile_all(config.priority_keywords, config.whole_word, flags)
        self._keywords = _compile_all(config.keywords, config.whole_word, flags)
        self._must_include = _compile_all(config.must_include, config.whole_word, flags)
        self._exclude = _compile_all(config.exclude, config.whole_word, flags)
        self._fuzzy: Any = None
        if self.fuzzy_enabled:
            try:
                from rapidfuzz import fuzz as _fuzz_mod  # noqa: WPS433
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise ConfigError(
                    "filters.fuzzy is true but 'rapidfuzz' is not installed. "
                    "Run:  pip install rapidfuzz"
                ) from exc
            self._fuzzy = _fuzz_mod

    def _find_matches(self, patterns: List[Tuple[str, Pattern[str]]], text: str, lower_text: str) -> List[str]:
        hits: List[str] = []
        for raw, pattern in patterns:
            if pattern.search(text):
                hits.append(raw)
            elif self.fuzzy_enabled and self._fuzzy is not None:
                score = self._fuzzy.partial_ratio(raw.lower(), lower_text)
                if score >= self.fuzzy_threshold:
                    hits.append(raw)
        return hits

    def match(self, text: str | None) -> MatchResult:
        if not text or not text.strip():
            return MatchResult(False)

        lower_text = text.lower() if self.fuzzy_enabled else ""

        # 1. Check must_include (AND requirements apply to all matches)
        for raw, pattern in self._must_include:
            if not pattern.search(text):
                return MatchResult(False, excluded_by=f"missing:{raw}")

        # 2. Check priority keywords: if any match, ignore excludes!
        priority_hits = self._find_matches(self._priority_keywords, text, lower_text)
        if priority_hits:
            # Also collect any regular keyword matches that also appear
            normal_hits = self._find_matches(self._keywords, text, lower_text)
            return MatchResult(True, keywords=tuple(priority_hits + normal_hits))

        # 3. If no priority keywords matched, check exclude list
        for raw, pattern in self._exclude:
            if pattern.search(text):
                return MatchResult(False, excluded_by=raw)

        # 4. Check regular keywords
        normal_hits = self._find_matches(self._keywords, text, lower_text)
        if not normal_hits:
            return MatchResult(False)

        return MatchResult(True, keywords=tuple(normal_hits))
