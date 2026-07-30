"""Unit tests for tools/meta-verdict-check.

`tools/meta-verdict-check` is the B2 fact-gate (META:substrate Initiative 12,
docs/spec-substrate-2026-06-15.md §17): it answers whether a PR carries an
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
import sys
from pathlib import Path

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
