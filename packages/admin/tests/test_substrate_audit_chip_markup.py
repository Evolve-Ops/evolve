"""Markup sanity check for the substrate audit chip helpers in index.html.

We don't run a JS engine here; instead we assert the index.html source
contains the expected element ids, function names, and inline-handler
hooks the chip pattern relies on. Catches regressions where a future
refactor removes one of the helpers the catalog/oauth code paths call.

Updated post-fix to assert chips live on the per-bot management surfaces
(Plugins → Plugins, Settings → OAuth providers, Skills → Installed
overlay) rather than the Skills → Browse catalog, which is pure discovery.
"""
from __future__ import annotations

from pathlib import Path

_WEB = Path(__file__).resolve().parents[1].parent / "admin" / "evolve_admin" / "web"
_INDEX = _WEB / "index.html"
_PLUGINS_JS = _WEB/ "static" / "js" / "pages" / "plugins.js"
_SKILLS_JS = _WEB / "static" / "js" / "pages" / "skills.js"
_CREDENTIALS_JS = _WEB / "static" / "js" / "pages" / "credentials.js"


def _index_text() -> str:
    # Skills page chip helpers live in pages/skills.js (Phase 3r); the
    # OAuth-row chip invocation (ikRenderKeys) lives in pages/credentials.js
    # (Phase 3ad). Concat all so the existing regex-shape assertions
    # stay valid.
    return (
        _INDEX.read_text()
        + "\n"
        + _SKILLS_JS.read_text()
        + "\n"
        + _PLUGINS_JS.read_text()
        + "\n"
        + _CREDENTIALS_JS.read_text()
    )


def test_substrate_audit_helpers_present() -> None:
    src = _index_text()
    # The helper functions the chip cluster depends on must exist.
    assert "function _renderSubstrateAuditRows(" in src
    assert "function _substrateAuditPill(" in src
    assert "async function _runSubstrateAuditNow(" in src
    assert "async function _setSubstrateAuditCadence(" in src
    assert "async function _openSubstrateTrail(" in src


def test_chip_cluster_not_invoked_from_skills_catalog() -> None:
    src = _index_text()
    # The catalog (Skills → Browse) is pure discovery. Audit chips here
    # were the regression PR #1222 introduced; the fix moves them to the
    # per-bot Plugins page. Guard against re-introduction.
    assert "_renderSubstrateAuditRows('skill', skillId, installedBots)" not in src


def test_chip_invoked_from_plugins_tab() -> None:
    src = _index_text()
    # Plugins → Plugins is the per-bot home for skill audit chips.
    assert "function _ikPluginAuditCellHtml(" in src
    assert "function _ikPluginAuditActionsHtml(" in src
    assert "function _ikAuditSkillsForPlugin(" in src
    # The plugin row HTML calls into the new chip-cell helper.
    assert "_ikPluginAuditCellHtml(bot, e.name)" in src


def test_chip_cluster_invoked_from_oauth_row() -> None:
    src = _index_text()
    # OAuth provider rows render the cluster for the active row's bot.
    # OAuth providers were already on a per-bot surface (Credentials tab)
    # so this assertion is unchanged.
    assert "_renderSubstrateAuditRows('provider', k.provider, [botId])" in src


def test_skills_installed_cell_overlays_audit_health() -> None:
    src = _index_text()
    # Skills → Installed cells overlay audit health on the inventory badge.
    assert "function _skillsInstalledCellBadge(" in src
    assert "Configured ⚠" in src  # warn glyph for findings
    assert "Configured ❌" in src  # red glyph for audit-failed


def test_pill_labels_match_spec() -> None:
    src = _index_text()
    # Plex-test the visible copy. The user task spec enumerated these
    # four states with this wording.
    assert "never audited" in src
    assert "audit failed" in src
    assert "✓ healthy" in src
    assert "Run audit now" in src


def test_cadence_options_inherit_through_quarterly() -> None:
    src = _index_text()
    # The dropdown must include all 6 cadence options.
    for label in ("inherit pod default", "never (manual only)",
                  "daily", "weekly", "monthly", "quarterly"):
        assert label in src, f"missing cadence option: {label}"


def test_chip_load_invoked_on_plugins_activate() -> None:
    src = _index_text()
    # loadIntegrationsKeys (Plugins page activator) loads both skill and
    # provider audit data. skillsLoadPod (Skills → Installed) loads skill
    # data for its cell-overlay.
    assert "_loadSubstrateAuditStatusForAllBots('skill')" in src
    assert "_loadSubstrateAuditStatusForAllBots('provider')" in src
