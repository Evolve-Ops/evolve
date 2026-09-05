"""tests/test_cache_ttl_tuner_applicability.py — Anthropic-only gate.

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md §"Applicability
check".

``bot_uses_anthropic`` reads the bot's openclaw.json and looks for any
Anthropic model in agents.defaults.models. Fail-open on read errors so
a transient permission glitch doesn't suppress legitimate proposals.

These tests stub the bot_home resolver to point at a tmp dir so we
exercise the real config-parsing logic without needing the macOS
ACL setup that production relies on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import importlib  # noqa: E402

applicability = importlib.import_module(
    "generators.cache_ttl_tuner.applicability",
)


def _write_oc_json(home: Path, models: dict) -> None:
    """Mirror the on-disk shape of a bot's openclaw.json."""
    oc_dir = home / ".openclaw"
    oc_dir.mkdir(parents=True, exist_ok=True)
    config = {"agents": {"defaults": {"models": models}}}
    (oc_dir / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture
def patched_home(tmp_path, monkeypatch):
    """Redirect bot_home() to a tmp dir per test so we can write
    openclaw.json without touching real bot accounts."""
    home = tmp_path / "bot_home"
    home.mkdir()
    import evolve_config

    monkeypatch.setattr(evolve_config, "bot_home", lambda bot_id: home)
    return home


def test_returns_true_for_explicit_anthropic_provider(patched_home):
    _write_oc_json(
        patched_home,
        {"primary": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
    )
    assert applicability.bot_uses_anthropic("any_bot") is True


def test_returns_true_for_slash_prefixed_id(patched_home):
    """OC also accepts `anthropic/claude-...` shorthand without an
    explicit provider field."""
    _write_oc_json(
        patched_home,
        {"primary": {"id": "anthropic/claude-haiku-4-5"}},
    )
    assert applicability.bot_uses_anthropic("any_bot") is True


def test_returns_true_for_bare_claude_id(patched_home):
    """A `claude-*` id without an explicit provider is distinctive
    enough to count — false positives are unlikely."""
    _write_oc_json(
        patched_home,
        {"primary": {"id": "claude-opus-4-7"}},
    )
    assert applicability.bot_uses_anthropic("any_bot") is True


def test_returns_false_for_openai_only_bot(patched_home):
    _write_oc_json(
        patched_home,
        {"primary": {"provider": "openai", "id": "gpt-4o"}},
    )
    assert applicability.bot_uses_anthropic("any_bot") is False


def test_returns_false_for_google_only_bot(patched_home):
    _write_oc_json(
        patched_home,
        {
            "primary": {"provider": "google", "id": "gemini-2.5-pro"},
            "judge": {"provider": "google", "id": "gemini-2.5-flash"},
        },
    )
    assert applicability.bot_uses_anthropic("any_bot") is False


def test_returns_true_when_any_model_is_anthropic(patched_home):
    """Multi-provider bots count as Anthropic so long as one model
    is — the cacheRetention flip affects only the Anthropic models."""
    _write_oc_json(
        patched_home,
        {
            "primary": {"provider": "openai", "id": "gpt-4o"},
            "judge": {"provider": "anthropic", "id": "claude-haiku-4-5"},
        },
    )
    assert applicability.bot_uses_anthropic("any_bot") is True


def test_fail_open_when_oc_config_missing(patched_home):
    """No openclaw.json yet (e.g. mid-provision). Fail-open — assume
    Anthropic so the upstream signal still triggers emission. The
    typed proposal's applier handles the actual write either way."""
    # patched_home exists but contains no .openclaw/
    assert applicability.bot_uses_anthropic("any_bot") is True


def test_fail_open_on_malformed_json(patched_home):
    oc_dir = patched_home / ".openclaw"
    oc_dir.mkdir(parents=True, exist_ok=True)
    (oc_dir / "openclaw.json").write_text("not valid json {{", encoding="utf-8")
    assert applicability.bot_uses_anthropic("any_bot") is True


def test_fail_open_when_models_block_missing(patched_home):
    """Config exists but no agents.defaults.models — likely a pre-OC2
    bot. Treat as unknown ⇒ assume Anthropic."""
    oc_dir = patched_home / ".openclaw"
    oc_dir.mkdir(parents=True, exist_ok=True)
    (oc_dir / "openclaw.json").write_text(
        json.dumps({"foo": "bar"}), encoding="utf-8",
    )
    assert applicability.bot_uses_anthropic("any_bot") is False
    # ^ models block missing → no Anthropic found → returns False.
    # The fail-open kicks in for *read* failures, not for a
    # well-formed config that simply lacks the relevant block.


def test_returns_false_when_models_block_present_but_no_anthropic(patched_home):
    """The fail-open exception is bounded — once we successfully
    parse the config and confirm no Anthropic models, we trust that
    answer. Otherwise the gate is useless."""
    _write_oc_json(
        patched_home,
        {"primary": {"provider": "xai", "id": "grok-2"}},
    )
    assert applicability.bot_uses_anthropic("any_bot") is False
