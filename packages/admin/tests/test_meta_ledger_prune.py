"""Unit tests for tools/meta-ledger-prune's scaled size budget and activity dating.

The size budget used to be a flat 8192 bytes. That punished the busiest aspects
for being busy: a ledger legitimately tracking 40 live items cannot fit the same
cap as one tracking 3, so the flat cap was permanently red for every active
aspect and therefore carried no signal. The budget now scales with in-flight
work, which turns it into a DENSITY measure — "is this ledger carrying more
prose per item than an item is worth?" — with the two properties these tests
pin: a busy well-kept ledger passes, and a quiet bloated one fails.

Rules mirrored in docs/meta-ledger-schema.md ("Size budget & pruning").

The tool is an extensionless script under tools/, so we load it by path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "meta-ledger-prune"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("meta_ledger_prune", str(_TOOL))
    spec = importlib.util.spec_from_loader("meta_ledger_prune", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_ledger_prune"] = mod
    loader.exec_module(mod)
    return mod


mlp = _load_tool()


def _chip(cid, bucket):
    return {"id": cid, "title": cid, "bucket": bucket}


def _ledger(live_chips=0, terminal_chips=0, **lists):
    obj = {"aspect": "t", "updated": "2026-08-18", "chips": []}
    for i in range(live_chips):
        obj["chips"].append(_chip("live-%d" % i, "open_green"))
    for i in range(terminal_chips):
        obj["chips"].append(_chip("done-%d" % i, "merged"))
    obj.update(lists)
    return obj


# ── the budget scales ────────────────────────────────────────────────────────


def test_empty_ledger_gets_only_the_base():
    budget, live, archived = mlp.size_budget(_ledger())
    assert (live, archived) == (0, 0)
    assert budget == mlp.BASE_BUDGET_BYTES


def test_each_live_chip_adds_a_live_allowance():
    base, _, _ = mlp.size_budget(_ledger())
    budget, live, archived = mlp.size_budget(_ledger(live_chips=3))
    assert (live, archived) == (3, 0)
    assert budget == base + 3 * mlp.PER_LIVE_ITEM_BYTES


def test_terminal_chips_are_archived_and_cost_less_than_live_ones():
    live_budget, _, _ = mlp.size_budget(_ledger(live_chips=5))
    arch_budget, live, archived = mlp.size_budget(_ledger(terminal_chips=5))
    assert (live, archived) == (0, 5)
    assert arch_budget < live_budget
    assert arch_budget == mlp.BASE_BUDGET_BYTES + 5 * mlp.PER_ARCHIVED_ITEM_BYTES


def test_backlog_decisions_and_gates_count_as_live():
    budget, live, archived = mlp.size_budget(
        _ledger(backlog=["a", "b"], decisions_pending=["d"], gates=["g"])
    )
    assert (live, archived) == (4, 0)
    assert budget == mlp.BASE_BUDGET_BYTES + 4 * mlp.PER_LIVE_ITEM_BYTES


def test_resolved_decisions_and_discharged_gates_count_as_archived():
    _, live, archived = mlp.size_budget(
        _ledger(decisions_resolved=["r"], gates_discharged=["g"], decisions_log=["l"])
    )
    assert (live, archived) == (0, 3)


def test_budget_uses_the_same_terminal_predicate_as_the_collapse_rule():
    """The budget and the prune must not disagree about what is finished."""
    for bucket in ("done", "live", "merged"):
        _, live, archived = mlp.size_budget(_ledger(**{"chips": [_chip("c", bucket)]}))
        assert (live, archived) == (0, 1), bucket
    for bucket in ("open_green", "open_red", "stalled", "click-pending", "dispatched"):
        _, live, archived = mlp.size_budget(_ledger(**{"chips": [_chip("c", bucket)]}))
        assert (live, archived) == (1, 0), bucket


# ── the two properties the scaling exists for ────────────────────────────────


def test_busy_but_well_kept_ledger_is_not_flagged():
    """40 live items with terse rows must PASS — the flat cap's core failure."""
    obj = _ledger(backlog=["item %d" % i for i in range(40)])
    budget, _, _ = mlp.size_budget(obj)
    size = len(json.dumps(obj))
    assert size < budget
    assert mlp.collect_flags(obj, size, "busy.json") == []


def test_quiet_but_bloated_ledger_is_flagged():
    """Few items, huge file — exactly what the budget should still catch."""
    obj = _ledger(live_chips=1)
    obj["chips"][0]["note"] = "x" * 20000
    size = len(json.dumps(obj))
    flags = mlp.collect_flags(obj, size, "bloated.json")
    assert any("bloated.json" in f and "budget" in f for f in flags)


def test_flag_reports_the_inputs_and_the_ratio():
    """An over-budget warning must show WHY, so the reader can act on it."""
    obj = _ledger(live_chips=2, terminal_chips=1)
    flags = mlp.collect_flags(obj, 99999, "x.json")
    warning = next(f for f in flags if "budget" in f)
    assert "2 live + 1 archived items" in warning
    assert "x)" in warning  # the density ratio


def test_a_ledger_at_exactly_its_budget_is_not_flagged():
    obj = _ledger(live_chips=2)
    budget, _, _ = mlp.size_budget(obj)
    assert not any("budget" in f for f in mlp.collect_flags(obj, budget, "edge.json"))
    assert any("budget" in f for f in mlp.collect_flags(obj, budget + 1, "edge.json"))


# ── prose flagging is unchanged ──────────────────────────────────────────────


def test_prose_flags_still_fire_independently_of_the_size_budget():
    obj = _ledger()
    obj["bout"] = "y" * (mlp.PROSE_MAX_CHARS + 1)
    obj["next_action"] = "z" * (mlp.PROSE_MAX_CHARS + 1)
    flags = mlp.collect_flags(obj, 10, "prose.json")
    assert sum("chars" in f for f in flags) == 2
    assert not any("budget" in f for f in flags)


# ── activity dating (what decides current-bout vs prior-bout) ────────────────

import datetime  # noqa: E402

BOUT = datetime.date(2026, 8, 18)


def test_a_sha_last_commit_no_longer_hides_a_same_bout_merge():
    """The #3661 case: merged today, dispatched three days ago, SHA in last_commit.

    Reading only {dispatched, last_commit} dated this prior-bout and collapsed it
    twenty minutes after it merged, dropping the merge commit and the reconcile record.
    """
    chip = _chip("WO-H0-9", "merged")
    chip["dispatched"] = "2026-08-15"
    chip["last_commit"] = "64468c6bbec8dbe79cae6264a286546cd3860c9b"
    chip["reconciled_sweep89"] = "2026-08-18 sweep 89: AUTO-MERGED, squash 64468c6bb."
    assert mlp._chip_activity_date(chip, BOUT) == datetime.date(2026, 8, 18)
    assert not mlp._should_collapse(chip, BOUT, 1)


def test_a_date_embedded_mid_prose_is_found():
    chip = _chip("c", "merged")
    chip["dispatched"] = "2026-06-01"
    chip["note"] = "Row minted 2026-08-18 by meta-reconcile on operator instruction."
    assert mlp._chip_activity_date(chip, BOUT) == BOUT


def test_dates_after_the_bout_date_are_ignored():
    """A reference is not activity — otherwise 'revisit 2026-12-01' pins a row forever."""
    chip = _chip("c", "merged")
    chip["dispatched"] = "2026-06-01"
    chip["note"] = "revisit 2026-12-01 once the gate clears"
    assert mlp._chip_activity_date(chip, BOUT) == datetime.date(2026, 6, 1)
    assert mlp._should_collapse(chip, BOUT, 1)


def test_a_genuinely_old_terminal_chip_still_collapses():
    """The widening must not make the tool toothless."""
    chip = _chip("old", "merged")
    chip["dispatched"] = "2026-06-23"
    chip["note"] = "landed 2026-06-29, nothing since"
    assert mlp._should_collapse(chip, BOUT, 1)


def test_widening_can_only_keep_never_collapse():
    """Property: any chip the OLD two-field logic kept, the new logic also keeps."""
    for dispatched, last_commit in (
        ("2026-08-18", "deadbeef"), ("2026-06-01", "2026-08-18"), ("2026-08-17", None),
    ):
        chip = _chip("c", "merged")
        chip["dispatched"] = dispatched
        if last_commit:
            chip["last_commit"] = last_commit
        old_dates = [
            d for d in (mlp._parse_date(chip.get("dispatched")),
                        mlp._parse_date(chip.get("last_commit"))) if d
        ]
        old_kept = bool(old_dates) and max(old_dates) >= BOUT - datetime.timedelta(days=1)
        if old_kept:
            assert not mlp._should_collapse(chip, BOUT, 1), (dispatched, last_commit)


# ── the undated default ──────────────────────────────────────────────────────


def test_an_undated_terminal_chip_is_kept_by_default():
    """Absence of a date is not evidence of age."""
    chip = _chip("c", "merged")
    assert mlp._chip_activity_date(chip, BOUT) is None
    assert not mlp._should_collapse(chip, BOUT, 1)


def test_collapse_undated_opts_back_into_the_old_behaviour():
    chip = _chip("c", "merged")
    assert mlp._should_collapse(chip, BOUT, 1, True)


def test_undated_non_terminal_chips_are_still_never_collapsed():
    chip = _chip("c", "open_green")
    assert not mlp._should_collapse(chip, BOUT, 1, True)


def test_prune_ledger_threads_the_flag_through():
    obj = _ledger()
    obj["chips"] = [dict(_chip("c", "merged"), note="x" * 500)]
    _, n_default = mlp.prune_ledger(obj, BOUT, 1)
    _, n_forced = mlp.prune_ledger(obj, BOUT, 1, True)
    assert (n_default, n_forced) == (0, 1)


# ── bout_start (which date starts the current bout) ──────────────────────────


def test_bout_start_wins_over_updated():
    obj = {"updated": "2026-08-18", "bout_start": "2026-08-17"}
    assert mlp._ledger_bout_date(obj) == datetime.date(2026, 8, 17)


def test_updated_is_the_fallback_when_bout_start_is_absent():
    assert mlp._ledger_bout_date({"updated": "2026-08-18"}) == BOUT


def test_a_malformed_bout_start_falls_back_rather_than_failing():
    obj = {"updated": "2026-08-18", "bout_start": "not-a-date"}
    assert mlp._ledger_bout_date(obj) == BOUT


def test_no_date_at_all_is_still_none():
    assert mlp._ledger_bout_date({}) is None


def test_touching_a_ledger_no_longer_ages_its_own_chips():
    """The B1-census case: a chip that merged 2 days ago, and a prose edit that bumped
    `updated` by a day. With bout_start pinned to the real bout, the chip survives an
    edit it had nothing to do with."""
    chip = _chip("B1-census", "merged")
    chip["merged"] = "2026-08-16"

    # before: bout read from `updated`, which a prose edit moved 08-17 -> 08-18
    aged = mlp._ledger_bout_date({"updated": "2026-08-18"})
    assert mlp._should_collapse(chip, aged, 1)

    # after: bout_start pins the real bout start, so the same edit is inert
    pinned = mlp._ledger_bout_date({"updated": "2026-08-18", "bout_start": "2026-08-17"})
    assert not mlp._should_collapse(chip, pinned, 1)
