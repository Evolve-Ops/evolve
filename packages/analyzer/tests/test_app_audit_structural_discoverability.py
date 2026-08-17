"""Unit tests for the v21 discoverability assertion.

The discoverability check mirrors app_registry.render_installed_apps_md —
fields that the renderer would emit as empty cause findings here. This is
the "is the bot LLM aware of this app at all" gate, complementary to the
structural checks that verify on-disk artifacts.

Pure function over (manifest, ctx). No filesystem, no Signal store, no LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_structural import (  # noqa: E402
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    check_discoverability,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _healthy_user_routed_manifest() -> dict:
    """A manifest that passes every discoverability check.

    All routing fields populated; usage.model = user-initiated. Tests below
    derive degraded manifests by selectively stripping fields off this base.
    """
    return {
        "id": "test-app",
        "name": "Test App",
        "description": "Captures the user's morning thoughts into a daily journal.",
        "identity": {"purpose": "personal journaling helper"},
        "usage": {
            "model": "user-initiated",
            "how_to_use": "When the user wants to log a thought, call journal-add.",
            "trigger_recognition": {
                "hint_words": ["journal", "log", "capture", "remember"],
            },
            "bot_voice_examples": ["Logged to your journal."],
        },
        "example_triggers": ["log this: …", "add to journal: …"],
        "interface_contract": {
            "cli": [{"command": "journal-add", "key_flags": ["--text", "--tag"]}],
        },
    }


# ── Healthy baseline ────────────────────────────────────────────────────────


def test_healthy_manifest_produces_no_findings() -> None:
    findings = check_discoverability(_healthy_user_routed_manifest(), {})
    assert findings == []


# ── Per-field gaps (user-routed) ────────────────────────────────────────────


def test_missing_usage_model_is_minor() -> None:
    m = _healthy_user_routed_manifest()
    del m["usage"]["model"]
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_invocation_model" in ids
    no_model = next(f for f in findings if f.assertion_id == "app_discoverability_no_invocation_model")
    assert no_model.severity == SEVERITY_MINOR


def test_missing_all_prose_is_major() -> None:
    """When how_to_use AND description AND identity.purpose are all empty,
    the LLM has no prose at all — the renderer falls back to a placeholder."""
    m = _healthy_user_routed_manifest()
    m["usage"].pop("how_to_use", None)
    m["description"] = ""
    m["identity"]["purpose"] = ""
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_how_to_use" in ids
    f = next(f for f in findings if f.assertion_id == "app_discoverability_no_how_to_use")
    assert f.severity == SEVERITY_MAJOR


def test_description_fallback_satisfies_prose_requirement() -> None:
    """If how_to_use is empty but description is set, no finding — the
    renderer falls back to description."""
    m = _healthy_user_routed_manifest()
    m["usage"].pop("how_to_use", None)
    # description still set on the healthy baseline
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_how_to_use" not in ids


def test_thin_hint_words_is_major() -> None:
    """Below the floor → major. Counts the union of hint_words +
    capability_tags + session_keywords."""
    m = _healthy_user_routed_manifest()
    m["usage"]["trigger_recognition"]["hint_words"] = ["journal"]  # only 1
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_thin_hint_words" in ids
    f = next(f for f in findings if f.assertion_id == "app_discoverability_thin_hint_words")
    assert f.severity == SEVERITY_MAJOR
    assert f.evidence["count"] == 1


def test_thin_hint_words_uses_fallback_sources() -> None:
    """capability_tags + session_keywords count toward the floor when
    explicit hint_words are empty (matches the renderer's fallback)."""
    m = _healthy_user_routed_manifest()
    m["usage"]["trigger_recognition"]["hint_words"] = []
    m["capability_tags"] = ["note", "todo"]
    m["session_keywords"] = ["remember", "log"]
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_thin_hint_words" not in ids


def test_no_example_triggers_is_major() -> None:
    m = _healthy_user_routed_manifest()
    m["example_triggers"] = []
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_example_triggers" in ids
    f = next(f for f in findings if f.assertion_id == "app_discoverability_no_example_triggers")
    assert f.severity == SEVERITY_MAJOR


def test_empty_string_triggers_dont_count() -> None:
    m = _healthy_user_routed_manifest()
    m["example_triggers"] = ["", "   ", None]  # all invalid
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_example_triggers" in ids


def test_no_cli_is_major_for_user_routed() -> None:
    m = _healthy_user_routed_manifest()
    m["interface_contract"]["cli"] = []
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_cli" in ids
    f = next(f for f in findings if f.assertion_id == "app_discoverability_no_cli")
    assert f.severity == SEVERITY_MAJOR


def test_cli_entry_with_no_command_doesnt_count() -> None:
    m = _healthy_user_routed_manifest()
    m["interface_contract"]["cli"] = [{"key_flags": ["--whatever"]}]  # no command
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_cli" in ids


# ── Meta-installer carve-out ────────────────────────────────────────────────


def _meta_installer_manifest() -> dict:
    """app_dependencies-only manifest with no files, no scheduled_actions,
    no event_triggers, no CLI. Matches ea-pack's actual shape."""
    return {
        "id": "ea-pack",
        "name": "EA Pack",
        "description": "Curated bundle of executive-assistant behaviors.",
        "identity": {"purpose": "Package four single-purpose apps as one installable unit."},
        "usage": {
            "model": "user-initiated",
            "how_to_use": "Describe the bundle when the user asks what's in it.",
            "trigger_recognition": {
                "hint_words": ["EA pack", "EA bundle", "install everything"],
            },
        },
        "example_triggers": ["Install the EA pack", "What's in the EA bundle?"],
        "interface_contract": {"cli": []},
        "app_dependencies": [{"pkg_id": "p-x"}, {"pkg_id": "p-y"}],
        "files": [],
        "scheduled_actions": [],
        "event_triggers": [],
    }


def test_meta_installer_skips_no_cli_check() -> None:
    """Meta-installer (app_dependencies-only) has no CLI by design.
    The discoverability check should recognize the shape and skip the
    no_cli finding — otherwise every install of ea-pack fires a
    permanent residual signal."""
    findings = check_discoverability(_meta_installer_manifest(), {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_cli" not in ids
    # All other discoverability dimensions should still be checked —
    # the carve-out is narrow (CLI only).
    assert findings == [], (
        f"healthy meta-installer fired unexpected findings: {findings}"
    )


def test_meta_installer_still_requires_hint_words() -> None:
    """The carve-out is CLI-only. A meta-installer with thin hint_words
    still fires that finding — the bot still needs to route to the
    package when the user asks about it."""
    m = _meta_installer_manifest()
    m["usage"]["trigger_recognition"]["hint_words"] = []
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_thin_hint_words" in ids
    # Still no no_cli finding
    assert "app_discoverability_no_cli" not in ids


def test_non_meta_installer_with_files_still_fires_no_cli() -> None:
    """If the manifest has app_dependencies AND files (a hybrid), it is
    NOT a pure meta-installer — its own code needs a way to be invoked."""
    m = _meta_installer_manifest()
    m["files"] = [{"path": "scripts/installer.py"}]
    m["interface_contract"]["cli"] = []
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_cli" in ids


def test_app_dependencies_alone_doesnt_skip_check() -> None:
    """app_dependencies present but with scheduled_actions OR event_triggers
    means the manifest has runtime — not a meta-installer."""
    m = _meta_installer_manifest()
    m["scheduled_actions"] = [{"id": "x", "mechanism": "launchd"}]
    m["interface_contract"]["cli"] = []
    # model is user-initiated; the routing checks still run
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_cli" in ids


# ── usage.model-aware routing skips ─────────────────────────────────────────


def test_scheduled_skips_routing_checks() -> None:
    """Scheduled apps don't need hint_words / example_triggers / CLI for
    user routing — the bot relays their output, doesn't invoke them on
    intent. Only the prose check still applies (the LLM still needs to
    know what the scheduled output is about)."""
    m = _healthy_user_routed_manifest()
    m["usage"]["model"] = "scheduled"
    m["usage"]["trigger_recognition"]["hint_words"] = []
    m["example_triggers"] = []
    m["interface_contract"]["cli"] = []
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_thin_hint_words" not in ids
    assert "app_discoverability_no_example_triggers" not in ids
    assert "app_discoverability_no_cli" not in ids


def test_event_driven_skips_routing_checks() -> None:
    m = _healthy_user_routed_manifest()
    m["usage"]["model"] = "event-driven"
    m["usage"]["trigger_recognition"]["hint_words"] = []
    m["example_triggers"] = []
    m["interface_contract"]["cli"] = []
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_thin_hint_words" not in ids
    assert "app_discoverability_no_example_triggers" not in ids
    assert "app_discoverability_no_cli" not in ids


def test_ambient_still_requires_routing() -> None:
    """ambient apps decide invocation from conversation context — they
    still need the routing surface to recognize when to fire."""
    m = _healthy_user_routed_manifest()
    m["usage"]["model"] = "ambient"
    m["example_triggers"] = []
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_example_triggers" in ids


def test_unset_model_treats_as_user_routed() -> None:
    """No usage.model → assume user-routed (permissive interpretation, so
    the routing checks run). Also emits the no_invocation_model finding."""
    m = _healthy_user_routed_manifest()
    m["usage"].pop("model", None)
    m["example_triggers"] = []
    findings = check_discoverability(m, {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_invocation_model" in ids
    assert "app_discoverability_no_example_triggers" in ids


# ── Usage-block placement (top-level vs identity-nested) ────────────────────


def test_usage_nested_in_identity_is_read() -> None:
    """Mirrors app_registry._usage_block: usage can live at the top level
    or nested inside identity. Either works."""
    m = _healthy_user_routed_manifest()
    nested_usage = m.pop("usage")
    m["identity"]["usage"] = nested_usage
    findings = check_discoverability(m, {})
    assert findings == []


# ── Signature stability ─────────────────────────────────────────────────────


def test_finding_signatures_are_stable_per_dimension() -> None:
    """Each discoverability dimension produces a distinct signature so
    repeated runs converge on one Signal per dimension per app — not a
    new Signal each time the audit re-runs."""
    m = _healthy_user_routed_manifest()
    m["usage"]["trigger_recognition"]["hint_words"] = []
    m["capability_tags"] = []
    m["session_keywords"] = []
    m["example_triggers"] = []
    findings_a = check_discoverability(m, {})
    findings_b = check_discoverability(m, {})
    sigs_a = sorted(f.signature("bot-x", "app-y") for f in findings_a)
    sigs_b = sorted(f.signature("bot-x", "app-y") for f in findings_b)
    assert sigs_a == sigs_b
    # And different dimensions produce different signatures
    assert len(set(sigs_a)) == len(sigs_a)
