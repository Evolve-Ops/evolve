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
