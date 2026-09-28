"""Target-based model selection for forge_engine builder/critic (#3466 PR-4).

Pins the provider-agnostic contract that replaced the old
``_resolve_tier2_anthropic`` / ``_get_models`` pair:

  - ``_get_targets`` reads network.json::forge.builder_model /
    forge.critic_model as the highest-priority pin. A provider-QUALIFIED
    pin binds fully (ignored with a warning when its provider has no
    key); a BARE pin (the historic form) pins the model id on whatever
    provider resolution lands on.
  - When the pin is unset, ``_resolve_standard_target`` routes through
    ``models.resolve_tier("tier2", config, bot_id=bot_id)`` — pod-wide
    tier2 swaps and per-bot tier_assignments both propagate — and takes
    that model when its provider is credentialed.
  - A tier2 pinned to an UNCREDENTIALED provider is walked past to the
    derived catalog pick among the credentialed providers — the old
    "discard a correctly configured non-Anthropic tier2 and call Claude
    anyway" guard is dead.
  - No credentialed provider at all → ``(None, None)`` (callers degrade).

Background: forge_engine's builder/critic calls were Anthropic-only urllib
posts keyed by an anthropic-specific resolver before #3466 PR-4.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.applications import bot_forge, forge_engine  # noqa: E402
from infra_llm import InfraLLMTarget  # noqa: E402


_FAKE_KEYS = {
    "anthropic": "sk-ant-test-not-a-real-key",
    "openai": "sk-oai-test-not-a-real-key",
}


# ─────────────────────────────────────────────────────────────────────────────
# _target_for_model — qualified/credentialed contract
# ─────────────────────────────────────────────────────────────────────────────


def test_target_for_model_qualified_credentialed():
    t = forge_engine._target_for_model("openai/gpt-4o", _FAKE_KEYS)
    assert t is not None
    assert (t.provider, t.model, t.api_key) == (
        "openai", "openai/gpt-4o", _FAKE_KEYS["openai"],
    )


def test_target_for_model_bare_id_never_presumed():
    """A bare id carries no provider — it must NOT be presumed to one."""
    assert forge_engine._target_for_model("claude-sonnet-4-6", _FAKE_KEYS) is None


def test_target_for_model_uncredentialed_provider_refused():
    assert forge_engine._target_for_model("google/gemini-2.5-pro", _FAKE_KEYS) is None


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_standard_target — tier2 → derived walk, never discard-to-claude
# ─────────────────────────────────────────────────────────────────────────────


def test_standard_target_takes_credentialed_tier2(monkeypatch):
    import models  # noqa: F401 — ensure real module is imported + cached
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "openai/gpt-4o",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    t = forge_engine._resolve_standard_target("atlas", _FAKE_KEYS)
    assert t is not None and t.provider == "openai"
    assert t.model == "openai/gpt-4o"


def test_standard_target_walks_past_uncredentialed_tier2(monkeypatch):
    """The discard-guard kill (#3466): a tier2 pinned to a provider the pod
    has no key for is walked past to a CREDENTIALED derived pick — never
    'corrected' back to a presumed provider."""
    import models  # noqa: F401
    import evolve_config  # noqa: F401

    monkeypatch.setattr(
        "models.resolve_tier",
        lambda tier, config, bot_id=None: "anthropic/claude-sonnet-4-6",
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    openai_only = {"openai": "sk-oai-test-not-a-real-key"}
    t = forge_engine._resolve_standard_target("atlas", openai_only)
    assert t is not None
    assert t.provider == "openai"  # walked to the credentialed provider


def test_standard_target_survives_import_failure(monkeypatch):
    """Analyzer unimportable (test isolation, broken paths) → the resolve
    step degrades; the derived walk import failing too yields None. Must
    not raise — the dispatch path can't tolerate exceptions here."""
    monkeypatch.setitem(sys.modules, "models", None)
    monkeypatch.setitem(sys.modules, "evolve_config", None)

    assert forge_engine._resolve_standard_target("atlas", _FAKE_KEYS) is None


# ─────────────────────────────────────────────────────────────────────────────
# _get_targets — full priority chain (operator pin → tier2 → derived)
# ─────────────────────────────────────────────────────────────────────────────


def _write_network(tmp_path: Path, forge_config: dict) -> Path:
    """Drop a minimal network.json one level above shared_dir."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (tmp_path / "network.json").write_text(json.dumps({"forge": forge_config}))
    return shared


def _base_target() -> InfraLLMTarget:
    return InfraLLMTarget(
        "openai", "openai/gpt-4o", _FAKE_KEYS["openai"],
    )


def test_get_targets_no_keys_degrades_to_none(tmp_path, monkeypatch):
    shared = _write_network(tmp_path, {})
    monkeypatch.setattr(forge_engine, "_resolve_llm_keys", lambda bot_id=None: {})
    assert forge_engine._get_targets(shared, bot_id="atlas") == (None, None)


def test_get_targets_qualified_operator_pin_wins(tmp_path, monkeypatch):
    shared = _write_network(tmp_path, {
        "builder_model": "anthropic/claude-opus-4-6",
        "critic_model": "openai/gpt-4o-mini",
    })
    monkeypatch.setattr(
        forge_engine, "_resolve_llm_keys", lambda bot_id=None: dict(_FAKE_KEYS))
    monkeypatch.setattr(
        forge_engine, "_resolve_standard_target",
        lambda bot_id, keys: _base_target())
    builder, critic = forge_engine._get_targets(shared, bot_id="atlas")
    assert (builder.provider, builder.model) == ("anthropic", "anthropic/claude-opus-4-6")
    assert (critic.provider, critic.model) == ("openai", "openai/gpt-4o-mini")


def test_get_targets_uncredentialed_pin_ignored_with_warning(tmp_path, monkeypatch, caplog):
    """A qualified pin naming a provider without a key must NOT produce an
    uncredentialed target — it is ignored (warned) and the resolved base
    stands."""
    import logging

    shared = _write_network(tmp_path, {"builder_model": "google/gemini-2.5-pro"})
    monkeypatch.setattr(
        forge_engine, "_resolve_llm_keys", lambda bot_id=None: dict(_FAKE_KEYS))
    monkeypatch.setattr(
        forge_engine, "_resolve_standard_target",
        lambda bot_id, keys: _base_target())
    with caplog.at_level(logging.WARNING):
        builder, _critic = forge_engine._get_targets(shared, bot_id="atlas")
    assert builder.provider == "openai"  # the base, not google
    assert "uncredentialed" in caplog.text


def test_get_targets_bare_pin_rides_resolved_provider(tmp_path, monkeypatch):
    """Legacy bare pins ('claude-sonnet-4-6') pin the MODEL ID on whatever
    provider resolution lands on — exact legacy behavior on the
    single-provider pods that wrote them."""
    shared = _write_network(tmp_path, {"builder_model": "claude-sonnet-4-6"})
    anthropic_only = {"anthropic": _FAKE_KEYS["anthropic"]}
    monkeypatch.setattr(
        forge_engine, "_resolve_llm_keys", lambda bot_id=None: dict(anthropic_only))
    monkeypatch.setattr(
        forge_engine, "_resolve_standard_target",
        lambda bot_id, keys: InfraLLMTarget(
            "anthropic", "anthropic/claude-sonnet-4-5", anthropic_only["anthropic"]),
    )
    builder, critic = forge_engine._get_targets(shared, bot_id="atlas")
    assert (builder.provider, builder.model) == ("anthropic", "claude-sonnet-4-6")
    # critic had no pin → the untouched base model.
    assert critic.model == "anthropic/claude-sonnet-4-5"


def test_get_targets_per_bot_resolution_threaded(tmp_path, monkeypatch):
    """bot_id reaches the resolver — pod tier2 + per-bot tier_assignments
    can choose a different model per-bot."""
    shared = _write_network(tmp_path, {})
    seen: list[str | None] = []

    def fake_resolve(bot_id, keys):
        seen.append(bot_id)
        return _base_target()

    monkeypatch.setattr(
        forge_engine, "_resolve_llm_keys", lambda bot_id=None: dict(_FAKE_KEYS))
    monkeypatch.setattr(forge_engine, "_resolve_standard_target", fake_resolve)
    forge_engine._get_targets(shared, bot_id="atlas")
    forge_engine._get_targets(shared, bot_id="team_bot_a")
    assert seen == ["atlas", "team_bot_a"]


def test_get_targets_no_network_json_falls_to_resolver(tmp_path, monkeypatch):
    """No network.json at all → the standard-target resolver still runs."""
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(
        forge_engine, "_resolve_llm_keys", lambda bot_id=None: dict(_FAKE_KEYS))
    monkeypatch.setattr(
        forge_engine, "_resolve_standard_target",
        lambda bot_id, keys: _base_target())
    builder, critic = forge_engine._get_targets(shared, bot_id="atlas")
    assert builder is not None and critic is not None
    assert builder.model == "openai/gpt-4o"


# ─────────────────────────────────────────────────────────────────────────────
# _provisioning_build_model — job_type scoping (decision C)
# Install jobs (app provisioning) run on the standard role; steady-state
# forge work (improvement / update / hotfix) keeps the bot's default.
# internal/finding-new-bot-activation-cost-2026-06-12.md
# ─────────────────────────────────────────────────────────────────────────────


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


# ─────────────────────────────────────────────────────────────────────────────
# _call_llm — infra_llm flow-through
# ─────────────────────────────────────────────────────────────────────────────


def test_call_llm_flows_through_infra_llm(monkeypatch):
    """Forge admin-side calls ride infra_llm.complete verbatim (any
    credentialed provider), with the forge-sized 120s timeout."""
    import infra_llm as _infra

    captured = {}

    def fake_complete(target, *, prompt=None, system="", max_tokens=0,
                      timeout=0, **kw):
        captured.update(target=target, prompt=prompt, system=system,
                        max_tokens=max_tokens, timeout=timeout)
        return "ok"

    monkeypatch.setattr(_infra, "complete", fake_complete)
    target = InfraLLMTarget("openai", "openai/gpt-4o", _FAKE_KEYS["openai"])
    out = forge_engine._call_llm("SYSTEM", "USER", target, max_tokens=4096)
    assert out == "ok"
    assert captured["target"] is target
    assert captured["system"] == "SYSTEM"
    assert captured["prompt"] == "USER"
    assert captured["max_tokens"] == 4096
    assert captured["timeout"] == 120
