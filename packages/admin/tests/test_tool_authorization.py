"""tests/test_tool_authorization.py — evo tool authorization framework (Phase 2).

Covers the new ``authorization_scope`` field on ``Tool``, the
``CallerIdentity`` dataclass, the dispatcher gate, and the
``meta.tools`` visibility filter. Spec lives in
``docs/audit-evo-tool-coverage-2026-06-02.md`` (§Authorization-tier
classification) + the Phase 2 framework task on the worktree.

The 10 tests follow the task brief verbatim:

  1. admin-scope tool + admin caller → dispatches normally
  2. admin-scope tool + cross-bot caller → refusal envelope
  3. user-scope tool + admin caller → dispatches (admin can do user-tier)
  4. user-scope tool + non-admin caller → dispatches (it's user-tier)
  5. anyone-scope tool + cross-bot caller → dispatches
  6. Default scope is "admin" (no explicit tier = locked down)
  7. meta.tools for admin caller → ALL tools
  8. meta.tools for non-admin caller → only user+anyone tools + footer
  9. CallerIdentity.is_admin maps surfaces correctly
 10. Refusal message names the tool the caller tried to invoke

Tests construct tools directly via the registry helpers + invoke the
gate / filter primitives in ``evolve_admin.evo.tools.authorization``.
The MCP-transport layer's own integration is tested in
``test_evo_tools_mcp.py``; here we exercise the framework primitives.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo.tools import (  # noqa: E402
    RiskTier,
    Tool,
    all_tools,
)
from evolve_admin.evo.tools.authorization import (  # noqa: E402
    NON_ADMIN_HELP_FOOTER,
    CallerIdentity,
    check_authorization,
    scope_allows,
    visible_tools,
)
from evolve_admin.evo.tools.meta_tools import _handler as meta_tools_handler  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures — synthetic tools at each scope
# ─────────────────────────────────────────────────────────────────────────────


def _ok_handler(**_kwargs) -> dict:
    """Trivial handler used in scope tests — returns a marker dict.

    Caller asserts the marker reached the handler when authorization
    passed (vs the refusal envelope when it didn't).
    """
    return {"ok": True, "where": "handler"}


def _ok_validate(**_kwargs) -> dict:
    return {"ok": True}


def _make_tool(name: str, scope: str | None = None) -> Tool:
    """Build a synthetic read-tier tool with a given authorization_scope.

    Read-tier picked so we don't have to declare a validate(); the
    authorization mechanism is independent of risk-tier.
    """
    kwargs: dict = {
        "name": name,
        "description": "test tool",
        "input_schema": {"type": "object", "properties": {}},
        "handler": _ok_handler,
        "risk_tier": RiskTier.READ,
    }
    if scope is not None:
        kwargs["authorization_scope"] = scope
    return Tool(**kwargs)


_ADMIN_CALLER = CallerIdentity(surface="admin_ui")
_TELEGRAM_CALLER = CallerIdentity(surface="telegram_operator")
_CROSS_BOT_CALLER = CallerIdentity(
    surface="cross_bot_member", bot_id="team-bot-b", operator_handle="alice",
)


# ─────────────────────────────────────────────────────────────────────────────
# 1–5. Gate behavior per (scope × caller) pair
# ─────────────────────────────────────────────────────────────────────────────


def test_admin_scope_admin_caller_dispatches():
    """admin tool + admin caller → no refusal; handler is invoked."""
    tool = _make_tool("test.admin_tool", scope="admin")
    assert check_authorization(tool, _ADMIN_CALLER) is None
    # Confirm scope_allows agrees end-to-end.
    assert scope_allows(tool.authorization_scope, _ADMIN_CALLER) is True


def test_admin_scope_cross_bot_caller_refused():
    """admin tool + cross-bot caller → refusal envelope with the right shape."""
    tool = _make_tool("test.admin_tool", scope="admin")
    refusal = check_authorization(tool, _CROSS_BOT_CALLER)
    assert refusal is not None
    assert refusal["error"] == "authorization_required"
    assert refusal["scope"] == "admin"
    # Refusal envelope also names the surface so the model can hint at
    # the right escalation path.
    assert refusal["surface"] == "cross_bot_member"
    assert scope_allows(tool.authorization_scope, _CROSS_BOT_CALLER) is False


def test_user_scope_admin_caller_dispatches():
    """user-tier tool + admin caller → admin can do user-tier (cascade)."""
    tool = _make_tool("test.user_tool", scope="user")
    assert check_authorization(tool, _ADMIN_CALLER) is None
    assert scope_allows(tool.authorization_scope, _ADMIN_CALLER) is True


def test_user_scope_non_admin_caller_dispatches():
    """user-tier tool + cross-bot caller → user-tier is open to non-admins."""
    tool = _make_tool("test.user_tool", scope="user")
    assert check_authorization(tool, _CROSS_BOT_CALLER) is None
    assert scope_allows(tool.authorization_scope, _CROSS_BOT_CALLER) is True


def test_anyone_scope_cross_bot_caller_dispatches():
    """anyone-tier tool + cross-bot caller → no refusal; read-side is open."""
    tool = _make_tool("test.anyone_tool", scope="anyone")
    assert check_authorization(tool, _CROSS_BOT_CALLER) is None
    assert scope_allows(tool.authorization_scope, _CROSS_BOT_CALLER) is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. Default scope is "admin"
# ─────────────────────────────────────────────────────────────────────────────


def test_default_scope_is_admin():
    """A tool registered without an explicit authorization_scope must
    default to admin-only. Conservative default keeps every existing
    tool gated until a follow-up PR opens it deliberately."""
    tool = _make_tool("test.default_tool", scope=None)
    assert tool.authorization_scope == "admin"
    # Confirm the default keeps cross-bot callers out by default.
    assert check_authorization(tool, _CROSS_BOT_CALLER) is not None


# Authorization-scope distribution snapshot — locked-in by the
# retrofit PR (follow-on to PR #1992). The map below records the
# deliberate per-tool scope decision; any PR that touches this
# distribution must update the fixture deliberately. The retrofit
# rationale lives in docs/audit-evo-tool-coverage-2026-06-02.md
# §Authorization-tier classification — non-admin entries are
# documented at their definition site with a per-tool comment.
EXPECTED_AUTHORIZATION_SCOPE_2026_06_02 = {
    # Pod-state reads that are operationally visible without leaking
    # secrets — opened to anyone (incl. cross-bot members).
    "pod_state.signals.firing": "anyone",
    "pod_state.signals.history": "anyone",
    "pod_state.home_narrative": "anyone",
    "pod_state.pause_state": "anyone",
    "pod_state.breakers": "anyone",
    "pod_state.app_scan_status": "anyone",
    # Registry introspection — handler self-filters by caller scope,
    # so safe to open. Without this, a cross-bot member has no way to
    # learn what they CAN run.
    "meta.tools": "anyone",
    # Telemetry log — caller (evo) records its own missing-tool
    # observation; no chat content, fingerprint shape only.
    "action.evo.log_tool_gap": "anyone",
}


def test_authorization_scope_distribution_2026_06_02():
    """Lock in the per-tool authorization_scope decisions made by the
    retrofit PR. Any future change to the distribution — new tool, a
    rescope, removal — must update EXPECTED_AUTHORIZATION_SCOPE_2026_06_02
    deliberately so the security posture stays a reviewed artifact.

    The default for unlisted tools is ``admin``; the assertion compares
    only the non-admin set so the test stays compact AND a tool that
    accidentally gets opened to user/anyone trips loudly.
    """
    non_admin_actual = {
        t.name: t.authorization_scope
        for t in all_tools()
        if t.authorization_scope != "admin"
    }
    assert non_admin_actual == EXPECTED_AUTHORIZATION_SCOPE_2026_06_02, (
        "Authorization-scope distribution has drifted from the locked "
        "fixture. If the change is deliberate (you really did mean to "
        "open a new tool to user/anyone), update "
        "EXPECTED_AUTHORIZATION_SCOPE_2026_06_02 in the same PR. If it "
        "is accidental, revert the offending tool to admin."
    )


def test_every_registered_tool_has_explicit_scope_in_the_table():
    """Every registered tool's name should appear in the audit table OR
    map to the default ``admin``. We assert here that every tool name
    is recognized — catches the case where a renamed/added tool slips
    in without the retrofit's per-tool consideration.

    Concretely: a tool is "explicitly classified" if it is either:
      * in EXPECTED_AUTHORIZATION_SCOPE_2026_06_02 (the non-admin set), OR
      * carries authorization_scope == "admin" (the conservative default).

    The third case — a tool at user/anyone NOT listed in the fixture —
    is caught by test_authorization_scope_distribution_2026_06_02 above.
    """
    for tool in all_tools():
        if tool.authorization_scope == "admin":
            continue
        assert tool.name in EXPECTED_AUTHORIZATION_SCOPE_2026_06_02, (
            f"Tool '{tool.name}' has scope {tool.authorization_scope!r} "
            "but isn't in EXPECTED_AUTHORIZATION_SCOPE_2026_06_02. "
            "Add an entry to the fixture (and document why the tool is "
            "safe at this scope) so the decision is reviewed."
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7–8. meta.tools visibility filter + non-admin footer
# ─────────────────────────────────────────────────────────────────────────────


def test_meta_tools_admin_returns_all_tools():
    """For admin callers meta.tools returns the full registry — no
    authorization-scope filtering."""
    result = meta_tools_handler(caller_identity=_ADMIN_CALLER)
    assert result["count"] == len(all_tools())
    assert "non_admin_footer" not in result


def test_meta_tools_non_admin_returns_filtered_with_footer():
    """For cross-bot callers meta.tools strips admin-tier tools and
    appends the "ask your pod admin" footer."""
    result = meta_tools_handler(caller_identity=_CROSS_BOT_CALLER)
    # Post-retrofit (#1992 follow-up): a non-admin caller now sees the
    # explicitly-opened user/anyone tools (signals.firing, breakers,
    # meta.tools itself, etc.) — every other tool is still gated. The
    # footer always appends so the caller knows the admin has more.
    for tool_proj in result["tools"]:
        assert tool_proj["authorization_scope"] in ("user", "anyone"), (
            f"Tool '{tool_proj['name']}' leaked into a cross-bot caller's "
            f"meta.tools result with scope {tool_proj['authorization_scope']!r}."
        )
    assert result["non_admin_footer"] == NON_ADMIN_HELP_FOOTER


def test_visible_tools_filters_by_scope():
    """The visible_tools helper drops scopes the caller can't invoke."""
    tools = (
        _make_tool("anyone_tool", scope="anyone"),
        _make_tool("user_tool", scope="user"),
        _make_tool("admin_tool", scope="admin"),
    )
    admin_visible = visible_tools(tools, _ADMIN_CALLER)
    assert {t.name for t in admin_visible} == {
        "anyone_tool", "user_tool", "admin_tool",
    }
    cross_visible = visible_tools(tools, _CROSS_BOT_CALLER)
    assert {t.name for t in cross_visible} == {"anyone_tool", "user_tool"}


# ─────────────────────────────────────────────────────────────────────────────
# 9. CallerIdentity.is_admin surface mapping
# ─────────────────────────────────────────────────────────────────────────────


def test_caller_identity_is_admin_surface_mapping():
    """admin_ui + telegram_operator → admin; cross_bot_member → not admin.

    Adding a new surface means updating both the Literal type and this
    test — caught here so the property's contract can't drift silently.
    """
    assert CallerIdentity(surface="admin_ui").is_admin is True
    assert CallerIdentity(surface="telegram_operator").is_admin is True
    assert CallerIdentity(surface="cross_bot_member").is_admin is False
    # Context fields don't affect the admin decision.
    assert CallerIdentity(
        surface="cross_bot_member", bot_id="team-bot-b", operator_handle="alice",
    ).is_admin is False


# ─────────────────────────────────────────────────────────────────────────────
# 10. Refusal message names the tool
# ─────────────────────────────────────────────────────────────────────────────


def test_refusal_message_names_the_tool():
    """The refusal envelope's ``message`` includes the exact tool name
    the caller tried to invoke. Operators report that bare "denied"
    responses are harder to debug than "you tried action.bot.redeploy
    — that's admin-only."""
    tool = _make_tool("action.bot.redeploy", scope="admin")
    refusal = check_authorization(tool, _CROSS_BOT_CALLER)
    assert refusal is not None
    assert "action.bot.redeploy" in refusal["message"]
    assert refusal["tool"] == "action.bot.redeploy"


# ─────────────────────────────────────────────────────────────────────────────
# Construction-time validation — Tool's __post_init__ rejects bad scopes
# ─────────────────────────────────────────────────────────────────────────────


def test_tool_rejects_unknown_scope():
    """Tool() refuses an unknown authorization_scope at construction
    time so the typo can't survive into the registry."""
    import pytest
    with pytest.raises(ValueError, match="authorization_scope"):
        Tool(
            name="bogus_scope_tool",
            description="x",
            input_schema={"type": "object"},
            handler=_ok_handler,
            risk_tier=RiskTier.READ,
            authorization_scope="superuser",  # type: ignore[arg-type]
        )
