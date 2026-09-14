"""Tests for mcp_admin.allowlist.

Pin two contracts:
 1. The operator-curated on-disk allowlist behaves as documented in
    internal/spec-mcp-administration-2026-05-10.md §3.2.
 2. The Evolve-shipped allowlist (added in the 2026-06-04 quality-
    control pass) recognizes MCPs that are part of the platform
    itself — today, evo's evo_tools MCP. Operator on-disk allowlist
    edits are additive on top of the shipped set.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from mcp_admin import allowlist as al  # noqa: E402


def test_empty_allowlist_does_not_recognize_unknown_server():
    a = al.Allowlist()
    assert a.entry_for("any_bot", "third_party_mcp") is None


def test_operator_allowlist_entry_recognized():
    a = al.Allowlist(entries=[
        al.AllowlistEntry(
            name="third_party_mcp", bot_id="team_bot_a",
            config_signature=None, notes="operator-added",
        )
    ])
    assert a.entry_for("team_bot_a", "third_party_mcp") is not None
    # Different bot — same name should miss because the entry is bot-scoped
    assert a.entry_for("team_bot_b", "third_party_mcp") is None


def test_pod_wide_operator_entry_matches_every_bot():
    a = al.Allowlist(entries=[
        al.AllowlistEntry(
            name="third_party_mcp", bot_id="*",
            config_signature=None, notes="operator-added pod-wide",
        )
    ])
    assert a.entry_for("team_bot_a", "third_party_mcp") is not None
    assert a.entry_for("team_bot_b", "third_party_mcp") is not None


def test_evo_tools_recognized_on_every_bot_via_shipped_entry():
    """evo's evo_tools MCP is part of the Evolve platform itself
    (post evo-account-separation, 2026-05-30). The shipped allowlist
    must recognize it on any bot without requiring the operator to
    add an entry to the on-disk allowlist — the pre-2026-06-04
    behavior fired 'unknown MCP server' on the primary bot from day
    one of the account-separation deploy."""
    a = al.Allowlist()  # empty operator allowlist
    entry = a.entry_for("evolve", "evo_tools")
    assert entry is not None, (
        "evo_tools must be recognized via EVOLVE_SHIPPED_ENTRIES even "
        "with an empty operator allowlist — see "
        "internal/diagnosis-oc-noisy-advisories-2026-06-04.md"
    )
    assert entry.name == "evo_tools"
    # The shipped entry is pod-wide (bot_id="*") so any bot recognizes it
    assert a.entry_for("admin_bot", "evo_tools") is not None
    assert a.entry_for("team_bot_a", "evo_tools") is not None


def test_evolve_shipped_entries_pinned_to_minimal_set():
    """Guard against accidental expansion of the shipped allowlist.
    Adding to EVOLVE_SHIPPED_ENTRIES is a platform-level decision
    (the corresponding MCP must be part of Evolve itself); operator-
    installed servers belong in the on-disk allowlist instead.
    Pin the set so a stray addition trips this test rather than
    silently expanding the trust surface."""
    names = sorted(e.name for e in al.EVOLVE_SHIPPED_ENTRIES)
    assert names == ["evo_tools"], (
        f"Unexpected shipped MCP entries: {names}. Adding a new entry "
        "here means we're shipping that MCP as part of Evolve itself. "
        "If it's an operator-installed server, use the on-disk "
        "allowlist instead."
    )


def test_operator_allowlist_precedence_over_shipped():
    """Operator entries are checked first — so an operator can
    override the shipped notes (or any other metadata) by adding a
    matching entry on disk. Today that's a no-op for evo_tools
    (shipped entry is pod-wide), but the behavior matters for any
    future shipped entries that are bot-scoped."""
    a = al.Allowlist(entries=[
        al.AllowlistEntry(
            name="evo_tools", bot_id="*",
            config_signature=None, notes="operator override",
        )
    ])
    entry = a.entry_for("evolve", "evo_tools")
    assert entry is not None
    assert entry.notes == "operator override", (
        "Operator-allowlist entries must take precedence over the "
        "Evolve-shipped allowlist — entry_for checks operator first"
    )
