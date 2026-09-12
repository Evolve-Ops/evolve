"""BM25 search over the help index.

Spec: internal/spec-primary-bot-interface-2026-05-14.md §4.3.

Why BM25 and not embeddings:
  - Corpus is ~27 docs, ~140KB total. The recall problem isn't real.
  - Embedding-provider config is still in flux upstream (see
    project_embedding_provider_config.md in memory). Don't ship a
    dependent feature before the dependency settles.
  - BM25 regenerates in milliseconds; embeddings would mean an
    index file we'd have to keep coherent across deploys.

BM25 parameters chosen for short titles + medium bodies:
  k1 = 1.5   — moderate term-frequency saturation
  b  = 0.75  — moderate length normalization (standard)

Fields are concatenated with a small title/summary boost (titles
typed twice, summary once, body once). Cheaper than per-field BM25F
for the same effect on a corpus this size.

Hit shape:
  doc_id, title, snippet, score, path

Snippet is a windowed extract around the highest-scoring matched
token, ≤500 chars, sentence-aware where possible.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from .help_index.schema import Doc, Index


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Hit:
    doc_id: str
    title: str
    snippet: str
    score: float
    path: str

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "snippet": self.snippet,
            "score": round(self.score, 4),
            "path": self.path,
        }


def search(index: Index, query: str, *, k: int = 3) -> list[Hit]:
    """Return the top-``k`` :class:`Hit`s for ``query`` against ``index``.

    Empty query or empty index returns ``[]``. ``k`` is capped at the
    document count so we never return padding.
    """
    q_tokens = _tokenize(query)
    if not q_tokens or not index.docs:
        return []

    scorer = _Scorer(index)
    raw = [(doc, scorer.score(doc, q_tokens)) for doc in index.docs]
    raw = [(doc, s) for doc, s in raw if s > 0.0]
    raw.sort(key=lambda x: x[1], reverse=True)

    out: list[Hit] = []
    for doc, score in raw[: max(1, k)]:
        snippet = _make_snippet(doc, q_tokens)
        out.append(
            Hit(
                doc_id=doc.doc_id,
                title=doc.title,
                snippet=snippet,
                score=score,
                path=doc.path,
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Tokenization
# ─────────────────────────────────────────────────────────────────────────────


# Word + digit characters; also keeps internal hyphens so e.g.
# "pod-conduct" is one token instead of two. ASCII-only is fine for the
# current corpus.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")

# A short, hand-curated stoplist. Pure-Python; no NLTK dependency.
# Conservative — only the very-high-frequency tokens that hurt scoring
# without adding meaning. (`evo` and `bot` are explicitly NOT here —
# they're high-information for this corpus.)
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if then else of in on at to for from by with
    is are was were be been being am do does did doing have has had
    having i you we they it this that these those there here as
    not no yes can cannot will would could should may might must
    """.split()
)


def _tokenize(text: str) -> list[str]:
    return [
        m.group(0).lower()
        for m in _TOKEN_RE.finditer(text)
        if m.group(0).lower() not in _STOPWORDS
    ]


# ─────────────────────────────────────────────────────────────────────────────
# BM25
# ─────────────────────────────────────────────────────────────────────────────


_K1 = 1.5
_B = 0.75


class _Scorer:
    """Precomputes document stats once per query.

    Cheap: ~27 docs × ~few-thousand-tokens. Building the stats takes
    <10ms; scoring is constant-time per (doc, term).
    """

    def __init__(self, index: Index) -> None:
        self._tokens_per_doc: dict[str, list[str]] = {}
        self._tf_per_doc: dict[str, dict[str, int]] = {}
        self._df: dict[str, int] = {}
        self._lengths: dict[str, int] = {}

        for doc in index.docs:
            toks = _doc_tokens(doc)
            self._tokens_per_doc[doc.doc_id] = toks
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self._tf_per_doc[doc.doc_id] = tf
            self._lengths[doc.doc_id] = len(toks)
            for t in set(toks):
                self._df[t] = self._df.get(t, 0) + 1

        self._n_docs = len(index.docs)
        self._avgdl = (
            sum(self._lengths.values()) / self._n_docs
            if self._n_docs
            else 0.0
        )

    def score(self, doc: Doc, q_tokens: Iterable[str]) -> float:
        tf = self._tf_per_doc.get(doc.doc_id) or {}
        dl = self._lengths.get(doc.doc_id, 0) or 1
        total = 0.0
        for t in q_tokens:
            f = tf.get(t, 0)
            if f == 0:
                continue
            df = self._df.get(t, 0)
            # Standard BM25 IDF, clamped at 0 so common-word matches
            # don't go negative and pull good docs down.
            idf = max(0.0, math.log((self._n_docs - df + 0.5) / (df + 0.5) + 1.0))
            denom = f + _K1 * (1.0 - _B + _B * dl / (self._avgdl or 1.0))
            total += idf * (f * (_K1 + 1.0)) / denom
        return total


def _doc_tokens(doc: Doc) -> list[str]:
    """Tokens used for indexing one doc.

    Title 4×, summary 2×, body 1×. The repetition is a cheap
    substitute for field-weighted BM25 — on a corpus this small the
    difference is invisible in practice but the bias toward "doc
    whose title is about X" matters for natural queries like "cost"
    where a single title hit should beat several scattered body
    mentions in an unrelated doc.

    Numbers chosen so a 1× title hit (4 boosted tokens) outweighs a
    3× body hit (3 raw tokens) after BM25 length normalization on
    typical short help docs.
    """
    title_tokens = _tokenize(doc.title)
    summary_tokens = _tokenize(doc.summary)
    body_tokens = _tokenize(doc.body)
    return title_tokens * 4 + summary_tokens * 2 + body_tokens


# ─────────────────────────────────────────────────────────────────────────────
# Snippet
# ─────────────────────────────────────────────────────────────────────────────


_SNIPPET_HALF = 240  # window of characters either side of the hit
_SNIPPET_MAX = 500


def _make_snippet(doc: Doc, q_tokens: list[str]) -> str:
    """Return a ~500-char excerpt around the first query-token hit.

    Falls back to the doc summary, then to the first 500 chars of body.
    """
    body = doc.body
    if not body:
        return doc.summary

    q_set = {t for t in q_tokens}
    # Case-insensitive search using lowercase tokens against the body
    body_lower = body.lower()
    best_pos = -1
    for t in q_set:
        idx = body_lower.find(t)
        if idx >= 0 and (best_pos < 0 or idx < best_pos):
            best_pos = idx

    if best_pos < 0:
        # No body match — return summary, capped.
        if doc.summary:
            return doc.summary[:_SNIPPET_MAX]
        return body[:_SNIPPET_MAX].rstrip()

    start = max(0, best_pos - _SNIPPET_HALF)
    end = min(len(body), best_pos + _SNIPPET_HALF)

    # Round outward to whitespace so words aren't cut mid-token.
    while start > 0 and not body[start - 1].isspace():
        start -= 1
    while end < len(body) and not body[end].isspace():
        end += 1

    excerpt = body[start:end].strip()
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(body):
        excerpt = excerpt + "…"
    if len(excerpt) > _SNIPPET_MAX:
        excerpt = excerpt[: _SNIPPET_MAX - 1].rstrip() + "…"
    return excerpt
