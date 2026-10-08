"""Regression test for the 2026-05-31 Maintenance "Backed up" column bug.

Companion to ``test_backup_status_summary_honors_attempt.py`` (PR #1842).
That fix taught Backup → Status to distinguish ``last_attempt_status ===
'skipped'`` (blocked-by-config) from ``'failed'`` (real failure). The
Maintenance → System tab's BACKED UP column was a parallel surface and
kept the old logic — both attempt outcomes rendered as red "✗ failed".

Effect on the operator: Backup tab said "1 fresh / 5 blocked / 2 failing",
Maintenance tab said "7/8 ✗ failed". Two surfaces of the same data
disagreed about whether the daemon was healthy.

This pins the new invariants into the source so a future edit can't
silently revert the alignment.

Pins:
  1. The Maintenance backup cell considers ``last_attempt_status``.
  2. A ``skipped`` branch renders "⊘ blocked" with the yellow / warn
     colour (NOT red).
  3. A ``failed`` branch renders "✗ failed" with the red colour.
  4. The gating comparison uses ``lastAttempt > lastSuccess`` so a
     newer skipped attempt supersedes an older success.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_INDEX_HTML = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "index.html"
_BACKUP_JS = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "static" / "js" / "pages" / "backup.js"
_TEXT = _INDEX_HTML.read_text() + "\n" + _BACKUP_JS.read_text()


def _extract_function(name: str) -> str:
    """Slice out one async function body from index.html for regex tests.

    Naive but adequate — function bodies in this file are not nested,
    and ``loadSysm`` is unique by name. Same shape as the helper in
    test_backup_status_summary_honors_attempt.
    """
    start = _TEXT.find(f"async function {name}(")
    if start < 0:
        start = _TEXT.find(f"function {name}(")
    assert start >= 0, f"function {name} not found in index.html"
    # Walk braces from the first `{` to find the matching close.
    open_brace = _TEXT.find("{", start)
    depth = 0
    for i in range(open_brace, len(_TEXT)):
        c = _TEXT[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return _TEXT[start : i + 1]
    raise AssertionError(f"could not find matching close brace for {name}")


@pytest.fixture(scope="module")
def fn() -> str:
    return _extract_function("loadSysm")


def test_backup_cell_considers_last_attempt_status(fn: str) -> None:
    """The cell must branch on ``last_attempt_status`` — pre-fix it
    only knew about 'failed' (collapsing 'skipped' into 'failed')."""
    assert "last_attempt_status" in fn, (
        "Maintenance backup cell must consider last_attempt_status "
        "to distinguish skipped (blocked-by-config) from failed (real "
        "failure). Without it, 5 blocked bots render as red ✗ failed."
    )
    # Both branches must be present.
    assert "'failed'" in fn or '"failed"' in fn, (
        "expected an explicit `last_attempt_status === 'failed'` branch"
    )
    assert "'skipped'" in fn or '"skipped"' in fn, (
        "expected an explicit `last_attempt_status === 'skipped'` branch "
        "— without it skipped attempts render as ✗ failed"
    )


def test_blocked_branch_uses_warn_not_error_color(fn: str) -> None:
    """The skipped/blocked branch must render in yellow (warn), not red
    (error). A config gap is actionable but not a system failure — it
    shouldn't draw the same eye as a daemon crash.

    The Maintenance cell uses --yellow (the var token); Backup → Status
    uses #ffa502 directly. Either is fine here — pin that the blocked
    span's style line contains a warn token, not the red token.
    """
    # Locate the skipped branch by anchoring on the string literal it
    # tests against, then grab a window of text immediately after.
    m = re.search(r"last_attempt_status === ['\"]skipped['\"]", fn)
    assert m, "couldn't find the skipped branch — see test docstring"
    window = fn[m.end() : m.end() + 600]
    # Must mention a warn colour token.
    assert "var(--yellow)" in window or "#ffa502" in window, (
        "skipped/blocked branch must use a yellow/warn colour. "
        "Found neither `var(--yellow)` nor `#ffa502` in the 600 chars "
        "after the skipped check — likely rendered red, conflating "
        "config-gap with system failure."
    )
    # Must NOT use red in this branch (would be the bug we're guarding).
    assert "var(--red)" not in window and "#ff4757" not in window, (
        "skipped/blocked branch must NOT use a red colour — that's the "
        "exact bug this test is guarding against (skipped → ✗ failed)"
    )


def test_blocked_label_carries_blocked_word(fn: str) -> None:
    """The skipped branch's user-facing label must say 'blocked' so the
    Maintenance tab and Backup tab use the same vocabulary.
    """
    m = re.search(r"last_attempt_status === ['\"]skipped['\"]", fn)
    assert m
    window = fn[m.end() : m.end() + 600]
    assert "blocked" in window.lower(), (
        "expected the word 'blocked' in the skipped branch label — "
        "matches the chip label on Backup → Status"
    )


def test_attempt_supersedes_success_when_newer(fn: str) -> None:
    """The skipped/failed branches must gate on ``lastAttempt >
    lastSuccess`` (or equivalent) so a newer skipped attempt
    supersedes an older success.  Without this, an old success would
    keep rendering as ✓ green even after the daemon starts skipping.
    """
    assert re.search(r"bvLastAttempt\s*>\s*bvLastSuccess", fn) or \
        re.search(r"lastAttempt\s*>\s*lastSuccess", fn), (
            "expected `bvLastAttempt > bvLastSuccess` (or equivalent) "
            "comparison gating the authoritative-attempt branch — "
            "without it, a fresh skipped attempt stays hidden behind "
            "the previous success timestamp."
        )
