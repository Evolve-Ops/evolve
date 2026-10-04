"""tests/test_model_swap_cli.py — ``evolve-admin models swaps`` / ``rollback``.

Design: internal/design-model-swap-behavior-guard-2026-08-19.md.

``rollback`` is the operator's one-command undo for a model swap — the thing
the 2026-08-14 incident did not have. It is only worth shipping if it actually
writes, so these drive the real click commands against a fake OpenClaw config
layer and assert on the resulting state, not on a mock being called.

The two contracts the write must inherit from the admin UI's path
(``model_tier_apply``) and that a naive implementation would drop:

  * send ONLY the changed tier — the full synthesized dict lets an unchanged
    sibling sharing the same rung clobber the restore;
  * a truthy setter result is NOT proof of persistence — verify the models
    landed, and fail loudly if they did not.
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

OLD = "anthropic/claude-sonnet-4-6"
NEW = "anthropic/claude-sonnet-5"


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A pod with one bot whose standard rung was swapped OLD -> NEW."""
    import model_swap_ledger
    import oc_cli

    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "bots": {"team-bot-a": {"user": "team-bot-a"}},
        "sharedDir": str(shared),
    }))

    state = {"team-bot-a": {"tiers": {
        "standard": {"models": [NEW, "openai/gpt-4o"]},
        "fast": {"models": ["anthropic/claude-haiku-4-5"]},
    }}}
    seen: dict = {}

    def fake_get(bot_id, network_path=None):
        return {"bot": bot_id, **state.get(bot_id, {})}

    def fake_set_with_error(bot_id, updates, network_path=None):
        seen["updates"] = updates
        if "tiers" in updates:
            state[bot_id]["tiers"].update(updates["tiers"])
        return {"bot": bot_id, **state[bot_id]}, None

    monkeypatch.setattr(oc_cli, "oc_full_config_get", fake_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", fake_set_with_error)

    model_swap_ledger.record_swap("team-bot-a", "standard", "anthropic",
                                  [OLD, "openai/gpt-4o"], [NEW, "openai/gpt-4o"],
                                  source="admin_ui_bulk", shared_dir=shared)
    return {"network_path": network_path, "shared": shared, "state": state, "seen": seen}


def _run(pod, args):
    from click.testing import CliRunner

    from evolve_admin.cli import main

    return CliRunner().invoke(main, ["--network", str(pod["network_path"]), *args])


def test_registration_does_not_replace_the_existing_models_group():
    """`swaps` / `rollback` ATTACH to cli.py's existing `models` group.

    The first version declared `@main.group("models")` in the helper, which
    click accepts silently — it replaced the group cli.py had already defined
    and deleted `models set` / `list` / `show` / `cap` / `usage` from the CLI.
    Preflight's targeted admin subset did not include the suite that covers
    those, so it only surfaced in CI.
    """
    from evolve_admin.cli import main

    commands = set(main.commands["models"].commands)
    assert {"swaps", "rollback", "pins", "unpin"} <= commands, \
        "the new commands must be attached"
    assert {"set", "list", "show", "cap", "usage", "user-tier-control"} <= commands, (
        "registration must not displace the pre-existing models commands"
    )


def test_swaps_lists_the_recorded_change(pod):
    result = _run(pod, ["models", "swaps"])
    assert result.exit_code == 0, result.output
    assert "team-bot-a" in result.output and "standard" in result.output
    assert "claude-sonnet-4-6" in result.output and "claude-sonnet-5" in result.output


def test_swaps_says_so_when_the_ledger_is_empty(tmp_path, pod):
    """A pod that has never swapped must not read as an error."""
    (pod["shared"] / "model_swaps.jsonl").unlink()
    result = _run(pod, ["models", "swaps"])
    assert result.exit_code == 0
    assert "No model swaps recorded" in result.output


def test_rollback_restores_the_previous_models(pod):
    result = _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    assert result.exit_code == 0, result.output
    assert pod["state"]["team-bot-a"]["tiers"]["standard"]["models"] == [OLD, "openai/gpt-4o"]


def test_rollback_sends_only_the_changed_tier(pod):
    """The full synthesized dict would let a rung-sharing sibling clobber it."""
    _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    assert list(pod["seen"]["updates"]["tiers"]) == ["standard"]


def test_rollback_is_itself_recorded_so_it_can_be_undone(pod):
    import model_swap_ledger

    _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    rows = model_swap_ledger.read_swaps(pod["shared"])
    assert len(rows) == 2
    assert rows[1]["source"] == "cli_rollback"
    assert rows[1]["previous_models"] == [NEW, "openai/gpt-4o"]
    assert rows[1]["new_models"] == [OLD, "openai/gpt-4o"]


def test_dry_run_writes_nothing(pod):
    import model_swap_ledger

    result = _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard", "--dry-run"])
    assert result.exit_code == 0
    assert "no write performed" in result.output
    assert pod["state"]["team-bot-a"]["tiers"]["standard"]["models"] == [NEW, "openai/gpt-4o"]
    assert len(model_swap_ledger.read_swaps(pod["shared"])) == 1


def test_rollback_without_a_recorded_swap_fails_loudly(pod):
    result = _run(pod, ["models", "rollback", "team-bot-a", "--tier", "fast"])
    assert result.exit_code == 1
    assert "No recorded swap" in result.output


def test_rollback_is_a_noop_when_already_at_the_previous_models(pod):
    _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    pod["seen"].clear()
    result = _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    assert result.exit_code == 0
    assert "nothing to do" in result.output
    assert "updates" not in pod["seen"], "a no-op must not write"


def test_silent_non_persist_is_reported_as_a_failure(pod, monkeypatch):
    """A truthy setter result is not proof — the false-success class
    model_tier_apply exists to prevent."""
    import oc_cli

    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error",
        lambda bot_id, updates, network_path=None: (
            {"bot": bot_id, "tiers": {"standard": {"models": [NEW, "openai/gpt-4o"]}}}, None
        ),
    )
    result = _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    assert result.exit_code == 1
    assert "did not persist" in result.output


# ── Sticky rollback: behavior pins (the 2026-08-21 recurrence) ───────────────


def test_rollback_pins_the_backed_out_model(pod):
    """The fix for the 2026-08-21 recurrence: a rollback must leave a durable
    'behavior-rejected' record, or the next Model Freshness apply silently
    re-swaps the very model the operator just backed out."""
    import model_swap_ledger

    result = _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    assert result.exit_code == 0, result.output
    assert "pinned" in result.output
    pin = model_swap_ledger.find_active_pin(
        "team-bot-a", "standard", NEW, pod["shared"])
    assert pin is not None
    assert pin["source"] == "cli_rollback"
    # The surviving sibling (openai/gpt-4o was in both before and after) and
    # the restored model must NOT be pinned.
    assert model_swap_ledger.find_active_pin(
        "team-bot-a", "standard", OLD, pod["shared"]) is None
    assert model_swap_ledger.find_active_pin(
        "team-bot-a", "standard", "openai/gpt-4o", pod["shared"]) is None


def test_rollback_dry_run_writes_no_pin(pod):
    import model_swap_ledger

    _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard", "--dry-run"])
    assert model_swap_ledger.read_pin_events(pod["shared"]) == []


def test_rollback_on_an_already_reverted_rung_still_pins(pod):
    """A manual revert before running the command must not leave the rollback
    non-sticky — the operator's intent is 'reject this model'."""
    import model_swap_ledger

    pod["state"]["team-bot-a"]["tiers"]["standard"]["models"] = [OLD, "openai/gpt-4o"]
    result = _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    assert result.exit_code == 0
    assert "nothing to do" in result.output
    assert model_swap_ledger.find_active_pin(
        "team-bot-a", "standard", NEW, pod["shared"]) is not None


def test_pins_lists_the_active_pin(pod):
    _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    result = _run(pod, ["models", "pins"])
    assert result.exit_code == 0, result.output
    assert "team-bot-a" in result.output and "claude-sonnet-5" in result.output


def test_pins_empty_state_is_not_an_error(pod):
    result = _run(pod, ["models", "pins"])
    assert result.exit_code == 0
    assert "No active behavior pins" in result.output


def test_unpin_lifts_the_pin(pod):
    import model_swap_ledger

    _run(pod, ["models", "rollback", "team-bot-a", "--tier", "standard"])
    result = _run(pod, ["models", "unpin", "team-bot-a",
                        "--tier", "standard", "--model", NEW])
    assert result.exit_code == 0, result.output
    assert model_swap_ledger.find_active_pin(
        "team-bot-a", "standard", NEW, pod["shared"]) is None


def test_unpin_without_an_active_pin_fails_loudly(pod):
    result = _run(pod, ["models", "unpin", "team-bot-a",
                        "--tier", "standard", "--model", NEW])
    assert result.exit_code == 1
    assert "No active pin" in result.output
