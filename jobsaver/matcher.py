"""Priority Substring Matching System using Aho-Corasick automaton.

Technical Specification:
- Rules are provided as an ordered list of strings (index 0 is highest priority).
- Inclusion rule: Any string not beginning with exclude_symbol (e.g. "critical bug").
- Exclusion rule: Any string beginning with exclude_symbol (e.g. "!bug").
- Conflict Resolution: "Lowest index wins" (min(matched_indices)).
- Three-state output: Decision.INCLUDE, Decision.EXCLUDE, Decision.UNMATCHED.
- Early exit optimization: If index 0 is matched, traversal terminates immediately.
- Duplicate keywords: First occurrence takes precedence.
- Also preserves:
  * `must_include`: Mandatory substrings that all must appear in text.
  * `fuzzy`: Fallback approximate matching via rapidfuzz if exact Aho-Corasick matches nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

try:
    import ahocorasick
except ImportError:  # pragma: no cover
    ahocorasick = None  # type: ignore

from .config import ConfigError, FilterConfig

T = TypeVar("T")


class Decision(str, Enum):
    INCLUDE = "INCLUDE"
    EXCLUDE = "EXCLUDE"
    UNMATCHED = "UNMATCHED"


@dataclass(frozen=True)
class MatchResult:
    """Outcome of evaluating one text string against rules."""

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
        """Allows `if result:` to evaluate True if INCLUDE."""
        return self.matched


class PriorityMatcher:
    """Aho-Corasick priority substring matching engine."""

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
        self.must_include = [w.strip() for w in (must_include or []) if w.strip()]

        # Compile must_include
        self._must_include_normalized = [
            w if case_sensitive else w.lower() for w in self.must_include
        ]

        # Prepare rules
        self.raw_rules: List[str] = list(rules)
        self._compiled_rules: List[Tuple[int, Decision, str, str]] = []  # (orig_idx, decision, target, raw)
        seen_targets: set[str] = set()

        for idx, raw in enumerate(rules):
            stripped = raw.strip()
            if not stripped:
                continue

            if stripped.startswith(exclude_symbol):
                target = stripped[len(exclude_symbol):].strip()
                decision = Decision.EXCLUDE
            else:
                target = stripped
                decision = Decision.INCLUDE

            if not target:
                # Bare prefix like "!" with no keyword
                continue

            norm_target = target if case_sensitive else target.lower()
            if norm_target in seen_targets:
                # Duplicate keyword: lowest index takes permanent precedence
                continue

            seen_targets.add(norm_target)
            self._compiled_rules.append((idx, decision, norm_target, stripped))

        # Build Aho-Corasick automaton
        if ahocorasick is not None:
            self.automaton = ahocorasick.Automaton()
            for orig_idx, decision, norm_target, raw in self._compiled_rules:
                # Value stored in automaton: (orig_idx, decision, target, raw)
                self.automaton.add_word(norm_target, (orig_idx, decision, norm_target, raw))
            if self._compiled_rules:
                self.automaton.make_automaton()
        else:
            self.automaton = None

        # Prepare fuzzy matching if enabled
        self._fuzz: Any = None
        if self.fuzzy_enabled:
            try:
                from rapidfuzz import fuzz as _fuzz_mod  # noqa: WPS433
                self._fuzz = _fuzz_mod
            except ImportError as exc:  # pragma: no cover
                raise ConfigError(
                    "filters.fuzzy is true but 'rapidfuzz' is not installed. "
                    "Run: pip install rapidfuzz"
                ) from exc

    def evaluate(self, text: str | None) -> MatchResult:
        """Evaluate text against must_include and the ordered priority rules."""
        if not text or not text.strip():
            return MatchResult(Decision.UNMATCHED)

        search_text = text if self.case_sensitive else text.lower()

        # 1. Check must_include (AND logic)
        for idx, req in enumerate(self._must_include_normalized):
            if req not in search_text:
                return MatchResult(Decision.UNMATCHED, excluded_by=f"missing:{self.must_include[idx]}")

        # If rule list is empty, returns UNMATCHED
        if not self._compiled_rules:
            return MatchResult(Decision.UNMATCHED)

        # 2. Check matches across ordered rules
        # First, run Aho-Corasick exact matching with early-exit on index 0
        exact_matches: Dict[int, Tuple[Decision, str]] = {}
        if self.automaton is not None:
            for _, (orig_idx, decision, _, raw) in self.automaton.iter(search_text):
                if orig_idx not in exact_matches:
                    exact_matches[orig_idx] = (decision, raw)
                # Early exit optimization: Index 0 is the highest possible priority
                if orig_idx == 0:
                    break

        if 0 in exact_matches:
            dec, kw = exact_matches[0]
            return MatchResult(
                decision=dec,
                matched_rule_index=0,
                matched_keyword=kw,
                excluded_by=kw if dec == Decision.EXCLUDE else None,
            )

        # Iterate rules in priority order (0 to N).
        # At each index: if it matched exactly via Aho-Corasick, or if fuzzy is enabled
        # and it fuzzy-matches with score >= fuzzy_threshold, this rule WINS immediately.
        for orig_idx, decision, norm_target, raw in self._compiled_rules:
            # Check exact match
            if orig_idx in exact_matches:
                dec, kw = exact_matches[orig_idx]
                return MatchResult(
                    decision=dec,
                    matched_rule_index=orig_idx,
                    matched_keyword=kw,
                    excluded_by=kw if dec == Decision.EXCLUDE else None,
                )

            # Check fuzzy match if enabled
            if self.fuzzy_enabled and self._fuzz is not None:
                score = self._fuzz.partial_ratio(norm_target, search_text)
                if score >= self.fuzzy_threshold:
                    return MatchResult(
                        decision=decision,
                        matched_rule_index=orig_idx,
                        matched_keyword=raw,
                        excluded_by=raw if decision == Decision.EXCLUDE else None,
                    )

        return MatchResult(Decision.UNMATCHED)

    # Alias match for compatibility
    def match(self, text: str | None) -> MatchResult:
        return self.evaluate(text)

    # ── Collection Operations ─────────────────────────────────────────────
    def filter_included(self, texts: Sequence[str]) -> List[str]:
        """Returns only items that evaluate to Decision.INCLUDE."""
        return [t for t in texts if self.evaluate(t).decision == Decision.INCLUDE]

    def filter_excluded(self, texts: Sequence[str]) -> List[str]:
        """Returns only items that evaluate to Decision.EXCLUDE."""
        return [t for t in texts if self.evaluate(t).decision == Decision.EXCLUDE]

    def filter_unmatched(self, texts: Sequence[str]) -> List[str]:
        """Returns only items that evaluate to Decision.UNMATCHED."""
        return [t for t in texts if self.evaluate(t).decision == Decision.UNMATCHED]

    def partition_items(
        self, items: Sequence[T], key: Callable[[T], str] = lambda x: str(x)
    ) -> Dict[Decision, List[T]]:
        """Partitions an arbitrary sequence of objects by Decision."""
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
    """Adapter bridging legacy FilterConfig (or new rules) to PriorityMatcher."""

    def __init__(self, config: FilterConfig):
        # Build unified ordered rules list
        if config.rules:
            combined_rules = list(config.rules)
        else:
            # Automatic mapping for legacy config format:
            # priority_keywords (0..P-1) -> exclude (!...) -> keywords
            combined_rules = []
            for kw in config.priority_keywords:
                combined_rules.append(kw)
            for ex in config.exclude:
                prefix = config.exclude_symbol if hasattr(config, "exclude_symbol") else "!"
                combined_rules.append(f"{prefix}{ex.lstrip(prefix)}")
            for kw in config.keywords:
                combined_rules.append(kw)

        exclude_symbol = getattr(config, "exclude_symbol", "!")

        super().__init__(
            rules=combined_rules,
            exclude_symbol=exclude_symbol,
            case_sensitive=config.case_sensitive,
            must_include=config.must_include,
            fuzzy=config.fuzzy,
            fuzzy_threshold=config.fuzzy_threshold,
        )
