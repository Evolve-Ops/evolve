"""tests/test_proposal_title_humanization.py — pins the operator-facing
titles produced by the worst-offender generators after the 2026-06-02
humanization sweep.

Spec: docs/spec-recommendations-rework-2026-06-02.md Phase 1 §"Plain-
language title rule" + §"Quick wins #2".

Each generator below previously produced jargon-leaky titles that
prompted the rework (raw rule names like ``static_bloat_drives_envelope``,
audit category prefixes like ``(broken_path)``, mid-word truncations
like ``"... — can'"``, etc). These tests pin the new shapes so a future
edit that reintroduces the jargon fails CI loudly.

Sibling: test_proposals_grouping.py (PR #1) — the coalescing layer
benefits from the humanized titles, since the collapsed group header
shows the template_title verbatim.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ANALYZER = REPO_ROOT / "packages" / "analyzer"
ADMIN = REPO_ROOT / "packages" / "admin"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# primary_model_floor_advisor — RETIRED 2026-06-04
# ─────────────────────────────────────────────────────────────────────────────
#
# The 4 tests previously here pinned headline-humanization details for
# the floor advisor's "no primary configured" + "Investigation"
# branches. The generator was retired on 2026-06-04 (see
# docs/decision-retire-primary-model-floor-advisor-2026-06-04.md and
# packages/analyzer/generator_runner.py for the registry note). With
# the generator gone, the title-shape pinning is no longer relevant —
# the proposals it produced can never fire again. Retention of the
# regression invariants happens at the generator-existence layer:
# see test_no_primary_floor_advisor_regenerates.py for the lock-in
# that prevents the generator from coming back without a design
# conversation.


def test_primary_model_floor_advisor_dir_is_gone():
    """Sanity: the generator directory genuinely was removed when it
    was retired (vs. only being unwired from the runner). Catches a
    half-retirement where the code still ships but doesn't run."""
    gen_dir = ANALYZER / "generators" / "primary_model_floor_advisor"
    assert not gen_dir.exists(), (
        f"primary_model_floor_advisor directory still exists at "
        f"{gen_dir} — full retirement requires removing the directory "
        f"so the import path doesn't resolve. See "
        f"docs/decision-retire-primary-model-floor-advisor-2026-06-04.md"
    )


# ─────────────────────────────────────────────────────────────────────────────
# app_audit_tier3 — strip "(category) on" prefix
# ─────────────────────────────────────────────────────────────────────────────


def test_app_audit_problem_line_no_jargon_prefix():
    """The original template was
    ``f"App audit ({category}) on `{app_id}`: {description[:150]}"``.
    PR #2 dropped the prefix and led with the app name; Slice 2
    (2026-06-04) goes further — the LLM's verbose description no
    longer appears in the title at all (it now lives only in the
    proposal body via _audit_context_builder). The title is built
    from the category alone via _CATEGORY_HEADLINE."""
    src = _read(ADMIN / "evolve_admin" / "applications" / "audit_poller.py")
    # Capture the whole _problem_line function body up to the next
    # top-level `def` (or end of file). The previous non-greedy
    # `(.+?)(?=\n\n|\Z)` stopped at the first blank line, which under
    # Slice 2's longer docstring + module-level _CATEGORY_HEADLINE
    # dict cut the body off mid-docstring.
    fn = re.search(
        r"def _problem_line\(record: dict\) -> str:(.+?)(?=^def |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    assert fn, "_problem_line not located"
    body = fn.group(1)
    assert "App audit (" not in body, (
        "_problem_line must drop the 'App audit ({category}) on' prefix"
    )
    # Slice 2 contract: the title reads ``app_id`` from the record,
    # looks up the category in _CATEGORY_HEADLINE, and returns the
    # plain-English phrase. The verbose ``description`` is no longer
    # in the title — that was the operator's "too technical" complaint.
    assert "app_id" in body, "_problem_line must still surface app_id"
    assert "_CATEGORY_HEADLINE" in body, (
        "Slice 2: _problem_line must read from _CATEGORY_HEADLINE to "
        "produce plain-English titles. The verbose LLM description "
        "now lives in the proposal body only."
    )


# plugin_curator title humanization + the admin-API bulk-allow-deny
# title pins were both retired alongside the generator/endpoint —
# see docs/spec-plugin-posture-rework-2026-06-06.md.


# ─────────────────────────────────────────────────────────────────────────────
# bloat_investigator — map cause_key → operator phrase
# ─────────────────────────────────────────────────────────────────────────────


def test_bloat_investigator_uses_cause_phrase_map():
    """The previous template was
    ``f"Investigate {bot} envelope growth — {attr.cause_key}"`` which
    surfaced raw rule names (``static_bloat_drives_envelope``,
    ``efficiency_drift_without_envelope``, ``ambiguous``). PR #2 maps
    each cause_key to an operator-language phrase."""
    src = _read(ANALYZER / "generators" / "bloat_investigator" / "observe.py")
    # The old template must be gone.
    assert "envelope growth — {attr.cause_key}" not in src, (
        "old jargon template still present — humanization regressed"
    )
    # And the map must exist with entries for all four cause_keys
    # currently produced by attribution.py (verified at the time this
    # test was written — if attribution.py adds a new cause_key without
    # an entry here, observe() falls through to a generic phrase).
    map_match = re.search(
        r"_CAUSE_PHRASE\s*=\s*\{(.+?)\}", src, re.DOTALL,
    )
    assert map_match, "_CAUSE_PHRASE map not found"
    map_body = map_match.group(1)
    for cause in (
        "growing_memory_drives_envelope",
        "static_bloat_drives_envelope",
        "efficiency_drift_without_envelope",
        "ambiguous",
    ):
        assert cause in map_body, (
            f"_CAUSE_PHRASE missing entry for cause_key {cause!r} — "
            f"the operator phrase will fall through to the generic default"
        )


def test_bloat_investigator_headline_leads_with_bot_id():
    """Coalescing on the Proposals page strips bot_id from titles to
    group across bots. For that to work, bot_id has to be the leading
    token of the headline so its removal produces a clean
    "{bot}: ..." → "{bot}: ..." template across all bots."""
    src = _read(ANALYZER / "generators" / "bloat_investigator" / "observe.py")
    m = re.search(
        r"headline_short\s*=\s*f\"\{bot_id\}:\s*\{cause_phrase\}\"",
        src,
    )
    assert m, (
        "bloat_investigator headline must lead with '{bot_id}: ' so the "
        "Proposals-page coalescer collapses across bots"
    )
