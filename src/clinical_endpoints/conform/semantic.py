"""The semantic fallback: a deterministic, explainable approximate match tried
only for MEASUREMENT (the one dimension whose cascade -- vocab.matching_cascade
-- ends in `fallback: review_queue` rather than a term id), after the literal
exact/syntactic_rule cascade steps have both failed. There is no embedding
model available in this pipeline's environment, so "semantic" here means
token-overlap similarity against each term's own label + synonyms (read from
vocab.*, never invented): a term's descriptive vocabulary has to cover most of
its own words AND share at least two content words with the endpoint text
before it is offered as a candidate. A garbled string shares no tokens with
any term's vocabulary and correctly falls through to the review queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import duckdb

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "of", "a", "an", "in", "at", "to", "for", "with", "and", "or",
        "from", "on", "is", "are", "by", "as", "this", "that", "was", "were",
    }
)


def _tokenise(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD_RE.findall(text.lower()) if len(w) >= 3 and w not in _STOPWORDS)


@dataclass(frozen=True)
class SemanticTerm:
    term_id: str
    tokens: frozenset[str]


@dataclass(frozen=True)
class SemanticResult:
    term_id: str
    score: float


def build_semantic_index(con: duckdb.DuckDBPyConnection, dimension: str, table: str) -> tuple[SemanticTerm, ...]:
    labels = con.execute(f"SELECT id, label FROM vocab.{table}").fetchall()
    synonyms_by_term: dict[str, list[str]] = {}
    for term_id, synonym in con.execute(
        "SELECT term_id, synonym FROM vocab.synonyms WHERE dimension = ?", [dimension]
    ).fetchall():
        synonyms_by_term.setdefault(term_id, []).append(synonym)

    terms = []
    for term_id, label in labels:
        vocabulary = " ".join([label or "", *synonyms_by_term.get(term_id, [])])
        tokens = _tokenise(vocabulary)
        if tokens:
            terms.append(SemanticTerm(term_id, tokens))
    return tuple(terms)


def best_match(
    text: Optional[str],
    terms: tuple[SemanticTerm, ...],
    *,
    min_score: float = 0.6,
    min_overlap: int = 2,
) -> Optional[SemanticResult]:
    if not text:
        return None
    text_tokens = _tokenise(text)
    if not text_tokens:
        return None

    best: Optional[SemanticResult] = None
    for term in sorted(terms, key=lambda t: t.term_id):
        overlap = term.tokens & text_tokens
        needed = min(min_overlap, len(term.tokens))
        if len(overlap) < needed:
            continue
        score = len(overlap) / len(term.tokens)
        if best is None or score > best.score:
            best = SemanticResult(term.term_id, score)

    if best is not None and best.score >= min_score:
        return best
    return None
