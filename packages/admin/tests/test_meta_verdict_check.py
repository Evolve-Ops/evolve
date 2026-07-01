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
