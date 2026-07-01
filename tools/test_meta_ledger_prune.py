"""Mechanical tests for tools/meta-ledger-prune (META:substrate Initiative 5, lever E3).

Pure-data + filesystem tests; no network, no live ledgers — every test runs against a
fixture written into a tmp dir.

Run with:
  cd tools && python3 -m pytest test_meta_ledger_prune.py -v
"""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

# The tool has no .py extension (like the other tools/ executables), so load it by path.
_TOOL = Path(__file__).parent / "meta-ledger-prune"
_loader = SourceFileLoader("meta_ledger_prune", str(_TOOL))
_spec = importlib.util.spec_from_loader("meta_ledger_prune", _loader)
mlp = importlib.util.module_from_spec(_spec)
_loader.exec_module(mlp)

BOUT = "2026-06-18"   # the fixture ledger's `updated` / current-bout date


def _fixture():
    """A ledger exercising every branch: non-terminal (old + current), terminal-prior-bout,
    terminal-current-bout, and a terminal chip with no date."""
    return {
        "aspect": "fixture",
        "updated": BOUT,
        "bout": "one line is fine",
        "next_action": "also one line",
        "chips": [
            # non-terminal, OLD — must survive verbatim (bucket, not date, decides keep)
            {"id": "NT1", "title": "still building", "pr": 100, "bucket": "dispatched",
             "two_pass": "pending", "dispatched": "2026-06-01", "note": "long note kept verbatim"},
            # non-terminal, open_green OLD — survive verbatim
            {"id": "NT2", "title": "awaiting merge", "pr": 101, "bucket": "open_green",
             "two_pass": "PASS", "dispatched": "2026-06-02", "note": "kept"},
            # terminal (merged), PRIOR bout — must collapse
            {"id": "T1", "title": "old merged thing", "pr": 102, "branch": "claude/x",
             "bucket": "merged", "two_pass": "PASS", "privileged": False, "reversible": True,
             "dispatched": "2026-06-05", "last_commit": "abc1234",
             "note": "a long narrative that belongs in git history + memory, not the ledger",
             "output": "memory:something"},
            # terminal (live), PRIOR bout — must collapse
            {"id": "T2", "title": "old live thing", "pr": 103, "bucket": "live",
             "two_pass": "PASS", "dispatched": "2026-06-06", "note": "drop me"},
            # terminal (done), CURRENT bout — must survive verbatim
            {"id": "C1", "title": "done this bout", "pr": 104, "bucket": "done",
             "two_pass": "PASS", "dispatched": BOUT, "note": "still relevant this bout"},
            # terminal, NO date — treated as old → collapse
            {"id": "U1", "title": "undated terminal", "pr": 105, "bucket": "merged",
             "two_pass": "n/a", "note": "no dispatched/last_commit"},
        ],
        "backlog": ["some idea", "another idea"],
    }


def _by_id(chips):
    return {c["id"]: c for c in chips}


def _write(tmp_path, obj, name="fixture.json"):
    p = tmp_path / name
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


# ── pure prune logic ────────────────────────────────────────────────────────────────────

def test_non_terminal_chips_survive_untouched():
    obj = _fixture()
    bd = mlp._parse_date(BOUT)
    new_obj, _ = mlp.prune_ledger(obj, bd, mlp.DEFAULT_KEEP_DAYS)
    new = _by_id(new_obj["chips"])
    old = _by_id(obj["chips"])
    for cid in ("NT1", "NT2"):
        assert new[cid] == old[cid], cid + " (non-terminal) must be kept verbatim, notes and all"


def test_terminal_prior_bout_collapses_to_archive_form():
    obj = _fixture()
    bd = mlp._parse_date(BOUT)
    new_obj, n = mlp.prune_ledger(obj, bd, mlp.DEFAULT_KEEP_DAYS)
    new = _by_id(new_obj["chips"])
    # T1, T2, U1 collapse; C1 (current bout) does not
    assert n == 3
    assert new["T1"] == {"id": "T1", "title": "old merged thing", "pr": 102,
                         "bucket": "merged", "two_pass": "PASS"}
    assert "note" not in new["T1"] and "output" not in new["T1"] and "branch" not in new["T1"]
    assert new["T2"] == {"id": "T2", "title": "old live thing", "pr": 103,
                         "bucket": "live", "two_pass": "PASS"}
    # undated terminal collapses too (no date → treated as old)
    assert new["U1"] == {"id": "U1", "title": "undated terminal", "pr": 105,
                         "bucket": "merged", "two_pass": "n/a"}


def test_current_bout_terminal_is_kept_verbatim():
    obj = _fixture()
    bd = mlp._parse_date(BOUT)
    new_obj, _ = mlp.prune_ledger(obj, bd, mlp.DEFAULT_KEEP_DAYS)
    new = _by_id(new_obj["chips"])
    assert new["C1"] == _by_id(obj["chips"])["C1"], "current-bout terminal chip must keep its note"


def test_chip_order_and_count_preserved():
    obj = _fixture()
    bd = mlp._parse_date(BOUT)
    new_obj, _ = mlp.prune_ledger(obj, bd, mlp.DEFAULT_KEEP_DAYS)
    assert [c["id"] for c in new_obj["chips"]] == [c["id"] for c in obj["chips"]]


def test_prune_does_not_mutate_input():
    obj = _fixture()
    snapshot = json.dumps(obj, sort_keys=True)
    mlp.prune_ledger(obj, mlp._parse_date(BOUT), mlp.DEFAULT_KEEP_DAYS)
    assert json.dumps(obj, sort_keys=True) == snapshot, "prune_ledger must be pure"


def test_idempotent():
    obj = _fixture()
    bd = mlp._parse_date(BOUT)
    once, n1 = mlp.prune_ledger(obj, bd, mlp.DEFAULT_KEEP_DAYS)
    twice, n2 = mlp.prune_ledger(once, bd, mlp.DEFAULT_KEEP_DAYS)
    assert n1 == 3 and n2 == 0
    assert twice == once
    assert mlp.emit_ledger(twice) == mlp.emit_ledger(once), "second pass must be byte-identical"


def test_emit_ledger_round_trips():
    obj = _fixture()
    new_obj, _ = mlp.prune_ledger(obj, mlp._parse_date(BOUT), mlp.DEFAULT_KEEP_DAYS)
    assert json.loads(mlp.emit_ledger(new_obj)) == new_obj


def test_no_bout_date_skips_pruning():
    obj = _fixture()
    del obj["updated"]
    # no `updated`, no override → bout_date is None → nothing collapses
    bd = mlp._parse_date(obj.get("updated"))
    assert bd is None
    # prune_ledger requires a date; the CLI is what skips. Verify via main() below.


def test_keep_days_widens_window():
    obj = _fixture()
    # A chip dispatched the day before the bout is prior-bout at keep_days=0 but current at 1.
    obj["chips"].append({"id": "EDGE", "title": "day before", "pr": 200, "bucket": "merged",
                         "two_pass": "PASS", "dispatched": "2026-06-17", "note": "edge"})
    bd = mlp._parse_date(BOUT)
    new0 = _by_id(mlp.prune_ledger(obj, bd, 0)[0]["chips"])
    new1 = _by_id(mlp.prune_ledger(obj, bd, 1)[0]["chips"])
    assert "note" not in new0["EDGE"], "keep_days=0 → 06-17 is prior bout → collapses"
    assert new1["EDGE"].get("note") == "edge", "keep_days=1 → 06-17 is within window → kept"


# ── CLI: dry-run vs apply ─────────────────────────────────────────────────────────────────

def test_dry_run_default_writes_nothing(tmp_path, capsys):
    p = _write(tmp_path, _fixture())
    before = p.read_bytes()
    rc = mlp.main(["--dir", str(tmp_path)])          # dry-run is the default
    assert rc == 0
    assert p.read_bytes() == before, "dry-run must not touch the ledger"
    assert not list(tmp_path.parent.glob("*.backup-*")), "dry-run must not create a backup"
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "would collapse" in out


def test_apply_backs_up_then_writes(tmp_path, capsys):
    p = _write(tmp_path, _fixture())
    before = p.read_bytes()
    rc = mlp.main(["--dir", str(tmp_path), "--apply"])
    assert rc == 0
    # file shrank + collapsed
    after_obj = json.loads(p.read_text(encoding="utf-8"))
    assert "note" not in _by_id(after_obj["chips"])["T1"]
    assert len(p.read_bytes()) < len(before)
    # exactly one backup dir, holding the ORIGINAL bytes
    backups = list(tmp_path.parent.glob(tmp_path.name + ".backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "fixture.json").read_bytes() == before


def test_apply_is_idempotent(tmp_path):
    _write(tmp_path, _fixture())
    mlp.main(["--dir", str(tmp_path), "--apply"])
    first = (tmp_path / "fixture.json").read_bytes()
    mlp.main(["--dir", str(tmp_path), "--apply"])     # second apply: nothing to write
    assert (tmp_path / "fixture.json").read_bytes() == first
    assert len(list(tmp_path.parent.glob(tmp_path.name + ".backup-*"))) == 1, "no 2nd backup"


def test_cli_skips_ledger_with_no_bout_date(tmp_path, capsys):
    obj = _fixture()
    del obj["updated"]
    p = _write(tmp_path, obj)
    before = p.read_bytes()
    mlp.main(["--dir", str(tmp_path), "--apply"])
    assert p.read_bytes() == before, "no bout date → pruning skipped → file untouched"
    assert "no `updated` date" in capsys.readouterr().out


def test_bout_date_override(tmp_path):
    obj = _fixture()
    del obj["updated"]
    _write(tmp_path, obj)
    # override supplies the bout date the ledger lacks → pruning proceeds
    mlp.main(["--dir", str(tmp_path), "--bout-date", BOUT, "--apply"])
    after = json.loads((tmp_path / "fixture.json").read_text(encoding="utf-8"))
    assert "note" not in _by_id(after["chips"])["T1"]
