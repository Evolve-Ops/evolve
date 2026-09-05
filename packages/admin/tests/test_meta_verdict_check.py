"""Unit tests for tools/meta-verdict-check.

`tools/meta-verdict-check` is the B2 fact-gate (META:substrate Initiative 12,
internal/spec-substrate-2026-06-15.md §17): it answers whether a PR carries an
INDEPENDENT two-pass review artifact *in its body* whose overall verdict is
pass-equivalent — the artifact a chip cannot fake by writing a ledger `two_pass`
string in the same pulse that opened the PR (the #3347 root cause).

These tests pin the added artifact requirement (section present / absent, overall
verdict extraction, blocking wins) AND that the pass/blocking classification is
the reused canonical `verdict_is_pass` from tools/meta-queue — NOT a fork. The
tool is an extensionless script under tools/, so we load it by path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "meta-verdict-check"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("meta_verdict_check", str(_TOOL))
    spec = importlib.util.spec_from_loader("meta_verdict_check", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_verdict_check"] = mod
    loader.exec_module(mod)
    return mod


mvc = _load_tool()


def _q(body):
    """Just the boolean from verdict_qualifies (drops verdict + reason)."""
    return mvc.verdict_qualifies(body)[0]


# ── qualifies: a real PR-body review artifact with a pass-equivalent verdict ──


def test_labeled_pass_qualifies():
    body = "## Summary\nstuff\n\n## Two-pass review\n**Verdict:** PASS\n"
    assert _q(body) is True


def test_prose_nonblocking_qualifies():
    # The live shape that an exact `== PASS` match stranded — must qualify via
    # the reused verdict_is_pass.
    body = ("## Two-pass review\n"
            "SHIP (2 non-blocking concerns) — independent adversarial reviewer "
            "found no blocking flaw.\n")
    assert _q(body) is True


def test_independent_heading_variant_qualifies():
    body = "## Independent two-pass review\nVerdict: SHIP / CONCERNS-no-blockers\n"
    assert _q(body) is True


def test_two_pass_with_space_heading_qualifies():
    body = "### Two pass review\nOverall: APPROVED\n"
    assert _q(body) is True


def test_bold_verdict_value_is_cleaned_and_qualifies():
    # Markdown emphasis around the verdict value must be stripped so it opens with
    # its real token (verdict_is_pass keys on startswith).
    body = "## Two-pass review\n**Verdict:** **SHIP** — no blockers\n"
    assert _q(body) is True


def test_verdict_without_label_opening_line_qualifies():
    body = "## Two-pass review\nLGTM, no blockers found on a second read.\n"
    assert _q(body) is True


# ── does NOT qualify ─────────────────────────────────────────────────────────


def test_missing_section_does_not_qualify():
    # The whole point: a ledger string is not enough; no PR-body section → hold.
    body = "## Summary\nGreat change.\n\n## Testing\nran the suite.\n"
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert verdict is None
    assert "no two-pass review section" in reason


def test_empty_body_does_not_qualify():
    assert _q("") is False
    assert _q(None) is False


def test_fail_verdict_does_not_qualify():
    body = "## Two-pass review\nVerdict: FAIL — a real bug in the scanner path.\n"
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


def test_do_not_merge_does_not_qualify():
    body = "## Two-pass review\n**Verdict:** SHIP — but DO NOT MERGE until release.\n"
    assert _q(body) is False


def test_unexempted_concerns_does_not_qualify():
    body = "## Two-pass review\nVerdict: CONCERNS — cross-bot read leak, needs a fix.\n"
    assert _q(body) is False


def test_pending_verdict_does_not_qualify():
    # Unverified (neither pass nor blocking) — section exists but no approval.
    body = "## Two-pass review\nVerdict: pending independent review\n"
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "no pass-equivalent" in reason


def test_section_without_verdict_line_does_not_qualify():
    body = "## Two-pass review\nI looked at the diff and it seems fine to me.\n"
    assert _q(body) is False


def test_blocking_section_wins_over_pass_section():
    # Two sections; one PASS, one FAIL → the blocker holds the whole PR.
    body = ("## Two-pass review\nVerdict: SHIP\n\n"
            "## Independent two-pass review\nVerdict: FAIL — found a regression.\n")
    assert _q(body) is False


def test_pass_prefix_with_fail_substring_holds():
    # Canonical verdict_is_pass behaviour is reused: "no failures" trips the
    # "fail" substring and reads as not-pass (documented, conservative).
    body = "## Two-pass review\nVerdict: PASS, no failures observed\n"
    assert _q(body) is False


# ── labelled tier: the same guards the heading tier already applies ──────────
#
# `verdict_is_pass` keys on ``startswith``, so before these guards the labelled
# tier read any prose opening with pass/ship as pass-equivalent. Three live-shaped
# false MERGES are pinned below. The guards are deliberately PASS-side only:
# blocking stays ungated (gating it would loosen the gate), which the last two
# tests pin.


def test_labeled_prose_handoff_does_not_qualify():
    # "Passing this to the operator" is a hand-off, not a verdict — the exact
    # gap the whole-word guard closes on the labelled tier.
    body = ("## Independent two-pass review\n"
            "Verdict: Passing this to the operator for a look\n")
    ok, _verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "does not open with" in reason


def test_labeled_prose_status_note_does_not_qualify():
    body = "## Two-pass review\nVerdict: Shipped it to staging for soak\n"
    assert _q(body) is False


def test_labeled_pass_index_progress_marker_does_not_qualify():
    # `_heading_verdict` has always guarded "… : Pass 1 of 2"; the labelled tier
    # did not, so a progress marker read as a PASS.
    body = "## Two-pass review\nVerdict: Pass 1 of 2\n"
    assert _q(body) is False


def test_labeled_inflected_pass_prose_does_not_qualify():
    # "Passes cleanly" is a genuine-sounding human verdict but is not a token;
    # held (conservative) and consistent with the bare-line tier, which has
    # always rejected it. The documented form is the bare token.
    body = "## Two-pass review\nVerdict: Passes cleanly\n"
    assert _q(body) is False


def test_inconclusive_label_suppresses_fallback_line():
    # THE REGRESSION GUARD for this change: rejecting the prose label must not
    # let a later stray body line decide. The reviewer wrote an explicit,
    # inconclusive verdict — that holds the PR.
    body = ("## Two-pass review\n"
            "Verdict: Needs more work before merge\n"
            "PASS on the tests\n")
    assert _q(body) is False


def test_labeled_prose_naming_a_blocker_still_blocks():
    # Blocking is NOT token-gated: a prose label that names a blocker must still
    # hold the PR, exactly as before the guards.
    body = ("## Two-pass review\n"
            "Verdict: needs another look, one blocker remains\n")
    ok, _verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


def test_labeled_token_verdicts_still_qualify_after_the_guards():
    # The guards must not touch the forms the brief tells chips to write.
    assert _q("## Independent two-pass review\nVerdict: PASS — no blockers\n") is True
    assert _q("## Two-pass review\nVerdict: SHIP (2 non-blocking concerns)\n") is True
    assert _q("### Two pass review\nOverall: APPROVED\n") is True
    assert _q("## Two-pass review\n**Verdict:** **LGTM** — clean\n") is True


# ── section extraction boundaries ────────────────────────────────────────────


def test_section_stops_at_next_same_level_heading():
    # A verdict token under a LATER, unrelated section must not leak into the
    # review section's verdict extraction.
    body = ("## Two-pass review\nlooked good\n\n"
            "## Deployment\nFAIL-safe rollback documented.\n")
    # The review section itself has no verdict line → does not qualify (the
    # "FAIL-safe" under Deployment is out of the section).
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "no pass-equivalent" in reason


def test_subheading_stays_in_section():
    body = ("## Two-pass review\n"
            "### Pass 1\nread the diff\n"
            "### Pass 2\nVerdict: SHIP — no blockers\n")
    assert _q(body) is True


def test_fenced_heading_is_not_a_real_section():
    # A ## Two-pass review heading shown as an EXAMPLE inside a code fence (e.g.
    # a PR body documenting the review format) must NOT count as a real artifact.
    body = ("## Summary\nHere is the review template we use:\n\n"
            "```\n## Two-pass review\nVerdict: SHIP\n```\n\n"
            "That's the format.\n")
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "no two-pass review section" in reason


def test_tilde_fenced_heading_ignored():
    body = "~~~\n## Two-pass review\nVerdict: PASS\n~~~\n"
    assert _q(body) is False


def test_real_section_after_fenced_example_qualifies():
    # A fenced example followed by a genuine review section → qualifies on the
    # real one.
    body = ("```\n## Two-pass review\nVerdict: FAIL (example)\n```\n\n"
            "## Two-pass review\nVerdict: SHIP — no blockers\n")
    assert _q(body) is True


def test_fenced_verdict_line_within_real_section_ignored():
    # Inside a real section, a Verdict: line shown inside a fence is an example,
    # not the overall verdict; the real labelled line outside the fence wins.
    body = ("## Two-pass review\n"
            "For reference the blocking form looks like:\n"
            "```\nVerdict: FAIL\n```\n"
            "Verdict: SHIP — no blockers\n")
    assert _q(body) is True


# ── the verdict predicate is the reused canonical one, not a fork ────────────


def test_reuses_meta_queue_predicate():
    # The predicates mvc uses must be DEFINED in tools/meta-queue (loaded from
    # there), not forked into meta-verdict-check — that is what keeps the verdict
    # reading canonical. co_filename proves the source file.
    assert mvc.verdict_is_pass.__code__.co_filename.endswith("meta-queue")
    assert mvc.verdict_is_blocking.__code__.co_filename.endswith("meta-queue")


# ── main() exit-code contract ────────────────────────────────────────────────


def test_main_exit_codes(tmp_path):
    good = tmp_path / "good.md"
    good.write_text("## Two-pass review\nVerdict: PASS\n")
    bad = tmp_path / "bad.md"
    bad.write_text("## Summary\nno review here\n")
    assert mvc.main([str(1), "--body-file", str(good)]) == 0
    assert mvc.main([str(2), "--body-file", str(bad)]) == 1


def test_main_rejects_non_numeric_pr(tmp_path):
    import pytest
    with pytest.raises(SystemExit) as ei:
        mvc.main(["not-a-number", "--body-file", "-"])
    assert ei.value.code == 2


# ── regression: the REAL #3395 false-block (2026-07-03) ─────────────────────
#
# A genuine two-pass review whose verdict was carried IN THE HEADING
# ("## Two-pass review verdict: PASS") with no labelled Verdict: line inside the
# section. extract_verdict ignored the heading, and the token fallback latched
# onto the "**Pass 1 (self-review):**…" reviewer-STRUCTURE line — whose prose
# ("encoded as `xfail(strict=True)`") tripped the "fail" hard-block substring —
# so a real PASS artifact reported "a review section carries a blocking verdict"
# and stranded the PR until a human reformatted the body. The section text below
# is the live artifact VERBATIM (preserved as the PR's first comment).

_PR3395_HEADING = "## Two-pass review verdict: PASS"

_PR3395_TAIL = """\

**Pass 1 (self-review):** exercised every check against live gallery data; the gate caught real drift on its first run (4 stale `pkg_version` index rows, fixed in-diff) and one product-level defect (`packages/gallery/catalog.json` fully disjoint from the gallery, encoded as `xfail(strict=True)`).

**Pass 2 (independent adversarial subagent):** returned **PASS — no blockers in the diff**, verifying hermeticity (stub interception is real; nothing touches `/Users/<bot>`; fully deterministic under Linux/CI), the worktree `evolve_admin` rebind, all 4 version fixes (exact, zero other drift), the mechanism source-scrape tripwire, and shard/parametrize determinism. It surfaced one **major** finding + minors, addressed as follows:

- **[major — FIXED in 61d9abf]** The gate didn't run on gallery-only PRs: `ci.yml`'s `changes` job (and its `tools/preflight` mirror) gated the admin suite on `packages/admin|analyzer` only, so a `gallery/**`-only PR set `python=false` → admin suite (which owns this gate) skipped → the bad package would merge green and red main on the push (the documented "green PR reds main" shape). Added `^gallery/` and `^packages/gallery/` to the python predicate in **both** files, byte-for-byte in sync. This PR's own edit to `ci.yml`/`gallery/` now also forces the suite to run here.
- **[minor — FIXED]** Added `oc_*_instruction` install-shape coverage (`file`/`section_anchor`/`body` must be non-empty strings). The live preflight sweep couldn't catch a *type-malformed* value here because the wiring validator raises `AttributeError` on a non-empty dict/list and `preflight_check` swallows it (`sa_result=None` → no row).
- **[minor — FIXED]** Removed a `.strip()` drift in the integration mirror so it matches `_check_one_integration`'s unstripped `req['id']` read exactly (a dead `" github "` id no longer passes the mirror).
- **[minor — kept as-is]** `crontab` stays in `_KNOWN_MECHANISMS` (it *is* in the materializer's dispatch ladder and the task brief explicitly enumerates it, so the source tripwire is correct); a crontab package is still rejected end-to-end via the `scheduled_actions` preflight row. No current package uses it.
- **[nits — acknowledged, not actioned]** xfail-on-retire silence, in-process `__import__` for python_package reqs (zero such reqs today), and `installed_artifact` stamp bypass (no stamped packages today) are latent-only; noted for a future package that trips them.

**Verification after follow-ups:** module 161 passed / 1 xfailed; `tools/preflight` 22 passed / 0 failed; path-gate parity confirmed (both `ci.yml` and `preflight` match `gallery/`, `packages/gallery/`). CI on the prior commit was 19/19 green; re-running on the follow-up."""

# The original build-chip form: heading-carried verdict, no labelled line.
_PR3395_ORIGINAL = _PR3395_HEADING + "\n" + _PR3395_TAIL + "\n"

# The human workaround that unstranded the PR: a labelled Verdict: line added.
_PR3395_LABELED_LINE = ("Verdict: PASS — no blockers; the one major finding was "
                        "fixed in-diff (61d9abf), minors fixed or acknowledged.")
_PR3395_EDITED = "%s\n\n%s\n%s\n" % (_PR3395_HEADING, _PR3395_LABELED_LINE, _PR3395_TAIL)


def test_pr3395_original_heading_carried_form_qualifies():
    # The heading itself carries the explicit verdict — same tier as a label.
    ok, verdict, reason = mvc.verdict_qualifies(_PR3395_ORIGINAL)
    assert ok is True
    assert verdict == "PASS"


def test_pr3395_edited_labeled_line_form_qualifies():
    ok, verdict, reason = mvc.verdict_qualifies(_PR3395_EDITED)
    assert ok is True
    assert verdict.startswith("PASS")


def test_pr3395_labeled_line_under_plain_heading_qualifies():
    # The labelled path must survive the structure lines independent of the
    # heading fix (a plain "## Two-pass review" heading, verdict in the body).
    body = "## Two-pass review\n%s\n%s\n" % (_PR3395_LABELED_LINE, _PR3395_TAIL)
    assert _q(body) is True


def test_pr3395_tail_without_verdict_is_unverified_not_blocking():
    # Item 3 of the fix: with NO verdict anywhere, the Pass-1/Pass-2 structure
    # lines (xfail prose and all) must not be extracted — the section reads as
    # UNVERIFIED (safe hold -> auto-review), never as a blocking verdict.
    body = "## Two-pass review\n" + _PR3395_TAIL + "\n"
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "no pass-equivalent" in reason
    assert "blocking" not in reason


def _main_stdin(monkeypatch, body):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    return mvc.main(["3395", "--body-file", "-"])


def test_main_stdin_pr3395_original_exits_zero(monkeypatch):
    assert _main_stdin(monkeypatch, _PR3395_ORIGINAL) == 0


def test_main_stdin_pr3395_edited_exits_zero(monkeypatch):
    assert _main_stdin(monkeypatch, _PR3395_EDITED) == 0


# ── heading-carried verdicts (tier 1b) ───────────────────────────────────────


def test_heading_dash_ship_variant_qualifies():
    body = "## Independent two-pass review — SHIP\nno blockers found on a second read.\n"
    ok, verdict, _ = mvc.verdict_qualifies(body)
    assert ok is True
    assert verdict == "SHIP"


def test_heading_carried_fail_blocks():
    body = "## Two-pass review verdict: FAIL — regression found\nnotes\n"
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


def test_heading_pass_digit_is_structure_not_a_verdict():
    # "Pass 1 of 2" opens with a pass token but is a progress marker — reading
    # it as a verdict would false-MERGE. Must stay unverified.
    body = "## Two-pass review: Pass 1 of 2\nnotes only, review incomplete.\n"
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "no pass-equivalent" in reason


def test_heading_pass_vs_labeled_fail_contradiction_holds():
    # Contradictory explicit verdicts (heading PASS, labelled FAIL): the
    # blocking one wins — hold, never merge on the other.
    body = "## Two-pass review verdict: PASS\nVerdict: FAIL — found a real bug\n"
    assert _q(body) is False


def test_heading_prose_remainder_is_not_a_verdict():
    # A descriptive heading remainder is neither labelled nor token-opening —
    # the body's labelled line decides.
    body = "## Two-pass review of the failover patch\nVerdict: SHIP — no blockers\n"
    assert _q(body) is True


def test_structure_line_excluded_from_fallback_lets_real_token_line_win():
    body = ("## Two-pass review\n"
            "**Pass 1 (self-review):** clean, one xfail(strict=True) case documented.\n"
            "PASS — no blockers on either pass.\n")
    ok, verdict, _ = mvc.verdict_qualifies(body)
    assert ok is True
    assert verdict.startswith("PASS")


# ── adversarial-review hardening (Pass-2 verify, 2026-07-03) ─────────────────
#
# The first cut of the #3395 fix added a heading tier + a blanket "exclude the
# Pass-<digit> line" rule. An independent adversarial pass found that both opened
# FALSE-MERGE holes (the unsafe direction). These pin the redesign: heading
# verdicts need a real separator + whole-word token; a blocking verdict ANYWHERE
# in the section wins (including one carried on a Pass-N structure line, via
# prefix-strip); and descriptive/progress headings hold.


def test_descriptive_heading_no_separator_does_not_qualify():
    # "## Two-pass review shipping checklist" — "shipping" starts with "ship" but
    # is not a verdict, and there is no separator. Must HOLD, not merge.
    for h in ("## Two-pass review shipping checklist",
              "## Two-pass review pass criteria",
              "## Two-pass review-ship checklist"):
        ok, verdict, _ = mvc.verdict_qualifies(h + "\nbody text\n")
        assert ok is False, h
        assert verdict is None, h


def test_spelled_pass_index_heading_does_not_qualify():
    # "Pass one of two" is the spelled form of the "Pass 1 of 2" progress marker.
    body = "## Two-pass review: Pass one of two\nnotes only, review incomplete.\n"
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "no pass-equivalent" in reason


def test_shipping_word_not_read_as_ship_in_fallback():
    # Whole-word token gate: a body line "Shipping the release next week" must not
    # be read as a SHIP verdict.
    body = "## Two-pass review\nShipping the release is planned for next week.\n"
    assert _q(body) is False


def test_retraction_line_under_pass_heading_holds():
    # finding 2a: a PASS heading followed by a later bare FAIL line (a retraction)
    # must HOLD — blocking anywhere in the section wins over the heading verdict.
    body = ("## Two-pass review verdict: PASS\nSome notes.\n"
            "FAIL — regression found on rerun, retracting the pass.\n")
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


def test_fail_carried_on_pass_n_structure_line_blocks():
    # finding 2b/3: the KEY asymmetry — a genuine FAIL carried right after a
    # "Pass 2 (adversarial):" prefix must still block (prefix is stripped, the
    # remainder "FAIL — …" is classified), even with an innocuous later line.
    body = ("## Two-pass review\n"
            "**Pass 2 (adversarial):** FAIL — found an auth bypass, do not merge.\n"
            "Ship notes: rollout is staged behind a flag.\n")
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


def test_pass_n_narrative_prose_never_blocks_but_real_verdict_qualifies():
    # The #3395 shape distilled: a Pass-1 narrative line whose PROSE contains
    # "xfail" (a "fail" substring) must NOT block; a real PASS line then qualifies.
    body = ("## Two-pass review\n"
            "**Pass 1 (self-review):** exercised every check; one case is "
            "encoded as `xfail(strict=True)`.\n"
            "PASS — no blockers on either pass.\n")
    ok, verdict, reason = mvc.verdict_qualifies(body)
    assert ok is True
    assert verdict.startswith("PASS")


def test_default_repo_is_derived_not_hardcoded():
    # finding 4: DEFAULT_REPO must come from meta_config.repo_slug() (per-project),
    # not a literal — else a synced consuming repo's gate fetches evolve PRs.
    import meta_config
    assert mvc.DEFAULT_REPO == meta_config.repo_slug()


# ── Attribution (2026-08-19) — META:apps D-H's enforcement half ──────────────
#
# The bar: body text cannot PROVE who wrote a review, but it can refuse to let
# the question go unanswered. These pin (a) that the default behaviour is
# UNCHANGED — the tool is synced into consuming projects, so a default-on flag
# would retroactively disqualify every in-flight PR everywhere — and (b) that
# the opt-in flags actually bite.

_PASS_NO_ATTRIB = """## Independent two-pass review

Verdict: PASS — no blockers.

Pass 1 (self-review): checked the guardrails.
Pass 2 (adversarial): looked for real failure modes.
"""

def _with_attrib(value):
    return _PASS_NO_ATTRIB.replace(
        "Verdict: PASS", "Reviewer: %s\n\nVerdict: PASS" % value)


def test_default_behaviour_is_unchanged_by_the_attribution_work() -> None:
    """The keystone. This tool is synced into consuming projects via
    meta-substrate-sync, so if attribution were enforced by default it would
    disqualify every in-flight PR in every aspect AND every consumer at once."""
    assert _q(_PASS_NO_ATTRIB) is True
    assert mvc.verdict_qualifies(_PASS_NO_ATTRIB)[0] is True


def test_require_attribution_rejects_a_section_that_states_none() -> None:
    """The mutation check: the same body that passes by default must FAIL under
    the flag, or the flag is decoration."""
    ok, verdict, reason = mvc.verdict_qualifies(
        _PASS_NO_ATTRIB, require_attribution=True)
    assert ok is False
    assert verdict is not None, "the verdict is still reported, only the gate changed"
    assert "no attribution" in reason


def test_require_attribution_accepts_a_stated_reviewer() -> None:
    ok, _, _ = mvc.verdict_qualifies(
        _with_attrib("chip session al-1-6-review (dispatched)"),
        require_attribution=True)
    assert ok is True


@pytest.mark.parametrize("value", [
    "author-run adversarial pass, not a separately-dispatched reviewer",
    "self-review by the implementing agent",
    "the author",
    "same session that wrote the code",
    "myself",
])
def test_require_independent_rejects_an_author_run_attribution(value) -> None:
    """AL-1.5's three PRs all said this honestly. Under D-H that honesty is
    still correct — it just no longer qualifies."""
    ok, _, reason = mvc.verdict_qualifies(
        _with_attrib(value), require_independent=True)
    assert ok is False
    assert "author" in reason.lower()


def test_require_independent_accepts_a_genuinely_other_reviewer() -> None:
    ok, _, _ = mvc.verdict_qualifies(
        _with_attrib("review chip task_9f21ab, dispatched from the apps coordinator"),
        require_independent=True)
    assert ok is True


def test_require_independent_implies_require_attribution() -> None:
    """Otherwise the stronger flag would be WEAKER than the weaker one on a body
    that states nothing at all — the classic gate inversion."""
    ok, _, reason = mvc.verdict_qualifies(
        _PASS_NO_ATTRIB, require_independent=True)
    assert ok is False
    assert "no attribution" in reason


def test_a_blocking_verdict_still_wins_over_a_missing_attribution() -> None:
    """Ordering is load-bearing. A reviewer who said FAIL has told you something
    more urgent than a formatting gap; reporting the gap instead would bury it."""
    body = "## Two-pass review\n\nVerdict: FAIL — auth bypass in the new path.\n"
    ok, verdict, reason = mvc.verdict_qualifies(body, require_independent=True)
    assert ok is False
    assert "blocking" in reason
    assert "FAIL" in (verdict or "")


def test_attribution_inside_a_fence_does_not_count() -> None:
    """Same fence-awareness as every other read here — a template shown as an
    example must not satisfy the bar for the PR that documents it."""
    body = _PASS_NO_ATTRIB.replace(
        "Verdict: PASS", "```\nReviewer: someone else\n```\n\nVerdict: PASS")
    assert mvc.extract_attribution(mvc.extract_review_sections(body)[0]) is None
    assert mvc.verdict_qualifies(body, require_attribution=True)[0] is False


def test_the_first_attribution_wins() -> None:
    """A section that names its reviewer up top must not have a later prose
    mention overrule it."""
    body = _PASS_NO_ATTRIB.replace(
        "Verdict: PASS",
        "Reviewer: dispatched review chip\n\nVerdict: PASS")
    body += "\nReviewed by: CI, incidentally.\n"
    assert mvc.extract_attribution(
        mvc.extract_review_sections(body)[0]) == "dispatched review chip"


@pytest.mark.parametrize("label", [
    "Attribution", "Reviewer", "Reviewed by", "Reviewed-by", "Review by",
])
def test_every_documented_attribution_label_is_accepted(label) -> None:
    body = _PASS_NO_ATTRIB.replace(
        "Verdict: PASS", "%s: another session\n\nVerdict: PASS" % label)
    assert mvc.verdict_qualifies(body, require_independent=True)[0] is True


def test_an_unstated_attribution_is_not_independent() -> None:
    """Fail-safe: absent is never independent. A false hold is recoverable; a
    false independent is the thing the bar exists to stop."""
    assert mvc.attribution_is_independent(None) is False
    assert mvc.attribution_is_independent("") is False


# ── soft-wrap continuations (2026-08-31; the live #3813 / #3809 false holds) ─
#
# The PM review corpus is hand-wrapped markdown at ~90 columns and
# `meta-dispatch-eligible --pm-verdict` feeds a WHOLE review file through
# `extract_verdict`, so every prose line in it is a fallback-tier candidate. On
# 2026-08-27 that held #3813 — a unanimously-passed PR — on the CONTINUATION of
# "Its guardrails correctly encode the / fail-toward-doing gate and the
# spy-not-raising-sentinel rule.", a clause PRAISING the work.
#
# The guard drops wrap continuations from the FALLBACK tier only. The tests
# below pin BOTH directions: the wrap no longer blocks, and every shape in which
# a reviewer actually writes a verdict — set off by a separator, bare on its own
# line, emphasised, bulleted, or carried on a `Pass N:` structure line — still
# blocks even when the line above it did not end in punctuation.


def test_wrapped_prose_continuation_does_not_block():
    # The #3813 shape, distilled: line 2 is the wrap of line 1's sentence.
    body = ("## Independent two-pass review\n"
            "Verdict: PASS — 2 non-blocking findings.\n"
            "Its guardrails correctly encode the\n"
            "fail-toward-doing gate and the spy-not-raising-sentinel rule.\n")
    ok, verdict, _reason = mvc.verdict_qualifies(body)
    assert ok is True
    assert verdict.startswith("PASS")


def test_wrapped_do_not_merge_citation_does_not_block():
    # #3809: a wrapped line whose first token is the `do not merge` MARKER being
    # quoted — the review is asserting no such line exists.
    body = ("## Independent two-pass review\n"
            "Verdict: PASS — the reflow landed.\n"
            "Scanned every line against the verdict-token set — no line begins with\n"
            "`do not merge`, so the false hold is gone.\n")
    assert _q(body) is True


def test_wrapped_pass_continuation_does_not_merge_either():
    # The guard is symmetric: a wrap that happens to open with a PASS token is
    # not a verdict either, so a section with no real verdict stays unverified.
    body = ("## Independent two-pass review\n"
            "The retry budget is spent on the first attempt, so the second and third\n"
            "pass straight through to the fallback without ever retrying.\n")
    ok, verdict, _reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert verdict is None


def test_a_set_off_verdict_after_an_unterminated_line_still_blocks():
    # THE safety valve. The line above does not end in punctuation, so the wrap
    # test alone would drop this — but the reviewer set the verdict off with a
    # separator, so it is a statement and it holds the PR.
    for tail in ("— the rollback path is unproven.",
                 "- the rollback path is unproven.",
                 ": the rollback path is unproven.",
                 "– the rollback path is unproven.",
                 ", the rollback path is unproven.",
                 "; the rollback path is unproven.",
                 "(the rollback path is unproven)."):
        body = ("## Two-pass review verdict: PASS\n"
                "Reviewed the diff and the tests, and the one thing I could not\n"
                "FAIL %s\n" % tail)
        ok, _verdict, reason = mvc.verdict_qualifies(body)
        assert ok is False, tail
        assert "blocking" in reason, tail


def test_a_code_quoted_verdict_still_blocks_but_a_quoted_CITATION_does_not():
    # The backtick discriminator: `FAIL` — … is a verdict a reviewer typed in
    # code style; `do not merge`, so … (comma hard against the closing backtick)
    # is #3809's CITATION of the marker inside a wrapped sentence.
    held = ("## Two-pass review verdict: PASS\n"
            "Re-ran the suite and the one case I could not clear is the migration\n"
            "`FAIL` — the rollback path is unproven.\n")
    ok, _verdict, reason = mvc.verdict_qualifies(held)
    assert ok is False and "blocking" in reason

    cited = ("## Two-pass review verdict: PASS\n"
             "Scanned every line against the verdict-token set — no line begins with\n"
             "`do not merge`, so the false hold is gone.\n")
    assert _q(cited) is True


def test_a_bare_verdict_token_line_after_an_unterminated_line_still_blocks():
    body = ("## Two-pass review verdict: PASS\n"
            "Re-read the migration and could not convince myself it is reversible\n"
            "FAIL\n")
    ok, _verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


def test_an_emphasised_or_bulleted_verdict_after_an_unterminated_line_still_blocks():
    # An emphasised or list-item opener starts a new BLOCK, so it is never a
    # continuation whatever preceded it. (A NUMBERED list item is out of scope
    # here and always has been: `_clean_verdict` strips no digits, so "1. FAIL"
    # never opened with a verdict token on any tier — unchanged by this guard.)
    for opener in ("**FAIL** — auth bypass found.",
                   "- FAIL — auth bypass found.",
                   "* FAIL — auth bypass found.",
                   "> FAIL — auth bypass found."):
        body = ("## Two-pass review verdict: PASS\n"
                "Walked the whole call chain and the one case I could not clear\n"
                + opener + "\n")
        ok, _verdict, reason = mvc.verdict_qualifies(body)
        assert ok is False, opener
        assert "blocking" in reason, opener


def test_a_pass_n_structure_verdict_after_an_unterminated_line_still_blocks():
    # A reviewer-structure line opens its own block by construction.
    body = ("## Two-pass review verdict: PASS\n"
            "Pass 1 (self-review): clean, one xfail(strict=True) case documented\n"
            "Pass 2 (adversarial): FAIL — found an auth bypass, do not merge.\n")
    ok, _verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


def test_a_verdict_after_a_TERMINATED_sentence_is_read_through_markdown_noise():
    # The terminal character is read past trailing markdown/quote noise, so all
    # of these end the sentence above and leave the FAIL line a candidate.
    for tail in ("documented.", "documented.**", "documented.)", "`documented`.",
                 "documented!", "documented?", "as follows:", "| col |"):
        body = ("## Two-pass review verdict: PASS\n"
                "Pass 1 notes " + tail + "\n"
                "FAIL, the rollback path is unproven.\n")
        ok, _verdict, reason = mvc.verdict_qualifies(body)
        assert ok is False, tail
        assert "blocking" in reason, tail


def test_a_verdict_on_the_first_line_of_a_section_is_never_a_continuation():
    # There is no preceding line to continue — the heading is not prose.
    assert _q("## Two-pass review\nFAIL — regression found.\n") is False
    assert _q("## Two-pass review\nLGTM, no blockers found on a second read.\n") is True


def test_a_verdict_after_a_blank_line_is_never_a_continuation():
    body = ("## Two-pass review verdict: PASS\n"
            "Some notes that trail off without punctuation\n"
            "\n"
            "FAIL, retracting the pass after a rerun.\n")
    ok, _verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


def test_a_verdict_after_a_fenced_block_is_never_a_continuation():
    body = ("## Two-pass review verdict: PASS\n"
            "```\n"
            "$ pytest -q   # trailing shell line with no punctuation\n"
            "```\n"
            "FAIL, the suite is red on main.\n")
    ok, _verdict, reason = mvc.verdict_qualifies(body)
    assert ok is False
    assert "blocking" in reason


# ── the real corpus files the guard was built from ───────────────────────────
#
# Synthesised strings prove the rule; these two prove it on the actual
# hand-wrapped prose that produced the incident. `--pm-verdict` prepends a
# synthetic heading and hands the WHOLE file to `extract_verdict`, so that is
# how they are read here.

_REPO = Path(__file__).resolve().parents[3]
_SYNTHETIC_HEADING = "## PM review\n"


@pytest.mark.parametrize("pr", [3813, 3809])
def test_the_real_pm_review_files_extract_their_stated_pass(pr):
    path = _REPO / "internal" / "dispatch" / "reviews" / ("pr-%d.md" % pr)
    if not path.is_file():                  # synced consumer checkout: nothing to pin
        pytest.skip("%s not present in this checkout" % path)
    verdict = mvc.extract_verdict(_SYNTHETIC_HEADING + path.read_text(encoding="utf-8"))
    assert verdict is not None
    assert mvc.verdict_is_blocking(verdict) is False, verdict
    assert mvc.verdict_is_pass(verdict) is True, verdict


# ── heading tier: "— verdict: PASS" (separator THEN label) ───────────────────
#
# `_VERDICT_LABEL_RE`'s leading class does not cross an em dash, so
# `## Independent two-pass review — verdict: PASS` matched neither heading form
# and the whole heading yielded None — an unverified HOLD on a stated PASS. Six
# of the last 400 PR bodies use exactly that heading. The unwrapped value goes
# through the SAME guards, which the last two cases pin.


def test_heading_separator_then_label_verdict_qualifies():
    for h in ("## Independent two-pass review — verdict: PASS",
              "## Two-pass review – Verdict: SHIP",
              "## Two-pass review: overall: APPROVED"):
        ok, verdict, _reason = mvc.verdict_qualifies(h + "\nnotes.\n")
        assert ok is True, h
        assert verdict is not None, h


def test_heading_separator_then_label_blocking_verdict_holds():
    ok, _verdict, reason = mvc.verdict_qualifies(
        "## Independent two-pass review — verdict: FAIL, the migration is unsafe\n"
        "notes.\n")
    assert ok is False
    assert "blocking" in reason


def test_heading_separator_then_label_keeps_the_whole_word_and_index_guards():
    for h in ("## Independent two-pass review — verdict: pending independent review",
              "## Independent two-pass review — verdict: Passing this to the operator",
              "## Independent two-pass review — verdict: Pass 1 of 2"):
        ok, verdict, _reason = mvc.verdict_qualifies(h + "\nnotes.\n")
        assert ok is False, h
        assert verdict is None, h


# ── D-PM7: the `## Hold` section, and CONCERNS vs a rejection ────────────────
#
# The contract is stated once, in internal/dispatch/README.md: a CONCERNS verdict
# carries its COMPLETE remedy under `## Hold`, and anything not under it is
# non-blocking. The reconciler dispatches one bounded fix-forward chip whose whole
# prompt is that section, so what these pin is what a chip would be handed.


def test_a_bare_hold_heading_is_read():
    present, hold, _reason = mvc.hold_qualifies(
        "## Two-pass review\nVerdict: CONCERNS — see the hold.\n\n"
        "## Hold\nH1. `foo.py:12` — count the stubs.\n")
    assert present is True
    assert "H1. `foo.py:12`" in hold


def test_a_parenthesised_hold_heading_is_read():
    # The pr-3976 shape: `## Hold (anti-vacuity — same bar as #3959)`.
    present, hold, _reason = mvc.hold_qualifies(
        "## Hold (anti-vacuity — same bar as #3959)\nH1. `a.ts:9` — restate it.\n")
    assert present is True and "H1." in hold


def test_every_hold_section_is_concatenated_not_just_the_first():
    """pr-3948 wrote one section per item. Taking the first would hand the chip
    half the remedy — the failure that reads as success, because the PR comes back
    still held on an item nobody was asked to fix."""
    present, hold, reason = mvc.hold_qualifies(
        "## Hold item 1 — a re-minted link is refused\nFix `board_auth.py:127`.\n\n"
        "## Hold item 2 — the done/ entry wedges the lane\nRebase and `git rm`.\n")
    assert present is True
    assert "board_auth.py:127" in hold and "git rm" in hold
    assert "2 hold section" in reason


def test_a_section_ends_at_the_next_same_level_heading():
    present, hold, _reason = mvc.hold_qualifies(
        "## Hold\nH1. fix `a.py:1`.\n\n## Findings (non-blocking)\nF1. a nicety.\n")
    assert present is True
    assert "H1. fix `a.py:1`." in hold
    assert "a nicety" not in hold, "non-blocking findings must not reach the chip"


def test_a_deeper_subheading_stays_inside_the_hold():
    present, hold, _reason = mvc.hold_qualifies(
        "## Hold\nH1. fix it.\n\n### H1 detail\n`a.py:1` — the arithmetic.\n")
    assert present is True and "the arithmetic" in hold


def test_no_hold_section_is_the_malformed_case():
    present, hold, reason = mvc.hold_qualifies(
        "## Two-pass review\nVerdict: CONCERNS — one number is overstated.\n\n"
        "## Findings\n1. **BLOCKS** — `demo.py:147` overstates the saving.\n")
    assert present is False and hold is None
    assert "no `## Hold` section" in reason


def test_an_empty_hold_heading_is_not_a_remedy():
    present, hold, reason = mvc.hold_qualifies("## Hold\n\n## Findings\n1. a nicety.\n")
    assert present is False and hold is None
    assert "empty" in reason


@pytest.mark.parametrize("heading", ["## Holdover notes", "## What the PM held back",
                                     "## Withholding merge"])
def test_a_heading_that_merely_mentions_holding_is_not_the_hold(heading):
    present, _hold, _reason = mvc.hold_qualifies(heading + "\nsome prose.\n")
    assert present is False, heading


def test_a_fenced_hold_heading_is_an_example_not_a_hold():
    present, _hold, _reason = mvc.hold_qualifies(
        "Write it like this:\n\n```markdown\n## Hold\nH1. fix `a.py:1`.\n```\n")
    assert present is False


def test_hold_and_verdict_are_read_from_the_same_body_independently():
    """A PASS review has no hold, and the two reads do not interfere."""
    body = "## Two-pass review\nVerdict: PASS — brief followed.\n\n## Findings\n1. none\n"
    assert mvc.verdict_qualifies(body)[0] is True
    assert mvc.hold_qualifies(body)[0] is False


# ── verdict_is_concerns: a held-with-remedy verdict vs a rejection ───────────


@pytest.mark.parametrize("verdict", [
    "CONCERNS — two numbers are overstated; correct them and this is a PASS.",
    "CONCERNS — one lane-bookkeeping defect must be fixed before merge (H1).",
    "HOLD — one blocker at `a.py:1`.",
])
def test_a_concerns_verdict_is_blocking_and_concerns(verdict):
    assert mvc.verdict_is_blocking(verdict) is True, verdict
    assert mvc.verdict_is_concerns(verdict) is True, verdict
    assert mvc.verdict_is_pass(verdict) is False, verdict


@pytest.mark.parametrize("verdict", [
    "FAIL — the migration is unsafe.",
    "DO NOT MERGE — this needs a different design.",
    "REQUEST CHANGES — start over on the auth path.",
    "FAIL — concerns about the whole approach.",
])
def test_a_rejection_is_blocking_but_never_concerns(verdict):
    """The D-PM7 split. A rejection is a human's judgment about the shape of the
    change, so it must never license a fix-forward chip — even when it also uses
    the word "concerns"."""
    assert mvc.verdict_is_blocking(verdict) is True, verdict
    assert mvc.verdict_is_concerns(verdict) is False, verdict


@pytest.mark.parametrize("verdict", [
    "PASS — brief followed.", "SHIP (2 non-blocking concerns) — no blocker.",
    "pending", "", None,
])
def test_a_non_blocking_verdict_is_never_concerns(verdict):
    """`verdict_is_concerns` is a SUBSET of `verdict_is_blocking` by construction:
    it can only ever narrow an existing hold, never create one."""
    assert mvc.verdict_is_concerns(verdict) is False, verdict
    if not mvc.verdict_is_blocking(verdict):
        assert mvc.verdict_is_concerns(verdict) is False, verdict


# ── the real corpus: what `--hold` answers on this week's review files ───────


@pytest.mark.parametrize("pr,expect_hold", [
    (3964, True),    # `## Hold` — H1, the lane conflict, with the remedy
    (3976, True),    # `## Hold (anti-vacuity — …)` — H1 + H2
    (3948, True),    # one section per item: `## Hold item 1` / `## Hold item 2`
    (3959, False),   # CONCERNS whose blocking item sits under `## Findings`
    (3813, False),   # a PASS review — no hold to find
])
def test_the_real_review_corpus(pr, expect_hold):
    path = _REPO / "internal" / "dispatch" / "reviews" / ("pr-%d.md" % pr)
    if not path.is_file():                  # synced consumer checkout: nothing to pin
        pytest.skip("%s not present in this checkout" % path)
    present, hold, _reason = mvc.hold_qualifies(path.read_text(encoding="utf-8"))
    assert present is expect_hold, path
    assert (hold is not None) is expect_hold


# ── CLI: --hold ──────────────────────────────────────────────────────────────


def test_cli_hold_exit_codes_and_output(tmp_path, capsys):
    good = tmp_path / "pr-1.md"
    good.write_text("## Hold\nH1. fix `a.py:1`.\n", encoding="utf-8")
    assert mvc.main(["--hold", str(good)]) == 0
    assert "H1. fix `a.py:1`." in capsys.readouterr().out

    bare = tmp_path / "pr-2.md"
    bare.write_text("## Findings\n1. a nicety.\n", encoding="utf-8")
    assert mvc.main(["--hold", str(bare)]) == 1
    assert "no `## Hold` section" in capsys.readouterr().err


def test_cli_hold_json(tmp_path, capsys):
    path = tmp_path / "pr-1.md"
    path.write_text("## Hold\nH1. fix `a.py:1`.\n", encoding="utf-8")
    assert mvc.main(["--hold", str(path), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hold_present"] is True and "H1." in out["hold"]


def test_cli_hold_refuses_a_pr_number_and_an_unreadable_file(tmp_path):
    with pytest.raises(SystemExit) as e:
        mvc.main(["3771", "--hold", str(tmp_path / "x.md")])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        mvc.main(["--hold", str(tmp_path / "absent.md")])
    assert e.value.code == 2


def test_cli_still_requires_a_pr_without_hold():
    with pytest.raises(SystemExit) as e:
        mvc.main([])
    assert e.value.code == 2
