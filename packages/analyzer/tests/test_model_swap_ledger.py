"""tests/test_model_swap_ledger.py — the append-only model-swap ledger.

The ledger is the seam both halves of the 2026-08-14 guard hang off (design:
docs/design-model-swap-behavior-guard-2026-08-19.md): ``evolve-admin models
rollback`` reads it for the undo target, and ``model_swap_watch`` reads it to
learn when each rung changed. Its correctness contract is small but
load-bearing:

  * a real swap is recorded with the PRE-write models;
  * a no-op swap is not recorded (it would give the watcher a change instant
    at which nothing changed);
  * an unreadable or partially-torn ledger degrades to a partial read rather
    than raising — the file is appended to by a long-lived web process, so a
    torn final line is a live possibility;
  * a failed write never raises into the caller, because the config write it
    is reporting has ALREADY happened.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_swap_ledger as ledger  # noqa: E402


def test_records_a_real_swap(tmp_path):
    assert ledger.record_swap(
        "team-bot-a", "standard", "anthropic",
        ["anthropic/claude-sonnet-4-6"], ["anthropic/claude-sonnet-5"],
        source="admin_ui_bulk", shared_dir=tmp_path,
    ) is True
    rows = ledger.read_swaps(tmp_path)
    assert len(rows) == 1
    assert rows[0]["bot_id"] == "team-bot-a"
    assert rows[0]["tier"] == "standard"
    assert rows[0]["previous_models"] == ["anthropic/claude-sonnet-4-6"]
    assert rows[0]["new_models"] == ["anthropic/claude-sonnet-5"]
    assert rows[0]["source"] == "admin_ui_bulk"
    assert rows[0]["ts"]


def test_noop_swap_is_not_recorded(tmp_path):
    """previous == new carries no undo target and no behavioral boundary."""
    assert ledger.record_swap(
        "team-bot-a", "standard", "anthropic", ["a/m"], ["a/m"],
        source="admin_ui_single", shared_dir=tmp_path,
    ) is False
    assert ledger.read_swaps(tmp_path) == []


def test_missing_ledger_reads_empty_not_raises(tmp_path):
    assert ledger.read_swaps(tmp_path / "nope") == []
    assert ledger.latest_swaps_by_rung(tmp_path / "nope") == {}


def test_torn_final_line_does_not_blind_the_reader(tmp_path):
    ledger.record_swap("team-bot-a", "standard", "a", ["a/1"], ["a/2"],
                       source="s", shared_dir=tmp_path)
    with open(ledger.swap_ledger_path(tmp_path), "a") as fh:
        fh.write('{"bot_id": "team-bot-a", "tier": "sta')  # interrupted append
    rows = ledger.read_swaps(tmp_path)
    assert len(rows) == 1, "the intact record must still be readable"


def test_records_missing_required_fields_are_skipped(tmp_path):
    path = ledger.swap_ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"bot_id": "team-bot-a", "tier": "standard", "ts": "2026-08-14T00:00:00+00:00"})
        + "\n"
        + json.dumps({"bot_id": "team-bot-a"}) + "\n"       # no tier / ts
        + json.dumps(["not", "a", "dict"]) + "\n"
        + "\n"
    )
    assert len(ledger.read_swaps(tmp_path)) == 1


def test_latest_swap_per_rung_wins(tmp_path):
    """Rolling back undoes the MOST RECENT change, not the whole history."""
    ledger.record_swap("team-bot-a", "standard", "a", ["a/1"], ["a/2"], source="s", shared_dir=tmp_path)
    ledger.record_swap("team-bot-a", "standard", "a", ["a/2"], ["a/3"], source="s", shared_dir=tmp_path)
    ledger.record_swap("team-bot-a", "fast", "a", ["a/9"], ["a/8"], source="s", shared_dir=tmp_path)
    latest = ledger.latest_swaps_by_rung(tmp_path)
    assert latest[("team-bot-a", "standard")]["previous_models"] == ["a/2"]
    assert latest[("team-bot-a", "fast")]["previous_models"] == ["a/9"]


def test_write_failure_returns_false_instead_of_raising(tmp_path):
    """The config write already succeeded — losing the ledger line must not
    turn a successful apply into a reported failure."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    assert ledger.record_swap("team-bot-a", "standard", "a", ["a/1"], ["a/2"],
                              source="s", shared_dir=blocker) is False


def test_default_path_is_platform_keyed_not_a_users_literal():
    """A hardcoded /Users path is the Linux-silent-break class."""
    from platform_profile import get_profile

    assert ledger.swap_ledger_path() == Path(get_profile().shared_dir_default) / "model_swaps.jsonl"


def test_rollback_records_are_skippable_when_picking_an_undo_target(tmp_path):
    """`models rollback` must be idempotent. Without this, the rollback IS the
    most recent change, so running the command twice would restore the model
    the operator just backed out of — a silent re-break dressed as an undo."""
    ledger.record_swap("team-bot-a", "standard", "a", ["a/old"], ["a/new"],
                       source="admin_ui_bulk", shared_dir=tmp_path)
    ledger.record_swap("team-bot-a", "standard", "a", ["a/new"], ["a/old"],
                       source=ledger.ROLLBACK_SOURCE, shared_dir=tmp_path)

    # Unfiltered: the rollback is the latest record (what model_swap_watch
    # wants — a rollback is a fresh behavioral boundary to re-evaluate).
    unfiltered = ledger.latest_swaps_by_rung(tmp_path)[("team-bot-a", "standard")]
    assert unfiltered["source"] == ledger.ROLLBACK_SOURCE

    # Filtered: the operator swap is still the undo target.
    filtered = ledger.latest_swaps_by_rung(
        tmp_path, exclude_sources={ledger.ROLLBACK_SOURCE},
    )[("team-bot-a", "standard")]
    assert filtered["source"] == "admin_ui_bulk"
    assert filtered["previous_models"] == ["a/old"]


def test_rollbacks_still_appear_in_the_full_history(tmp_path):
    """Filtering is a choice at the call site, never a rewrite of the record."""
    ledger.record_swap("team-bot-a", "standard", "a", ["a/old"], ["a/new"],
                       source="admin_ui_bulk", shared_dir=tmp_path)
    ledger.record_swap("team-bot-a", "standard", "a", ["a/new"], ["a/old"],
                       source=ledger.ROLLBACK_SOURCE, shared_dir=tmp_path)
    assert [r["source"] for r in ledger.read_swaps(tmp_path)] == [
        "admin_ui_bulk", ledger.ROLLBACK_SOURCE
    ]
