"""Tests for autonomy.catalog (kind semantics) + autonomy.renderer (deny merge)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autonomy import catalog as _catalog
from autonomy import renderer as _renderer
from autonomy import store as _store


EMAIL = _catalog.KIND_SPECS["email"]
GW = _catalog.INTEGRATION_BINDINGS["google_workspace"]

# A representative tool surface — superset of the vetted catalog entry,
# so the classifier is exercised on draft/delete shapes too.
TOOLS = [
    "search_gmail_messages",
    "get_gmail_message",
    "send_gmail_message",
    "create_draft_gmail_message",
    "delete_gmail_message",
    "list_calendars",            # not email — outside the tool filter
    "create_calendar_event",     # not email
]


# ── Catalog ──────────────────────────────────────────────────────────────────

def test_classify_tool_verbs():
    assert _catalog.classify_tool(EMAIL, "send_gmail_message") == "send"
    assert _catalog.classify_tool(EMAIL, "delete_gmail_message") == "delete"
    assert _catalog.classify_tool(EMAIL, "create_draft_gmail_message") == "draft"
    assert _catalog.classify_tool(EMAIL, "search_gmail_messages") is None
    # Precedence: a send-of-draft tool is a send, not a draft.
    assert _catalog.classify_tool(EMAIL, "send_draft") == "send"
    # ...but a draft-reply tool is a draft (drafting never sends).
    assert _catalog.classify_tool(EMAIL, "draft_gmail_reply") == "draft"


def test_kind_tools_filter_scopes_to_email():
    kt = _catalog.kind_tools(GW, TOOLS)
    assert "send_gmail_message" in kt
    assert "list_calendars" not in kt
    assert "create_calendar_event" not in kt


def test_delete_in_never_at_every_rung():
    """Spec §1.4: email delete ships in `never` at every rung."""
    for rung in _catalog.RUNGS:
        assert "delete" in _catalog.denied_verbs(EMAIL, rung, None)


def test_expected_denied_tools_per_rung():
    draft = _catalog.expected_denied_tools(EMAIL, GW, TOOLS, "draft_only")
    assert draft == ["delete_gmail_message", "send_gmail_message"]

    ask = _catalog.expected_denied_tools(EMAIL, GW, TOOLS, "act_with_approval")
    assert ask == ["delete_gmail_message"]  # send reachable; delete never

    auto = _catalog.expected_denied_tools(
        EMAIL, GW, TOOLS, "autonomous_within_rules",
        rules={"reach_allow": ["*@x.com"], "actions_per_day": 5},
    )
    assert auto == ["delete_gmail_message"]


def test_rules_never_adds_mechanical_deny():
    auto = _catalog.expected_denied_tools(
        EMAIL, GW, TOOLS, "autonomous_within_rules",
        rules={"reach_allow": ["*@x.com"], "never": ["forward"]},
    )
    assert auto == ["delete_gmail_message"]  # no forward tools in surface
    verbs = _catalog.denied_verbs(
        EMAIL, "autonomous_within_rules", {"never": ["forward"]},
    )
    assert {"delete", "forward"} <= verbs


def test_ladder_eligibility():
    assert _catalog.is_ladder_eligible(EMAIL, GW, TOOLS)
    # Read-only surface → off the ladder (spec §1.2).
    assert not _catalog.is_ladder_eligible(
        EMAIL, GW, ["search_gmail_messages", "get_gmail_message"],
    )


def test_validate_rules_vocabulary():
    ok = {"reach_allow": ["*@x.com"], "actions_per_day": 5, "never": ["forward"]}
    assert _catalog.validate_rules(EMAIL, "autonomous_within_rules", ok) == []
    assert _catalog.validate_rules(EMAIL, "autonomous_within_rules", {"frobnicate": 1})
    assert _catalog.validate_rules(
        EMAIL, "autonomous_within_rules", {"actions_per_day": 0},
    )
    assert _catalog.validate_rules(
        EMAIL, "autonomous_within_rules", {"never": ["explode"]},
    )
    assert _catalog.validate_rules(EMAIL, "autonomous_within_rules", {})
    assert _catalog.validate_rules(EMAIL, "draft_only", {"actions_per_day": 5})


def test_guidance_includes_rules_summary():
    text = _catalog.guidance_for(
        EMAIL, "autonomous_within_rules",
        {"reach_allow": ["*@x.com"], "actions_per_day": 7},
    )
    assert "Acts within limits" in text
    assert "*@x.com" in text
    assert "7" in text


def test_known_server_tools_from_vetted_catalog():
    tools = _catalog.known_server_tools("google_workspace")
    assert "send_gmail_message" in tools
    assert _catalog.known_server_tools("never_heard_of_it") == []


# ── Plugin-provided Gmail (bare gmail_* tools, no mcp.servers entry) ──────────

PLUGIN = _catalog.INTEGRATION_BINDINGS[_catalog.PLUGIN_GMAIL_INTEGRATION_ID]


def test_plugin_binding_is_email_with_identical_label():
    assert PLUGIN.kind == "email"
    assert PLUGIN.source == _catalog.SOURCE_PLUGIN
    # Same operator-facing label as the MCP binding (the whole point of
    # reusing Google Workspace's display name).
    assert PLUGIN.display_name == GW.display_name


def test_plugin_classify_and_eligibility():
    # Bare gmail_* names classify into the same email verbs.
    assert _catalog.classify_tool(EMAIL, "gmail_send") == "send"
    assert _catalog.classify_tool(EMAIL, "gmail_delete_message") == "delete"
    assert _catalog.classify_tool(EMAIL, "gmail_list_messages") is None
    assert _catalog.classify_tool(EMAIL, "gmail_get_message") is None
    surface = list(PLUGIN.known_tools)
    assert _catalog.is_ladder_eligible(EMAIL, PLUGIN, surface)
    # Read-only subset → off the ladder.
    assert not _catalog.is_ladder_eligible(
        EMAIL, PLUGIN, ["gmail_list_messages", "gmail_get_message"],
    )


def test_plugin_deny_entries_are_bare_names():
    # Plugin deny spelling is the BARE tool name, never mcp__-prefixed.
    assert _catalog.oc_deny_entry(
        _catalog.PLUGIN_GMAIL_INTEGRATION_ID, "gmail_send",
    ) == "gmail_send"
    # MCP spelling unchanged.
    assert _catalog.oc_deny_entry("google_workspace", "send_gmail_message") == (
        "mcp__google_workspace__send_gmail_message"
    )


def test_plugin_expected_denied_tools_per_rung():
    surface = list(PLUGIN.known_tools)
    draft = _catalog.expected_denied_tools(EMAIL, PLUGIN, surface, "draft_only")
    # Both delete forms (permanent + trash) are denied with send at draft_only.
    assert draft == [
        "gmail_delete_message", "gmail_send", "gmail_trash_message",
    ]
    # "Asks first": send reachable, both delete forms still never (spec §1.4 —
    # trashing is the delete verb, denied at every rung).
    ask = _catalog.expected_denied_tools(
        EMAIL, PLUGIN, surface, "act_with_approval",
    )
    assert ask == ["gmail_delete_message", "gmail_trash_message"]
    assert "gmail_send" not in ask


def test_plugin_deny_entry_ownership_excludes_foreign_and_mcp():
    iid = _catalog.PLUGIN_GMAIL_INTEGRATION_ID
    assert _catalog.deny_entry_is_owned(iid, "gmail_send")
    assert _catalog.deny_entry_is_owned(iid, "gmail_delete_message")
    # A co-resident MCP server's denies are never claimed by the plugin.
    assert not _catalog.deny_entry_is_owned(
        iid, "mcp__google_workspace__send_gmail_message",
    )
    assert not _catalog.deny_entry_is_owned(iid, "exec_shell")
    # MCP binding still owns only its prefix.
    assert _catalog.deny_entry_is_owned(
        "google_workspace", "mcp__google_workspace__send_gmail_message",
    )
    assert not _catalog.deny_entry_is_owned("google_workspace", "gmail_send")


def test_discover_ladder_integrations_plugin_and_mcp():
    # Plugin source: gmail_* in alsoAllow, no mcp.servers.
    plugin_cfg = {"tools": {"alsoAllow": [
        "gmail_list_messages", "gmail_send", "gmail_delete_message",
        "drive_read_file",  # not email
        "skill_workshop",   # not gmail
    ]}}
    found = _catalog.discover_ladder_integrations(plugin_cfg)
    assert len(found) == 1
    iid, binding, tools = found[0]
    assert iid == _catalog.PLUGIN_GMAIL_INTEGRATION_ID
    assert binding.source == _catalog.SOURCE_PLUGIN
    assert set(tools) == {"gmail_list_messages", "gmail_send", "gmail_delete_message"}

    # MCP source present → plugin email is suppressed (kind not double-counted).
    both_cfg = {
        "mcp": {"servers": {"google_workspace": {"command": "uvx"}}},
        "tools": {"alsoAllow": ["gmail_send"]},
    }
    found2 = _catalog.discover_ladder_integrations(both_cfg)
    assert [iid for iid, _, _ in found2] == ["google_workspace"]


def test_merge_deny_slice_plugin_owns_bare_preserves_foreign():
    existing = {"tools": {"deny": [
        "mcp__github__delete_repo",   # foreign — preserved
        "gmail_old_tool",             # plugin-owned (bare gmail_*) — replaced
        "exec_shell",                 # foreign — preserved
    ]}}
    expected = {_catalog.PLUGIN_GMAIL_INTEGRATION_ID: [
        "gmail_send", "gmail_delete_message",
    ]}
    merged, changed = _renderer.merge_deny_slice(existing, expected)
    assert changed
    deny = merged["tools"]["deny"]
    assert "mcp__github__delete_repo" in deny
    assert "exec_shell" in deny
    assert "gmail_old_tool" not in deny
    assert "gmail_send" in deny and "gmail_delete_message" in deny


# ── Renderer: pure merge ─────────────────────────────────────────────────────

def test_merge_deny_slice_replaces_owned_preserves_foreign():
    existing = {
        "tools": {"deny": [
            "mcp__github__delete_repo",                 # foreign — preserved
            "mcp__google_workspace__old_entry",         # owned — replaced
            "some_builtin_tool",                        # foreign — preserved
        ]},
    }
    expected = {"google_workspace": [
        "mcp__google_workspace__send_gmail_message",
        "mcp__google_workspace__delete_gmail_message",
    ]}
    merged, changed = _renderer.merge_deny_slice(existing, expected)
    assert changed
    deny = merged["tools"]["deny"]
    assert deny[:2] == ["mcp__github__delete_repo", "some_builtin_tool"]
    assert "mcp__google_workspace__old_entry" not in deny
    assert "mcp__google_workspace__delete_gmail_message" in deny
    assert "mcp__google_workspace__send_gmail_message" in deny
    # Original untouched.
    assert "mcp__google_workspace__old_entry" in existing["tools"]["deny"]


def test_merge_deny_slice_noop_when_up_to_date():
    existing = {"tools": {"deny": ["mcp__google_workspace__send_gmail_message"]}}
    expected = {"google_workspace": ["mcp__google_workspace__send_gmail_message"]}
    _, changed = _renderer.merge_deny_slice(existing, expected)
    assert not changed


def test_merge_deny_slice_empty_expected_strips_owned():
    existing = {"tools": {"deny": ["mcp__google_workspace__send_gmail_message", "x"]}}
    merged, changed = _renderer.merge_deny_slice(
        existing, {"google_workspace": []},
    )
    assert changed
    assert merged["tools"]["deny"] == ["x"]


def test_merge_does_not_touch_unmanaged_prefixes():
    existing = {"tools": {"deny": ["mcp__zoom__delete_meeting"]}}
    merged, changed = _renderer.merge_deny_slice(
        existing, {"google_workspace": ["mcp__google_workspace__send_gmail_message"]},
    )
    assert changed
    assert "mcp__zoom__delete_meeting" in merged["tools"]["deny"]


# ── Renderer: I/O path (home_override) ───────────────────────────────────────

@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


@pytest.fixture
def bot_home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".openclaw").mkdir(parents=True)
    (h / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "mcp": {"servers": {"google_workspace": {"command": "uvx", "args": ["workspace-mcp"]}}},
        "tools": {"deny": ["mcp__github__delete_repo"]},
    }))
    return h


def test_render_bot_writes_deny_and_records_enforcement(
    shared_dir: Path, bot_home: Path,
):
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
    )
    result = _renderer.render_bot("alpha", shared_dir, home_override=bot_home)
    assert result.write_error is None
    assert result.changed and result.written

    cfg = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    deny = cfg["tools"]["deny"]
    assert "mcp__google_workspace__send_gmail_message" in deny
    assert "mcp__github__delete_repo" in deny  # foreign preserved

    doc = _store.load(shared_dir, "alpha")
    enf = doc.integrations["google_workspace"].enforcement
    surfaces = {e["surface"]: e for e in enf}
    assert surfaces["mcp_tool_allowlist"]["mode"] == "mechanical"
    assert surfaces["mcp_tool_allowlist"]["verified"] is True
    assert surfaces["bot_guidance"]["mode"] == "procedural"
    assert surfaces["bot_guidance"]["verified"] is True

    # Coherence: live state now matches the declared posture.
    assert _renderer.is_render_up_to_date(cfg, doc)


def test_render_bot_promotion_releases_send_deny(
    shared_dir: Path, bot_home: Path,
):
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
    )
    _renderer.render_bot("alpha", shared_dir, home_override=bot_home)
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="act_with_approval", actor="operator_ui",
    )
    result = _renderer.render_bot("alpha", shared_dir, home_override=bot_home)
    assert result.changed

    cfg = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    deny = cfg["tools"]["deny"]
    assert "mcp__google_workspace__send_gmail_message" not in deny
    assert "mcp__github__delete_repo" in deny


def test_render_bot_skips_backfill_inferred(shared_dir: Path, bot_home: Path):
    _store.ensure_entry(
        shared_dir, "alpha", "google_workspace",
        kind="email", rung="draft_only", actor=_store.ACTOR_BACKFILL,
    )
    before = (bot_home / ".openclaw" / "openclaw.json").read_text()
    result = _renderer.render_bot("alpha", shared_dir, home_override=bot_home)
    assert result.skipped_unconfirmed == ["google_workspace"]
    assert not result.changed and not result.written
    assert (bot_home / ".openclaw" / "openclaw.json").read_text() == before


def test_render_bot_no_posture_file_is_noop(shared_dir: Path, bot_home: Path):
    result = _renderer.render_bot("alpha", shared_dir, home_override=bot_home)
    assert not result.changed and result.write_error is None


def test_merge_autonomy_into_config_deploy_path(shared_dir: Path, bot_home: Path):
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
    )
    cfg = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    changed = _renderer.merge_autonomy_into_config(cfg, "alpha", shared_dir)
    assert changed
    assert "mcp__google_workspace__send_gmail_message" in cfg["tools"]["deny"]
    # Second pass: idempotent.
    assert not _renderer.merge_autonomy_into_config(cfg, "alpha", shared_dir)
