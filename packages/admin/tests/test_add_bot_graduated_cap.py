"""add_bot stamps a creation timestamp so the graduated new-bot daily-cap
default (better_engine_config) can age-grade the bot.

Decision A of the new-bot cost-defaults design-sync (META:user-value). The
old behavior materialized a static $10 per-bot cap at creation; a static value
never graduates, so the cap is now resolved in code from the bot's age and
add_bot only records ``created_at``. An explicit ``daily_cap_usd`` is still
written as a per-bot override.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.deploy import add_bot  # noqa: E402


def _write_network(path: Path, shared_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"bots": {}, "members": [], "sharedDir": str(shared_dir)}, indent=2
        )
    )


def _read_be(shared_dir: Path) -> dict:
    return json.loads((shared_dir / "better-engine-config.json").read_text())


def test_add_bot_stamps_created_at(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    _write_network(net, shared)

    add_bot("ledger", port=19000, network_path=net)

    be = _read_be(shared)
    assert "created_at" in be["bots"]["ledger"], "add_bot must stamp created_at"
    # No static per-bot daily hard cap is materialized by default — the
    # graduated default governs instead.
    budget = be["bots"]["ledger"].get("budget", {})
    assert "per_bot_daily_hard_usd" not in budget


def test_add_bot_explicit_cap_written_as_override(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    _write_network(net, shared)

    add_bot("darwin", port=19030, role="primary", daily_cap_usd=25.0,
            network_path=net)

    be = _read_be(shared)
    assert be["bots"]["darwin"]["budget"]["per_bot_daily_hard_usd"] == 25.0
    # created_at is still stamped so graduation applies once the explicit cap
    # is cleared.
    assert "created_at" in be["bots"]["darwin"]


def test_add_bot_zero_cap_skips_override_but_still_stamps(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    _write_network(net, shared)

    add_bot("ledger", port=19000, daily_cap_usd=0, network_path=net)

    be = _read_be(shared)
    assert "per_bot_daily_hard_usd" not in be["bots"]["ledger"].get("budget", {})
    assert "created_at" in be["bots"]["ledger"]


def test_add_bot_graduated_cap_resolves_for_fresh_bot(tmp_path: Path):
    """End-to-end: add_bot a fresh bot, then the resolver returns $10."""
    import better_engine_config as bec

    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    _write_network(net, shared)

    add_bot("ledger", port=19000, network_path=net)

    cfg = bec.load(shared)
    # Fresh bot (stamped ~now) is inside the activation window → $10.
    assert cfg.budget_hard_cap_usd("ledger") == 10.00
