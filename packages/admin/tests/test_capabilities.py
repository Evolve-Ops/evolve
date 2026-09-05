"""Unit tests for ``evolve_admin.capabilities``.

Covers built-in capability resolution, role-binding overrides, the
``"*"`` wildcard, the sticky-deny invariant on ``blocked``, and the
endpoint → capability map.

Spec: internal/spec-user-roster-and-roles-2026-06-07.md §4
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import capabilities as cap  # noqa: E402


# ── Default role resolution ───────────────────────────────────────────────


def test_admin_gets_all_capabilities_by_default():
    caps = cap.resolve_role_capabilities("admin")
    assert caps == set(cap.BUILTIN_CAPABILITIES.keys())


def test_primary_user_gets_bot_scoped_subset():
    caps = cap.resolve_role_capabilities("primary_user")
    # Primary user can manage their bot's roster + channel + config
    assert "bot.roster.mutate" in caps
    assert "bot.channel.config" in caps
    assert "bot.config.modify" in caps
    # But NOT pod-admin-only capabilities
    assert "bot.roles.bind" not in caps
    assert "bot.app.install" not in caps
    assert "bot.code.modify" not in caps


def test_participant_gets_no_builtins():
    """Participants get only app-declared capabilities (via role_bindings)
    — never the bot.* built-ins."""
    caps = cap.resolve_role_capabilities("participant")
    assert caps == set()


def test_blocked_role_sticky_deny():
    """Blocked is sticky — even an overlay role_binding with "*" cannot
    grant capabilities to a blocked role. The block index is the
    authority; capability resolution returns empty."""
    caps = cap.resolve_role_capabilities(
        "blocked",
        overlay_role_bindings={"blocked": ["*"]})
    assert caps == set()


def test_unknown_role_returns_empty():
    """Defense in depth — an unrecognized role name resolves to no
    capabilities, never silently inherits from a default."""
    caps = cap.resolve_role_capabilities("wizard")
    assert caps == set()


# ── Overlay binding overrides ─────────────────────────────────────────────


def test_overlay_explicit_binding_overrides_default():
    """Operator can tighten primary_user's defaults via the overlay."""
    caps = cap.resolve_role_capabilities(
        "primary_user",
        overlay_role_bindings={
            "primary_user": ["bot.roster.read"],  # read-only
        })
    assert caps == {"bot.roster.read"}
    # The default capabilities are gone.
    assert "bot.roster.mutate" not in caps


def test_overlay_wildcard_expands_to_all_known():
    """A "*" binding pulls in all built-ins + any registered app
    capabilities."""
    caps = cap.resolve_role_capabilities(
        "primary_user",
        overlay_role_bindings={"primary_user": ["*"]},
        app_capabilities=["app.archive.add", "app.notes.add"])
    assert caps == set(cap.BUILTIN_CAPABILITIES.keys()) | {
        "app.archive.add", "app.notes.add"}


def test_overlay_can_grant_app_capabilities_to_participant():
    """The expected app-capability binding flow: participant gets
    app.archive.add, nothing else."""
    caps = cap.resolve_role_capabilities(
        "participant",
        overlay_role_bindings={
            "participant": ["app.archive.add"],
        },
        app_capabilities=["app.archive.add", "app.archive.delete"])
    assert caps == {"app.archive.add"}
    assert "app.archive.delete" not in caps  # not granted


def test_overlay_with_invalid_entries_drops_them():
    """A role_binding with non-string entries doesn't crash — those
    entries are dropped silently. Defensive against malformed overlay
    files."""
    caps = cap.resolve_role_capabilities(
        "primary_user",
        overlay_role_bindings={
            "primary_user": ["bot.roster.read", 42, "", None, "bot.send_external"],
        })
    assert caps == {"bot.roster.read", "bot.send_external"}


def test_overlay_with_non_list_binding_uses_default():
    """A garbage binding value falls back to defaults rather than
    granting nothing."""
    caps = cap.resolve_role_capabilities(
        "primary_user",
        overlay_role_bindings={"primary_user": "not-a-list"})
    # Falls back to default primary_user bindings.
    assert "bot.roster.mutate" in caps


# ── has_capability convenience ────────────────────────────────────────────


def test_has_capability_true_when_granted():
    assert cap.has_capability("primary_user", "bot.roster.mutate") is True


def test_has_capability_false_when_not_granted():
    assert cap.has_capability("participant", "bot.roster.mutate") is False
    assert cap.has_capability("blocked", "bot.roster.read") is False


def test_has_capability_respects_overlay():
    assert cap.has_capability(
        "participant", "app.archive.add",
        overlay_role_bindings={"participant": ["app.archive.add"]},
        app_capabilities=["app.archive.add"]) is True


# ── Endpoint capability map ───────────────────────────────────────────────


def test_endpoint_capability_map_uses_known_capabilities():
    """Every endpoint's required capability is one we've defined.
    Catches typos before they ship as silent-deny."""
    for endpoint, required_cap in cap.ENDPOINT_CAPABILITIES.items():
        assert required_cap in cap.BUILTIN_CAPABILITIES, (
            f"Endpoint {endpoint!r} requires unknown capability {required_cap!r}"
        )


def test_endpoint_map_covers_all_mutation_endpoints():
    """Every routes_bot_users mutation endpoint has a capability mapping.
    Regression guard for the day someone adds a new endpoint without
    wiring its capability gate."""
    expected_endpoint_ids = {
        "roster.mutate.role",
        "roster.mutate.engagement",
        "roster.mutate.block",
        "roster.mutate.unblock",
        "channel.config.newcomer_mode",
        "roster.read",
    }
    missing = expected_endpoint_ids - set(cap.ENDPOINT_CAPABILITIES.keys())
    assert not missing, f"Missing endpoint mappings: {missing}"
