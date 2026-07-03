"""tests/test_anti_domains.py — pin the shared anti-domain parser.

The parser reads an AGENTS.md and returns the set of ``domain:*``
tags the operator has explicitly excluded. Both pattern monitors
(capability_gap_monitor, engagement_amplifier_monitor) call it to
get a ``contradicted`` state alongside the existing
confirmed/neutral/emergent fits.

The parser is intentionally conservative — it requires explicit
operator markers and won't try to interpret general negation in
prose. These tests pin that contract: what gets caught, what
doesn't, and what stays empty.
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
# Section-header path
# ─────────────────────────────────────────────────────────────────────────────


def test_section_header_with_bullets():
    """## Out of scope followed by markdown bullets — the canonical
    operator-friendly way to mark exclusions."""
    text = """
# Purpose
A sailing bot.

## Out of scope
- fitness
- finance
"""
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    assert "domain:finance" in result


def test_section_header_variants_all_recognized():
    """Operators write 'out of scope', 'Excluded', 'Not for this bot',
    'Don't' — the parser should accept any of those without forcing
    a single canonical spelling."""
    for header in [
        "Out of scope", "Out-of-scope", "Excluded", "Exclusions",
        "Not for this bot", "Don't", "Do not",
    ]:
        text = f"## {header}\n- fitness\n"
        assert "domain:fitness" in parse_anti_domains(text), (
            f"Header variant {header!r} not recognized"
        )


def test_section_header_with_numbered_list():
    text = """
## Out of scope
1. fitness
2. finance
"""
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    assert "domain:finance" in result


def test_section_header_with_comma_list_line():
    """A single line of comma-separated items below the exclusion
    header is also a valid format (some operators won't bullet)."""
    text = """
## Out of scope
fitness, finance, health
"""
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    assert "domain:finance" in result
    assert "domain:health" in result


def test_section_ends_at_next_header():
    """The exclusion section stops at the next markdown header. An
    item below another section must NOT count as excluded."""
    text = """
## Out of scope
- fitness

## Purpose
This bot handles finance, learning, and reading.
"""
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    # The "finance, learning, reading" line is in the Purpose section,
    # not the exclusion section — must not appear.
    assert "domain:finance" not in result
    assert "domain:learning" not in result


# ─────────────────────────────────────────────────────────────────────────────
# Inline-prefix path
# ─────────────────────────────────────────────────────────────────────────────


def test_inline_out_of_scope_prefix():
    """Inline `out of scope: X, Y, Z` in prose — operators who don't
    want to use a section header can still mark exclusions."""
    text = "This bot is a sailor's helper. Out of scope: fitness, finance."
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    assert "domain:finance" in result


def test_inline_excluded_prefix():
    text = "A general assistant. Excluded: workout, expense tracking."
    result = parse_anti_domains(text)
    assert "domain:fitness" in result  # via 'workout' keyword
    assert "domain:finance" in result  # via 'expense' keyword


def test_inline_and_separated_items():
    """Operators write 'X and Y' too, not just commas. The parser
    accepts both."""
    text = "Out of scope: fitness and finance."
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    assert "domain:finance" in result


def test_inline_prefix_case_insensitive():
    text = "OUT OF SCOPE: fitness."
    assert "domain:fitness" in parse_anti_domains(text)


# ─────────────────────────────────────────────────────────────────────────────
# Conservative — must NOT over-extract
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_text_returns_empty():
    assert parse_anti_domains("") == set()
    assert parse_anti_domains(None) == set()


def test_no_markers_returns_empty():
    """A normal AGENTS.md with no explicit exclusion markers must
    return empty. The pre-anti-domain behavior is preserved on every
    bot that hasn't adopted the marker convention."""
    text = """
# This bot
helps with sailing schedules and weather.
It does fitness tracking and reading suggestions too.
"""
    assert parse_anti_domains(text) == set()


def test_general_negation_in_prose_does_not_count():
    """The parser must NOT interpret general 'this bot doesn't do X'
    prose as an exclusion. That would be a false positive — operators
    write negation casually all the time. Only explicit markers count.
    """
    text = (
        "This bot doesn't handle finance. It does no fitness work. "
        "Don't ask it about health."
    )
    # No section header or inline 'out of scope:' prefix → empty.
    assert parse_anti_domains(text) == set()


def test_unknown_keyword_yields_no_domain():
    """If an excluded item has no overlap with the keyword vocab, no
    domain is returned (rather than guessing a domain). Keeps
    signal-to-noise high."""
    text = """
## Out of scope
- sailing
- weather
"""
    # Neither 'sailing' nor 'weather' is in the _DOMAIN_KEYWORDS vocab.
    assert parse_anti_domains(text) == set()


def test_long_prose_line_with_comma_is_not_treated_as_list():
    """A line over 80 chars with a comma in it is prose, not a comma
    list. The parser must NOT split it. False-positive guard."""
    text = """
## Out of scope
This bot is genuinely happy to discuss anything, including fitness, but it doesn't handle expert advice.
"""
    # The line has 'fitness' in prose, but the line is too long to
    # be treated as a comma-list, so fitness must NOT be extracted.
    assert parse_anti_domains(text) == set()


def test_items_match_via_any_keyword_in_phrase():
    """Multi-word items match if any of their words is a known
    keyword. 'no investment advice' excludes finance via the
    'investment' substring — wait, 'investment' isn't a keyword;
    'expense' and 'budget' are. Let's use a known one: 'fitness
    plan' should map to domain:fitness."""
    text = """
## Out of scope
- fitness plans
- detailed expense tracking
"""
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    assert "domain:finance" in result


# ─────────────────────────────────────────────────────────────────────────────
# Combined paths
# ─────────────────────────────────────────────────────────────────────────────


def test_section_and_inline_both_contribute():
    """When both a section header AND an inline prefix appear, the
    parser unions their domains."""
    text = """
## Out of scope
- fitness

Inline note: out of scope: finance.
"""
    result = parse_anti_domains(text)
    assert "domain:fitness" in result
    assert "domain:finance" in result


def test_dedup_via_set_semantics():
    """The same domain listed multiple times only appears once in
    the result (set semantics)."""
    text = """
## Out of scope
- fitness
- workout
- exercise

Note: out of scope: fitness.
"""
    result = parse_anti_domains(text)
    assert result == {"domain:fitness"}
