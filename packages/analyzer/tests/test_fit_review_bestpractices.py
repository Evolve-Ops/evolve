"""fit_review best-practices playbook corpus + compose layer (Wave 2, pure-data).

The corpus (``playbook_bestpractices._BEST_PRACTICES``) is the edr-market-intel
distillation of "what a good bot of each archetype actually does"; the compose
layer (``archetypes.composed_playbook_for`` → ``compose_playbook``) merges it onto
the base domain playbook so the L2 reflection runner reads one "what good looks
like" object. These tests pin three contracts from the build brief:

  * every real archetype carries non-empty best-practice entries;
  * compose merges base + enrichment WITHOUT dropping any base domain;
  * back-compat — ``custom`` (and any un-curated / unknown archetype) still yields
    a VALID playbook (empty enrichment, base preserved), never an error.

No LLM, no network — pure data, mirroring the targeting bite.
"""

from __future__ import annotations

import importlib

import pytest

from bot_purpose import ARCHETYPES

archetypes = importlib.import_module("fit_review.archetypes")
bp_mod = importlib.import_module("fit_review.playbook_bestpractices")

BestPractice = bp_mod.BestPractice
ComposedPlaybook = bp_mod.ComposedPlaybook

# The five archetypes that carry a canonical lens; ``custom`` is the deliberate
# no-lens back-compat case (empty base playbook AND empty enrichment).
_REAL_ARCHETYPES = tuple(a for a in ARCHETYPES if a != "custom")


# ─────────────────────────────────────────────────────────────────────────────
# Corpus shape
# ─────────────────────────────────────────────────────────────────────────────


def test_corpus_keys_are_valid_archetypes():
    """No typo'd / orphan archetype keys — every corpus key is a real archetype."""
    assert set(bp_mod._BEST_PRACTICES).issubset(set(ARCHETYPES))


def test_custom_carries_no_corpus_entry():
    """``custom`` has no canonical lens, so no canonical best practices (by design)."""
    assert "custom" not in bp_mod._BEST_PRACTICES
    assert bp_mod.best_practices_for("custom") == ()


@pytest.mark.parametrize("archetype", _REAL_ARCHETYPES)
def test_each_real_archetype_has_nonempty_best_practices(archetype):
    """Brief: each archetype has non-empty best-practice entries."""
    practices = bp_mod.best_practices_for(archetype)
    assert practices, f"{archetype} should have ≥1 best practice"
    assert all(isinstance(p, BestPractice) for p in practices)


@pytest.mark.parametrize("archetype", _REAL_ARCHETYPES)
def test_best_practice_fields_are_populated(archetype):
    """Every entry is a fully-grounded record: name, JTBD, domains, KB source."""
    for p in bp_mod.best_practices_for(archetype):
        assert p.capability.strip(), f"{archetype}: empty capability"
        assert p.description.strip(), f"{archetype}: '{p.capability}' has no description"
        assert p.domains, f"{archetype}: '{p.capability}' touches no domain"
        assert all(d == d.strip().lower() and d for d in p.domains), (
            f"{archetype}: '{p.capability}' has a malformed domain tag"
        )
        assert p.source.strip(), f"{archetype}: '{p.capability}' has no KB source citation"


@pytest.mark.parametrize("archetype", _REAL_ARCHETYPES)
def test_best_practices_are_grounded_in_the_archetype_purpose(archetype):
    """Enrichment is grounded, not orthogonal: a real archetype's best-practice
    domains overlap its base playbook (it may also ADD domains — that's the point —
    but it must not be entirely off-purpose)."""
    base = archetypes.aligned_domains_for(archetype)
    enrichment_domains: set[str] = set()
    for p in bp_mod.best_practices_for(archetype):
        enrichment_domains |= p.domains
    assert base & enrichment_domains, (
        f"{archetype}: best practices share no domain with the base playbook"
    )


def test_capabilities_unique_within_archetype():
    """No duplicate capability names within one archetype's list."""
    for archetype in _REAL_ARCHETYPES:
        names = [p.capability for p in bp_mod.best_practices_for(archetype)]
        assert len(names) == len(set(names)), f"{archetype} has duplicate capability names"


def test_kb_gaps_reference_real_archetypes():
    """Documented thin-coverage gaps point at real, curated archetypes."""
    assert set(bp_mod.KB_GAPS).issubset(set(ARCHETYPES))
    for archetype in bp_mod.KB_GAPS:
        # A gap is only meaningful for an archetype we DID curate (conservatively).
        assert bp_mod.best_practices_for(archetype), (
            f"KB_GAPS names {archetype} but it has no curated entries"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Compose — base + enrichment, base-preserving
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("archetype", _REAL_ARCHETYPES)
def test_compose_preserves_base_domains_verbatim(archetype):
    """Brief: compose merges base + enrichment WITHOUT dropping base entries.

    The composed ``aligned_domains`` must equal the base frozenset exactly —
    composition never drops or rewrites a base domain (targeting semantics
    are unchanged)."""
    base = archetypes.aligned_domains_for(archetype)
    composed = archetypes.composed_playbook_for(archetype)
    assert isinstance(composed, ComposedPlaybook)
    assert composed.aligned_domains == base
    # And the enrichment rode along.
    assert composed.has_enrichment()
    assert composed.best_practices == bp_mod.best_practices_for(archetype)


@pytest.mark.parametrize("archetype", _REAL_ARCHETYPES)
def test_all_domains_is_superset_of_base(archetype):
    """``all_domains`` = base ∪ best-practice domains ⊇ base (never narrower)."""
    composed = archetypes.composed_playbook_for(archetype)
    assert composed.aligned_domains <= composed.all_domains
    assert composed.best_practice_domains <= composed.all_domains


def test_compose_does_not_widen_targeting_lens():
    """The compose layer must NOT mutate the base targeting frozenset — even when a
    best practice references a domain outside the base (the enrichment case).

    Uses customer-facing, whose best practices reference 'coordination' (not in its
    base playbook): proves enrichment is additive AND that composition still returns
    the base aligned set untouched."""
    base_before = set(archetypes.aligned_domains_for("customer-facing"))
    composed = archetypes.composed_playbook_for("customer-facing")
    assert set(composed.aligned_domains) == base_before
    # This archetype's enrichment introduces a domain beyond the base — and that
    # extra domain rides only on best_practice_domains, never on aligned_domains.
    extra = composed.best_practice_domains - composed.aligned_domains
    assert extra, "expected customer-facing enrichment to add a domain beyond its base"
    assert not (extra & composed.aligned_domains)


# ─────────────────────────────────────────────────────────────────────────────
# Back-compat — custom / unknown / no-enrichment archetypes stay valid
# ─────────────────────────────────────────────────────────────────────────────


def test_custom_composes_to_valid_empty_playbook():
    """Brief: a ``custom`` bot still gets a valid playbook (empty, not an error)."""
    composed = archetypes.composed_playbook_for("custom")
    assert isinstance(composed, ComposedPlaybook)
    assert composed.aligned_domains == frozenset()
    assert composed.best_practices == ()
    assert composed.has_enrichment() is False
    assert composed.all_domains == frozenset()


@pytest.mark.parametrize("archetype", [None, "", "   ", "not-an-archetype", "PROJECT-MANAGER "])
def test_unknown_or_messy_archetype_is_valid(archetype):
    """Unknown / blank / None / mixed-case+whitespace archetypes never raise.

    Known-but-messy ('PROJECT-MANAGER ') normalizes and still resolves enrichment;
    genuinely unknown ones degrade to a valid empty playbook."""
    composed = archetypes.composed_playbook_for(archetype)
    assert isinstance(composed, ComposedPlaybook)
    # Always a valid object with a base frozenset (possibly empty) and a tuple.
    assert isinstance(composed.aligned_domains, frozenset)
    assert isinstance(composed.best_practices, tuple)


def test_messy_known_archetype_normalizes():
    """A known archetype with stray case/whitespace resolves to the same enrichment."""
    clean = archetypes.composed_playbook_for("project-manager")
    messy = archetypes.composed_playbook_for("  Project-Manager  ")
    assert messy.best_practices == clean.best_practices
    assert messy.aligned_domains == clean.aligned_domains


def test_compose_playbook_is_base_preserving_for_arbitrary_input():
    """``compose_playbook`` returns the passed-in domains verbatim — even an
    archetype with no enrichment keeps whatever base it was handed (the function
    never invents or drops base domains)."""
    fake_base = frozenset({"calendar", "email"})
    composed = bp_mod.compose_playbook("an-unenriched-archetype", fake_base)
    assert composed.aligned_domains == fake_base
    assert composed.best_practices == ()  # no corpus entry → empty enrichment
    assert composed.all_domains == fake_base
