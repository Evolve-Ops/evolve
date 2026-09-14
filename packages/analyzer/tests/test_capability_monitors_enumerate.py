"""Regression: the capability-gap and engagement-amplifier monitors must
enumerate member bots and run to completion.

Both monitors used to do ``from evolve_admin.config import all_bot_ids`` in
``main()``'s default (no ``--bot``) branch. ``all_bot_ids`` never existed in
``evolve_admin.config`` (phantom introduced 2026-06-05, #2175), so every
launchd run raised ImportError, the broad ``except`` swallowed it as SystemExit,
and the monitors emitted ZERO Signals — starving the capability generators
(app_suggester, engagement_amplifier, pod_capability_lift) that only fire on
those Signals. These tests pin the corrected enumeration (network.json primary +
members, the cost_watchdog pattern) and guard against the phantom returning.

GOTCHA (test_web_scheduler_seam pattern): the monitor modules are imported
LAZILY inside each test, never at the module top, so importing them can't
pollute a shard for unrelated tests in the same worker.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402


# Each monitor module name + the human label used in failure messages.
_MONITORS = [
    "capability_gap_monitor",
    "engagement_amplifier_monitor",
]


@pytest.mark.parametrize("module_name", _MONITORS)
def test_main_enumerates_members_without_import_error(module_name, monkeypatch):
    """main() with no --bot reads network.json for primary + members and hands
    the full list to run_for_pod — without raising ImportError on a phantom."""
    import importlib

    m = importlib.import_module(module_name)

    captured: dict[str, list] = {}

    def _fake_run_for_pod(bot_ids, shared_dir, **kwargs):
        captured["bot_ids"] = list(bot_ids)
        return {"detections": 0, "signals_written": 0}

    # Stub the config resolution onto the monitor's own namespace (it does
    # `from evolve_config import load_config, get_primary, get_members`).
    monkeypatch.setattr(m, "load_config", lambda network=None: {"_fake": True})
    monkeypatch.setattr(m, "get_primary", lambda config: "primary-bot")
    monkeypatch.setattr(m, "get_members", lambda config: ["bot-a", "bot-b"])
    monkeypatch.setattr(m, "run_for_pod", _fake_run_for_pod)
    # No --bot → exercises the network.json default branch (the buggy path).
    monkeypatch.setattr(sys, "argv", [module_name])

    # Must run to completion — the bug raised SystemExit here.
    m.main()

    # Primary is prepended (not already among members), then members.
    assert captured["bot_ids"] == ["primary-bot", "bot-a", "bot-b"]


@pytest.mark.parametrize("module_name", _MONITORS)
def test_main_dedups_primary_already_in_members(module_name, monkeypatch):
    """If the primary is also listed among members, it isn't duplicated —
    matches cost_watchdog's ``primary not in members`` guard."""
    import importlib

    m = importlib.import_module(module_name)

    captured: dict[str, list] = {}

    monkeypatch.setattr(m, "load_config", lambda network=None: {})
    monkeypatch.setattr(m, "get_primary", lambda config: "bot-a")
    monkeypatch.setattr(m, "get_members", lambda config: ["bot-a", "bot-b"])
    monkeypatch.setattr(
        m, "run_for_pod",
        lambda bot_ids, shared_dir, **kw: captured.__setitem__(
            "bot_ids", list(bot_ids)
        ) or {},
    )
    monkeypatch.setattr(sys, "argv", [module_name])

    m.main()

    assert captured["bot_ids"] == ["bot-a", "bot-b"]


@pytest.mark.parametrize("module_name", _MONITORS)
def test_source_has_no_phantom_all_bot_ids_import(module_name):
    """Guard: the phantom ``from evolve_admin.config import all_bot_ids`` must
    never come back. It does not exist and silently kills the producer."""
    src = (_ANALYZER_DIR / f"{module_name}.py").read_text()
    assert "all_bot_ids" not in src, (
        f"{module_name} references the non-existent all_bot_ids — "
        "use evolve_config.load_config + get_primary/get_members instead"
    )
