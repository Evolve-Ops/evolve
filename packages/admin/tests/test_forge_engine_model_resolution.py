"""Tier-resolved model selection for forge_engine builder/critic.

Pins the contract the A1 fix added:

  - _get_models reads network.json::forge.builder_model / forge.critic_model
    as the highest-priority override (operator's explicit pin).
  - When the operator override is unset, _get_models routes through
    models.resolve_tier("tier2", config, bot_id=bot_id) — so pod-wide
    tier2 swaps and per-bot tier_assignments overrides both propagate
    to forge builder/critic automatically.
  - Tier2 IDs are returned in the bare Anthropic form (no "anthropic/"
    prefix), because forge_engine posts directly to the Messages API.
  - Non-Anthropic tier2 (e.g. fallback to openai/gpt-4o) falls back to
    the hardcoded literal with a warning — forge_engine can't dispatch
    those.
  - Broken-config fallback lands on the hardcoded literal.

Background: forge_engine's _DEFAULT_BUILDER_MODEL / _DEFAULT_CRITIC_MODEL
were hardcoded Sonnet before A1, ignoring any pod tier2 changes. This
is the same bug shape as bot_forge.py before PR #1769.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.applications import forge_engine  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_tier2_anthropic — bare-id resolution with provider check
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_tier2_strips_anthropic_prefix(monkeypatch):
    """The tier registry stores ids like ``anthropic/claude-sonnet-4-6``;
    forge_engine wants ``claude-sonnet-4-6`` (Messages API form)."""
    import models  # noqa: F401 — ensure real module is imported + cached
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "anthropic/claude-sonnet-4-6",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    assert forge_engine._resolve_tier2_anthropic(bot_id="atlas") == "claude-sonnet-4-6"


def test_resolve_tier2_falls_back_when_non_anthropic(monkeypatch, caplog):
    """If the pod's tier2 fallback chain hits openai/gpt-4o, forge_engine
    can't dispatch it (calls Anthropic API directly), so we return None
    with a warning and the caller falls back to the literal."""
    import logging
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "openai/gpt-4o",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    with caplog.at_level(logging.WARNING):
        result = forge_engine._resolve_tier2_anthropic(bot_id="atlas")

    assert result is None
    assert "non-Anthropic" in caplog.text


def test_resolve_tier2_returns_none_on_import_failure(monkeypatch):
    """When the analyzer package isn't importable (test isolation, broken
    paths), return None so the caller hits the literal fallback. Must not
    raise — the dispatch path can't tolerate exceptions here."""
    # Setting a module's sys.modules entry to None makes `import` raise
    # ImportError deterministically — unlike the old empty-sys.path trick,
    # this also works when the analyzer is installed via a PEP 660
    # editable meta-path finder (e.g. `uv sync`), which is consulted
    # before sys.path and ignored emptying it.
    monkeypatch.setitem(sys.modules, "models", None)
    monkeypatch.setitem(sys.modules, "evolve_config", None)

    assert forge_engine._resolve_tier2_anthropic(bot_id="atlas") is None


# ─────────────────────────────────────────────────────────────────────────────
# _get_models — full priority chain (operator → tier → hardcoded)
# ─────────────────────────────────────────────────────────────────────────────


def _write_network(tmp_path: Path, forge_config: dict) -> Path:
    """Drop a minimal network.json one level above shared_dir."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (tmp_path / "network.json").write_text(json.dumps({"forge": forge_config}))
    return shared


def test_get_models_operator_override_wins(tmp_path, monkeypatch):
    """forge.builder_model in network.json is the highest-priority knob.
    Tier resolution must NOT run when both overrides are set (resolver
    short-circuit — avoid a wasted load_config disk read)."""
    shared = _write_network(tmp_path, {
        "builder_model": "claude-opus-4-6",
        "critic_model": "claude-haiku-4-5",
    })
    monkeypatch.setattr(
        forge_engine, "_resolve_tier2_anthropic",
        lambda bot_id: pytest.fail("tier resolver should not run when both overrides are set"),
    )
    assert forge_engine._get_models(shared, bot_id="atlas") == (
        "claude-opus-4-6", "claude-haiku-4-5",
    )


def test_get_models_tier_resolution_used_when_override_absent(tmp_path, monkeypatch):
    """When network.json has no forge.builder_model / critic_model,
    fall through to the tier2 resolver."""
    shared = _write_network(tmp_path, {})  # empty forge section
    monkeypatch.setattr(
        forge_engine, "_resolve_tier2_anthropic",
        lambda bot_id: "claude-sonnet-4-7-from-tier",
    )
    assert forge_engine._get_models(shared, bot_id="atlas") == (
        "claude-sonnet-4-7-from-tier", "claude-sonnet-4-7-from-tier",
    )


def test_get_models_per_bot_resolution_threaded(tmp_path, monkeypatch):
    """bot_id reaches the resolver — pod tier2 + per-bot tier_assignments
    can choose a different model per-bot. Team_bot_a's forge may differ from
    atlas's."""
    shared = _write_network(tmp_path, {})

    seen: list[str | None] = []

    def fake_resolve(bot_id):
        seen.append(bot_id)
        return "claude-sonnet-4-6" if bot_id != "team_bot_a" else "claude-opus-4-6"

    monkeypatch.setattr(forge_engine, "_resolve_tier2_anthropic", fake_resolve)
    assert forge_engine._get_models(shared, bot_id="atlas")[0] == "claude-sonnet-4-6"
    assert forge_engine._get_models(shared, bot_id="team_bot_a")[0] == "claude-opus-4-6"
    assert seen == ["atlas", "team_bot_a"]


def test_get_models_partial_override_runs_resolver(tmp_path, monkeypatch):
    """If only builder_model is pinned, the resolver still runs to fill
    critic. Operator can pin one phase without inheriting a stale literal
    on the other."""
    shared = _write_network(tmp_path, {"builder_model": "claude-opus-4-6"})
    monkeypatch.setattr(
        forge_engine, "_resolve_tier2_anthropic",
        lambda bot_id: "claude-sonnet-4-6-from-tier",
    )
    builder, critic = forge_engine._get_models(shared, bot_id="atlas")
    assert builder == "claude-opus-4-6"
    assert critic == "claude-sonnet-4-6-from-tier"


def test_get_models_hardcoded_fallback_when_resolver_returns_none(tmp_path, monkeypatch):
    """Resolver returning None (broken config OR non-Anthropic tier) →
    hardcoded literals stand. Dispatch never silently breaks."""
    shared = _write_network(tmp_path, {})
    monkeypatch.setattr(forge_engine, "_resolve_tier2_anthropic", lambda bot_id: None)
    assert forge_engine._get_models(shared, bot_id="atlas") == (
        forge_engine._DEFAULT_BUILDER_MODEL,
        forge_engine._DEFAULT_CRITIC_MODEL,
    )


def test_get_models_no_network_json_falls_to_resolver(tmp_path, monkeypatch):
    """No network.json at all → tier resolver still runs (operator may
    have configured tier2 elsewhere via the in-memory config layer)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(
        forge_engine, "_resolve_tier2_anthropic",
        lambda bot_id: "claude-sonnet-4-6-from-tier",
    )
    assert forge_engine._get_models(shared, bot_id="atlas") == (
        "claude-sonnet-4-6-from-tier", "claude-sonnet-4-6-from-tier",
    )


def test_get_models_accepts_none_bot_id(tmp_path, monkeypatch):
    """Backwards compat: callers that don't have bot_id (legacy path,
    pod-wide jobs) can still call _get_models. None propagates to the
    resolver, which falls back to pod-wide tier2."""
    shared = _write_network(tmp_path, {})
    seen: list[str | None] = []

    def fake_resolve(bot_id):
        seen.append(bot_id)
        return "claude-sonnet-4-6"

    monkeypatch.setattr(forge_engine, "_resolve_tier2_anthropic", fake_resolve)
    forge_engine._get_models(shared, bot_id=None)
    assert seen == [None]


# ─────────────────────────────────────────────────────────────────────────────
# _provisioning_build_model — job_type scoping (decision C)
# Install jobs (app provisioning) run on the standard role; steady-state
# forge work (improvement / update / hotfix) keeps the bot's default.
# docs/finding-new-bot-activation-cost-2026-06-12.md
# ─────────────────────────────────────────────────────────────────────────────

from types import SimpleNamespace  # noqa: E402

from evolve_admin.applications import bot_forge  # noqa: E402


def test_provisioning_build_model_install_resolves_standard(monkeypatch):
    """An install job (the wizard starter pack, a gallery install) pins the
    bot-driven build + refine dispatch to the resolved standard-role model."""
    monkeypatch.setattr(
        bot_forge, "_resolve_provisioning_build_model",
        lambda bot_id: "anthropic/claude-sonnet-4-6",
    )
    job = SimpleNamespace(job_type="install", bot_id="ledger")
    assert forge_engine._provisioning_build_model(job) == "anthropic/claude-sonnet-4-6"


@pytest.mark.parametrize("job_type", ["improvement", "update", "hotfix"])
def test_provisioning_build_model_non_install_inherits_default(monkeypatch, job_type):
    """Steady-state forge work refines EXISTING app code — it keeps the
    bot's default model (model=None). The standard-role resolver must not
    even run, so this never broadly downgrades non-provisioning builds."""
    monkeypatch.setattr(
        bot_forge, "_resolve_provisioning_build_model",
        lambda bot_id: pytest.fail("resolver must not run for non-install jobs"),
    )
    job = SimpleNamespace(job_type=job_type, bot_id="ledger")
    assert forge_engine._provisioning_build_model(job) is None
