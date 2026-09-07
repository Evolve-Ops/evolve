"""Tests for ``applications.apps_inherit_bot_llm_validator``.

Covers the four violation classes plus the gallery wire-up. The validator
is the install-time gate for docs/principle-apps-inherit-bot-llm.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.apps_inherit_bot_llm_validator import (  # noqa: E402
    validate_apps_inherit_bot_llm,
)


# ── Clean manifest cases ─────────────────────────────────────────────────────


def test_manifest_with_no_recursive_llm_is_clean() -> None:
    """An app that doesn't use LLM at all has no recursive_llm block; the
    validator must let it through with no violations."""
    pkg = {"id": "p-no-llm", "name": "Quiet app"}
    result = validate_apps_inherit_bot_llm(pkg)
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["severity"] == "info"


def test_manifest_with_valid_openclaw_headless_transport_is_clean() -> None:
    """The Atlas post-rearchitect shape: purposes + openclaw_headless
    transport + no api_key_source. Validator must approve."""
    pkg = {
        "id": "p-atlas-daily-digest",
        "recursive_llm": {
            "purposes": [
                {"name": "classifier", "intent": "classify into 5 buckets"}
            ],
            "transport": "openclaw_headless",
            "fallback_required": True,
        },
    }
    result = validate_apps_inherit_bot_llm(pkg)
    assert result["ok"] is True


def test_manifest_with_valid_bot_tool_transport_is_clean() -> None:
    pkg = {
        "recursive_llm": {
            "purposes": [{"name": "classifier", "intent": "classify"}],
            "transport": "bot_tool",
            "fallback_required": True,
        }
    }
    assert validate_apps_inherit_bot_llm(pkg)["ok"] is True


def test_manifest_with_valid_subagent_transport_is_clean() -> None:
    pkg = {
        "recursive_llm": {
            "purposes": [{"name": "classifier", "intent": "classify"}],
            "transport": "subagent",
            "fallback_required": True,
        }
    }
    assert validate_apps_inherit_bot_llm(pkg)["ok"] is True


# ── Violation #1: api_key_source declared ────────────────────────────────────


def test_api_key_source_is_blocked() -> None:
    """The Atlas pre-rearchitect shape: api_key_source pointing at a
    workspace credential file. This is the canonical anti-pattern the
    gate exists to catch."""
    pkg = {
        "recursive_llm": {
            "purposes": [{"name": "classifier", "model": "claude-haiku-4-5-20251001"}],
            "api_key_source": "atlas/llm-config.json",
            "fallback_required": True,
        }
    }
    result = validate_apps_inherit_bot_llm(pkg)
    assert result["ok"] is False
    assert result["severity"] == "build_blocker"
    assert any("api_key_source" in e for e in result["errors"])
    assert "atlas/llm-config.json" in result["message"]


def test_message_points_at_spec_and_principle() -> None:
    pkg = {"recursive_llm": {"api_key_source": "x/y.json"}}
    msg = validate_apps_inherit_bot_llm(pkg)["message"]
    assert "principle-apps-inherit-bot-llm" in msg
    assert "spec-apps-inherit-bot-llm-2026-06-06" in msg


# ── Violation #2: purposes set but no transport ──────────────────────────────


def test_purposes_without_transport_is_blocked() -> None:
    pkg = {
        "recursive_llm": {
            "purposes": [{"name": "classifier", "intent": "classify"}],
            "fallback_required": True,
        }
    }
    result = validate_apps_inherit_bot_llm(pkg)
    assert result["ok"] is False
    assert any("transport" in e for e in result["errors"])


def test_empty_purposes_with_no_transport_is_clean() -> None:
    """An empty recursive_llm.purposes (or absent) doesn't need a
    transport — the app doesn't use LLM."""
    pkg = {"recursive_llm": {"purposes": [], "fallback_required": False}}
    assert validate_apps_inherit_bot_llm(pkg)["ok"] is True


# ── Violation #3: invalid transport value ────────────────────────────────────


@pytest.mark.parametrize("bad_transport", [
    "anthropic_direct",   # the anti-pattern
    "openai_api",         # also bad
    "openclaw -p",        # the Phase-1 guess that didn't exist
    "openclaw_prompt",    # near-miss
    "",                   # empty string isn't allowed when purposes is set
    "BOT_TOOL",           # case-sensitive
])
def test_invalid_transport_value_is_blocked(bad_transport: str) -> None:
    pkg = {
        "recursive_llm": {
            "purposes": [{"name": "classifier", "intent": "classify"}],
            "transport": bad_transport,
            "fallback_required": True,
        }
    }
    result = validate_apps_inherit_bot_llm(pkg)
    assert result["ok"] is False
    assert any("transport" in e for e in result["errors"])


# ── Violation #4: credential template in files[] ─────────────────────────────


def test_llm_config_template_in_files_is_blocked() -> None:
    """Even if api_key_source is gone, shipping a credential template
    in files[] would carry the credential pattern back in. Catch it."""
    pkg = {
        "recursive_llm": {
            "purposes": [{"intent": "classify"}],
            "transport": "openclaw_headless",
        },
        "files": [
            {"path": "scripts/foo.py", "layer": "script"},
            {"path": "atlas/llm-config.json", "layer": "data", "data_kind": "template"},
        ],
    }
    result = validate_apps_inherit_bot_llm(pkg)
    assert result["ok"] is False
    assert any("llm-config" in e for e in result["errors"])


@pytest.mark.parametrize("bad_path", [
    "atlas/llm-config.json",
    "myapp/api-key.json",
    "config/anthropic-key.yaml",
    "secrets/openai-key.toml",
])
def test_credential_hint_paths_are_blocked(bad_path: str) -> None:
    pkg = {
        "recursive_llm": {
            "purposes": [{"intent": "classify"}],
            "transport": "openclaw_headless",
        },
        "files": [{"path": bad_path, "layer": "data"}],
    }
    assert validate_apps_inherit_bot_llm(pkg)["ok"] is False


def test_non_data_layer_files_are_not_flagged() -> None:
    """A script named `llm-config.py` is not a credential template, just
    a poorly-named script. The validator only flags layer=='data' entries."""
    pkg = {
        "recursive_llm": {
            "purposes": [{"intent": "classify"}],
            "transport": "openclaw_headless",
        },
        "files": [{"path": "scripts/llm-config-helper.py", "layer": "script"}],
    }
    assert validate_apps_inherit_bot_llm(pkg)["ok"] is True


# ── Manifest-like (object) input ─────────────────────────────────────────────


def test_validator_accepts_manifest_like_object() -> None:
    """The validator must accept an ApplicationManifest-style object as
    well as a raw dict, since gallery.preflight_check and forge_engine
    each pass different shapes."""

    class FakeManifest:
        def __init__(self) -> None:
            self.recursive_llm = {
                "purposes": [{"intent": "classify"}],
                "api_key_source": "atlas/llm-config.json",
            }
            self.files = []

    result = validate_apps_inherit_bot_llm(FakeManifest())
    assert result["ok"] is False


def test_validator_falls_back_to_raw_attr_on_manifest_object() -> None:
    """ApplicationManifest stores its full shape under `.raw` when the
    typed fields aren't set. Validator must look there too."""

    class RawOnlyManifest:
        def __init__(self) -> None:
            self.raw = {
                "recursive_llm": {
                    "purposes": [{"intent": "classify"}],
                    "transport": "openclaw_headless",
                }
            }

    assert validate_apps_inherit_bot_llm(RawOnlyManifest())["ok"] is True


# ── Atlas post-rearchitect regression guard ──────────────────────────────────


def test_atlas_post_rearchitect_manifests_pass() -> None:
    """The four Phase-2 Atlas manifests must pass the validator. This
    test pins the rearchitect; any future regression that re-introduces
    api_key_source into them will fire here."""
    import json
    repo_root = Path(__file__).resolve().parents[3]
    manifest_dir = repo_root / "docs" / "atlas-app-manifests"
    expected = [
        "atlas-daily-digest.json",
        "atlas-article-capture.json",
        "atlas-on-demand-research.json",
        "atlas-weekly-recap.json",
    ]
    for name in expected:
        path = manifest_dir / name
        if not path.exists():
            pytest.skip(f"{name} not present in this checkout")
        pkg = json.loads(path.read_text())
        result = validate_apps_inherit_bot_llm(pkg)
        assert result["ok"], (
            f"{name} regressed onto the apps-inherit-bot-llm anti-pattern: "
            f"{result['message']}"
        )
