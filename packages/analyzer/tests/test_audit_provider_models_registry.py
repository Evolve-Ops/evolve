"""Tests for audit._check_provider_models_registry.

The check fires when ``agents.defaults.models`` carries a slug whose
matching entry is missing from ``models.providers[<prov>].models[]``.
The OC runtime requires the link only when ``models.providers`` exists
at all — bots that omit the section entirely (the typical member-bot
shape on the reference pod) use OC's implicit registry and don't trip
this failure mode, so the audit MUST NOT fire on them.

Reference incident: 2026-06-03 personal-bot. ``agents.defaults.models``
listed six provider/model slugs; ``models.providers`` had partial
coverage. OC's failover runtime exhausted the chain on every
background turn and 9M cache_write_tokens × 2 turns burned $36.42 in
sonnet pricing before the cost ledger surfaced the spike.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def _collect(oc: dict) -> list[audit.Finding]:
    findings: list[audit.Finding] = []
    audit._check_provider_models_registry("test_bot", oc, findings)
    return findings


def test_noop_when_agents_defaults_models_absent():
    """No ``agents.defaults.models`` → nothing to validate, no finding."""
    assert _collect({}) == []
    assert _collect({"agents": {}}) == []
    assert _collect({"agents": {"defaults": {}}}) == []
    assert _collect({"agents": {"defaults": {"models": {}}}}) == []


def test_noop_when_models_providers_absent_entirely():
    """The typical member-bot shape — agents.defaults.models is populated
    but ``models.providers`` is absent. OC uses its implicit registry
    in this case and the bot works fine, so the audit MUST NOT fire."""
    oc = {
        "agents": {"defaults": {"models": {
            "anthropic/claude-haiku-4-5": {},
            "anthropic/claude-sonnet-4-6": {},
        }}},
    }
    assert _collect(oc) == []


def test_noop_when_models_providers_empty_dict():
    """``models.providers`` = {} is functionally the same as absent —
    OC has nothing to enforce against. No finding."""
    oc = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {"providers": {}},
    }
    assert _collect(oc) == []


def test_noop_when_registry_fully_covers_agents_models():
    """Every slug in agents.defaults.models has a matching entry."""
    oc = {
        "agents": {"defaults": {"models": {
            "anthropic/claude-haiku-4-5": {},
            "anthropic/claude-sonnet-4-6": {},
            "google/gemini-2.5-pro": {},
        }}},
        "models": {"providers": {
            "anthropic": {"models": [
                {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
                {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6"},
            ]},
            "google": {"models": [
                {"id": "gemini-2.5-pro", "name": "gemini-2.5-pro"},
            ]},
        }},
    }
    assert _collect(oc) == []


def test_fires_when_provider_block_present_but_entry_missing():
    """The 2026-06-03 personal-bot failure mode —
    ``models.providers.anthropic`` exists but lacks the haiku entry
    that agents.defaults.models lists."""
    oc = {
        "agents": {"defaults": {"models": {
            "anthropic/claude-haiku-4-5": {},
            "anthropic/claude-sonnet-4-6": {},
        }}},
        "models": {"providers": {
            "anthropic": {"models": [
                # sonnet registered, haiku NOT
                {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6"},
            ]},
        }},
    }
    findings = _collect(oc)
    assert len(findings) == 1
    f = findings[0]
    assert f.level == "warn"
    assert f.category == "config"
    assert f.bot_id == "test_bot"
    assert "anthropic/claude-haiku-4-5" in f.detail
    assert "1" in f.message  # count of missing


def test_fires_when_provider_entirely_absent_but_block_exists():
    """A bot may register only one provider in ``models.providers`` and
    list models from other providers in agents.defaults.models. Once
    the block exists, every slug must be covered — missing providers
    fire too, not just missing model ids."""
    oc = {
        "agents": {"defaults": {"models": {
            "anthropic/claude-haiku-4-5": {},
            "google/gemini-2.5-pro": {},
        }}},
        "models": {"providers": {
            "anthropic": {"models": [
                {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
            ]},
            # google block absent — google/gemini-2.5-pro is dangling
        }},
    }
    findings = _collect(oc)
    assert len(findings) == 1
    assert "google/gemini-2.5-pro" in findings[0].detail


def test_skips_malformed_slugs_in_agents_models():
    """A slug without a provider prefix (no ``/``) can't be validated
    against models.providers — it's a separate config bug, not this
    check's responsibility. Skip silently rather than miscategorize."""
    oc = {
        "agents": {"defaults": {"models": {
            "bare-no-slash": {},
            "/leading-slash": {},
            "trailing/": {},
            "anthropic/claude-haiku-4-5": {},  # ← real, but missing from registry
        }}},
        "models": {"providers": {
            "anthropic": {"models": [
                {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6"},
            ]},
        }},
    }
    findings = _collect(oc)
    # Only the real haiku slug fires; malformed slugs skip silently.
    assert len(findings) == 1
    assert "anthropic/claude-haiku-4-5" in findings[0].detail
    assert "bare-no-slash" not in findings[0].detail


def test_detail_truncates_long_missing_list():
    """When many slugs are missing, the message stays one line and the
    detail caps at 5 with a "+N more" tail so the Alerts row doesn't
    grow unbounded on a freshly-deployed bot."""
    missing_slugs = {f"anthropic/claude-model-{i}": {} for i in range(8)}
    oc = {
        "agents": {"defaults": {"models": missing_slugs}},
        "models": {"providers": {
            "anthropic": {"models": []},  # block present, empty
        }},
    }
    findings = _collect(oc)
    assert len(findings) == 1
    f = findings[0]
    assert "8" in f.message
    # Detail shows first 5 + summary tail
    assert f.detail.count("anthropic/claude-model-") == 5
    assert "+3 more" in f.detail


def test_provider_models_list_with_non_dict_entry_ignored():
    """Defensive — an unexpected list entry shape (str, None) doesn't
    crash the check, just gets ignored when building the registered
    set. The matching slug then falls through as missing."""
    oc = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {"providers": {
            "anthropic": {"models": [
                "garbage-string-entry",
                None,
                {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6"},
            ]},
        }},
    }
    findings = _collect(oc)
    assert len(findings) == 1
    assert "anthropic/claude-haiku-4-5" in findings[0].detail


def test_fix_steps_reference_deploy_command():
    """The fix is a redeploy — the audit's fix_steps must point the
    operator at ``evolve-admin deploy <bot>`` so the message is
    self-sufficient (no need to dig into deploy.py to learn the
    remediation)."""
    oc = {
        "agents": {"defaults": {"models": {"anthropic/claude-haiku-4-5": {}}}},
        "models": {"providers": {"anthropic": {"models": []}}},
    }
    findings = _collect(oc)
    assert len(findings) == 1
    assert "evolve-admin deploy" in findings[0].fix_steps
    assert "test_bot" in findings[0].fix_steps
