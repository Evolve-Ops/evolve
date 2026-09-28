"""CLI tests for provision-bot's provider-agnostic --auth-choice handling.

The provider-agnostic principle (docs/principle-llm-provider-agnostic.md)
forbids a presumed provider default. provision-bot must:
  - error (before any state is created) when --auth-choice is absent and
    onboard will run,
  - infer "anthropic" ONLY from the explicitly-named legacy
    --anthropic-api-key flag (the provider is encoded in the flag name),
  - pass an explicit --auth-choice through untouched,
  - accept the absence of a choice when --no-onboard is given.

provision_bot itself is stubbed — these tests exercise only the CLI
validation wiring (the backend's no-default contract is covered by
test_provision_bot.py).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

_ADMIN = Path(__file__).resolve().parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin.cli import main


def _stub_provision_bot(captured: dict):
    def stub(bot_id, **kwargs):
        captured["bot_id"] = bot_id
        captured.update(kwargs)
        return SimpleNamespace(
            success=True, user=bot_id, uid=502, port=19099,
            failed_stage=None, error=None, rollback_log=[],
        )
    return stub


def test_provision_bot_without_auth_choice_aborts(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "evolve_admin.provisioning.provision_bot", _stub_provision_bot(captured)
    )
    r = CliRunner().invoke(main, ["provision-bot", "newbot", "--dry-run"])
    assert r.exit_code != 0
    assert "--auth-choice is required" in r.output
    # Aborted before provisioning touched anything.
    assert captured == {}


def test_provision_bot_anthropic_key_implies_auth_choice(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "evolve_admin.provisioning.provision_bot", _stub_provision_bot(captured)
    )
    r = CliRunner().invoke(
        main,
        ["provision-bot", "newbot", "--anthropic-api-key", "sk-test", "--dry-run"],
    )
    assert r.exit_code == 0, r.output
    assert captured["auth_choice"] == "anthropic"
    assert captured["provider_api_key"] == "sk-test"


def test_provision_bot_explicit_auth_choice_passes_through(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "evolve_admin.provisioning.provision_bot", _stub_provision_bot(captured)
    )
    r = CliRunner().invoke(
        main,
        ["provision-bot", "newbot", "--auth-choice", "openai-api-key", "--dry-run"],
    )
    assert r.exit_code == 0, r.output
    assert captured["auth_choice"] == "openai-api-key"


def test_provision_bot_no_onboard_needs_no_auth_choice(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "evolve_admin.provisioning.provision_bot", _stub_provision_bot(captured)
    )
    r = CliRunner().invoke(
        main, ["provision-bot", "newbot", "--no-onboard", "--dry-run"]
    )
    assert r.exit_code == 0, r.output
    assert captured["auth_choice"] is None
