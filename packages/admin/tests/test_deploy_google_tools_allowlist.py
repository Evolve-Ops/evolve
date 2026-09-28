"""tests/test_deploy_google_tools_allowlist.py — deploy-time exposure of the
Google tools past a curated tool profile.

A Google-configured bot whose ``tools.profile`` is a curated baseline (e.g.
``"coding"``) has the Evolve plugin's runtime-registered Google tools filtered
OUT of the agent's executable toolset — the agent calls ``gmail_list_messages``
and gets "Tool gmail_list_messages not found" (confirmed live 2026-06-22:
profile-less bots dispatch Google fine; ``coding``-profile bots get 100%
not-found). ``deploy._ensure_google_tools_allowlisted`` re-exposes them by
adding the tool names to ``tools.alsoAllow`` (the additive "merged on top of
the profile" list — NOT ``tools.allow``, which replaces the profile).

Pairs with the capability-AWARENESS fix (the agent now knows to call the tool);
this is the tool-POLICY half.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from evolve_admin import google_service  # noqa: E402
from evolve_admin.google_tools_policy import (  # noqa: E402
    ensure_google_tools_allowlisted as _ensure_google_tools_allowlisted,
)


def _google_network(mode: str = "free_gmail_oauth") -> dict:
    return {"bots": {"atlas": {"google_integration": {"mode": mode}}}}


_ALL_GOOGLE_TOOLS = sorted(google_service.TOOL_SPECS.keys())


class TestEnsureGoogleToolsAllowlisted:
    def test_adds_all_google_tools_for_coding_profile_bot(self):
        cfg = {"tools": {"profile": "coding"}}
        changed = _ensure_google_tools_allowlisted(cfg, "atlas", _google_network())
        assert changed is True
        allow = cfg["tools"]["alsoAllow"]
        # Every registered Google tool is present, and the profile is untouched.
        assert set(_ALL_GOOGLE_TOOLS).issubset(set(allow))
        assert "gmail_list_messages" in allow and "drive_search" in allow
        assert cfg["tools"]["profile"] == "coding"
        # Uses alsoAllow (additive), never the exclusive allow.
        assert "allow" not in cfg["tools"]

    def test_idempotent(self):
        cfg = {"tools": {"profile": "coding"}}
        assert _ensure_google_tools_allowlisted(cfg, "atlas", _google_network()) is True
        before = list(cfg["tools"]["alsoAllow"])
        # Second run is a no-op — no duplicates, no change.
        assert _ensure_google_tools_allowlisted(cfg, "atlas", _google_network()) is False
        assert cfg["tools"]["alsoAllow"] == before

    def test_unions_with_existing_alsoallow_without_clobbering(self):
        cfg = {"tools": {"profile": "coding", "alsoAllow": ["skill_workshop"]}}
        changed = _ensure_google_tools_allowlisted(cfg, "atlas", _google_network())
        assert changed is True
        allow = cfg["tools"]["alsoAllow"]
        assert "skill_workshop" in allow  # operator-set entry preserved
        assert "gmail_send" in allow
        assert allow[0] == "skill_workshop"  # existing entries kept in front

    def test_noop_when_google_not_configured(self):
        cfg = {"tools": {"profile": "coding"}}
        changed = _ensure_google_tools_allowlisted(cfg, "atlas", {"bots": {"atlas": {}}})
        assert changed is False
        assert "alsoAllow" not in cfg["tools"]

    def test_noop_for_unsupported_google_mode(self):
        cfg = {"tools": {}}
        changed = _ensure_google_tools_allowlisted(
            cfg, "atlas", _google_network(mode="bogus_mode")
        )
        assert changed is False
        assert cfg["tools"].get("alsoAllow") is None

    def test_applies_regardless_of_profile_presence(self):
        # No profile at all → still adds (harmless; additive). Gate is
        # google_integration, not profile.
        cfg: dict = {}
        changed = _ensure_google_tools_allowlisted(cfg, "atlas", _google_network())
        assert changed is True
        assert "gmail_list_messages" in cfg["tools"]["alsoAllow"]

    def test_soft_fails_when_google_service_raises(self, monkeypatch):
        # An import/lookup quirk must never abort a deploy.
        monkeypatch.setattr(
            google_service, "is_google_configured",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        cfg = {"tools": {"profile": "coding"}}
        assert _ensure_google_tools_allowlisted(cfg, "atlas", _google_network()) is False
        assert "alsoAllow" not in cfg["tools"]

    def test_alsoallow_entries_are_real_oc_schema_tool_names(self):
        # The names must match what the plugin registers (GoogleTools TOOL_DEFS /
        # the plugin manifest contracts.tools) so the allowlist actually matches.
        cfg: dict = {}
        _ensure_google_tools_allowlisted(cfg, "atlas", _google_network())
        # Spot-check the canonical Google tool surface.
        for name in ("gmail_list_messages", "gmail_send", "calendar_list_events",
                     "drive_list_files", "drive_search"):
            assert name in cfg["tools"]["alsoAllow"]


class TestAutonomyPluginGmailSurfaceParity:
    """The autonomy ladder declares the plugin Gmail surface as data in
    ``autonomy.catalog._PLUGIN_GMAIL_TOOLS`` (so analyzer-side discovery
    stays free of a cross-package import). This guards it against drift
    from the upstream source of truth, ``google_service.TOOL_SPECS``."""

    def test_catalog_plugin_gmail_tools_match_google_service(self):
        from autonomy import catalog as _acat

        upstream_gmail = {
            n for n in google_service.TOOL_SPECS if n.startswith("gmail_")
        }
        declared = set(_acat._PLUGIN_GMAIL_TOOLS)
        assert declared == upstream_gmail, (
            "autonomy.catalog._PLUGIN_GMAIL_TOOLS drifted from "
            "google_service.TOOL_SPECS gmail_* entries; sync them "
            f"(catalog-only: {declared - upstream_gmail}; "
            f"upstream-only: {upstream_gmail - declared})"
        )

    def test_outward_gmail_tools_are_classified(self):
        # The write-outward gmail tools must classify into email verbs, or
        # the ladder would fail to lock them down. Both delete forms count:
        # gmail_delete_message (permanent) and gmail_trash_message (the
        # recoverable trash, spec §1.4) classify as ``delete``.
        from autonomy import catalog as _acat

        email = _acat.KIND_SPECS["email"]
        assert _acat.classify_tool(email, "gmail_send") == "send"
        assert _acat.classify_tool(email, "gmail_delete_message") == "delete"
        assert _acat.classify_tool(email, "gmail_trash_message") == "delete"
        # The general labeling tool stays benign (read/modify-tier) — denying
        # it wholesale would break the promised "labels, files" capability.
        assert _acat.classify_tool(email, "gmail_label_message") is None

    def test_every_outward_email_tool_is_covered_by_the_binding(self):
        # Defense against a future upstream tool that classifies outward
        # (send/forward/delete) but escapes the plugin binding's
        # tool_filter — e.g. a non-``gmail_``-prefixed ``send_mail`` or a
        # new ``gmail_forward_message`` not yet in _PLUGIN_GMAIL_TOOLS.
        # Such a tool would be exposed in alsoAllow yet never denied at
        # draft_only — a silent widening. Every outward-classified
        # email-shaped TOOL_SPEC must be claimed by the binding.
        from fnmatch import fnmatchcase

        from autonomy import catalog as _acat

        email = _acat.KIND_SPECS["email"]
        binding = _acat.INTEGRATION_BINDINGS[_acat.PLUGIN_GMAIL_INTEGRATION_ID]
        for name in google_service.TOOL_SPECS:
            verb = _acat.classify_tool(email, name)
            if verb in email.outward_verbs:
                covered = any(fnmatchcase(name, p) for p in binding.tool_filter)
                assert covered and name in binding.known_tools, (
                    f"outward email tool {name!r} ({verb}) is exposed in "
                    "alsoAllow but not claimed by the plugin Gmail binding "
                    "— it would never be denied at draft_only"
                )
