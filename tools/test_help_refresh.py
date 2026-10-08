"""Unit tests for tools/help-refresh — the weekly knowledge-refresh routine.

Pure-logic tests against FIXTURE PR data + a fixed run-date + a tiny fake
_index.yaml / docs tree. No network (never calls `gh`), no wall clock (every
date is passed in explicitly). This proves the four behaviours the README spec
turns on:

  * TRIAGE skips internal-only PRs and keeps user-facing ones;
  * CONCEPT-MATCH maps a PR's words/paths to the right help files;
  * STALE-DETECT respects the 30-day AND concept-overlap conjunction;
  * the ZERO-candidate run is a clean no-op (no file writes).

Plus the mechanical bits: the last_refreshed_against stamp (a machine run can NEVER
move last_reviewed), REVIEW-QUEUE rendering with carry-forward and capped lists,
and the site half — a stamping run leaves docs/gitpages/help/ matching a fresh
tools/build_help_site.py (what `help-site-check` asserts), a no-change run touches
no HTML, and a stamp that can't be rebuilt refuses and writes nothing.

Run with:
  cd tools && python3 -m pytest test_help_refresh.py -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import shutil
import subprocess
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


def _stale(plan):
    """Docs flagged STALE (>30d). Informational: nothing here is ever bumped."""
    return [d for d in plan.docs if d.stale and d.exists]


def _pr(number, title, files, body=""):
    return {"number": number, "title": title, "body": body, "files": files}


# ── Triage ───────────────────────────────────────────────────────────────────


def test_triage_skips_internal_only_pr():
    # ONLY internal paths → skipped.
    pr = _pr(1, "refactor admin server", [
        "packages/admin/evolve_admin/web/server.py",
        "packages/admin/tests/test_server.py",
        "internal/spec-foo-2026-06-01.md",
        "internal/incident-bar.md",
        "internal/diagnosis-baz.md",
        "internal/audit-qux.md",
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
    assert hr.is_internal_path("internal/spec-thing-2026.md") is True
    assert hr.is_internal_path("internal/audit-thing.md") is True
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

    stale = {d.slug for d in _stale(plan)}
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
    assert "security" not in {d.slug for d in _stale(plan)}
    sec = next(d for d in plan.docs if d.slug == "security")
    assert sec.stale is False


def test_fresh_doc_with_concept_overlap_is_not_stale():
    # model-tiers concept → ai-optimization.md + cost-optimization.md, both fresh.
    prs = [_pr(21, "rebalance model tiers", ["packages/router/tiers.py"])]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    assert {d.slug for d in plan.docs} == {"ai-optimization", "cost-optimization"}
    assert _stale(plan) == []         # both fresh → no bump


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
    assert "apps" in {d.slug for d in _stale(plan)}


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
    assert "ghost" not in {d.slug for d in _stale(plan)}


# ── Zero-candidate no-op ─────────────────────────────────────────────────────


def test_zero_candidates_produces_empty_plan(tmp_path):
    prs = [
        _pr(30, "internal refactor", ["packages/admin/evolve_admin/web/server.py"]),
        _pr(31, "more tests", ["packages/admin/tests/test_a.py"]),
        _pr(32, "spec note", ["internal/spec-x-2026.md"]),
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


def test_stamp_refreshed_against_writes_its_own_field_and_never_last_reviewed():
    text = (
        "---\n"
        "title: \"Help: Apps Page\"\n"
        "slug: apps\n"
        "last_reviewed: 2026-04-01\n"
        "concepts:\n  - apps\n"
        "---\n\n"
        "# Help: Apps Page\n\n"
        "Body mentioning last_refreshed_against casually should be untouched.\n"
    )
    out = hr.stamp_refreshed_against(text, "2026-06-16..2026-06-23; 1 merged PR touched concepts: apps")
    assert 'last_refreshed_against: "2026-06-16..2026-06-23; 1 merged PR touched concepts: apps"' in out
    assert "last_reviewed: 2026-04-01\n" in out            # the human field is untouched
    assert out.index("last_reviewed:") < out.index("last_refreshed_against:") < out.index("concepts:")
    assert "Body mentioning last_refreshed_against casually" in out
    # re-stamping replaces, never duplicates
    again = hr.stamp_refreshed_against(out, "2026-06-23..2026-06-30; 2 merged PRs touched concepts: apps")
    assert again.count("last_refreshed_against:") == 1
    assert "2026-06-16..2026-06-23" not in again


def test_stamp_noop_without_frontmatter():
    text = "# no frontmatter here\n"
    assert hr.stamp_refreshed_against(text, "x") == text


def test_render_review_queue_lists_prs_and_stale_marker():
    prs = [_pr(40, "Forge reliability", ["docs/help/apps.md"], body="apps forge")]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    md = hr.render_review_queue(plan)
    assert "## apps.md" in md
    assert "STALE >30d" in md and "bumped" not in md
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
    apps = _seed_site_repo(tmp_path)
    prs = [_pr(50, "Forge + apps", ["docs/help/apps.md"], body="apps forge")]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    changed = hr.apply_plan(plan, tmp_path, dry_run=False)

    queue = help_dir / "REVIEW-QUEUE.md"
    assert apps in changed and queue in changed
    assert "last_reviewed: 2026-04-01" in apps.read_text()      # a machine run never moves it
    assert "last_refreshed_against:" in apps.read_text()
    assert "## apps.md" in queue.read_text()

    # Idempotent: re-applying the SAME plan changes nothing (already stamped + queue identical).
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
    assert "last_refreshed_against" not in apps.read_text()
    assert not (help_dir / "REVIEW-QUEUE.md").exists()


# ── Site half: a stamp is a site change (RULINGS 2026-09-28 (3)) ──────────────
#
# help-site-check rebuilds docs/gitpages/help/ from docs/help/ and fails on any
# diff. Every topic page renders "Last reviewed <date>", so a stamp that doesn't
# regenerate the site reds that job — #4390 and #4559 both did. These run the
# REAL tools/build_help_site.py (copied into a tmp repo; it resolves paths from
# its own location), never the committed site.

_BUILDER = Path(__file__).parent / "build_help_site.py"


def _doc(slug, last_reviewed, concept):
    return (
        f"---\ntitle: \"Help: {slug.title()}\"\nslug: {slug}\naudience: public\n"
        f"last_reviewed: {last_reviewed}\nconcepts:\n  - {concept}\n---\n\n"
        f"# Help: {slug.title()}\n\nWhat the {slug} page does.\n"
    )


def _seed_site_repo(root: Path) -> Path:
    """A tmp repo with the real site builder, two public docs, and a committed
    site that matches them — the state `main` is in before a refresh runs."""
    (root / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy(_BUILDER, root / "tools" / "build_help_site.py")
    help_dir = root / "docs" / "help"
    help_dir.mkdir(parents=True, exist_ok=True)
    apps = help_dir / "apps.md"
    apps.write_text(_doc("apps", "2026-04-01", "apps"), encoding="utf-8")            # STALE
    (help_dir / "security.md").write_text(_doc("security", "2026-06-22", "security"), encoding="utf-8")  # fresh
    _build(root)
    return apps


def _build(root: Path) -> None:
    subprocess.run([sys.executable, str(root / "tools" / "build_help_site.py")],
                   cwd=root, check=True, capture_output=True)


def _site(root: Path) -> dict[str, bytes]:
    out = root / "docs" / "gitpages" / "help"
    return {p.name: p.read_bytes() for p in sorted(out.glob("*.html"))}


def _load(root: Path) -> dict:
    return {slug: {"last_reviewed": m["last_reviewed"]} for slug, m in hr.load_docs(root).items()}


def test_stamp_run_leaves_site_matching_a_fresh_build(tmp_path):
    # The help-site-check assertion, run against the tree a stamping run leaves.
    apps = _seed_site_repo(tmp_path)
    prs = [_pr(70, "apps gallery rework", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, _load(tmp_path), RUN_DATE, since_date=SINCE_DATE)
    assert [d.slug for d in _stale(plan)] == ["apps"]

    changed = hr.apply_plan(plan, tmp_path, dry_run=False)

    apps_html = tmp_path / "docs" / "gitpages" / "help" / "apps.html"
    assert "last_reviewed: 2026-04-01" in apps.read_text()       # never moved by a machine
    assert apps_html in changed                       # the page rides in the same PR
    page = apps_html.read_text()
    assert "Last reviewed 2026-04-01" in page         # the human date, unchanged...
    assert "Machine-checked against 2026-06-16..2026-06-23" in page   # ...and the machine check, named
    assert "not a human read" in page
    left = _site(tmp_path)
    _build(tmp_path)                                  # what help-site-check does
    assert _site(tmp_path) == left                    # ...and it is a no-op


def test_rerun_of_the_same_plan_rebuilds_nothing(tmp_path, monkeypatch):
    # Stamps already current and queue identical: nothing changed, so the site
    # build must not even run.
    _seed_site_repo(tmp_path)
    prs = [_pr(71, "security hardening", ["docs/help/security.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, _load(tmp_path), RUN_DATE, since_date=SINCE_DATE)
    hr.apply_plan(plan, tmp_path, dry_run=False)
    before = _site(tmp_path)

    def _must_not_run(_root):
        raise AssertionError("a run that changes no doc must not rebuild the site")
    monkeypatch.setattr(hr, "rebuild_site", _must_not_run)

    assert hr.apply_plan(plan, tmp_path, dry_run=False) == []
    assert _site(tmp_path) == before


def test_untouched_doc_page_shows_no_machine_line(tmp_path):
    _seed_site_repo(tmp_path)
    page = (tmp_path / "docs" / "gitpages" / "help" / "apps.html").read_text()
    assert "Last reviewed 2026-04-01" in page and "Machine-checked" not in page


def test_stamp_refuses_and_writes_nothing_when_site_cannot_build(tmp_path):
    # Fails closed, checked up front: no builder -> refuse before any write.
    apps = _seed_site_repo(tmp_path)
    (tmp_path / "tools" / "build_help_site.py").unlink()
    before_md, before_site = apps.read_text(), _site(tmp_path)
    prs = [_pr(72, "apps gallery rework", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, _load(tmp_path), RUN_DATE, since_date=SINCE_DATE)

    try:
        hr.apply_plan(plan, tmp_path, dry_run=False)
    except hr.SiteRebuildError as exc:
        assert "build_help_site.py not found" in str(exc)
    else:
        raise AssertionError("a stamp with no site builder must refuse")

    assert apps.read_text() == before_md
    assert not (tmp_path / "docs" / "help" / "REVIEW-QUEUE.md").exists()
    assert _site(tmp_path) == before_site


def test_stamp_rolls_back_when_site_build_fails(tmp_path):
    # Fails closed, after the fact: the build runs and errors -> every write undone.
    apps = _seed_site_repo(tmp_path)
    (tmp_path / "tools" / "build_help_site.py").write_text(
        "import sys\nsys.stderr.write('boom\\n')\nsys.exit(1)\n", encoding="utf-8")
    before_md, before_site = apps.read_text(), _site(tmp_path)
    prs = [_pr(73, "apps gallery rework", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, _load(tmp_path), RUN_DATE, since_date=SINCE_DATE)

    try:
        hr.apply_plan(plan, tmp_path, dry_run=False)
    except hr.SiteRebuildError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("a failed site build must refuse")

    assert apps.read_text() == before_md
    assert not (tmp_path / "docs" / "help" / "REVIEW-QUEUE.md").exists()
    assert _site(tmp_path) == before_site


def test_main_exits_2_and_names_the_cause_on_refusal(tmp_path, monkeypatch, capsys):
    apps = _seed_site_repo(tmp_path)
    (tmp_path / "docs" / "help" / "_index.yaml").write_text(
        "version: 1\nconcepts:\n  apps: [apps.md]\n", encoding="utf-8")
    (tmp_path / "tools" / "build_help_site.py").unlink()
    monkeypatch.setattr(hr, "fetch_merged_prs",
                        lambda *a, **k: [_pr(74, "apps rework", ["docs/help/apps.md"])])

    rc = hr.main(["--repo-root", str(tmp_path), "--run-date", "2026-06-23"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "build_help_site.py not found" in err
    assert "last_reviewed: 2026-04-01" in apps.read_text()


# ── The class this tool must not be: a control passed by absence of evidence ──
#
# A refresh run records what a MACHINE checked. Only a person reading the prose
# may move `last_reviewed`. PR #4268 bumped thirteen docs' dates with no prose
# edit; nothing could tell that from a review. These pin the split.


def test_a_machine_run_can_never_move_last_reviewed(tmp_path):
    # FAILS against the pre-change tree, where a STALE doc's last_reviewed was
    # rewritten to the run date. Stale + concept overlap is exactly that case.
    apps = _seed_site_repo(tmp_path)
    before = apps.read_text()
    assert "last_reviewed: 2026-04-01" in before
    prs = [_pr(80, "apps gallery rework", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, _load(tmp_path), RUN_DATE, since_date=SINCE_DATE)
    assert [d.slug for d in _stale(plan)] == ["apps"]

    hr.apply_plan(plan, tmp_path, dry_run=False)

    after = apps.read_text()
    assert "last_reviewed: 2026-04-01" in after
    fm = after.split("---")[1]
    assert [l for l in fm.splitlines() if l.startswith("last_reviewed:")] == ["last_reviewed: 2026-04-01"]
    assert 'last_refreshed_against: "2026-06-16..2026-06-23; 1 merged PR touched concepts: apps"' in fm


def test_refreshed_against_names_its_subject():
    prs = [_pr(81, "apps and forge", ["docs/help/apps.md"], body="apps forge"),
           _pr(82, "apps again", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    apps = next(d for d in plan.docs if d.slug == "apps")
    v = hr.refreshed_against(plan, apps)
    assert "2026-06-16..2026-06-23" in v and "2 merged PRs" in v and "apps" in v and "forge" in v


def test_fresh_stamp_with_old_review_reads_as_machine_checked_not_read():
    # last_refreshed_against current, last_reviewed old -> the queue says so.
    prs = [_pr(83, "apps gallery rework", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    md = hr.render_review_queue(plan)
    assert "NEEDS REVIEW" in md and "STALE >30d" in md
    assert "never touches `last_reviewed`" in md


def test_bot_written_date_is_not_a_human_review():
    # security.md's date lies INSIDE the window, but the bot wrote it.
    docs = {"security": {"last_reviewed": "2026-06-22"}}
    prs = [_pr(84, "security hardening", ["docs/help/security.md"])]
    honest = hr.build_plan(prs, FAKE_INDEX, docs, RUN_DATE, since_date=SINCE_DATE)
    assert honest.docs[0].needs_review is False
    plan = hr.build_plan(prs, FAKE_INDEX, docs, RUN_DATE, since_date=SINCE_DATE,
                         machine_bumps={"security": {"2026-06-22"}})
    d = plan.docs[0]
    assert d.machine_dated and d.needs_review
    assert "written by the refresh bot — NOT HUMAN-REVIEWED" in hr.render_review_queue(plan)


def test_deprecated_docs_are_excluded_not_listed():
    docs = dict(FAKE_DOC_META, apps={"last_reviewed": "2026-04-01", "status": "deprecated"})
    prs = [_pr(85, "apps gallery rework", ["docs/help/apps.md"])]
    plan = hr.build_plan(prs, FAKE_INDEX, docs, RUN_DATE, since_date=SINCE_DATE)
    assert "apps" not in {d.slug for d in plan.docs}


def test_unverified_entries_survive_a_regeneration():
    # Week 1 lists apps.md; week 2 has NO PR touching apps. The old entry must
    # still be in the new queue — the queue is not reset every Monday.
    wk1 = hr.build_plan([_pr(90, "apps gallery rework", ["docs/help/apps.md"])],
                        FAKE_INDEX, FAKE_DOC_META, dt.date(2026, 6, 16), since_date=dt.date(2026, 6, 9))
    q1 = hr.render_review_queue(wk1)
    wk2 = hr.build_plan([_pr(91, "security hardening", ["docs/help/security.md"])],
                        FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE, previous_queue=q1)
    md = hr.render_review_queue(wk2)
    apps = next(d for d in wk2.docs if d.slug == "apps")
    assert apps.prs == [] and [n for n, _ in apps.carried] == [90]
    assert "Carried forward from earlier runs (queued 2026-06-16)" in md
    assert "- #90 — apps gallery rework" in md
    # ...and it keeps surviving: a third regeneration still carries it.
    wk3 = hr.build_plan([], FAKE_INDEX, FAKE_DOC_META, dt.date(2026, 6, 30), since_date=dt.date(2026, 6, 23),
                        previous_queue=md)
    assert [n for n, _ in next(d for d in wk3.docs if d.slug == "apps").carried] == [90]


def test_carried_entry_is_dropped_once_a_human_reads_the_doc():
    wk1 = hr.build_plan([_pr(92, "apps gallery rework", ["docs/help/apps.md"])],
                        FAKE_INDEX, FAKE_DOC_META, dt.date(2026, 6, 16), since_date=dt.date(2026, 6, 9))
    q1 = hr.render_review_queue(wk1)
    read = dict(FAKE_DOC_META, apps={"last_reviewed": "2026-06-20"})   # a person, after 06-16
    wk2 = hr.build_plan([], FAKE_INDEX, read, RUN_DATE, since_date=SINCE_DATE, previous_queue=q1)
    assert wk2.docs == []
    # ...but a bump ON the generation day (the old bot behaviour) does not count:
    same_day = dict(FAKE_DOC_META, apps={"last_reviewed": "2026-06-16"})
    wk3 = hr.build_plan([], FAKE_INDEX, same_day, RUN_DATE, since_date=SINCE_DATE, previous_queue=q1)
    assert [d.slug for d in wk3.docs] == ["apps"]


def test_per_doc_list_is_capped_ranked_and_bookkeeping_collapsed():
    prs = [_pr(100 + i, f"apps change {i}", ["docs/help/apps.md"]) for i in range(12)]
    prs.append(_pr(200, "pm(opus): apps lane state", ["internal/pm/state.md"]))
    prs.append(_pr(201, "docs(help): weekly knowledge refresh 2026-06-16", ["docs/help/apps.md"]))
    plan = hr.build_plan(prs, FAKE_INDEX, FAKE_DOC_META, RUN_DATE, since_date=SINCE_DATE)
    md = hr.render_review_queue(plan)
    bullets = [l for l in md.splitlines() if l.startswith("- #")]
    assert len(bullets) == hr.PER_DOC_PR_CAP
    assert "- #111 — apps change 11" in md                  # newest first among equals
    assert "Beyond the cap (8), still unread: #103 #102 #101 #100" in md
    assert "Lane bookkeeping" in md and "#200" in md and "#201" in md
    assert not any(l.startswith("- #200") or l.startswith("- #201") for l in md.splitlines())


def test_truncated_pr_list_is_refused(monkeypatch):
    class _P:
        stdout = "[" + ",".join('{"number": %d, "files": []}' % i for i in range(3)) + "]"
    monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: _P())
    try:
        hr.fetch_merged_prs(SINCE_DATE, limit=3)
    except RuntimeError as exc:
        assert "truncated" in str(exc)
    else:
        raise AssertionError("a list cut off at the limit must not read as complete")
