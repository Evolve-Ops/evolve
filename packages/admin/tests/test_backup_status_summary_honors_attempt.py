"""Regression test for the 2026-05-30 Backup Status page silent-failure bug.

The Backup → Status page rendered all 8 bots as "✓ Nh ago / 8 fresh"
while six were `skipped` (PAT missing) and two were `failed` with .git
permission errors. ``loadBackupStatusSummary`` was bucketing purely off
the [backup] commit recency from `git log`, ignoring the
``last_attempt_status`` / ``last_error`` / ``consecutive_failures``
fields that the backend already grafts in via ``_graft_run_state``.

This is a structural regression guard against the function silently
reverting to the old logic. We can't easily run the JS in a unit
test, but we can pin the key invariants into the source so a future
edit that drops them trips this test rather than re-shipping the bug.

Pins:
  1. The bucket loop must consider ``last_attempt_status``.
  2. A "blocked (config)" bucket exists for skipped runs.
  3. The failing threshold is 1, not 3 (a single failure must be visible).
  4. When ``last_attempt_at > last_success_at``, the status cell shows
     the attempt outcome, not the obsolete success timestamp.
  5. The header chip set distinguishes blocked (orange) from failing (red).
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

    Naive but adequate: function bodies in this file are not nested,
    and ``loadBackupStatusSummary`` is unique by name.
    """
    start = _TEXT.find(f"async function {name}(")
    if start < 0:
        start = _TEXT.find(f"function {name}(")
    assert start >= 0, f"function {name} not found in index.html"
    # Find the matching closing brace by tracking depth from the first {.
    body_start = _TEXT.find("{", start)
    assert body_start >= 0
    depth = 0
    i = body_start
    while i < len(_TEXT):
        c = _TEXT[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return _TEXT[start:i + 1]
        i += 1
    raise AssertionError("unbalanced braces in extracted function")


@pytest.fixture(scope="module")
def fn() -> str:
    return _extract_function("loadBackupStatusSummary")


def test_bucket_logic_considers_last_attempt_status(fn: str):
    """The renderer must look at ``last_attempt_status`` somewhere in
    its bucketing. Pre-fix it ignored this field entirely.
    """
    assert "last_attempt_status" in fn, (
        "Backup Status page must consider last_attempt_status when "
        "bucketing — otherwise skipped/failed bots render as ✓ fresh "
        "(the 2026-05-30 wedge that hid 8/8 broken backups)"
    )


def test_blocked_bucket_exists(fn: str):
    """A ``blocked`` (skipped-but-recent) bucket must exist with a
    distinct chip label so skipped-by-config bots stop being counted
    as fresh.
    """
    assert "'blocked'" in fn or '"blocked"' in fn, (
        "Backup Status page must have a 'blocked' bucket for "
        "last_attempt_status === 'skipped' attempts. Without it, "
        "skipped bots silently count as fresh."
    )
    # The chip label must say "blocked (config)" so the operator
    # immediately knows it's a setup gap rather than a flake.
    assert "blocked (config)" in fn, (
        "expected operator-facing chip label 'blocked (config)' — "
        "without the parenthetical, the chip looks like a transient "
        "failure rather than a setup gap"
    )


def test_failing_threshold_is_one_not_three(fn: str):
    """The "✗ N× in a row" cell must surface a single failure. The
    pre-fix code only showed red at ``>= 3``, so the team_bot_c/security_bot
    consecutive_failures=2 case rendered gray ("2 recent") and stayed
    out of the failing rollup. Pin the new threshold so a future edit
    can't silently push it back to 3.
    """
    # Permissive regex: look for `failCount >= 1` somewhere in the
    # function. The actual line in source is `failCount >= 1` with a
    # space around the operator.
    assert re.search(r"failCount\s*>=\s*1", fn), (
        "expected `failCount >= 1` in the recent-failures cell. "
        "Pre-fix threshold was >= 3 which silenced team_bot_c + security_bot's "
        "2-in-a-row failures on 2026-05-30."
    )


def test_attempt_supersedes_old_success_in_status_cell(fn: str):
    """When the last attempt is newer than the last success AND it
    failed or was skipped, the Last-backup cell must render the
    attempt outcome (✗ failed Nh ago / ⊘ blocked Nh ago), not the
    obsolete ✓ success time.

    Pin the load-bearing comparison so a refactor can't silently lose it.
    """
    assert "lastAttempt > lastSuccess" in fn, (
        "expected `lastAttempt > lastSuccess` comparison gating the "
        "authoritative-attempt branch. Without it, a fresh failure "
        "stays hidden behind the previous success timestamp."
    )


def test_chip_colors_distinguish_blocked_from_failing(fn: str):
    """The blocked chip must be orange (config gap — actionable but
    not a system failure); the failing chip must be red. Sharing one
    color would force the operator to read the count instead of
    skimming a glance.
    """
    # Orange ≈ #ffa502 (matches existing "stale" chip)
    assert "#ffa502" in fn
    # Red ≈ #ff4757 (matches existing "failing" / "drifted" chip)
    assert "#ff4757" in fn


def test_helpers_are_named_and_present(fn: str):
    """``deriveHealth`` + ``relTime`` + ``firstLine`` are the named
    helpers introduced for this rewrite. Catching their absence here
    surfaces accidental deletions faster than an in-browser visual
    regression.
    """
    for helper in ("deriveHealth", "relTime", "firstLine"):
        assert f"function {helper}" in fn, (
            f"expected named helper `{helper}` in loadBackupStatusSummary — "
            f"removed during a refactor? The structure was deliberately "
            f"separated so each branch is testable in isolation."
        )
