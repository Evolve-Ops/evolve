"""tests/test_forge_discoverability_warn.py — integration between forge
apply and the analyzer's discoverability assertion.

Phase 5a of the apply path (added in
docs/manifest-authoring-guide.md §5.22) runs `check_discoverability` from
`packages/analyzer/app_audit_structural.py` against the freshly-applied
manifest. The check operates on a manifest dict; the apply path produces
an ApplicationManifest dataclass; the integration is `manifest.to_dict()`.

These tests pin that integration: if the dataclass field names drift away
from what the analyzer's check reads (or vice versa), one of these tests
breaks instead of the warning silently disappearing in production.

The check's own field-by-field behavior is covered by
``packages/analyzer/tests/test_app_audit_structural_discoverability.py``;
that test file is the source of truth for the assertion. This file only
covers the cross-package wiring.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (str(_ADMIN), str(_ANALYZER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402

from app_audit_structural import check_discoverability  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _healthy_manifest() -> ApplicationManifest:
    """Mirrors the worked example in manifest-authoring-guide.md §5.22 —
    every field the renderer reads is populated."""
    return ApplicationManifest(
        id="test-app",
        name="Test App",
        bot_id="test-bot",
        description="Captures the user's morning thoughts into a daily journal.",
        identity={"purpose": "personal journaling helper"},
        usage={
            "model": "user-initiated",
            "how_to_use": "When the user wants to log a thought, call journal-add.",
            "trigger_recognition": {
                "hint_words": ["journal", "log", "capture", "remember"],
            },
            "bot_voice_examples": ["Logged to your journal."],
        },
        example_triggers=["log this: …", "add to journal: …"],
        interface_contract={
            "cli": [{"command": "journal-add", "key_flags": ["--text"]}],
        },
    )


def _thin_manifest() -> ApplicationManifest:
    """A forge-produced manifest that passes structural checks but is
    conversationally invisible — no usage block, no example_triggers,
    no CLI surface. The shape forge has historically emitted when the
    builder prompt under-specified the routing fields."""
    return ApplicationManifest(
        id="thin-app",
        name="Thin App",
        bot_id="test-bot",
        description="Does a thing.",   # description is set so prose check passes
    )


# ── Wiring tests ────────────────────────────────────────────────────────────


def test_healthy_manifest_to_dict_passes_check() -> None:
    """manifest.to_dict() produces the field shape the analyzer's check
    reads. Healthy in → no findings out."""
    m = _healthy_manifest()
    findings = check_discoverability(m.to_dict(), {})
    assert findings == [], (
        "healthy manifest produced findings — likely a schema drift between "
        "ApplicationManifest fields and check_discoverability's field reads. "
        f"Findings: {[(f.assertion_id, f.summary) for f in findings]}"
    )


def test_thin_manifest_to_dict_produces_expected_findings() -> None:
    """A thin manifest exercises every routing-only finding so we catch
    integration drift on each assertion the apply-time warn surfaces."""
    m = _thin_manifest()
    findings = check_discoverability(m.to_dict(), {})
    ids = {f.assertion_id for f in findings}
    # description is set, so app_discoverability_no_how_to_use does NOT fire
    assert "app_discoverability_no_how_to_use" not in ids
    # All four routing checks should fire (usage.model unset → treated as
    # user-routed, so the floor applies)
    assert "app_discoverability_no_invocation_model" in ids
    assert "app_discoverability_thin_hint_words" in ids
    assert "app_discoverability_no_example_triggers" in ids
    assert "app_discoverability_no_cli" in ids


def test_thin_manifest_finding_shape_matches_phase5a_log_format() -> None:
    """Phase 5a in forge_engine.py builds log lines and a context_snapshot
    summary from f.assertion_id / f.severity / f.summary. This test pins
    that shape — if any of those Finding attribute names change, the
    Phase 5a logging breaks silently."""
    m = _thin_manifest()
    findings = check_discoverability(m.to_dict(), {})
    assert findings, "expected findings on thin manifest"
    for f in findings:
        # These are the three attributes Phase 5a reads. They must all
        # be present and non-empty for the log to render usefully.
        assert getattr(f, "assertion_id", None), f"{f!r} missing assertion_id"
        assert getattr(f, "severity", None), f"{f!r} missing severity"
        assert getattr(f, "summary", None), f"{f!r} missing summary"


def test_scheduled_model_skips_routing_findings_in_integration() -> None:
    """Schedule-only apps (cron-driven, no user routing) shouldn't trip the
    routing-floor checks. Confirms the model-aware skip survives the
    dataclass → dict round-trip."""
    m = _thin_manifest()
    m.usage = {"model": "scheduled"}
    findings = check_discoverability(m.to_dict(), {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_no_invocation_model" not in ids
    assert "app_discoverability_thin_hint_words" not in ids
    assert "app_discoverability_no_example_triggers" not in ids
    assert "app_discoverability_no_cli" not in ids


def test_capability_tags_count_toward_hint_floor_via_dataclass() -> None:
    """The dict-level test covers this for explicit usage.trigger_recognition;
    this one confirms the fallback through ApplicationManifest's
    capability_tags + session_keywords fields (separate dataclass attrs)
    survives the to_dict() conversion."""
    m = _thin_manifest()
    m.capability_tags = ["note", "todo"]
    m.session_keywords = ["remember", "log"]
    findings = check_discoverability(m.to_dict(), {})
    ids = {f.assertion_id for f in findings}
    assert "app_discoverability_thin_hint_words" not in ids
