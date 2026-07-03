"""Unit tests for tools/help-refresh — the weekly knowledge-refresh routine.

Pure-logic tests against FIXTURE PR data + a fixed run-date + a tiny fake
_index.yaml / docs tree. No network (never calls `gh`), no wall clock (every
date is passed in explicitly). This proves the four behaviours the README spec
turns on:

  * TRIAGE skips internal-only PRs and keeps user-facing ones;
  * CONCEPT-MATCH maps a PR's words/paths to the right help files;
  * STALE-DETECT respects the 30-day AND concept-overlap conjunction;
  * the ZERO-candidate run is a clean no-op (no file writes).

Plus the mechanical bits: last_reviewed bump and REVIEW-QUEUE rendering.

Run with:
  cd tools && python3 -m pytest test_help_refresh.py -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

# The tool has no .py extension (like the other tools/ executables), so load it by path.
# It must be registered in sys.modules BEFORE exec so the @dataclass decorator can
# resolve its own module to evaluate `str | None` field annotations (otherwise
# dataclasses' _is_type lookup hits a None module and raises AttributeError).
_TOOL = Path(__file__).parent / "help-refresh"
_loader = SourceFileLoader("help_refresh", str(_TOOL))
_spec = importlib.util.spec_from_loader("help_refresh", _loader)
hr = importlib.util.module_from_spec(_spec)
sys.modules["help_refresh"] = hr
_loader.exec_module(hr)


RUN_DATE = dt.date(2026, 6, 23)
SINCE_DATE = dt.date(2026, 6, 16)


# A tiny controlled vocabulary mirroring the real _index.yaml shape:
#   concepts: { <concept>: [<file>.md, ...] }
FAKE_INDEX = {
    "version": 1,
    "concepts": {
        "apps": ["apps.md"],
        "forge": ["apps.md"],
        "model-tiers": ["ai-optimization.md", "cost-optimization.md"],
        "billing": ["usage.md"],
        "security": ["security.md"],
        "evo": ["overview.md"],
    },
}

# slug -> {"last_reviewed": "YYYY-MM-DD"|None}. apps + usage are STALE (>30d before
# RUN_DATE), the rest are recent. A concept can also be owned by a not-yet-written
# doc (no entry here) — exercised by test_concept_owned_by_unwritten_doc_is_not_bumped.
FAKE_DOC_META = {
    "apps": {"last_reviewed": "2026-04-01"},        # 83 days → STALE
    "ai-optimization": {"last_reviewed": "2026-06-20"},   # 3 days → fresh
    "cost-optimization": {"last_reviewed": "2026-06-20"}, # fresh
    "usage": {"last_reviewed": "2026-05-01"},       # 53 days → STALE
    "security": {"last_reviewed": "2026-06-22"},    # 1 day → fresh
    "overview": {"last_reviewed": "2026-06-21"},    # fresh
}


def _pr(number, title, files, body=""):
    return {"number": number, "title": title, "body": body, "files": files}


# ── Triage ───────────────────────────────────────────────────────────────────


def test_triage_skips_internal_only_pr():
    # ONLY internal paths → skipped.
    pr = _pr(1, "refactor admin server", [
        "packages/admin/evolve_admin/web/server.py",
        "packages/admin/tests/test_server.py",
        "docs/spec-foo-2026-06-01.md",
        "docs/incident-bar.md",
        "docs/diagnosis-baz.md",
        "docs/audit-qux.md",
    ])
    assert hr.is_internal_only_pr(pr) is True


def test_triage_keeps_pr_with_one_user_facing_file():
    # A single non-internal file flips the PR to a candidate.
    pr = _pr(2, "tweak admin + help", [
        "packages/admin/evolve_admin/web/index.html",  # internal
        "docs/help/apps.md",                            # user-facing → candidate
    ])
    assert hr.is_internal_only_pr(pr) is False


def test_triage_skips_pr_with_no_files():
    assert hr.is_internal_only_pr(_pr(3, "empty", [])) is True


def test_internal_path_classifier():
    assert hr.is_internal_path("packages/admin/evolve_admin/web/server.py") is True
    assert hr.is_internal_path("tests/test_x.py") is True
    assert hr.is_internal_path("edr/tests/test_y.py") is True
    assert hr.is_internal_path("docs/spec-thing-2026.md") is True
    assert hr.is_internal_path("docs/audit-thing.md") is True
    # user-facing / capability paths are NOT internal:
    assert hr.is_internal_path("docs/help/apps.md") is False
    assert hr.is_internal_path("packages/analyzer/forge/forge.py") is False
    assert hr.is_internal_path("docs/spec.md") is False  # 'spec' without trailing '-' is not the spec prefix


# ── Concept match ────────────────────────────────────────────────────────────


def test_concept_match_finds_terms_in_title_and_paths():
    matchers = hr.build_term_matchers(hr.vocabulary_terms(FAKE_INDEX))
    pr = _pr(10, "Forge: rework the apps gallery",
             ["packages/analyzer/forge/score.py"])
    # 'forge' (title + path) and 'apps' (title) both match the STRONG surface.
    assert hr.match_concepts(pr, matchers) == {"apps", "forge"}


def test_concept_match_body_only_hyphenated_concept_matches():
    matchers = hr.build_term_matchers(hr.vocabulary_terms(FAKE_INDEX))
    # 'model-tiers' is multi-word → safe to match in free-prose body.
    pr = _pr(16, "router cleanup", ["packages/router/r.py"],
             body="Rebalances the model-tiers cascade for cheaper routing.")
    assert "model-tiers" in hr.match_concepts(pr, matchers)


def test_concept_match_body_only_single_token_is_ignored():
    matchers = hr.build_term_matchers(hr.vocabulary_terms(FAKE_INDEX))
    # 'apps' (single token) mentioned ONLY in prose → NOT matched (noise control).
    pr = _pr(17, "internal refactor", ["packages/router/r.py"],
             body="Incidentally improves the apps experience a bit.")
    assert "apps" not in hr.match_concepts(pr, matchers)


def test_concept_match_hyphen_space_underscore_variants():
    matchers = hr.build_term_matchers(hr.vocabulary_terms(FAKE_INDEX))
    # "model tiers" (space) must match the kebab concept "model-tiers".
    assert "model-tiers" in hr.match_concepts(_pr(11, "rebalance model tiers", ["x.md"]), matchers)
    assert "model-tiers" in hr.match_concepts(_pr(12, "x", ["packages/model/tiers/r.py"]), matchers)
    assert "model-tiers" in hr.match_concepts(_pr(13, "model_tiers cleanup", ["x.md"]), matchers)


def test_concept_match_word_boundary_no_substring_false_positive():
    matchers = hr.build_term_matchers(hr.vocabulary_terms(FAKE_INDEX))
    # "evo" must NOT match inside "evolve".
    assert "evo" not in hr.match_concepts(_pr(14, "evolve the platform", ["x.md"]), matchers)
    # but DOES match as its own token / path segment.
    assert "evo" in hr.match_concepts(_pr(15, "evo tray fix", ["x.md"]), matchers)


def test_concept_owner_map_strips_md_and_keeps_primary_first():
    owners = hr.concept_owner_map(FAKE_INDEX)
    assert owners["apps"] == ["apps"]
    assert owners["model-tiers"] == ["ai-optimization", "cost-optimization"]


# ── Stale detect (30-day AND concept-overlap conjunction) ────────────────────


def test_stale_detect_requires_both_age_and_concept_overlap():
    # A PR touching "apps" (owned by STALE apps.md) and "security" (owned by
    # FRESH security.md). Only apps.md should be stale-flagged.
    prs = [_pr(20, "apps + security polish", ["docs/help/apps.md"],
               body="security audit and apps gallery")]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)

    affected = {d.slug for d in plan.docs}
    assert affected == {"apps", "security"}

    stale = {d.slug for d in plan.stale_docs}
    assert stale == {"apps"}             # apps.md is old AND its concept appeared
    # security.md owns a touched concept but is FRESH → affected, not bumped.
    sec = next(d for d in plan.docs if d.slug == "security")
    assert sec.stale is False


def test_exactly_30_days_old_doc_with_concept_overlap_is_not_stale():
    # BOUNDARY: the rule is `age > 30` (strictly greater), so a doc whose
    # last_reviewed is EXACTLY 30 days before the run date is NOT stale. The
    # doc owns a touched concept (security), so concept-overlap is satisfied
    # and ONLY the age condition decides the outcome.
    docs = dict(FAKE_DOC_META)
    docs["security"] = {"last_reviewed": "2026-05-24"}   # exactly 30 days before RUN_DATE
    assert hr.doc_age_days("2026-05-24", RUN_DATE) == 30  # pin the boundary
    prs = [_pr(25, "security hardening", ["docs/help/security.md"], body="security audit")]
    plan = hr.build_plan(prs, FAKE_INDEX, docs, RUN_DATE, since_date=SINCE_DATE)
    # affected (concept overlap met) but NOT stale (age == 30 is not > 30):
    assert "security" in {d.slug for d in plan.docs}
    assert "security" not in {d.slug for d in plan.stale_docs}
    sec = next(d for d in plan.docs if d.slug == "security")
    assert sec.stale is False


def test_fresh_doc_with_concept_overlap_is_not_stale():
    # model-tiers concept → ai-optimization.md + cost-optimization.md, both fresh.
    prs = [_pr(21, "rebalance model tiers", ["packages/router/tiers.py"])]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    assert {d.slug for d in plan.docs} == {"ai-optimization", "cost-optimization"}
    assert plan.stale_docs == []         # both fresh → no bump


def test_old_doc_without_concept_overlap_is_not_flagged():
    # usage.md is STALE (2026-05-01) but NO PR touches its 'billing' concept.
    prs = [_pr(22, "apps gallery", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    assert "usage" not in {d.slug for d in plan.docs}   # not affected at all


def test_missing_last_reviewed_counts_as_stale():
    docs = dict(FAKE_DOC_META)
    docs["apps"] = {"last_reviewed": None}
    prs = [_pr(23, "apps", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, docs, RUN_DATE, since_date=SINCE_DATE)
    assert "apps" in {d.slug for d in plan.stale_docs}


def test_concept_owned_by_unwritten_doc_is_not_bumped():
    # 'forge' is owned by apps.md here; add a concept owned by a missing doc.
    index = {
        "version": 1,
        "concepts": {"apps": ["apps.md"], "ghosted": ["ghost.md"]},
    }
    prs = [_pr(24, "ghosted apps feature", ["x.md"], body="ghosted")]
    plan = hr.build_plan(prs, index, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    slugs = {d.slug: d for d in plan.docs}
    assert "ghost" in slugs and slugs["ghost"].exists is False
    # an unwritten doc is affected (shows in queue) but never bumped:
    assert "ghost" not in {d.slug for d in plan.stale_docs}


# ── Zero-candidate no-op ─────────────────────────────────────────────────────


def test_zero_candidates_produces_empty_plan(tmp_path):
    prs = [
        _pr(30, "internal refactor", ["packages/admin/evolve_admin/web/server.py"]),
        _pr(31, "more tests", ["packages/admin/tests/test_a.py"]),
        _pr(32, "spec note", ["docs/spec-x-2026.md"]),
    ]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    assert plan.candidates == []
    assert plan.skipped == [30, 31, 32]
    assert plan.docs == []
    assert plan.has_changes is False


def test_candidate_with_no_concept_match_makes_no_changes():
    # A candidate (user-facing path) whose words match NO vocabulary concept.
    prs = [_pr(33, "unrelated change", ["docs/help/whatever.md"], body="nothing here")]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    assert len(plan.candidates) == 1
    assert plan.docs == []
    assert plan.has_changes is False


def test_apply_plan_writes_nothing_when_no_changes(tmp_path):
    # Build a clean repo skeleton; a no-change plan must not create REVIEW-QUEUE.
    (tmp_path / "docs" / "help").mkdir(parents=True)
    plan = hr.build_plan([], FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    changed = hr.apply_plan(plan, tmp_path, dry_run=False)
    assert changed == []
    assert not (tmp_path / "docs" / "help" / "REVIEW-QUEUE.md").exists()


# ── Mechanical: bump + render ────────────────────────────────────────────────


def test_bump_last_reviewed_rewrites_only_frontmatter_line():
    text = (
        "---\n"
        "title: \"Help: Apps Page\"\n"
        "slug: apps\n"
        "last_reviewed: 2026-04-01\n"
        "concepts:\n  - apps\n"
        "---\n\n"
        "# Help: Apps Page\n\n"
        "Body mentioning last_reviewed casually should be untouched.\n"
    )
    out = hr.bump_last_reviewed(text, "2026-06-23")
    assert "last_reviewed: 2026-06-23" in out
    assert "last_reviewed: 2026-04-01" not in out
    # the body line containing the words is NOT a frontmatter field → untouched.
    assert "Body mentioning last_reviewed casually" in out
    # exactly one replacement
    assert out.count("last_reviewed: 2026-06-23") == 1


def test_bump_last_reviewed_noop_without_field():
    text = "# no frontmatter here\n"
    assert hr.bump_last_reviewed(text, "2026-06-23") == text


def test_render_review_queue_lists_prs_and_stale_marker():
    prs = [_pr(40, "Forge reliability", ["docs/help/apps.md"], body="apps forge")]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    md = hr.render_review_queue(plan)
    assert "## apps.md" in md
    assert "STALE, bumped to 2026-06-23" in md
    assert "#40 — Forge reliability" in md
    assert "`apps`" in md and "`forge`" in md
    assert plan.run_date in md and plan.since_date in md


def test_render_review_queue_empty_plan():
    plan = hr.build_plan([], FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    md = hr.render_review_queue(plan)
    assert "No affected docs this week" in md


def test_apply_plan_end_to_end_in_tmp_repo(tmp_path):
    # Full disk round-trip: a stale doc gets bumped, REVIEW-QUEUE gets written.
    help_dir = tmp_path / "docs" / "help"
    help_dir.mkdir(parents=True)
    apps = help_dir / "apps.md"
    apps.write_text(
        "---\ntitle: Apps\nslug: apps\nlast_reviewed: 2026-04-01\n"
        "concepts:\n  - apps\nui_surface: admin.apps\nrelated_specs: []\n---\n\n# Apps\n",
        encoding="utf-8",
    )
    prs = [_pr(50, "Forge + apps", ["docs/help/apps.md"], body="apps forge")]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    changed = hr.apply_plan(plan, tmp_path, dry_run=False)

    queue = help_dir / "REVIEW-QUEUE.md"
    assert apps in changed and queue in changed
    assert "last_reviewed: 2026-06-23" in apps.read_text()
    assert "## apps.md" in queue.read_text()

    # Idempotent: re-applying the SAME plan changes nothing (already bumped + queue identical).
    changed2 = hr.apply_plan(plan, tmp_path, dry_run=False)
    assert changed2 == []


def test_dry_run_writes_nothing(tmp_path):
    help_dir = tmp_path / "docs" / "help"
    help_dir.mkdir(parents=True)
    apps = help_dir / "apps.md"
    apps.write_text("---\nslug: apps\nlast_reviewed: 2026-04-01\n---\n# Apps\n", encoding="utf-8")
    prs = [_pr(60, "apps forge", ["docs/help/apps.md"], body="apps forge")]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    changed = hr.apply_plan(plan, tmp_path, dry_run=True)
    # reports what WOULD change, but writes nothing:
    assert apps in changed
    assert "last_reviewed: 2026-04-01" in apps.read_text()
    assert not (help_dir / "REVIEW-QUEUE.md").exists()
