"""End-to-end CLI tests for the lifecycle commands.

Verifies:
  - `evolve-admin lifecycle inventory <bot>` renders human-readable output
  - `--json` mode emits valid JSON
  - `--action {detach,archive,delete}` filters the output
  - `evolve-admin detach-bot --help` loads cleanly (alias for remove-evolve)
  - `evolve-admin lifecycle --help` lists `inventory` as a subcommand
  - Pre-flight preview function tolerates missing bots / unreadable state

The compile_bot_inventory logic itself is covered by test_lifecycle_inventory.py;
these tests just exercise the CLI wiring.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_ADMIN = Path(__file__).resolve().parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin.cli import main


# ── help / discoverability ───────────────────────────────────────────


def test_top_level_help_lists_all_four_lifecycle_commands():
    runner = CliRunner()
    r = runner.invoke(main, ["--help"])
    assert r.exit_code == 0
    out = r.output
    assert "detach-bot" in out
    assert "retire-bot" in out
    assert "remove-evolve" in out
    assert "lifecycle" in out


def test_lifecycle_group_help_lists_inventory():
    runner = CliRunner()
    r = runner.invoke(main, ["lifecycle", "--help"])
    assert r.exit_code == 0
    assert "inventory" in r.output


def test_detach_bot_help_documents_alias():
    runner = CliRunner()
    r = runner.invoke(main, ["detach-bot", "--help"])
    assert r.exit_code == 0
    # The help text should make clear this is a remove-evolve alias so
    # operators looking at one find the other.
    assert "remove-evolve" in r.output.lower() or "alias" in r.output.lower()


# ── lifecycle inventory ──────────────────────────────────────────────


def _write_network(tmp_path: Path, bot_id: str = "admin_bot", **extra) -> Path:
    """Write a minimal network.json under tmp_path; return the path."""
    p = tmp_path / "network.json"
    bot_cfg = {"role": "member", "port": 18800}
    bot_cfg.update(extra)
    p.write_text(json.dumps({
        "members": [bot_id, "evolve"],
        "primary": "evolve",
        "bots": {bot_id: bot_cfg},
        "sharedDir": str(tmp_path / "shared"),
    }))
    return p


def test_lifecycle_inventory_renders_human_output(tmp_path: Path, monkeypatch):
    network_path = _write_network(tmp_path)
    # Stub bot_home so the inventory checks the tmp_path
    from evolve_admin import lifecycle as _lc_pkg
    monkeypatch.setattr(
        "evolve_admin.lifecycle.inventory._default_resolvers",
        lambda: (
            lambda bid, nw: tmp_path / "Users" / bid,
            lambda bid, nw: bid,
        ),
    )
    # Create the bot home so workspace_summary doesn't fire
    (tmp_path / "Users" / "admin_bot" / ".openclaw" / "workspace").mkdir(parents=True)

    runner = CliRunner()
    r = runner.invoke(
        main,
        ["--network", str(network_path), "lifecycle", "inventory", "admin_bot"],
    )
    assert r.exit_code == 0, r.output
    assert "admin_bot" in r.output
    assert "total items" in r.output
    # The category labels should appear
    assert "macos_user" in r.output or "network" in r.output


def test_lifecycle_inventory_json_emits_valid_json(tmp_path: Path, monkeypatch):
    network_path = _write_network(tmp_path)
    monkeypatch.setattr(
        "evolve_admin.lifecycle.inventory._default_resolvers",
        lambda: (
            lambda bid, nw: tmp_path / "Users" / bid,
            lambda bid, nw: bid,
        ),
    )
    (tmp_path / "Users" / "admin_bot" / ".openclaw" / "workspace").mkdir(parents=True)

    runner = CliRunner()
    r = runner.invoke(
        main,
        ["--network", str(network_path), "lifecycle", "inventory", "admin_bot", "--json"],
    )
    assert r.exit_code == 0, r.output
    # Must parse cleanly
    payload = json.loads(r.output)
    assert payload["bot_id"] == "admin_bot"
    assert "items" in payload
    assert "summary" in payload
    # Removed_by lists should be present per item
    for item in payload["items"]:
        assert isinstance(item["removed_by"], list)


def test_lifecycle_inventory_action_filter(tmp_path: Path, monkeypatch):
    """--action detach should narrow to only items removed by detach."""
    network_path = _write_network(tmp_path)
    monkeypatch.setattr(
        "evolve_admin.lifecycle.inventory._default_resolvers",
        lambda: (
            lambda bid, nw: tmp_path / "Users" / bid,
            lambda bid, nw: bid,
        ),
    )
    (tmp_path / "Users" / "admin_bot" / ".openclaw" / "workspace").mkdir(parents=True)

    runner = CliRunner()
    r = runner.invoke(
        main,
        ["--network", str(network_path),
         "lifecycle", "inventory", "admin_bot", "--action", "detach"],
    )
    assert r.exit_code == 0, r.output
    assert "Filtered to items removed by" in r.output
    assert "detach" in r.output
    # macOS user is delete-only; should NOT appear under --action detach
    # (the section header may still appear in the summary line, so check
    # the actual rendered list for the macOS user item)
    assert "macOS user 'admin_bot'" not in r.output


def test_lifecycle_inventory_for_nonexistent_bot_still_runs(tmp_path: Path, monkeypatch):
    """compile_bot_inventory shouldn't crash on a bot that isn't in network.json
    (it still surfaces macOS-user state from the resolver fallback)."""
    network_path = _write_network(tmp_path, "admin_bot")
    monkeypatch.setattr(
        "evolve_admin.lifecycle.inventory._default_resolvers",
        lambda: (
            lambda bid, nw: tmp_path / "Users" / bid,
            lambda bid, nw: bid,
        ),
    )

    runner = CliRunner()
    r = runner.invoke(
        main,
        ["--network", str(network_path), "lifecycle", "inventory", "ghost-bot"],
    )
    # Shouldn't crash; should produce some output.
    assert r.exit_code == 0, r.output
    assert "ghost-bot" in r.output


# ── pre-flight preview helper ────────────────────────────────────────


def test_pre_flight_preview_helper_tolerates_missing_state(tmp_path: Path, capsys):
    """Helper used by retire-bot and remove-evolve. Must not raise on a
    bot whose state is missing — the lifecycle command continues.

    Moved out of ``cli`` into ``lifecycle.cli_output`` (cli.py is at its
    no-growth cap, and the lifecycle package already owns the manual-cleanup
    checklist this presentation layer feeds). The console is now passed in,
    which also makes the output capturable — so this asserts on it rather than
    only on the absence of an exception.
    """
    from io import StringIO

    from rich.console import Console

    from evolve_admin.lifecycle.cli_output import print_lifecycle_preview

    buf = StringIO()
    bogus_network_path = tmp_path / "no-such-network.json"

    try:
        print_lifecycle_preview(
            Console(file=buf, no_color=True), "admin_bot",
            bogus_network_path, action="detach",
        )
    except Exception as e:
        pytest.fail(f"preview helper raised on missing network: {e}")

    # Either outcome is fine — a degraded-but-real preview (load_network falls
    # back to defaults rather than raising) or the "unavailable" note. What
    # must never happen is an exception or silence.
    out = buf.getvalue()
    assert "Pre-flight inventory" in out or "inventory preview unavailable" in out
