"""Regression: our own POD_CONDUCT.md must scan clean against the default catalog.

The April 2026 scanner caught HTML comments with markers outside the
allowlist. Initially POD_CONDUCT.md used `<!-- summary-start -->` /
`<!-- summary-end -->` while the allowlist contained
`<!-- evolve-pod-conduct:begin/end -->` — a meta-bug where our own
pod-wide injection file failed our own scan with a "warn" severity. This
test guards against any future drift between the file we ship and the
allowlist we ship.

Spec: internal/spec-prompt-injection-scanner-2026-05-10.md
"""
from __future__ import annotations

from pathlib import Path

from content_scan.default_patterns import default_catalog
from content_scan.patterns import scan_file


REPO_ROOT = Path(__file__).resolve().parents[3]
POD_CONDUCT = REPO_ROOT / "docs" / "system" / "POD_CONDUCT.md"


def test_pod_conduct_scans_clean_against_default_catalog():
    """POD_CONDUCT.md must produce zero matches against the shipped catalog."""
    assert POD_CONDUCT.exists(), f"missing source file: {POD_CONDUCT}"
    text = POD_CONDUCT.read_text()
    catalog = default_catalog()
    matches = scan_file(
        text=text,
        filename="POD_CONDUCT.md",
        patterns=catalog.deny_patterns,
        evolve_markers_allowlist=catalog.evolve_markers_allowlist,
    )
    # If this fails, the failure message includes which pattern fired so an
    # operator can either fix POD_CONDUCT.md or extend the allowlist.
    fired = [(m.pattern_id, m.line, m.excerpt[:80] if m.excerpt else "") for m in matches]
    assert not matches, (
        f"POD_CONDUCT.md triggered {len(matches)} content-scan match(es) — our own "
        f"injected file fails our own scan. Patterns that fired:\n  "
        + "\n  ".join(f"{pid} at line {ln}: {snip!r}" for pid, ln, snip in fired)
    )


def test_pod_conduct_summary_markers_are_in_allowlist():
    """The markers session_surface.py reads must match the scan allowlist.

    session_surface._SUMMARY_BEGIN / _SUMMARY_END drive the conduct-injection
    text; if these drift from the content-scan allowlist, POD_CONDUCT.md will
    fail its own scan (the meta-bug this test guards against).
    """
    from session_surface import _SUMMARY_BEGIN, _SUMMARY_END
    catalog = default_catalog()
    allowlist = catalog.evolve_markers_allowlist
    assert _SUMMARY_BEGIN in allowlist, (
        f"session_surface marker {_SUMMARY_BEGIN!r} not in content-scan allowlist; "
        "POD_CONDUCT.md will warn on every scan"
    )
    assert _SUMMARY_END in allowlist, (
        f"session_surface marker {_SUMMARY_END!r} not in content-scan allowlist; "
        "POD_CONDUCT.md will warn on every scan"
    )


# ── Rule 14 / §15 — group-chat silence ───────────────────────────────────────
# Added after the 2026-08-14 incident (internal/design-model-swap-behavior-guard-
# 2026-08-19.md): a model swap left a bot narrating its silence into four
# requireMention:false Slack channels. The etiquette lived only in each bot's
# hand-written AGENTS.md, inconsistently, and in the affected bot's case
# contradicted by its own "always respond" protocol. POD_CONDUCT is the
# pod-wide standard that outranks bot-local config, so the rule belongs here.
#
# The summary block is what actually reaches the model — a rule that exists
# only in the prose below it is not injected and does not fire.


def _summary_block() -> str:
    from session_surface import _SUMMARY_BEGIN, _SUMMARY_END

    text = POD_CONDUCT.read_text()
    start = text.index(_SUMMARY_BEGIN) + len(_SUMMARY_BEGIN)
    return text[start:text.index(_SUMMARY_END)]


def test_group_chat_silence_rule_is_in_the_injected_summary():
    """Not just in the prose — the summary block is the injected surface."""
    summary = _summary_block()
    assert "Group-chat silence" in summary
    assert "NO_REPLY" in summary, "the rule must name the sentinel the runtime honors"


def test_summary_rule_names_the_explaining_failure_mode():
    """Naming only 'stay silent' was never the gap — every bot's AGENTS.md
    said that. The failure is emitting prose ABOUT staying silent, which the
    runtime strips the token out of and posts."""
    summary = _summary_block()
    assert "IS a reply" in summary


def test_summary_rule_covers_speculative_tool_calls():
    """Second observed failure mode: a failed exec posts its own visible
    failure notice, so silence has to cover tool calls, not just text."""
    assert "tool call" in _summary_block()


def test_summary_block_carries_no_per_bot_or_per_turn_templating():
    """The block is a prompt-cache prefix (PR #3508 / prefixHashLedger). Any
    templated value would churn the prefix on every turn and every bot."""
    summary = _summary_block()
    for marker in ("{", "}", "%s", "$("):
        assert marker not in summary, (
            f"summary block contains {marker!r} — it must be a static string"
        )


def test_full_prose_section_exists_for_the_rule():
    """House style: terse numbered line in the summary, fuller section below."""
    text = POD_CONDUCT.read_text()
    assert "## 15. Group-Chat Silence" in text
    body = text.split("## 15. Group-Chat Silence", 1)[1]
    # The prose must carry the mechanical detail the one-liner can't: why
    # appending the token to prose does not suppress the message.
    assert "strips the token" in body
    assert "requireMention" in body, "name the config that makes this load-bearing"
