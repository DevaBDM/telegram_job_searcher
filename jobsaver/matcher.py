"""Priority rule matching with an Aho-Corasick substring automaton.

Everything is one ordered list: ``filters.rules``.  Index 0 wins — the first
rule that matches decides the outcome, so a high-priority inclusion beats an
exclusion lower down.

Rule grammar (``!`` is ``filters.exclude_symbol``)::

    "Software Developer"      substring include
    "!Marketing Management"   substring exclude
    "w:IT"                    whole-word include (no fuzzy)
    "!w:IT"                   whole-word exclude (no fuzzy)
    "re:\\b(?:api|rest)\\b"     regular expression include (no fuzzy)
    "!re:\\bintern\\b"          regular expression exclude (no fuzzy)

Details
-------
* ``must_include`` entries must ALL appear literally in the text (AND logic).
* Duplicate targets: the first occurrence wins.
* ``fuzzy`` / ``fuzzy_threshold`` apply to plain substring rules only, so a
  misspelling such as "pythn" can still hit "python".  Whole-word and regex
  rules are always exact — that is the point of ``w:``.
* An early hit on index 0 terminates evaluation immediately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Pattern, Sequence, Tuple, TypeVar

try:
    import ahocorasick
except ImportError:  # pragma: no cover - checked in PriorityMatcher.__init__
    ahocorasick = None  # type: ignore

from .config import ConfigError, FilterConfig

T = TypeVar("T")

WORD_PREFIXES = ("word:", "w:")
REGEX_PREFIX = "re:"

MODE_SUBSTRING = "substring"
MODE_WORD = "word"
MODE_REGEX = "regex"


class Decision(str, Enum):
    INCLUDE = "INCLUDE"
    EXCLUDE = "EXCLUDE"
    UNMATCHED = "UNMATCHED"


@dataclass(frozen=True)
class MatchResult:
    """Outcome of evaluating one text string against the rules."""

    decision: Decision
    matched_rule_index: Optional[int] = None
    matched_keyword: Optional[str] = None
    excluded_by: Optional[str] = None

    @property
    def matched(self) -> bool:
        """True if the decision is INCLUDE."""
        return self.decision == Decision.INCLUDE

    @property
    def keywords(self) -> Tuple[str, ...]:
        """Tuple of matched keywords for backwards-compatibility with logging/storage."""
        if self.matched_keyword is not None:
            return (self.matched_keyword,)
        return ()

    def __bool__(self) -> bool:
        """Allows ``if result:`` to evaluate True if INCLUDE."""
        return self.matched


@dataclass(frozen=True)
class _Rule:
    index: int
    decision: Decision
    mode: str
    target: str                  # as written, without "!" / "w:" / "re:"
    match: str                   # normalised target used by the automaton/fuzzy
    raw: str                     # the whole rule as written
    regex: Optional[Pattern[str]] = None


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _word_pattern(target: str, flags: int) -> Pattern[str]:
    """Match ``target`` only when it is not embedded in a larger word.

    Boundaries are only applied where the target starts/ends with a word
    character, so ``w:C++`` still works (it ends with ``+``).
    """
    left = r"(?<!\w)" if target and _is_word_char(target[0]) else ""
    right = r"(?!\w)" if target and _is_word_char(target[-1]) else ""
    return re.compile(left + re.escape(target) + right, flags)


def _split_rule(raw: str, exclude_symbol: str) -> Optional[Tuple[Decision, str, str]]:
    """Parse ``[!][w:|re:]target``. Returns (decision, mode, target) or None."""
    text = raw.strip()
    if not text:
        return None

    decision = Decision.INCLUDE
    if exclude_symbol and text.startswith(exclude_symbol):
        decision = Decision.EXCLUDE
        text = text[len(exclude_symbol):].strip()
        if not text:
            return None

    mode = MODE_SUBSTRING
    lowered = text.lower()
    for prefix in WORD_PREFIXES:
        if lowered.startswith(prefix):
            mode = MODE_WORD
            text = text[len(prefix):].strip()
            break
    else:
        if lowered.startswith(REGEX_PREFIX):
            mode = MODE_REGEX
            text = text[len(REGEX_PREFIX):].strip()

    if not text:
        return None
    return decision, mode, text


class PriorityMatcher:
    """Ordered rule engine: substring (Aho-Corasick), whole-word and regex."""

    def __init__(
        self,
        rules: Sequence[str],
        exclude_symbol: str = "!",
        case_sensitive: bool = False,
        must_include: Optional[Sequence[str]] = None,
        fuzzy: bool = False,
        fuzzy_threshold: int = 70,
    ) -> None:
        self.exclude_symbol = exclude_symbol
        self.case_sensitive = case_sensitive
        self.fuzzy_enabled = fuzzy
        self.fuzzy_threshold = fuzzy_threshold
        self.raw_rules: List[str] = list(rules)
        self.must_include = [w.strip() for w in (must_include or []) if w.strip()]
        self._must_include_normalized = [
            w if case_sensitive else w.lower() for w in self.must_include
        ]

        flags = 0 if case_sensitive else re.IGNORECASE
        compiled: List[_Rule] = []
        seen: set[Tuple[str, str]] = set()

        for index, raw in enumerate(rules):
            parsed = _split_rule(raw, exclude_symbol)
            if parsed is None:
                continue
            decision, mode, target = parsed
            norm = target if case_sensitive else target.lower()
            # First occurrence of a (mode, target) pair wins; an earlier include
            # therefore beats a later exclusion of the same text, as before.
            if (mode, norm) in seen:
                continue
            seen.add((mode, norm))

            regex: Optional[Pattern[str]] = None
            if mode == MODE_WORD:
                regex = _word_pattern(target, flags)
            elif mode == MODE_REGEX:
                try:
                    regex = re.compile(target, flags)
                except re.error as exc:
                    raise ConfigError(
                        f"Invalid regular expression in filters.rules: {raw!r}\n  {exc}"
                    ) from exc

            compiled.append(
                _Rule(
                    index=index,
                    decision=decision,
                    mode=mode,
                    target=target,
                    match=norm,
                    raw=raw.strip(),
                    regex=regex,
                )
            )

        self._rules: List[_Rule] = compiled
        self._substring_rules = [r for r in compiled if r.mode == MODE_SUBSTRING]

        # Plain substring rules go into the automaton; word/regex rules are
        # checked by their compiled pattern in priority order.
        if self._substring_rules:
            if ahocorasick is None:
                raise ConfigError(
                    "Priority substring matching needs 'pyahocorasick', which is "
                    "not installed.\n  Run:  pip install pyahocorasick"
                )
            self.automaton: Any = ahocorasick.Automaton()
            for rule in self._substring_rules:
                self.automaton.add_word(rule.match, rule.index)
            self.automaton.make_automaton()
        else:
            self.automaton = None

        self._fuzz: Any = None
        if self.fuzzy_enabled:
            try:
                from rapidfuzz import fuzz as _fuzz_mod  # noqa: WPS433
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise ConfigError(
                    "filters.fuzzy is true but 'rapidfuzz' is not installed. "
                    "Run:  pip install rapidfuzz"
                ) from exc
            self._fuzz = _fuzz_mod

    # ------------------------------------------------------------------ #
    def evaluate(self, text: str | None) -> MatchResult:
        """Evaluate text against ``must_include`` and the ordered rules."""
        if not text or not text.strip():
            return MatchResult(Decision.UNMATCHED)

        search_text = text if self.case_sensitive else text.lower()

        # 1. must_include (AND logic)
        for raw, required in zip(self.must_include, self._must_include_normalized):
            if required not in search_text:
                return MatchResult(Decision.UNMATCHED, excluded_by=f"missing:{raw}")

        if not self._rules:
            return MatchResult(Decision.UNMATCHED)

        # 2. Exact substring hits, collected once for the whole text.
        exact: set[int] = set()
        if self.automaton is not None:
            for _end, rule_index in self.automaton.iter(search_text):
                exact.add(rule_index)

        # 3. Walk the rules in priority order. The first hit wins.
        for rule in self._rules:
            if rule.index in exact:
                return self._result(rule)
            if rule.regex is not None and rule.regex.search(text):
                return self._result(rule)
            if (
                self._fuzz is not None
                and rule.mode == MODE_SUBSTRING
                and self._fuzz.partial_ratio(rule.match, search_text)
                >= self.fuzzy_threshold
            ):
                return self._result(rule)

        return MatchResult(Decision.UNMATCHED)

    @staticmethod
    def _result(rule: _Rule) -> MatchResult:
        return MatchResult(
            decision=rule.decision,
            matched_rule_index=rule.index,
            matched_keyword=rule.raw,
            excluded_by=rule.raw if rule.decision == Decision.EXCLUDE else None,
        )

    # Alias match for compatibility
    def match(self, text: str | None) -> MatchResult:
        return self.evaluate(text)

    # ── Collection Operations ─────────────────────────────────────────────
    def filter_included(self, texts: Sequence[str]) -> List[str]:
        """Return only items that evaluate to Decision.INCLUDE."""
        return [t for t in texts if self.evaluate(t).decision == Decision.INCLUDE]

    def filter_excluded(self, texts: Sequence[str]) -> List[str]:
        """Return only items that evaluate to Decision.EXCLUDE."""
        return [t for t in texts if self.evaluate(t).decision == Decision.EXCLUDE]

    def filter_unmatched(self, texts: Sequence[str]) -> List[str]:
        """Return only items that evaluate to Decision.UNMATCHED."""
        return [t for t in texts if self.evaluate(t).decision == Decision.UNMATCHED]

    def partition_items(
        self, items: Sequence[T], key: Callable[[T], str] = lambda x: str(x)
    ) -> Dict[Decision, List[T]]:
        """Partition an arbitrary sequence of objects by Decision."""
        partitioned: Dict[Decision, List[T]] = {
            Decision.INCLUDE: [],
            Decision.EXCLUDE: [],
            Decision.UNMATCHED: [],
        }
        for item in items:
            decision = self.evaluate(key(item)).decision
            partitioned[decision].append(item)
        return partitioned


class KeywordMatcher(PriorityMatcher):
    """Adapter from :class:`FilterConfig` to :class:`PriorityMatcher`."""

    def __init__(self, config: FilterConfig) -> None:
        super().__init__(
            rules=config.rules,
            exclude_symbol=config.exclude_symbol,
            case_sensitive=config.case_sensitive,
            must_include=config.must_include,
            fuzzy=config.fuzzy,
            fuzzy_threshold=config.fuzzy_threshold,
        )
