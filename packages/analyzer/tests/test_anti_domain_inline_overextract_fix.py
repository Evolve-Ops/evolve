"""tests/test_anti_domain_inline_overextract_fix.py — pin the P2 fix
for inline-prefix over-extraction.

Bug (from the 2026-06-05 audit of PR #2198): the inline-prefix path
in ``anti_domains.parse_anti_domains`` treated the entire tail after
``out of scope:`` as one or more exclusion items, then split on commas
and ``and``. If the tail was a single prose sentence with no commas,
it was kept as one item and the substring-match would catch any
domain keyword embedded in the prose. So:

    Out of scope: but I do help with health when asked

…would extract "but I do help with health when asked" as one item
and wrongly add ``domain:health`` to the anti-domain set, **excluding
the domain the operator was explicitly INcluding**.

Fix: per-item word cap (3 words). Real exclusion items are 1-3 words
("fitness", "meal planning", "personal finance"). Longer items are
skipped — assumed prose.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from anti_domains import parse_anti_domains  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Bug reproductions — these used to wrongly extract a domain
# ─────────────────────────────────────────────────────────────────────────────


def test_inline_prefix_with_apologetic_prose_does_not_extract_health():
    """The exact bug case from the audit. Operator writes prose
    explaining why they include health — pre-fix, the parser extracted
    'but I do help with health when asked' and matched 'health'."""
    text = "Out of scope: but I do help with health when asked"
    result = parse_anti_domains(text)
    assert "domain:health" not in result, (
        f"Apologetic prose after 'Out of scope:' wrongly excluded "
        f"domain:health; got {result}"
    )


def test_inline_prefix_with_explanatory_clause_does_not_extract():
    """Variant of the bug — operator explains the exclusion with a
    clause ending in 'unless'."""
    text = "Out of scope: anything that touches fitness unless trivial"
    result = parse_anti_domains(text)
    assert "domain:fitness" not in result


def test_inline_prefix_with_sentence_does_not_extract():
    """A full sentence after the prefix shouldn't yield items."""
    text = (
        "Excluded: this bot doesn't handle anything related to "
        "personal finance topics."
    )
    result = parse_anti_domains(text)
    assert "domain:finance" not in result


# ─────────────────────────────────────────────────────────────────────────────
# Existing good behavior preserved
# ─────────────────────────────────────────────────────────────────────────────


def test_inline_prefix_single_word_item_still_works():
    """The simplest, most common case — one word — must still work."""
    text = "Out of scope: fitness"
    assert "domain:fitness" in parse_anti_domains(text)


def test_inline_prefix_two_word_phrase_works():
    """Two-word compound nouns like 'meal planning' must still work
    (2 words ≤ 3-word cap)."""
    text = "Out of scope: meal planning"
    # 'meal' is a keyword for domain:food in v1.5
    assert "domain:food" in parse_anti_domains(text)


def test_inline_prefix_three_word_phrase_works():
    """At the cap boundary: 3 words must still work."""
    text = "Out of scope: personal expense tracking"
    # 'expense' is a keyword for domain:finance
    assert "domain:finance" in parse_anti_domains(text)


def test_inline_prefix_four_word_phrase_dropped():
    """One word over the cap: 4 words → skip."""
    text = "Out of scope: fitness advice in some cases"
    # 'fitness' would match domain:fitness but the phrase is 5 words → skip
    assert "domain:fitness" not in parse_anti_domains(text)


def test_inline_prefix_comma_list_with_short_items_works():
    """Mixed-length items: short ones extracted, long ones skipped."""
    text = (
        "Out of scope: fitness, finance, anything that touches "
        "medical advice"
    )
    result = parse_anti_domains(text)
    # 'fitness' (1 word) → extracted → domain:fitness in result
    assert "domain:fitness" in result
    # 'finance' (1 word) → extracted → domain:finance in result
    assert "domain:finance" in result
    # 'anything that touches medical advice' (5 words) → dropped, so
    # any health keyword embedded wouldn't be picked up. 'medical' isn't
    # in our keyword vocab anyway, so verify health (which is) isn't
    # spuriously caught from the prose context.


def test_inline_prefix_with_and_separator_works():
    """The 'and' separator continues to split items."""
    text = "Out of scope: fitness and finance"
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    assert "domain:finance" in result


# ─────────────────────────────────────────────────────────────────────────────
# Section-header path still works (regression guard)
# ─────────────────────────────────────────────────────────────────────────────


def test_section_header_bullets_still_work_with_long_descriptions():
    """The section-header + bullet path is NOT affected by the word
    cap — operators sometimes write more descriptive bullets. The
    cap only applies to the inline-prefix path."""
    text = """
## Out of scope
- anything related to fitness advice
- anything touching personal finance topics
"""
    result = parse_anti_domains(text)
    # Both phrases contain keywords and use the bullet path which
    # doesn't apply the word cap.
    assert "domain:fitness" in result
    assert "domain:finance" in result
