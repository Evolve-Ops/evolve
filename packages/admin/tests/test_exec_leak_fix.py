"""tests/test_exec_leak_fix.py — exec-approval-leak Fix 1 + Fix 2 unit tests.

Pins the two ``_infer_exec_policy`` behaviors the 2026-05-21 diagnosis
(docs/diagnosis-evo-exec-approval-leak-2026-05-21.md) demanded:

  * **Fix 1 (primary carve-out)** — REMOVED in Phase E.4 (2026-05-25).
    The carve-out was a defense-in-depth measure for the ``/approve``
    exfiltration leak. Phase E.2.b's account separation (evo runs as the
    unprivileged ``evo`` macOS user) closes the leak fundamentally:
    even with exec=full, evo's shell runs as a user with no sudo, no
    cross-bot ACL, no admin-daemon reach. Tests in this section now
    assert the *new* behavior (no carve-out; primary bots fall through
    to the same default as any other bot). See
    docs/spec-evo-account-separation-2026-05-25.md §"Phase E.4".

  * **Fix 2 (socket-meta false positive)**: ``defaults: {"security":
    "full"}`` is the unix-socket auth posture for the exec-approvals
    daemon, NOT a per-agent approval list — it must not infer
    ``"allowlist"``. Only an actual ``allowlist``/``approvals``/``allow``
    array under ``defaults`` counts. Unchanged by Phase E.4.

Member-bot default pivoted 2026-05-25 from ``"deny"`` to ``"full"`` — see
docs/spec-app-derived-permissions-2026-05-24.md. The Fix 2 structural
invariant (socket-meta must not infer allowlist) is independent of that
pivot and of the Phase-E.4 carve-out removal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: E402

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.deploy import _infer_exec_policy  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Phase E.4 — primary-bot carve-out REMOVED
# ─────────────────────────────────────────────────────────────────────────────


def test_evolve_bot_no_longer_short_circuits_to_deny():
    """Phase E.4 removed the ``bot_id="evolve"`` carve-out. Evo now
    follows the same inference flow as any other bot:
      - explicit override wins (Priority 1)
      - real allowlist content → "allowlist"
      - otherwise → "full"

    This regression test pins the *new* behavior so a re-introduction
    of the carve-out (e.g. an over-eager security review) trips the
    test rather than silently re-blocking evo from exec.
    """
    # Empty exec-approvals (the typical post-cutover state): default is
    # now "full", same as any member bot.
    assert _infer_exec_policy(
        {}, {}, bot_id="evolve", bot_role="primary",
    ) == "full"

    # With a real allowlist agent block: now infers "allowlist".
    ea_with_real_allowlist = {
        "agents": {
            "evolve": {"allowlist": [{"cmd": "ls"}]},
        },
    }
    assert _infer_exec_policy(
        {}, ea_with_real_allowlist, bot_id="evolve", bot_role="primary",
    ) == "allowlist"


def test_non_evolve_primary_role_bot_also_gets_uniform_treatment():
    """A non-evo bot configured as primary used to be forced to deny by
    the carve-out's ``role == "primary"`` branch. Phase E.4 dropped that
    branch — they get the same default as a member bot.
    """
    ea_with_real_allowlist = {
        "agents": {
            "team_bot_a": {"allowlist": [{"cmd": "ls"}]},
        },
    }
    assert _infer_exec_policy(
        {}, ea_with_real_allowlist, bot_id="team_bot_a", bot_role="primary",
    ) == "allowlist"

    # No exec-approvals at all → "full", not "deny"
    assert _infer_exec_policy(
        {}, None, bot_id="team_bot_a", bot_role="primary",
    ) == "full"


def test_explicit_operator_override_still_wins():
    """Priority 1 (explicit ``execPolicy`` in network.json) wins
    regardless of any other signal. Carve-out removal didn't touch
    this — kept as a regression test."""
    ea_empty = {}
    assert _infer_exec_policy(
        {"execPolicy": "allowlist"}, ea_empty,
        bot_id="evolve", bot_role="primary",
    ) == "allowlist"

    assert _infer_exec_policy(
        {"execPolicy": "full"}, ea_empty,
        bot_id="evolve", bot_role="primary",
    ) == "full"

    # Operator can still pin a bot to deny if they want — escape hatch.
    assert _infer_exec_policy(
        {"execPolicy": "deny"}, ea_empty,
        bot_id="evolve", bot_role="primary",
    ) == "deny"


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2 — socket-meta false positive
# ─────────────────────────────────────────────────────────────────────────────


def test_member_bot_with_socket_meta_only_does_not_get_allowlist():
    """``defaults: {"security": "full"}`` is socket-protocol metadata
    (unix-socket auth posture for the exec-approvals daemon), not a
    per-agent approval list. The old code's "any non-empty defaults →
    allowlist" branch false-positived on this — the bug that surfaced
    as evo's openclaw.json having ``tools.exec.security = "allowlist"``
    despite no real allowlist content.

    Member-bot default is now ``"full"`` (post 2026-05-25 pivot) — the
    key invariant this test pins is that socket-meta does NOT spuriously
    upgrade the bot to ``"allowlist"`` mode."""
    ea = {
        "version": 1,
        "socket": {"path": "...", "token": "..."},
        "defaults": {"security": "full"},
        "agents": {},
    }
    result = _infer_exec_policy(
        {}, ea, bot_id="team_bot_a", bot_role="member",
    )
    assert result != "allowlist"
    assert result == "full"


def test_member_bot_with_real_allowlist_in_defaults_still_works():
    """The Fix 2 narrowing must not break the legitimate case: an
    actual ``allowlist``/``approvals``/``allow`` array under
    ``defaults`` still infers ``"allowlist"``."""
    ea_allowlist = {
        "defaults": {"allowlist": [{"cmd": "ls"}]},
    }
    assert _infer_exec_policy(
        {}, ea_allowlist, bot_id="team_bot_a", bot_role="member",
    ) == "allowlist"

    ea_approvals = {
        "defaults": {"approvals": [{"cmd": "rg"}]},
    }
    assert _infer_exec_policy(
        {}, ea_approvals, bot_id="team_bot_a", bot_role="member",
    ) == "allowlist"

    ea_allow = {
        "defaults": {"allow": [{"cmd": "find"}]},
    }
    assert _infer_exec_policy(
        {}, ea_allow, bot_id="team_bot_a", bot_role="member",
    ) == "allowlist"


def test_member_bot_with_real_allowlist_in_agents_still_works():
    """The agents-level allowlist path (Priority 3 in the original
    docstring) must keep working. Security_bot's monitoring scripts depend
    on this branch."""
    ea = {
        "agents": {
            "security_bot": {"allowlist": [{"cmd": "/Users/security_bot/bin/probe"}]},
        },
    }
    assert _infer_exec_policy(
        {}, ea, bot_id="security_bot", bot_role="member",
    ) == "allowlist"


def test_member_bot_with_no_exec_approvals_gets_full_default():
    """Member-bot default is ``"full"`` (pivoted 2026-05-25). Bots
    with no exec-approvals.json — plugin-only bots like brave, github,
    slack — land at the new default, not the old ``"deny"``. See
    docs/spec-app-derived-permissions-2026-05-24.md."""
    assert _infer_exec_policy(
        {}, None, bot_id="brave", bot_role="member",
    ) == "full"
    assert _infer_exec_policy(
        {}, {}, bot_id="brave", bot_role="member",
    ) == "full"


def test_empty_defaults_object_does_not_infer_allowlist():
    """``defaults: {}`` — empty object — must not infer allowlist (it's
    not content). The Fix 2 keyword check naturally handles this; the
    fallthrough lands at the new member-bot default of ``"full"``."""
    ea = {"defaults": {}}
    result = _infer_exec_policy(
        {}, ea, bot_id="team_bot_a", bot_role="member",
    )
    assert result != "allowlist"
    assert result == "full"


def test_defaults_with_irrelevant_keys_does_not_infer_allowlist():
    """A defaults block with keys OTHER than allowlist/approvals/allow
    (e.g. socket metadata, version stamps, future fields) must not
    infer allowlist. Future-proofs against new socket-meta fields."""
    ea = {
        "defaults": {
            "security": "full",
            "version": 2,
            "lastUpdated": "2026-05-21",
        },
    }
    result = _infer_exec_policy(
        {}, ea, bot_id="team_bot_a", bot_role="member",
    )
    assert result != "allowlist"
    assert result == "full"


# ─────────────────────────────────────────────────────────────────────────────
# Regression — backward-compatible defaults
# ─────────────────────────────────────────────────────────────────────────────


def test_signature_remains_backward_compatible_without_kwargs():
    """Existing call sites that don't pass bot_id/bot_role keep working.
    Catches the bug where a hot-patch update to one call site missed
    others (e.g. test code or future deploy.py refactors).

    Without bot_id/role kwargs the inference fall-through is the member-
    bot default — ``"full"`` post 2026-05-25 (and post-E.4 the result
    doesn't change based on bot_id/role anyway)."""
    assert _infer_exec_policy({}, None) == "full"
    assert _infer_exec_policy({}, {}) == "full"

    # Explicit override still wins.
    assert _infer_exec_policy({"execPolicy": "allowlist"}, None) == "allowlist"
    assert _infer_exec_policy({"execPolicy": "deny"}, None) == "deny"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
