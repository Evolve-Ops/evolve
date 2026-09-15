"""Regression: the CLI `evolve-admin remove` is a deprecated alias for
`detach-bot`, and the broken `deploy.remove_bot` function is gone.

PR #1903 fixed the UI button + the evo MCP tool + the admin daemon's
HTTP endpoint, but did not touch the CLI `remove` command — which still
called the broken `deploy.remove_bot` (only unloaded the long-defunct
`ai.openclaw.evolve.measure.<bot>` plist; silently left 7+ per-bot
Evolve daemons running). This follow-up wires the CLI through
`detach-bot` and deletes the broken function entirely.

These tests pin both invariants so a future PR can't silently regress
either.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))


def test_deploy_remove_bot_no_longer_exists() -> None:
    """``deploy.remove_bot`` was removed after PR #1903 deprecated all
    its callers. If a future PR reintroduces it, this test trips so
    the reviewer can question whether the old (broken) behavior is
    really what they want.
    """
    from evolve_admin import deploy
    assert not hasattr(deploy, "remove_bot"), (
        "deploy.remove_bot was removed because it only unloaded the "
        "legacy `ai.openclaw.evolve.measure.<bot>.plist` (not installed "
        "per-bot since the measure migration to pod-wide) and stripped "
        "the network.json entry — leaving 7+ per-bot Evolve daemons "
        "running. Use retire.remove_evolve_plugin (disconnect, via "
        "/api/lifecycle/detach), retire.retire_bot (graceful archive, "
        "via /api/lifecycle/retire), or retire.delete_bot (irreversible, "
        "via /api/lifecycle/delete) instead."
    )


def test_cli_remove_command_is_deprecated_alias_for_detach_bot() -> None:
    """`evolve-admin remove` is a deprecated alias for `detach-bot`.

    Both Click commands must exist; the `remove` command's help text
    must call out the deprecation so operators see it when they reach
    for --help before changing their scripts. (We don't invoke the
    runner — that would require root for the launchctl sudo check.)
    """
    from evolve_admin.cli import main as _cli_main

    remove_cmd = _cli_main.commands.get("remove")
    detach_cmd = _cli_main.commands.get("detach-bot")
    assert remove_cmd is not None, "evolve-admin remove command missing"
    assert detach_cmd is not None, "evolve-admin detach-bot command missing"

    help_text = (remove_cmd.help or "") + " " + (remove_cmd.__doc__ or "")
    assert "deprecated" in help_text.lower(), (
        "`evolve-admin remove --help` must call out the deprecation; "
        "operators look at --help before changing scripts"
    )
