"""tests/test_help_search.py — BM25 search behavior + snippet extraction."""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
for p in (str(_ADMIN_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_index(docs):
    """Build an Index from a list of (id, title, summary, body) tuples."""
    from evolve_admin.help_index.schema import Doc, Index

    return Index(docs=[
        Doc(
            doc_id=d[0],
            title=d[1],
            summary=d[2],
            body=d[3],
            path=f"docs/help/{d[0]}.md",
            sha="x" * 64,
            size=len(d[3]),
            category="help",
        )
        for d in docs
    ])


def test_empty_query_returns_empty():
    from evolve_admin.help_search import search

    idx = _make_index([("cost", "Cost", "Spend.", "Body.")])
    assert search(idx, "") == []
    assert search(idx, "   ") == []


def test_empty_index_returns_empty():
    from evolve_admin.help_search import search
    from evolve_admin.help_index.schema import Index

    assert search(Index(docs=[]), "anything") == []


def test_query_with_no_match_returns_empty():
    from evolve_admin.help_search import search

    idx = _make_index([
        ("cost", "Cost", "Spend.", "Spend by model and channel."),
        ("security", "Security", "Audit.", "Audit findings and posture."),
    ])
    assert search(idx, "kubernetes") == []


def test_title_match_ranks_first():
    """A doc whose *title* contains the query should outrank one whose
    title doesn't, even if the latter has the term in its body."""
    from evolve_admin.help_search import search

    idx = _make_index([
        ("a", "About audits", "Overview.",
         "Some unrelated body content here just to fill space."),
        ("b", "Misc page",     "Overview.",
         "audits audits audits we mention them a lot in the body."),
    ])
    hits = search(idx, "audits", k=2)
    assert hits, "expected at least one hit"
    assert hits[0].doc_id == "a", f"got {hits[0].doc_id}, ordering: {[h.doc_id for h in hits]}"


def test_top_k_caps():
    from evolve_admin.help_search import search

    idx = _make_index([
        (f"d{i}", "Cost page", "Spend.", "Cost cost cost spend.")
        for i in range(10)
    ])
    hits = search(idx, "cost", k=3)
    assert len(hits) == 3


def test_top_k_default_is_three():
    from evolve_admin.help_search import search

    idx = _make_index([
        (f"d{i}", "Cost page", "Spend.", "cost cost cost spend.")
        for i in range(5)
    ])
    hits = search(idx, "cost")
    assert len(hits) == 3


def test_snippet_window_around_match():
    from evolve_admin.help_search import search

    body = ("filler " * 80) + " HERE-IS-THE-MATCH " + ("more " * 80)
    idx = _make_index([("d", "Title", "Sum.", body)])
    hits = search(idx, "here-is-the-match")
    assert hits, "expected a hit"
    assert "HERE-IS-THE-MATCH" in hits[0].snippet
    assert len(hits[0].snippet) <= 500
    # Should have an ellipsis on at least one side because of the surrounding filler
    assert hits[0].snippet.startswith("…") or hits[0].snippet.endswith("…")


def test_snippet_falls_back_to_summary_when_no_body_match():
    """When the matched token is in title/summary only (not body), the
    snippet still renders something usable (the summary, capped)."""
    from evolve_admin.help_search import search

    idx = _make_index([
        ("x", "About audits", "Audit overview.", "Body without the term."),
    ])
    hits = search(idx, "audits")
    assert hits
    # The body doesn't contain "audits" — snippet falls back to summary
    assert "Audit overview" in hits[0].snippet or "Body without the term" in hits[0].snippet


def test_stopwords_dont_count():
    """Pure-stopword queries should return no hits (or at least no
    confidence — they're noise on a real corpus)."""
    from evolve_admin.help_search import search

    idx = _make_index([
        ("a", "Cost page", "Spend.", "Cost and channels and tokens."),
        ("b", "Security page", "Audit.", "Audit and posture and findings."),
    ])
    assert search(idx, "the and of in on") == []


def test_token_split_handles_hyphens():
    """`pod-conduct` should be one token (or at least co-locatable)."""
    from evolve_admin.help_search import search

    idx = _make_index([
        ("a", "Pod-conduct charter", "The pod conduct contract.",
         "Discusses pod-conduct.md and how it injects at session start."),
    ])
    hits = search(idx, "pod-conduct")
    assert hits, "expected a hit on hyphenated token"
    assert hits[0].doc_id == "a"


def test_hit_to_dict_shape():
    from evolve_admin.help_search import search

    idx = _make_index([
        ("cost", "Cost page", "Spend.", "Cost and channels and tokens.")
    ])
    hit = search(idx, "cost", k=1)[0].to_dict()
    assert set(hit.keys()) == {"doc_id", "title", "snippet", "score", "path"}
    assert isinstance(hit["score"], float)


def test_search_is_case_insensitive():
    from evolve_admin.help_search import search

    idx = _make_index([
        ("a", "Cost page", "Spend.", "Cost and channels.")
    ])
    a = search(idx, "Cost", k=1)
    b = search(idx, "COST", k=1)
    c = search(idx, "cost", k=1)
    assert a[0].doc_id == b[0].doc_id == c[0].doc_id == "a"
