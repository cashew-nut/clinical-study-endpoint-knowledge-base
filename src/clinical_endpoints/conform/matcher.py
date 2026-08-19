"""Generic term matcher implementing matching.yaml's rules: whole-token synonym
matching, case-sensitive short acronyms, `not_if_matches` vetoes, and either
precedence-ordered first-match-wins (forms, references) or longest-match-wins
(measurements, which declare no match_precedence).

Every rule here is read from `vocab.matching_*` and `vocab.synonyms` /
`vocab.patterns` / `vocab.term_precedence` -- the tables `vocab validate`
writes from matching.yaml and the term files. No term id, synonym, or pattern
is hardcoded; only the matching ALGORITHM (how a synonym becomes a regex, how
a tie is broken) lives in code, exactly as matching.yaml's own comments say it
must ("matching rules are part of the vocabulary, not an implementation detail
of whoever writes the matcher").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import duckdb

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class MatchSettings:
    synonym_match: str
    case_sensitivity_rule: str
    min_synonym_length: int
    strategy: str
    strategy_when_unordered: str


@dataclass(frozen=True)
class MatchResult:
    term_id: str
    matched_text: str
    span: int
    via: str  # 'synonym' | 'pattern'


@dataclass(frozen=True)
class _CompiledTerm:
    term_id: str
    synonym_res: tuple[re.Pattern, ...]
    pattern_res: tuple[re.Pattern, ...]
    guard_res: tuple[re.Pattern, ...]


class TermMatcher:
    """One dimension's compiled terms, ready to match normalised text."""

    def __init__(
        self,
        terms: list[_CompiledTerm],
        precedence: Optional[list[str]],
        settings: MatchSettings,
        file_order: Optional[dict[str, int]] = None,
    ):
        self._by_id = {t.term_id: t for t in terms}
        self._precedence = precedence
        self._settings = settings
        # matching.yaml: longest-match ties break on "the earlier term in file
        # order" -- terms with no recorded order (shouldn't happen once
        # vocab validate has run) sort last, deterministically.
        self._file_order = file_order or {}

    @property
    def term_ids(self) -> list[str]:
        return list(self._by_id)

    def term_matches(self, term_id: str, text: str) -> Optional[MatchResult]:
        """Whether `term_id` specifically matches `text`, independent of
        precedence/longest-match order -- used to detect genuine ambiguity
        between two named terms (forms.yaml's `disambiguation` block)."""
        term = self._by_id.get(term_id)
        if term is None or not text or self._vetoed(term, text):
            return None
        for via, regexes in (("synonym", term.synonym_res), ("pattern", term.pattern_res)):
            for regex in regexes:
                found = regex.search(text)
                if found:
                    return MatchResult(term_id, found.group(0), found.end() - found.start(), via)
        return None

    def match(self, text: str) -> Optional[MatchResult]:
        if not text:
            return None
        if self._precedence is not None:
            return self._match_first(text)
        return self._match_longest(text)

    def _vetoed(self, term: _CompiledTerm, text: str) -> bool:
        return any(g.search(text) for g in term.guard_res)

    def _match_first(self, text: str) -> Optional[MatchResult]:
        for term_id in self._precedence:
            term = self._by_id.get(term_id)
            if term is None or self._vetoed(term, text):
                continue
            for regex in term.synonym_res:
                found = regex.search(text)
                if found:
                    return MatchResult(term_id, found.group(0), found.end() - found.start(), "synonym")
            for regex in term.pattern_res:
                found = regex.search(text)
                if found:
                    return MatchResult(term_id, found.group(0), found.end() - found.start(), "pattern")
        return None

    def _match_longest(self, text: str) -> Optional[MatchResult]:
        best: Optional[MatchResult] = None
        for term_id in sorted(self._by_id, key=lambda tid: (self._file_order.get(tid, 10**9), tid)):
            term = self._by_id[term_id]
            if self._vetoed(term, text):
                continue
            for via, regexes in (("synonym", term.synonym_res), ("pattern", term.pattern_res)):
                for regex in regexes:
                    found = regex.search(text)
                    if not found:
                        continue
                    span = found.end() - found.start()
                    if best is None or span > best.span:
                        best = MatchResult(term_id, found.group(0), span, via)
        return best


def _synonym_boundary_pattern(synonym: str) -> str:
    """matching.yaml: whole-token match, internal spaces match space OR hyphen.
    Built by escaping each token independently and joining on `[\\s\\-]+`, rather
    than trusting re.escape's whitespace handling, which has changed across
    Python versions."""
    tokens = [t for t in _WHITESPACE_RE.split(synonym.strip()) if t]
    escaped = [re.escape(t) for t in tokens]
    return r"(?<!\w)" + r"[\s\-]+".join(escaped) + r"(?!\w)"


def _is_short_allcaps_acronym(synonym: str) -> bool:
    return synonym.isupper() and any(c.isalpha() for c in synonym) and len(synonym) <= 5


def load_settings(con: duckdb.DuckDBPyConnection) -> MatchSettings:
    rows = dict(con.execute("SELECT key, value FROM vocab.matching_settings").fetchall())
    match_rule = rows["synonyms.match"]
    if match_rule != "whole_token":
        raise NotImplementedError(f"conform/matcher.py only implements whole_token synonym matching, got {match_rule!r}")
    case_rule = rows["synonyms.case_sensitivity.rule"]
    if case_rule != "case_sensitive_if_all_caps_and_length_le_5":
        raise NotImplementedError(f"conform/matcher.py does not implement case_sensitivity rule {case_rule!r}")
    return MatchSettings(
        synonym_match=match_rule,
        case_sensitivity_rule=case_rule,
        min_synonym_length=int(rows["synonyms.case_sensitivity.min_synonym_length"]),
        strategy=rows["precedence.strategy"],
        strategy_when_unordered=rows["precedence.strategy_when_unordered"],
    )


def _compile_synonym(synonym: str, settings: MatchSettings) -> Optional[re.Pattern]:
    if _is_short_allcaps_acronym(synonym):
        if len(synonym) < settings.min_synonym_length:
            return None  # matching.yaml: too short to trust even in capitals
        return re.compile(_synonym_boundary_pattern(synonym))  # case-sensitive
    return re.compile(_synonym_boundary_pattern(synonym), re.IGNORECASE)


def build_matcher(
    con: duckdb.DuckDBPyConnection, dimension: str, table: str, settings: MatchSettings
) -> TermMatcher:
    """Compile every term of `dimension` (whose scalar-column table is `table`,
    e.g. 'forms' for dimension 'form') from vocab.synonyms / vocab.patterns /
    vocab.term_precedence into a ready-to-match TermMatcher."""
    all_ids = [row[0] for row in con.execute(f"SELECT id FROM vocab.{table}").fetchall()]

    synonyms_by_term: dict[str, list[str]] = {tid: [] for tid in all_ids}
    for term_id, synonym in con.execute(
        "SELECT term_id, synonym FROM vocab.synonyms WHERE dimension = ?", [dimension]
    ).fetchall():
        synonyms_by_term.setdefault(term_id, []).append(synonym)

    match_patterns_by_term: dict[str, list[str]] = {tid: [] for tid in all_ids}
    guard_patterns_by_term: dict[str, list[str]] = {tid: [] for tid in all_ids}
    for term_id, pattern, role in con.execute(
        "SELECT term_id, pattern, pattern_role FROM vocab.patterns WHERE dimension = ? ORDER BY term_id, ordinal",
        [dimension],
    ).fetchall():
        target = match_patterns_by_term if role == "match" else guard_patterns_by_term
        target.setdefault(term_id, []).append(pattern)

    terms: list[_CompiledTerm] = []
    for term_id in all_ids:
        synonym_res = tuple(
            r for r in (_compile_synonym(s, settings) for s in synonyms_by_term.get(term_id, [])) if r is not None
        )
        pattern_res = tuple(re.compile(p, re.IGNORECASE) for p in match_patterns_by_term.get(term_id, []))
        guard_res = tuple(re.compile(p, re.IGNORECASE) for p in guard_patterns_by_term.get(term_id, []))
        terms.append(_CompiledTerm(term_id, synonym_res, pattern_res, guard_res))

    precedence_rows = con.execute(
        "SELECT term_id FROM vocab.term_precedence WHERE dimension = ? ORDER BY rank", [dimension]
    ).fetchall()
    precedence = [row[0] for row in precedence_rows] if precedence_rows else None

    file_order = dict(
        con.execute("SELECT term_id, ordinal FROM vocab.term_order WHERE dimension = ?", [dimension]).fetchall()
    )

    return TermMatcher(terms, precedence, settings, file_order)
