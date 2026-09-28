"""
test_oauth_orchestrator.py — Tests for the V2.1-1 oauth/orchestrator module.

These tests exercise the provider-registry-based orchestrator directly,
independent of V2-4's injected-reader path (which is covered by the existing
13 tests in test_gallery_oauth_orchestration.py).

Tests
-----
- test_evaluate_with_no_requirements — empty requirements → satisfied
- test_evaluate_with_satisfied_gog   — active GOG → satisfied
- test_evaluate_with_unsatisfied_gog — no GOG profile → not satisfied, missing=[gog]
- test_evaluate_with_unknown_integration — "slack" (not registered) → not satisfied,
  status="unknown_integration"
- test_provider_registry_is_singleton — same provider not registered twice
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path):
    """Minimal shared_dir with a network.json (no real bots)."""
    sd = tmp_path / "shared"
    sd.mkdir()
    import json
    (tmp_path / "network.json").write_text(json.dumps({"members": ["admin_bot"]}))
    return sd


def _active_readers():
    """Return injectable readers that report GOG as fully active."""
    return (
        lambda bot_id: True,   # plugin enabled
        lambda bot_id: {"status": "active", "google_account": "test@example.com",
                        "services": ["gmail_readonly", "calendar_readonly"]},
        lambda: True,          # client configured
    )


def _missing_readers():
    """Return injectable readers that report GOG as oauth_pending (plugin on, no profile)."""
    return (
        lambda bot_id: True,   # plugin enabled
        lambda bot_id: None,   # no OAuth profile → oauth_pending
        lambda: True,          # client configured
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_evaluate_with_no_requirements(shared_dir):
    """Empty requirements list returns satisfied=True, missing=[]."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    result = evaluate_install_prerequisites("admin_bot", {}, shared_dir=shared_dir)
    assert result["satisfied"] is True
    assert result["missing"] == []


def test_evaluate_with_no_integrations_key(shared_dir):
    """requirements dict with no 'integrations' key → satisfied."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    result = evaluate_install_prerequisites(
        "admin_bot", {"secrets": [], "python_packages": []}, shared_dir=shared_dir
    )
    assert result["satisfied"] is True
    assert result["missing"] == []


def test_evaluate_with_satisfied_gog(shared_dir):
    """Bot has GOG active → gmail and calendar requirements satisfied."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    plugin_fn, profile_fn, client_fn = _active_readers()

    requirements = {
        "integrations": [
            {"id": "gog", "display_name": "Google (GOG)", "required": True,
             "reason": "Reads Gmail and Google Calendar for briefing content"}
        ]
    }
    result = evaluate_install_prerequisites(
        "admin_bot", requirements,
        shared_dir=shared_dir,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result["satisfied"] is True
    assert result["missing"] == []


def test_evaluate_with_unsatisfied_gog(shared_dir):
    """Bot lacks GOG OAuth profile → satisfied=False, missing contains gog item."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    plugin_fn, profile_fn, client_fn = _missing_readers()

    requirements = {
        "integrations": [
            {"id": "gog", "display_name": "Google (GOG)", "required": True,
             "reason": "Reads Gmail and Google Calendar for briefing content"}
        ]
    }
    result = evaluate_install_prerequisites(
        "admin_bot", requirements,
        shared_dir=shared_dir,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result["satisfied"] is False
    assert len(result["missing"]) == 1
    item = result["missing"][0]
    assert item["integration_id"] == "gog"
    assert item["skill_id"] == "gog"
    assert item["action_url"] == "/api/skills/install/gog"
    assert item["action_label"] == "Set up Gmail & Calendar"
    assert "install_plan_steps" in item
    assert len(item["install_plan_steps"]) > 0


def test_evaluate_with_unknown_integration(shared_dir):
    """A requirement for a truly unregistered integration → satisfied=False,
    status='unknown_integration', action_url=None.

    Note: 'slack' was used here before V2.1-2 registered the Slack provider.
    Now using 'notion' (not yet a registered provider) as the unknown example.
    """
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    requirements = {
        "integrations": [
            {"id": "notion", "display_name": "Notion", "required": True,
             "reason": "Notes access via Notion"}
        ]
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    assert result["satisfied"] is False
    assert len(result["missing"]) == 1
    item = result["missing"][0]
    assert item["integration_id"] == "notion"
    assert item["status"] == "unknown_integration"
    assert item["action_url"] is None
    assert "notion" in item["action_label"].lower()


def test_optional_integration_is_skipped(shared_dir):
    """A requirement marked required:False is skipped entirely — neither
    satisfied nor missing. The manifest author has opted it out of the
    gating contract.

    Regression: pre-fix, the orchestrator iterated every integration and
    added unknown ones to the missing list regardless of the required
    flag. Atlas's manifest declares github as required:False but the
    orchestrator still reported it missing, blocking the install.
    """
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    requirements = {
        "integrations": [
            {"id": "github", "display_name": "GitHub", "required": False,
             "reason": "Optional release feed reader"},
        ],
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    # No required integrations → nothing missing → satisfied.
    assert result["satisfied"] is True
    assert result["missing"] == []


def _stub_plugins(monkeypatch, *, live_enabled=(), required=()):
    """Stub plugins.inventory.load_inventory + plugins.baseline.load so the
    orchestrator's v2 lookup paths produce the requested union of enabled
    plugin names.
    """
    import types
    fake_inv = types.SimpleNamespace(
        load_inventory=lambda shared_dir, bot_id: {
            "entries": [
                {"name": n, "enabled": True} for n in live_enabled
            ],
        },
    )
    fake_baseline_obj = types.SimpleNamespace(required_plugins=list(required))
    fake_bl = types.SimpleNamespace(load=lambda shared_dir: fake_baseline_obj)
    fake_plugins = types.ModuleType("plugins")
    fake_plugins.inventory = fake_inv
    fake_plugins.baseline = fake_bl
    monkeypatch.setitem(sys.modules, "plugins", fake_plugins)
    monkeypatch.setitem(sys.modules, "plugins.inventory", fake_inv)
    monkeypatch.setitem(sys.modules, "plugins.baseline", fake_bl)


def test_pod_invariant_plugin_is_satisfied(shared_dir, monkeypatch):
    """A plugin-only integration (no OAuth provider) is satisfied if the
    bot's live inventory has it enabled.

    Regression: pre-fix, the orchestrator treated brave as
    'unknown_integration' because there's no brave_provider — even though
    brave was visible as enabled in the Plugins UI.
    """
    _stub_plugins(monkeypatch, live_enabled=["brave", "evolve"])

    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    requirements = {
        "integrations": [
            {"id": "brave", "display_name": "Brave Search", "required": True,
             "reason": "Web search"},
        ],
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    assert result["satisfied"] is True
    assert result["missing"] == []


def test_pod_invariant_does_not_mask_real_missing(shared_dir, monkeypatch):
    """Live-inventory shortcut only applies when the integration_id matches
    a currently-enabled plugin. A different unregistered integration still
    gets flagged.
    """
    _stub_plugins(monkeypatch, live_enabled=["brave"])

    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    requirements = {
        "integrations": [
            {"id": "brave", "display_name": "Brave Search", "required": True},
            # notion isn't a registered provider and isn't pod-enabled
            {"id": "notion", "display_name": "Notion", "required": True},
        ],
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    assert result["satisfied"] is False
    # Only notion should appear missing; brave was satisfied by the pod-wide check.
    assert len(result["missing"]) == 1
    assert result["missing"][0]["integration_id"] == "notion"


def test_baseline_required_plugins_satisfies_integration(shared_dir, monkeypatch):
    """A required_plugins entry in the v2 baseline satisfies an
    integration even when the live inventory isn't populated. Covers the
    early-deploy window before the first inventory scan.
    """
    _stub_plugins(monkeypatch, live_enabled=[], required=["brave"])

    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    requirements = {
        "integrations": [
            {"id": "brave", "display_name": "Brave Search", "required": True},
        ],
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    assert result["satisfied"] is True


def test_baseline_unavailable_falls_back_to_missing(shared_dir, monkeypatch):
    """If the plugins.baseline module isn't importable (degraded env, repo
    checkout without the analyzer on the path), fall back to the previous
    behaviour of treating no-provider integrations as missing. We can't
    falsely claim 'satisfied' if we can't verify.
    """
    # Force the plugins import to fail
    import builtins
    real_import = builtins.__import__
    def _bad_import(name, *args, **kwargs):
        if name == "plugins" or name.startswith("plugins."):
            raise ImportError("simulated missing plugins package")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", _bad_import)

    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    requirements = {
        "integrations": [
            {"id": "brave", "display_name": "Brave Search", "required": True},
        ],
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    assert result["satisfied"] is False
    assert len(result["missing"]) == 1
    assert result["missing"][0]["integration_id"] == "brave"
    assert result["missing"][0]["status"] == "unknown_integration"


def test_provider_registry_is_singleton(shared_dir):
    """Importing gog_provider multiple times does not duplicate the registry entry."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY
    import importlib
    import evolve_admin.oauth.providers.gog_provider as _gog

    # Count GOG entries before re-importing
    gog_count_before = sum(1 for p in PROVIDER_REGISTRY if "gog" in p.integration_ids)

    # Re-import the module (simulates double import)
    importlib.reload(_gog)

    gog_count_after = sum(1 for p in PROVIDER_REGISTRY if "gog" in p.integration_ids)

    # Should still be exactly 1 — the guard prevents double registration
    assert gog_count_after == gog_count_before == 1


# ── S1: unknown-provider fallback + declared-alternatives consultation ────────
# Gallery framework audit 2026-07-02 S1: an integration id with no registered
# provider used to get an unconditional missing-item (action_url=None), which
# detoured every non-forced install into awaiting_oauth until the 30-min
# sweeper abandon — regardless of actual configuration.


_REPO_ROOT = _ADMIN_DIR.parent.parent
_CALENDAR_SYNC_PKG = _REPO_ROOT / "gallery" / "calendar-sync" / "p-fe9acef3.json"


def _patch_integration_checker(monkeypatch, fn):
    """Replace the gallery preflight satisfaction check the orchestrator
    falls back to. fn(bot_id, req) → (state, message)."""
    import evolve_admin.applications.gallery as gallery_mod
    monkeypatch.setattr(gallery_mod, "check_integration_requirement", fn)


def test_unknown_but_satisfied_integration_no_detour(shared_dir, monkeypatch):
    """Unknown-provider id whose preflight check says satisfied → no missing
    item, no awaiting_oauth detour."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    _patch_integration_checker(
        monkeypatch,
        lambda bot_id, req: ("satisfied", "Notion is configured at openclaw.json → integrations.notion."),
    )

    requirements = {
        "integrations": [
            {"id": "notion", "display_name": "Notion", "required": True,
             "check_path": "openclaw.json → integrations.notion"},
        ],
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    assert result["satisfied"] is True
    assert result["missing"] == []


def test_unknown_unsatisfied_carries_preflight_context(shared_dir, monkeypatch):
    """Unknown-provider id whose preflight check says missing → missing item
    that carries the preflight diagnostic and the manifest's setup_doc
    (not a bare null-action_url dead end)."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    diagnostic = (
        "Notion not configured (no 'integrations.notion' in openclaw.json). "
        "Setup: docs/integrations/notion.md"
    )
    _patch_integration_checker(monkeypatch, lambda bot_id, req: ("missing", diagnostic))

    requirements = {
        "integrations": [
            {"id": "notion", "display_name": "Notion", "required": True,
             "check_path": "openclaw.json → integrations.notion",
             "setup_doc": "docs/integrations/notion.md"},
        ],
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    assert result["satisfied"] is False
    assert len(result["missing"]) == 1
    item = result["missing"][0]
    assert item["status"] == "unknown_integration"
    assert item["action_url"] is None  # no install route exists for the id
    assert item["detail"] == diagnostic
    assert item["setup_doc"] == "docs/integrations/notion.md"


def test_unknown_checker_failure_preserves_old_behavior(shared_dir, monkeypatch):
    """If the preflight fallback itself blows up, keep the pre-fix fail-safe:
    treat the unknown integration as missing (never falsely satisfied)."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    def _boom(bot_id, req):
        raise RuntimeError("simulated gallery checker failure")
    _patch_integration_checker(monkeypatch, _boom)

    requirements = {
        "integrations": [
            {"id": "notion", "display_name": "Notion", "required": True},
        ],
    }
    result = evaluate_install_prerequisites("admin_bot", requirements, shared_dir=shared_dir)
    assert result["satisfied"] is False
    assert result["missing"][0]["status"] == "unknown_integration"


def test_google_calendar_alias_routes_to_calendar_provider():
    """The gallery-standard `google_calendar` id resolves to the calendar
    provider (short-term registry alias) instead of the unknown dead end."""
    from evolve_admin.oauth.providers import find_provider

    provider = find_provider("google_calendar")
    assert provider is not None
    assert provider.skill_id == "calendar"


def _calendar_sync_requirements() -> dict:
    """The real Calendar Sync requirements block — the exact shape that
    used to hang every non-forced install for 30 minutes."""
    import json as _json
    pkg = _json.loads(_CALENDAR_SYNC_PKG.read_text())
    integ = pkg["requirements"]["integrations"][0]
    # Guard against fixture drift: this test suite exists because of this
    # exact id + declared-alternatives shape.
    assert integ["id"] == "google_calendar"
    assert integ.get("alternatives"), "calendar-sync no longer declares alternatives"
    return pkg["requirements"]


def test_calendar_sync_missing_gets_actionable_link(shared_dir, monkeypatch):
    """Calendar Sync on a bot with nothing configured: missing item now
    carries the calendar provider's real install route, not action_url=None."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    _patch_integration_checker(monkeypatch, lambda bot_id, req: ("missing", "not configured"))
    plugin_fn, profile_fn, client_fn = _missing_readers()

    result = evaluate_install_prerequisites(
        "admin_bot", _calendar_sync_requirements(),
        shared_dir=shared_dir,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result["satisfied"] is False
    assert len(result["missing"]) == 1
    item = result["missing"][0]
    assert item["skill_id"] == "calendar"
    assert item["action_url"] == "/api/skills/install/calendar"


def test_calendar_sync_satisfied_via_path_c_alternative(shared_dir, monkeypatch):
    """Calendar Sync on a bot using path-C service-account auth: the calendar
    provider says missing (it only knows the OAuth-skill flow), but the
    declared google_path_c alternative satisfies the requirement."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    def _checker(bot_id, req):
        # Faithful to the real check_integration_requirement contract: it walks
        # the primary requirement THEN each declared alternative, first match
        # wins. So when handed the full google_calendar req (whose alternatives
        # include google_path_c) it resolves satisfied; when handed a bare alt
        # dict it matches on that dict's own id.
        for entry in [req, *(req.get("alternatives") or [])]:
            if entry.get("id") == "google_path_c":
                return "satisfied", "path-C service-account configured"
        return "missing", "not configured"
    _patch_integration_checker(monkeypatch, _checker)
    plugin_fn, profile_fn, client_fn = _missing_readers()

    result = evaluate_install_prerequisites(
        "admin_bot", _calendar_sync_requirements(),
        shared_dir=shared_dir,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result["satisfied"] is True
    assert result["missing"] == []


def test_calendar_sync_satisfied_via_legacy_primary_check_path(shared_dir, monkeypatch):
    """Finding-1 guard (adversarial review of PR #3403): a bot configured via
    the manifest's declared PRIMARY check_path — the legacy
    `integrations.google_calendar` bearer-token block the product's own setup
    UI tells operators to add — is satisfied even though the aliased calendar
    provider (which only knows the calendar OAuth-skill mechanism) reports
    missing. The google_calendar→calendar alias must not dead-end the
    legacy-auth cohort that the unknown-provider fallback alone would have
    covered.
    """
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    def _checker(bot_id, req):
        # Primary google_calendar check_path satisfied; path-C alt not needed.
        if req.get("id") == "google_calendar":
            return "satisfied", (
                "Google Calendar is configured at "
                "openclaw.json → integrations.google_calendar."
            )
        return "missing", "not configured"
    _patch_integration_checker(monkeypatch, _checker)
    plugin_fn, profile_fn, client_fn = _missing_readers()

    result = evaluate_install_prerequisites(
        "admin_bot", _calendar_sync_requirements(),
        shared_dir=shared_dir,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result["satisfied"] is True
    assert result["missing"] == []


def test_canonical_provider_not_overridden_by_primary_check_path(shared_dir, monkeypatch):
    """Companion to the aliased case: a CANONICAL provider (calendar reached by
    its own id) is NOT satisfied by a satisfied-looking primary check_path when
    the provider reports missing — only a declared alternative widens the gate.
    Guards against the hollow-config slip-past on plugin-presence check_paths
    (note-taker / calendar-summary use `plugins.entries.google + calendar
    scope`)."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    _patch_integration_checker(monkeypatch, lambda bot_id, req: ("satisfied", "plugin present"))
    plugin_fn, profile_fn, client_fn = _missing_readers()

    requirements = {
        "integrations": [
            {"id": "calendar", "display_name": "Google Calendar", "required": True,
             "check_path": "openclaw.json → plugins.entries.google + calendar scope"},
        ],
    }
    result = evaluate_install_prerequisites(
        "admin_bot", requirements,
        shared_dir=shared_dir,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result["satisfied"] is False
    assert result["missing"][0]["integration_id"] == "calendar"


def test_calendar_sync_satisfied_via_calendar_oauth(shared_dir, monkeypatch):
    """Calendar Sync on a bot with the calendar OAuth skill active: the
    provider itself reports satisfied; the preflight fallback is never
    needed (checker would say missing)."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    _patch_integration_checker(monkeypatch, lambda bot_id, req: ("missing", "not configured"))
    plugin_fn, profile_fn, client_fn = _active_readers()

    result = evaluate_install_prerequisites(
        "admin_bot", _calendar_sync_requirements(),
        shared_dir=shared_dir,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result["satisfied"] is True
    assert result["missing"] == []


def test_provider_verdict_not_overridden_by_primary_check_path(shared_dir, monkeypatch):
    """A registered provider stays authoritative for its own flow: with no
    declared alternatives, a satisfied-looking check_path does NOT override
    the provider's missing verdict (no slip-past for genuinely-unconnected
    integrations)."""
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

    _patch_integration_checker(monkeypatch, lambda bot_id, req: ("satisfied", "looks fine"))
    plugin_fn, profile_fn, client_fn = _missing_readers()

    requirements = {
        "integrations": [
            {"id": "gog", "display_name": "Google (GOG)", "required": True,
             "check_path": "openclaw.json → plugins.entries.google"},
        ],
    }
    result = evaluate_install_prerequisites(
        "admin_bot", requirements,
        shared_dir=shared_dir,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result["satisfied"] is False
    assert result["missing"][0]["integration_id"] == "gog"
