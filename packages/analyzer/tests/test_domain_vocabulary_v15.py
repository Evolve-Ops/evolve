"""tests/test_domain_vocabulary_v15.py — pin the v1.5 expansion of
``_DOMAIN_KEYWORDS`` + ``catalog.json``.

Spec: docs/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2"
follow-up. The v1 substrate shipped with 7 domains driven by the
catalog seed; obvious household / personal use cases (cooking, home,
family, entertainment, travel, music, work, pets) fell outside the
vocab and got silently dropped by the pattern monitors. v1.5 extends
the default vocabulary so the substrate has end-to-end coverage of
those cases without any LLM cost.

These tests pin:
  1. Each new domain has at least one keyword that resolves to it.
  2. Each new domain has at least one catalog entry tagged with it.
  3. Original v1 vocabulary unchanged — no regression in keyword
     mappings for the 7 original domains.
  4. Substring-match false-positive guardrails: the keyword choices
     don't pull in obvious wrong-domain matches.
  5. Pattern monitor end-to-end: a synthetic "cooking" cluster on
     a bot resolves to domain:food, and the catalog has a matching
     entry for capability_gap_monitor to fire on.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.app_suggester.observe import (  # noqa: E402
    _DOMAIN_KEYWORDS,
    _CATALOG_PATH,
    _load_catalog,
)


# The 8 new domains v1.5 adds.
NEW_DOMAINS = (
    "domain:food",
    "domain:home",
    "domain:family",
    "domain:entertainment",
    "domain:travel",
    "domain:music",
    "domain:work",
    "domain:pets",
)

# The 7 original v1 domains — pinned so a future cleanup that
# accidentally renames or drops one fails loudly.
ORIGINAL_DOMAINS = (
    "domain:health",
    "domain:fitness",
    "domain:finance",
    "domain:learning",
    "domain:creative",
    "domain:productivity",
    "domain:social",
)


# ─────────────────────────────────────────────────────────────────────────────
# v1 backward-compat
# ─────────────────────────────────────────────────────────────────────────────


def test_original_v1_keyword_mappings_unchanged():
    """The 19 v1 keyword → domain mappings must still resolve as before.
    Pre-existing pattern monitors + tests rely on this exact set."""
    expected = {
        "health": "domain:health",
        "fitness": "domain:fitness",
        "workout": "domain:fitness",
        "finance": "domain:finance",
        "budget": "domain:finance",
        "expense": "domain:finance",
        "learning": "domain:learning",
        "reading": "domain:learning",
        "course": "domain:learning",
        "study": "domain:learning",
        "creative": "domain:creative",
        "writing": "domain:creative",
        "idea": "domain:creative",
        "journal": "domain:productivity",
        "reflection": "domain:productivity",
        "productivity": "domain:productivity",
        "social": "domain:social",
        "relationship": "domain:social",
        "contact": "domain:social",
    }
    for kw, tag in expected.items():
        assert _DOMAIN_KEYWORDS.get(kw) == tag, (
            f"v1 keyword {kw!r} → {tag!r} regressed; "
            f"now resolves to {_DOMAIN_KEYWORDS.get(kw)!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# New domain coverage
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("domain", NEW_DOMAINS)
def test_each_new_domain_has_at_least_one_keyword(domain):
    """Each v1.5 domain must have ≥ 1 keyword in ``_DOMAIN_KEYWORDS``,
    otherwise the catalog entry for that domain is unreachable from
    the pattern monitors."""
    keywords_for_domain = [
        kw for kw, tag in _DOMAIN_KEYWORDS.items() if tag == domain
    ]
    assert keywords_for_domain, f"{domain} has no keywords"


@pytest.mark.parametrize("domain", NEW_DOMAINS)
def test_each_new_domain_has_at_least_one_catalog_entry(domain):
    """Each v1.5 domain must have ≥ 1 catalog entry tagged with it,
    otherwise capability_gap_monitor can't fire on patterns mapped to
    this domain — the keyword mapping would be inert."""
    catalog = _load_catalog(_CATALOG_PATH)
    matching = [
        e for e in catalog
        if isinstance(e.get("tags"), list) and domain in e["tags"]
    ]
    assert matching, (
        f"{domain} has no catalog entry tagged with it — the keyword "
        f"vocab maps to a domain the catalog can't surface as a "
        f"capability gap"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Total counts
# ─────────────────────────────────────────────────────────────────────────────


def test_total_distinct_domain_count_is_at_least_15():
    """v1 had 7 domains; v1.5 adds 8 → 15 minimum. A regression that
    drops a domain would fail here."""
    distinct = set(_DOMAIN_KEYWORDS.values())
    assert len(distinct) >= 15, (
        f"Expected ≥ 15 distinct domains; got {len(distinct)}: "
        f"{sorted(distinct)}"
    )


def test_catalog_has_at_least_18_entries():
    """v1 catalog had 10; v1.5 adds 8 → 18 minimum."""
    catalog = _load_catalog(_CATALOG_PATH)
    assert len(catalog) >= 18, (
        f"Expected ≥ 18 catalog entries; got {len(catalog)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# False-positive guardrails
# ─────────────────────────────────────────────────────────────────────────────


def test_no_overly_short_pet_keywords():
    """The pet domain uses 'pet', 'puppy', 'kitten', 'veterinarian' —
    explicitly NOT 'cat' (matches catastrophe / catalogue) or 'dog'
    (matches dogma) or 'vet' (matches veteran). This pin documents
    the choice so a future cleanup that 'helpfully' adds them gets
    caught."""
    bad_keywords = {"cat", "dog", "vet", "animal"}
    pet_keywords = {
        kw for kw, tag in _DOMAIN_KEYWORDS.items() if tag == "domain:pets"
    }
    overlap = bad_keywords & pet_keywords
    assert not overlap, (
        f"Pet domain has high-false-positive keywords: {overlap}. "
        f"See test docstring for the substring-match risk."
    )


def test_no_overly_short_entertainment_keywords():
    """'game' and 'show' both have strong false-positive risk
    ('mind games', 'show up'). v1.5 ships 'gaming'/'movie' instead."""
    bad_keywords = {"game", "show"}
    ent_keywords = {
        kw for kw, tag in _DOMAIN_KEYWORDS.items()
        if tag == "domain:entertainment"
    }
    overlap = bad_keywords & ent_keywords
    assert not overlap, (
        f"Entertainment domain has high-false-positive keywords: "
        f"{overlap}. See test docstring."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Catalog entry well-formedness
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("category,domain", [
    ("meal_planning", "domain:food"),
    ("household_upkeep", "domain:home"),
    ("family_log", "domain:family"),
    ("entertainment_log", "domain:entertainment"),
    ("travel_log", "domain:travel"),
    ("music_log", "domain:music"),
    ("career_log", "domain:work"),
    ("pet_care_log", "domain:pets"),
])
def test_new_catalog_entry_well_formed(category, domain):
    """Each new catalog entry must have title + description +
    example_apps + tags with the right domain tag. The Phase A
    operator-first proposal layer reads these fields verbatim into
    the operator pitch — empty fields would show as empty in the UI."""
    catalog = _load_catalog(_CATALOG_PATH)
    entry = next(
        (e for e in catalog if e.get("category") == category), None
    )
    assert entry is not None, f"missing catalog entry {category!r}"
    assert entry.get("title"), f"{category} missing title"
    assert entry.get("description"), f"{category} missing description"
    apps = entry.get("example_apps") or []
    assert isinstance(apps, list) and len(apps) >= 1, (
        f"{category} missing example_apps"
    )
    tags = entry.get("tags") or []
    assert domain in tags, (
        f"{category} missing {domain!r} tag (has {tags})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: pattern monitor sees the new vocab
# ─────────────────────────────────────────────────────────────────────────────


def test_cooking_noun_resolves_to_food_domain():
    """The capability_gap_monitor's _noun_to_domains helper must map
    'cooking' to domain:food via the new keyword."""
    from capability_gap_monitor import _noun_to_domains, _domain_keywords
    kw = _domain_keywords()
    assert "domain:food" in _noun_to_domains("cooking dinner", kw)


def test_gardening_noun_resolves_to_home_domain():
    from capability_gap_monitor import _noun_to_domains, _domain_keywords
    kw = _domain_keywords()
    assert "domain:home" in _noun_to_domains("gardening", kw)


def test_career_noun_resolves_to_work_domain():
    from capability_gap_monitor import _noun_to_domains, _domain_keywords
    kw = _domain_keywords()
    assert "domain:work" in _noun_to_domains("career planning", kw)


def test_unknown_noun_still_returns_empty():
    """A noun outside the (now expanded) vocab still returns empty.
    The substrate isn't suddenly mapping every word to a domain —
    only the explicit keywords work. v1.5 widens the net; it doesn't
    turn the matcher into a fuzzy classifier."""
    from capability_gap_monitor import _noun_to_domains, _domain_keywords
    kw = _domain_keywords()
    assert _noun_to_domains("xylophone", kw) == set()
    assert _noun_to_domains("philosophy", kw) == set()
