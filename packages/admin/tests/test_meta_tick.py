"""`tools/meta-tick` — the dispatch tick as a command (D-MT1).

The runner is the procedure's steps 0 -> 7 as a fixed sequence of granted verbs. These
tests drive it with a FAKE subprocess (no git, no gh, no claude) and pin what the
procedure used to ask a model to remember: the ORDER of the verbs, every stop rule, the
`session_actions` shapes, the never-re-poke rule, and that nothing outside the granted
set can run. Several tests here replace text pins that used to live in
`test_meta_dispatch_procedure_text.py` — each names the pin it replaces, because the
behaviour moved from prose the model followed into code the runner executes.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "tools"))
import meta_tick as mt  # noqa: E402

SETUP = _REPO / "internal" / "meta-system-setup.md"
PROCEDURE = _REPO / "internal" / "meta-dispatch-procedure.md"

GREEN = {"ok": True, "lines": [], "may_reconcile": True, "may_launch": True,
         "login_note": "login: ok (cached 1h)"}


RIG_LINE = "rig 24h: merges 3 (chip 1 · PM 1 · app 0 · dependabot 1) · …"
STALE_LINE = "  48h+ #101 (72h, chip widget-one) → merge"


def _elig(**over):
    e = {"now": "2026-09-23T18:00:00Z", "blocked_by": None, "base": {"behind": 0},
         "base_line": "base: origin/main +0", "cards_waiting_line":
         "cards waiting for a tap: none", "candidates": [], "conflicts": [],
         "repairable": [], "orphan_done": [], "invalid": [], "held": [],
         "headless_liveness": [], "cards": [], "back_pressure": {"paused": False},
         "headless": {"available": True, "per_tick": 2}, "headless_slots": 2,
         "in_motion_slots": 1, "cap": 6, "prepared_count": 0, "prepared_cap": 4,
         "slots": 5, "prepared_slots": 4}
    e.update(over)
    return e


def _cand(bid, route="headless", privileged=False):
    return {"id": bid, "aspect": "substrate", "title": "Make the widget frobnicate",
            "chip_title": "[META:substrate] Make the widget frobnicate",
            "chip_title_timed": "[META:substrate] 11:00 [%s] Make the widget frobnicate"
                                % bid,
            "route": route, "privileged": privileged,
            "path": "internal/dispatch/queued/%s.md" % bid}


class Fake:
    """Answers by verb. `answers[key]` is a dict (stdout JSON, exit 0), a (code, stdout,
    stderr) tuple, or a list of either consumed in order."""

    def __init__(self, **answers):
        self.answers = {"now": {"now": "2026-09-23T18:00:00Z"}, "rig-preflight": GREEN,
                        "probe": GREEN,
                        "sync": {"pulled": True, "reason": "clean"},
                        "repair": {"line": "repair: queue empty"}, "gh": [],
                        "rig-line": {"line": RIG_LINE, "stale_lines": [STALE_LINE]},
                        "eligible": _elig(), "inflight": {"actionable_count": 0,
                                                          "count": 0, "warnings": []},
                        "land": {"landed": False, "reason": "clean",
                                 "lane_pr_line": "lane PRs open: 0",
                                 "lane_prs": {"poke_signature": None}},
                        "launch": {"moved": True, "headless": True, "session": "s1",
                                   "dispatched": "2026-09-23",
                                   "dispatched_at": "2026-09-23T18:00:00Z"}}
        self.answers.update(answers)

    @staticmethod
    def key(argv):
        if argv[0] == "gh":
            return "gh"
        tool = argv[1].rsplit("/", 1)[-1]
        if tool == "meta-dispatch-eligible":
            return "eligible"
        if tool == "meta-inflight":
            return "inflight"
        if argv[2] == "rig-preflight" and "--probe-login" in argv:
            return "probe"
        if argv[2] == "land-first":      # a land-first verb answers as the verb it wraps
            return argv[3]
        return argv[2]

    def __call__(self, argv, cwd):
        a = self.answers.get(self.key(argv), {})
        if isinstance(a, list) and a and self.key(argv) != "gh":
            a = a.pop(0) if len(a) > 1 else a[0]
        if isinstance(a, tuple):
            code, out, err = a
        else:
            code, out, err = 0, json.dumps(a), ""
        return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


def _tick(tmp_path, fake, last=None, queued=(), inflight=None, dry_run=False):
    lane = tmp_path / "internal" / "dispatch"
    for d in ("queued", "inflight", "done"):
        (lane / d).mkdir(parents=True, exist_ok=True)
    for bid in queued:
        (lane / "queued" / ("%s.md" % bid)).write_text(
            "---\nid: %s\naspect: substrate\ntitle: \"Make the widget frobnicate\"\n"
            "privileged: false\n---\nBODY of %s, verbatim.\n" % (bid, bid))
    for bid, fm in (inflight or {}).items():
        (lane / "inflight" / ("%s.md" % bid)).write_text(
            "---\nid: %s\n%s---\nbody\n" % (bid, "".join("%s: %s\n" % kv
                                                        for kv in fm.items())))
    ls = tmp_path / "last-seen.json"
    ls.write_text(json.dumps(last or {"poked": []}))
    return mt.Tick(tmp_path, run=fake, last_seen_path=ls, dry_run=dry_run,
                   ledger_dir=tmp_path / "no-ledgers")


def verbs(tick):
    return [Fake.key(a) for a in tick.argvs]


def state_written(tick):
    argv = [a for a in tick.argvs if a[2:3] == ["state"]][-1]
    return json.loads(argv[argv.index("--json") + 1])


# ── the argv table ──────────────────────────────────────────────────────────


def test_argv_table_holds_only_granted_commands():
    """The runner widens no grant: every prefix it may execute is a command the setup
    doc already grants the `meta-dispatch` task, and `meta-tick` is the one new line."""
    setup = SETUP.read_text(encoding="utf-8")
    for prefix in mt.GRANTED:
        grant = "Bash(%s:*)" % " ".join(prefix)
        assert grant in setup, grant
    assert "Bash(python3 tools/meta-tick:*)" in setup


def test_an_ungranted_argv_is_refused_before_it_runs(tmp_path):
    t = _tick(tmp_path, Fake())
    with pytest.raises(mt.NotGranted):
        t.call("git", "reset", "--hard")
    with pytest.raises(mt.NotGranted):
        t.call("python3", "tools/meta-dispatch-moveX", "now")
    assert t.argvs == []


# ── order ───────────────────────────────────────────────────────────────────


def test_a_quiet_tick_runs_the_procedures_steps_in_order(tmp_path):
    t = _tick(tmp_path, Fake())
    out = t.run()
    assert verbs(t) == ["now", "rig-preflight", "rig-line", "sync", "repair", "gh",
                        "recover-stalls", "gh", "eligible", "cards", "land", "heartbeat",
                        "state"]
    assert out["session_actions"] == []
    assert out["heartbeat"] is not None


def test_step_2c_writes_the_durable_card_surface(tmp_path):
    """Replaces the procedure-text pin of the same name: `cards` runs on every tick
    that reached eligibility, at the END (after every move, before land), and on a tick
    that moved nothing it is handed step 2's `cards[]` — never a second scan."""
    cards = [{"id": "c", "state": "prepared", "action": "tap", "age_h": 1,
              "tray_title": "[META:s] 01:00 [c] T"}]
    t = _tick(tmp_path, Fake(eligible=_elig(blocked_by="cap", cards=cards)))
    t.run()
    v = verbs(t)
    assert v.index("eligible") < v.index("cards") == v.index("land") - 1
    argv = [a for a in t.argvs if a[2:3] == ["cards"]][0]
    assert json.loads(argv[argv.index("--cards-json") + 1]) == cards


def test_cards_md_carries_the_blocked_by_line_when_eligible_renders_one(tmp_path):
    """RULINGS 2026-10-01 10:50 PDT: what stops the routine is readable from `main`."""
    line = "blocked_by: stale-checkout — run the sync"
    elig = dict(_elig(blocked_by="stale-checkout"), blocked_by_line=line)
    t = _tick(tmp_path, Fake(eligible=elig))
    t.run()
    argv = [a for a in t.argvs if a[2:3] == ["cards"]][0]
    assert argv[argv.index("--blocked-by") + 1] == line


def test_cards_md_is_rendered_after_this_ticks_own_launch(tmp_path):
    """#4260 review: a card this tick moved must be in the file this tick lands. A
    headless start moved an entry, so step 2's `cards[]` is stale: `cards` re-reads."""
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")])), queued=["a"])
    t.run()
    v = verbs(t)
    assert v.index("launch") < v.index("cards") < v.index("land")
    argv = [a for a in t.argvs if a[2:3] == ["cards"]][0]
    assert "--cards-json" not in argv


def test_the_card_verb_renders_cards_md_after_the_move(tmp_path):
    fake = Fake(launch={"moved": True, "chip_title_time": "11:00", "chip_title_tz": "PDT"})
    t = _tick(tmp_path, fake, queued=["a"])
    mt.card(t, "a", "task_1")
    v = verbs(t)
    assert v.index("launch") < v.index("cards")


def test_the_standing_waiting_line_is_on_every_tick(tmp_path):
    """Replaces the procedure-text pin: the waiting line and the repair line are in the
    report, on a quiet tick. The waiting line is the TICK's own (`tray_line`) — it
    counts what this session holds, so zero cards reads `no cards waiting`, whatever
    the helper's internal line says."""
    line = "cards waiting for a tap: 1 — [x] 10:00 (2h)"
    t = _tick(tmp_path, Fake(eligible=_elig(cards_waiting_line=line),
                             repair={"line": "repair: 1 executed — r1 (req pm)"}))
    report = t.run()["report"]
    assert "no cards waiting" in report.splitlines()
    assert line not in report
    assert "repair: 1 executed — r1 (req pm)" in report.splitlines()
    assert "lane PRs open: 0" in report.splitlines()


# ── stop rules ──────────────────────────────────────────────────────────────


def test_every_tick_rewrites_tick_md_with_its_report(tmp_path):
    """RULINGS 2026-10-02 07:50 PDT: why a tick launched nothing is readable from the
    checkout, not only from the routine's sidebar run."""
    t = _tick(tmp_path, Fake(eligible=_elig(blocked_by="cap")))
    out = t.run()
    body = (tmp_path / "internal" / "dispatch" / "TICK.md").read_text(encoding="utf-8")
    assert body == mt.TICK_HEADER + out["report"] + "\n"
    red = {"ok": False, "may_reconcile": False, "may_launch": False,
           "lines": ["rig: checkout on 'main' is dirty"], "login_note": "login: -"}
    t2 = _tick(tmp_path, Fake(**{"rig-preflight": red}))
    out2 = t2.run()
    body2 = (tmp_path / "internal" / "dispatch" / "TICK.md").read_text(encoding="utf-8")
    assert body2 == mt.TICK_HEADER + out2["report"] + "\n"
    assert "rig: checkout on 'main' is dirty" in body2


def test_a_dry_run_tick_writes_no_tick_md(tmp_path):
    t = _tick(tmp_path, Fake(), dry_run=True)
    t.run()
    assert not (tmp_path / "internal" / "dispatch" / "TICK.md").exists()


def test_rig_red_stops_the_tick_after_the_heartbeat(tmp_path):
    red = {"ok": False, "may_reconcile": False, "may_launch": False,
           "lines": ["rig: checkout on 'main' is dirty"], "login_note": "login: -"}
    t = _tick(tmp_path, Fake(**{"rig-preflight": red}))
    out = t.run()
    assert verbs(t) == ["now", "rig-preflight", "rig-line", "heartbeat", "state"]
    assert "rig: checkout on 'main' is dirty" in out["report"]
    assert out["session_actions"][0]["signature"] == "lane:rig-halt:checkout"


def _heartbeat_argv(tick):
    return [a for a in tick.argvs if a[2:3] == ["heartbeat"]][-1]


def test_rig_line_prints_after_the_preflight_lines_and_rides_the_heartbeat(tmp_path):
    """D-TP10: the tick prints the rig line (and its 48-hour list) right after the rig
    preflight lines — on a halted tick too — and records it on the heartbeat, with the
    blocker class the preflight line names (A: a precondition)."""
    red = {"ok": False, "may_reconcile": False, "may_launch": False,
           "lines": ["rig: login expired — run /login on the laptop (x)"],
           "login_note": "login: probed"}
    t = _tick(tmp_path, Fake(**{"rig-preflight": red}))
    out = t.run()
    report = out["report"].splitlines()
    i = report.index("rig: login expired — run /login on the laptop (x)")
    assert report[i + 1:i + 4] == ["login: probed", RIG_LINE, STALE_LINE]
    hb = _heartbeat_argv(t)
    assert hb[hb.index("--rig-line") + 1] == RIG_LINE
    assert "; block:A; " in hb[hb.index("--note") + 1]


def test_a_blocked_by_reason_is_stamped_with_its_class(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(blocked_by="back-pressure")))
    t.run()
    hb = _heartbeat_argv(t)
    assert "block:C" in hb[hb.index("--note") + 1]


def test_a_full_lane_is_not_a_block(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(blocked_by="cap")))
    t.run()
    assert "block:" not in _heartbeat_argv(t)[-2]


def test_rig_line_unavailable_is_said_and_never_stops_the_tick(tmp_path):
    t = _tick(tmp_path, Fake(**{"rig-line": (1, "", "gh: HTTP 403")}))
    out = t.run()
    assert "rig line: unavailable — gh: HTTP 403" in out["report"]
    assert "--rig-line" not in _heartbeat_argv(t)
    assert "land" in verbs(t)


def test_rig_red_keeps_every_old_signature(tmp_path):
    """A stopped tick evaluated nothing, so it prunes nothing: pruning then would re-poke
    every standing condition the moment the rig clears."""
    red = {"may_reconcile": False, "may_launch": False, "lines": ["rig: lane bad"]}
    t = _tick(tmp_path, Fake(**{"rig-preflight": red}),
              last={"poked": ["3:invalid:x.md", "2:back-pressure"]})
    t.run()
    assert state_written(t)["poked"][:2] == ["3:invalid:x.md", "2:back-pressure"]


def test_may_launch_false_reconciles_but_launches_nothing(tmp_path):
    rig = dict(GREEN, may_launch=False)
    t = _tick(tmp_path, Fake(**{"rig-preflight": rig},
                             eligible=_elig(candidates=[_cand("a")])), queued=["a"])
    out = t.run()
    assert "inflight" not in verbs(t) and "launch" not in verbs(t)
    assert "land" in verbs(t) and "heartbeat" in verbs(t)
    assert out["session_actions"] == []


def test_sync_refused_never_lands(tmp_path):
    t = _tick(tmp_path, Fake(sync={"pulled": False, "reason": "dirty", "message": "m"},
                             eligible=_elig(candidates=[_cand("a")])), queued=["a"])
    out = t.run()
    assert "eligible" in verbs(t) and "launch" in verbs(t)
    assert "land" not in verbs(t)
    assert "land: skipped" in out["report"]


@pytest.mark.parametrize("over,sig", [
    ({"blocked_by": "stale-checkout", "base": {"behind": 3, "ref": "origin/main"}},
     "lane:stale-checkout"),
    ({"blocked_by": "lane-conflict", "conflicts": [{"id": "x", "states": ["a", "b"]}]},
     "3:conflict:x"),
    ({"back_pressure": {"paused": True, "unreviewed_prs": [1, 2, 3]}}, "2:back-pressure"),
])
def test_a_blocked_lane_dispatches_nothing_and_pokes(tmp_path, over, sig):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")], **over)),
              queued=["a"])
    out = t.run()
    assert "inflight" not in verbs(t) and "launch" not in verbs(t)
    assert [a["signature"] for a in out["session_actions"]] == [sig]


# ── launch ──────────────────────────────────────────────────────────────────


def test_step_4_names_the_headless_command_exactly(tmp_path):
    """Replaces the procedure-text pin: the headless start is exactly
    `launch <id> --headless --json`, after ONE login probe, then the chip row."""
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")])), queued=["a"])
    t.run()
    assert ["python3", "tools/meta-dispatch-move", "land-first", "launch", "a", "--headless",
            "--json"] in t.argvs
    v = verbs(t)
    assert v.index("inflight") < v.index("probe") < v.index("launch") < v.index("chip-row")
    row = [a for a in t.argvs if a[2:3] == ["chip-row"]][0]
    fields = json.loads(row[row.index("--append") + 1])
    assert fields["launch"] == "headless" and fields["task_id"] == "s1"
    assert fields["bucket"] == "dispatched"
    assert fields["dispatched_at"] == "2026-09-23T18:00:00Z"


def test_step_3_always_passes_candidate_and_comma_keywords(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")])), queued=["a"])
    t.run()
    argv = [a for a in t.argvs if Fake.key(a) == "inflight"][0]
    assert argv[argv.index("--candidate") + 1] == "a"
    assert argv[argv.index("--keywords") + 1] == "make,widget,frobnicate"


def test_step_3_passes_repairs_for_a_repair_brief_and_not_otherwise(tmp_path):
    """A `class: repair` candidate carries `repair_pr`; the duplicate check must be told,
    or the PR being repaired reads as a collision (2026-09-25: `hold-fix-4440` gated on
    #4440 itself for two days)."""
    c = dict(_cand("hold-fix-4440"), repair_pr=4440)
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[c])), queued=["hold-fix-4440"])
    t.run()
    argv = [a for a in t.argvs if Fake.key(a) == "inflight"][0]
    assert argv[argv.index("--repairs") + 1] == "4440"
    t2 = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")])), queued=["a"])
    t2.run()
    argv2 = [a for a in t2.argvs if Fake.key(a) == "inflight"][0]
    assert "--repairs" not in argv2


def test_repair_boilerplate_never_becomes_a_keyword():
    """Two repair briefs share the convention's phrase and nothing else; the duplicate
    check must not be handed the phrase (2026-09-27: `hold-fix-4471` gated on the
    `hold-fix-4453` card because both queried as `…,repair,branch`)."""
    a = mt.keywords("Board sheet (#4471) repair, on its own branch before merge — merge "
                    "main (the branch is dirty), read the red Admin suite shard 1")
    b = mt.keywords("Intelligence apps tile (#4453) repair, on its own branch before "
                    "merge — merge main, read the red Admin suite shard 0/4")
    for kw in ("repair", "branch", "merge", "main", "before"):
        assert kw not in a.split(",") and kw not in b.split(",")
    assert len(set(a.split(",")) & set(b.split(","))) < 2
    assert a.startswith("board,sheet")


def test_actionable_duplicate_gates_the_candidate_and_is_not_replaced(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")]),
                             inflight={"actionable_count": 1, "count": 4, "warnings": [],
                                       "overlaps": [{"title": "same thing"}]}),
              queued=["a"])
    out = t.run()
    assert "launch" not in verbs(t)
    assert "duplicates_exhausted" in out["report"]


def test_count_without_actionable_still_launches(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")]),
                             inflight={"actionable_count": 0, "count": 18,
                                       "warnings": []}), queued=["a"])
    t.run()
    assert "launch" in verbs(t)


def test_step_4_routes_on_the_helpers_answer_not_a_re_derivation(tmp_path):
    """Replaces the procedure-text pin: a NON-privileged brief the helper routed
    `prepared` gets a card; the runner never reads `privileged` to decide."""
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a", route="prepared")])),
              queued=["a"])
    out = t.run()
    assert "launch" not in verbs(t) and "probe" not in verbs(t)
    assert [a["kind"] for a in out["session_actions"]] == ["prepare_card"]


def test_the_hard_rules_forbid_a_headless_privileged_launch(tmp_path):
    """Replaces the procedure-text pin: a privileged brief (routed `prepared` by the
    helper) is never passed `--headless`."""
    t = _tick(tmp_path, Fake(eligible=_elig(
        candidates=[_cand("p", route="prepared", privileged=True)])), queued=["p"])
    out = t.run()
    assert not any("--headless" in a for a in t.argvs)
    assert out["session_actions"][0]["privileged"] is True


def test_the_hard_rules_cap_headless_starts_per_tick(tmp_path):
    """Replaces the procedure-text pin: after each start the helper is re-asked with
    `--headless-started n`; at `headless_slots: 0` nothing further starts."""
    elig = [_elig(candidates=[_cand("a"), _cand("b"), _cand("c")]),
            _elig(headless_slots=1), _elig(headless_slots=0)]
    t = _tick(tmp_path, Fake(eligible=elig), queued=["a", "b", "c"])
    out = t.run()
    launched = [a[4] for a in t.argvs if a[2:4] == ["land-first", "launch"]]
    assert launched == ["a", "b"]
    assert [a for a in t.argvs if "--headless-started" in a][-1][-1] == "2"
    assert verbs(t).count("probe") == 1
    assert "hold c: no headless slot left this tick" in out["report"]


def test_login_probe_red_falls_through_to_a_card(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")]),
                             probe=dict(GREEN, may_launch=False,
                                        lines=["rig: login expired"])), queued=["a"])
    out = t.run()
    assert "launch" not in verbs(t)
    assert out["session_actions"][0]["kind"] == "prepare_card"


def test_prerequisite_fallback_becomes_a_card(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")]),
                             launch={"moved": False, "route": "prepared",
                                     "note": "headless: unavailable (grant)"}),
              queued=["a"])
    out = t.run()
    assert [a["kind"] for a in out["session_actions"]] == ["prepare_card"]


def test_a_running_session_whose_move_failed_is_recorded_and_stops(tmp_path):
    err = ("meta-dispatch-move: refused: lock held — NOTE: headless session abc123 is "
           "already RUNNING in /w; record it in prepared_but_unmoved[] before retrying")
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a"), _cand("b")]),
                             launch=(2, "", err)), queued=["a", "b"])
    out = t.run()
    assert [a[4] for a in t.argvs if a[2:4] == ["land-first", "launch"]] == ["a"]
    assert state_written(t)["prepared_but_unmoved"] == [{"id": "a", "task_id": "abc123"}]
    assert out["session_actions"][0]["signature"] == "1:launch-failed:a"


def test_prepared_but_unmoved_is_never_prepared_twice(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a", route="prepared")])),
              queued=["a"], last={"poked": [], "prepared_but_unmoved":
                                  [{"id": "a", "task_id": "t"}]})
    out = t.run()
    assert out["session_actions"] == []


# ── session_actions shapes ──────────────────────────────────────────────────


def test_step_4a_spawns_under_the_timed_chip_title(tmp_path):
    """Replaces the procedure-text pin: the card carries the helper's
    `chip_title_timed` untouched and the body VERBATIM, and says what to run after."""
    c = _cand("a", route="prepared")
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[c])), queued=["a"])
    card = t.run()["session_actions"][0]
    assert card == {"kind": "prepare_card", "id": "a", "aspect": "substrate",
                    "title_timed": c["chip_title_timed"], "body_path": c["path"],
                    "body": "BODY of a, verbatim.", "privileged": False,
                    "then": "python3 tools/meta-tick card a --task-id <task_id>"}


def test_step_4d_poke_pins_the_created_time_and_session(tmp_path):
    """Replaces the procedure-text pin: `card` moves the entry with the session, writes
    the row, records `1:card:<id>:0h`, and returns 4d's template — title clipped at 64,
    time and session never."""
    long = "x" * 120
    fake = Fake(launch={"moved": True, "dispatched": "2026-09-23",
                        "dispatched_at": "2026-09-23T18:00:00Z",
                        "chip_title_time": "11:00", "chip_title_tz": "PDT"})
    t = _tick(tmp_path, fake, queued=["a"])
    (tmp_path / "internal/dispatch/queued/a.md").write_text(
        "---\nid: a\naspect: substrate\ntitle: \"%s\"\nprivileged: false\n---\nb\n" % long)
    out = mt.card(t, "a", "task_9f")
    assert ["python3", "tools/meta-dispatch-move", "land-first", "launch", "a", "--session",
            "task_9f", "--json"] in t.argvs
    text = out["poke"]["text"]
    assert text == ("[META:substrate] [a] %s is prepared in the tray — created 11:00 PDT, "
                    "session `task_9f` — one tap to start it." % ("x" * 63 + "…"))
    assert len(text) <= mt.POKE_BUDGET
    merge = [a for a in t.argvs if "--merge" in a][0]
    assert json.loads(merge[-1]) == {"poked": ["1:card:a:0h"],
                                     "launch_kinds": {"a": "prepared"}}
    row = [a for a in t.argvs if a[2:3] == ["chip-row"]][0]
    row = json.loads(row[row.index("--append") + 1])
    assert row["launch"] == "prepared" and row["task_id"] == "task_9f"


def test_card_move_failure_records_prepared_but_unmoved(tmp_path):
    t = _tick(tmp_path, Fake(launch=(2, "", "meta-dispatch-move: refused: lock")),
              queued=["a"])
    out = mt.card(t, "a", "task_1")
    assert out["ok"] is False
    merge = json.loads([a for a in t.argvs if "--merge" in a][0][-1])
    assert merge["prepared_but_unmoved"] == [{"id": "a", "task_id": "task_1"}]


def test_step_4d_poke_repeats_on_age(tmp_path):
    """Replaces the procedure-text pin: an untapped card re-pokes at 6h and 12h, the
    highest crossed threshold only, never past 12; a stale STARTED card never does."""
    cards = [{"id": "p", "aspect": "s", "state": "stale", "stale_kind": "prepared",
              "action": "decline", "age_h": 13, "tray_title": "[META:s] 01:00 [p] Title"},
             {"id": "q", "aspect": "s", "state": "prepared", "action": "tap",
              "age_h": 6, "tray_title": "[META:s] 02:00 [q] Title"},
             {"id": "r", "aspect": "s", "state": "stale", "stale_kind": "started",
              "action": "investigate", "age_h": 30, "tray_title": "[META:s] 03:00 [r] Title"}]
    t = _tick(tmp_path, Fake(eligible=_elig(cards=cards)))
    sigs = [a["signature"] for a in t.run()["session_actions"]]
    assert sigs == ["1:card:p:12h", "1:card:q:6h"]
    assert ("[p] Title waiting 12h — it is in the newest dispatch session: tap or "
            "decline") in t.actions[0]["text"]
    kept = state_written(t)["poked"]
    assert "1:card:p:6h" not in kept      # marked active, never sent


def test_a_stalled_headless_chip_is_poked_class_1(tmp_path):
    """Replaces the procedure-text pin: liveness comes off the helper, and the runner
    pokes; it never relaunches."""
    t = _tick(tmp_path, Fake(eligible=_elig(headless_liveness=[
        {"id": "h", "state": "stalled", "session": "s9"}])))
    out = t.run()
    assert out["session_actions"][0]["signature"] == "1:headless-stalled:h"
    assert "s9" in out["session_actions"][0]["text"]
    assert "launch" not in verbs(t)


# ── the never-re-poke rule ──────────────────────────────────────────────────


def test_a_signature_already_poked_is_never_poked_again(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(invalid=[{"path": "x.md", "error": "e"}])),
              last={"poked": ["3:invalid:x.md"]})
    assert t.run()["session_actions"] == []


def test_a_poke_is_recorded_before_it_is_sent_and_resent_once_if_unacked(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(invalid=[{"path": "x.md", "error": "e"}])))
    t.run()
    st = state_written(t)
    assert "3:invalid:x.md" in st["poked"]
    assert [u["signature"] for u in st["unsent"]] == ["3:invalid:x.md"]
    # next tick, the session never acked: re-sent ONCE, then gone from unsent[]
    t2 = _tick(tmp_path, Fake(eligible=_elig(invalid=[{"path": "x.md", "error": "e"}])),
               last=st)
    out = t2.run()
    assert [(a["signature"], a.get("retry")) for a in out["session_actions"]] == \
        [("3:invalid:x.md", True)]
    assert state_written(t2)["unsent"] == []


def test_ack_clears_unsent(tmp_path):
    t = _tick(tmp_path, Fake(), last={"poked": ["a"], "unsent": [{"text": "t"}],
                                      "login_probe": {"state": "ok"}})
    assert mt.ack(t) == {"ok": True, "cleared": 1}
    st = state_written(t)
    assert st["unsent"] == [] and st["login_probe"] == {"state": "ok"}


def test_a_cleared_condition_is_pruned_from_poked(tmp_path):
    t = _tick(tmp_path, Fake(), last={"poked": ["3:invalid:gone.md", "2:back-pressure"]})
    t.run()
    assert state_written(t)["poked"] == []


def test_lane_pr_signature_comes_from_land_verbatim(tmp_path):
    land = {"landed": False, "lane_pr_line": "lane PRs open: 4 (oldest 30h)",
            "lane_prs": {"poke_signature": "lane-state-prs-unmerged:4"}}
    t = _tick(tmp_path, Fake(land=land))
    out = t.run()
    assert out["session_actions"][0]["signature"] == "lane:lane-state-prs-unmerged:4"


# ── step 1 ──────────────────────────────────────────────────────────────────


def test_merged_inflight_is_cleared_and_its_row_marked(tmp_path):
    gh = [{"number": 7, "state": "MERGED", "headRefName": "b"}]
    t = _tick(tmp_path, Fake(gh=gh, done={"ok": True}), inflight={"m": {"pr": 7}})
    t.run()
    assert ["python3", "tools/meta-dispatch-move", "land-first", "done", "m",
            "--json"] in t.argvs
    row = [a for a in t.argvs if a[2:3] == ["chip-row"]][0]
    assert json.loads(row[row.index("--set") + 1]) == {"bucket": "merged", "pr": 7}
    el = [a for a in t.argvs if Fake.key(a) == "eligible"][0]
    assert el[el.index("--merged-prs") + 1] == "7"


def test_closed_inflight_is_abandoned_with_its_pr(tmp_path):
    gh = [{"number": 8, "state": "CLOSED", "headRefName": "b"}]
    t = _tick(tmp_path, Fake(gh=gh, abandon={"ok": True}), inflight={"c": {"pr": 8}})
    t.run()
    assert ["python3", "tools/meta-dispatch-move", "land-first", "abandon", "c", "--pr", "8",
            "--json"] in t.argvs


def test_dry_run_mutates_nothing(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")]),
                             gh=[{"number": 7, "state": "MERGED"}]),
              inflight={"m": {"pr": 7}}, queued=["a"], dry_run=True)
    t.run()
    mutating = {"launch", "done", "abandon", "complete", "bind", "cards", "chip-row",
                "heartbeat", "state", "probe", "repair-queued-copy", "recover-stalls"}
    assert not mutating & set(verbs(t))
    for a in t.argvs:
        if a[2:3] in (["sync"], ["repair"], ["land"]):
            assert "--dry-run" in a


# ── the new procedure body ──────────────────────────────────────────────────


def test_procedure_body_is_the_runner_call_and_short():
    body = PROCEDURE.read_text(encoding="utf-8")
    assert len(body.splitlines()) <= 60
    assert "python3 tools/meta-tick --json" in body
    assert "## HARD RULES" in body
    assert "turning it off is the kill switch" in body
    for banned in ("&&", "$(", "; done"):
        assert banned not in body, banned


# ── the two mover pieces the runner needs (`meta-dispatch-move`) ────────────


def _move_mod():
    import importlib.machinery
    import importlib.util
    if "meta_dispatch_move" in sys.modules:
        return sys.modules["meta_dispatch_move"]
    loader = importlib.machinery.SourceFileLoader(
        "meta_dispatch_move", str(_REPO / "tools" / "meta-dispatch-move"))
    spec = importlib.util.spec_from_loader("meta_dispatch_move", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_dispatch_move"] = mod
    loader.exec_module(mod)
    return mod


def _ledger(tmp_path, rows=()):
    d = tmp_path / "meta-state"
    d.mkdir()
    (d / "substrate.json").write_text(json.dumps({"aspect": "substrate",
                                                   "chips": list(rows)}))
    return d


def test_chip_row_append_fills_the_schema_and_is_idempotent(tmp_path):
    mdm = _move_mod()
    d = _ledger(tmp_path)
    f = {"title": "t", "dispatched_at": "2026-09-23T18:00:00Z", "launch": "headless"}
    out = mdm.chip_row("a", f, aspect="substrate", append=True, ledger_dir=d)
    assert out["written"] is True
    row = json.loads((d / "substrate.json").read_text())["chips"][0]
    assert set(mdm.CHIP_ROW_FIELDS) <= set(row) and row["two_pass"] is None
    again = mdm.chip_row("a", f, aspect="substrate", append=True, ledger_dir=d)
    assert again["written"] is False
    assert len(json.loads((d / "substrate.json").read_text())["chips"]) == 1


def test_chip_row_set_updates_the_newest_row_only(tmp_path):
    mdm = _move_mod()
    d = _ledger(tmp_path, [{"id": "a", "bucket": "returned_to_queue"},
                           {"id": "a", "bucket": "dispatched"}, {"id": "b"}])
    mdm.chip_row("a", {"bucket": "merged", "pr": 7}, ledger_dir=d)
    chips = json.loads((d / "substrate.json").read_text())["chips"]
    assert chips[0]["bucket"] == "returned_to_queue"
    assert chips[1] == {"id": "a", "bucket": "merged", "pr": 7}


def test_chip_row_never_creates_a_ledger(tmp_path):
    mdm = _move_mod()
    d = _ledger(tmp_path)
    with pytest.raises(mdm.Refused):
        mdm.chip_row("a", {}, aspect="nosuch", append=True, ledger_dir=d)
    assert not (d / "nosuch.json").exists()


def test_launch_session_stamps_the_prepared_fields_in_the_move(tmp_path):
    mdm = _move_mod()
    root = tmp_path / "dispatch"
    for sub in ("queued", "inflight", "done"):
        (root / sub).mkdir(parents=True)
    (root / "queued" / "b.md").write_text(
        "---\nid: b\naspect: apps\ntitle: \"t\"\nprivileged: true\n---\nbody\n")
    out = mdm.launch(root, "b", session="task_4e0a", ledger_dir=_ledger(tmp_path))
    text = (root / "inflight" / "b.md").read_text()
    assert "session: task_4e0a" in text and "launch: prepared" in text
    assert "branch: null" in text and out["session"] == "task_4e0a"
    (root / "queued" / "c.md").write_text(
        "---\nid: c\naspect: apps\ntitle: \"t\"\nprivileged: false\n---\nbody\n")
    with pytest.raises(mdm.Refused):
        mdm.launch(root, "c", headless=True, session="x")


# ── step 1b: recover-stalls ───────────────────────────────────────────────────


def test_step_1b_sweeps_stalls_after_reconcile_and_pokes_a_recovery(tmp_path):
    """2026-09-24: a headless chip that died before its first push read the whole
    launcher as STALLED (headless unavailable for every brief) for 23 h while the verb
    that recovers it ran nowhere. The runner calls it every tick, after reconcile and
    before eligibility, and pokes once per recovered id."""
    fake = Fake(**{"recover-stalls": {"ok": True, "recovered": [
        {"id": "dead-chip", "successor": "dead-chip-2"}],
        "lines": ["recover-stalls: dead-chip — STALLED, corroborated dead (4/4); "
                  "abandoned, successor dead-chip-2 queued"]}})
    t = _tick(tmp_path, fake)
    out = t.run()
    v = verbs(t)
    assert v.index("recover-stalls") > v.index("gh")
    assert v.index("recover-stalls") < v.index("eligible")
    assert any("dead-chip — STALLED" in ln for ln in out["lines"])
    pokes = [a for a in out["session_actions"] if a["kind"] == "poke"]
    assert [p["signature"] for p in pokes] == ["1:stall-recovered:dead-chip"]
    assert "successor dead-chip-2 queued" in pokes[0]["text"]


def test_step_1b_is_quiet_when_nothing_is_past_the_grace_window(tmp_path):
    fake = Fake(**{"recover-stalls": {"ok": True, "recovered": [], "lines": [
        "recover-stalls: nothing past the grace window to check"]}})
    t = _tick(tmp_path, fake)
    out = t.run()
    assert not any("recover-stalls" in ln for ln in out["lines"])
    assert out["session_actions"] == []


def test_step_1b_never_moves_anything_on_a_dry_run(tmp_path):
    t = _tick(tmp_path, Fake(), dry_run=True)
    out = t.run()
    assert "recover-stalls" not in verbs(t)
    assert any("would sweep stalled" in ln for ln in out["lines"])


# ── step 1c: re-route, don't nag — and the tray is the latest tick ───────────
#
# Brief `a-waiting-card-is-relaunched-headless-or-reissued-in-the-latest-tick`. A card
# prepared while headless was down is launched headless by the next tick that has a
# free headless slot, with no tap; every card that must stay a card is re-issued in the
# newest session, so the operator never opens an older one.

CARD_FM = {"aspect": "substrate", "title": '"Make the widget frobnicate"',
           "privileged": "false", "session": "task_old", "launch": "prepared",
           "dispatched_at": "2026-09-23T08:02:00Z"}


def _waiting(bid="w", age=10, state="stale", **over):
    c = {"id": bid, "aspect": "substrate", "state": state, "action": "decline",
         "stale_kind": "prepared" if state == "stale" else None, "age_h": age,
         "tray_title": "[META:substrate] 01:02 [%s] Make the widget frobnicate" % bid}
    c.update(over)
    return c


def _reroute_fake(cards, **over):
    answers = {"eligible": [_elig(cards=cards), _elig(cards=[], headless_slots=1)],
               "abandon": {"ok": True, "successor": "w-2"}}
    answers.update(over)
    return Fake(**answers)


def test_step_1c_reroutes_a_waiting_card_headless_with_no_tap(tmp_path):
    t = _tick(tmp_path, _reroute_fake([_waiting()]), inflight={"w": CARD_FM})
    out = t.run()
    assert ["python3", "tools/meta-dispatch-move", "land-first", "abandon", "w",
            "--reroute-headless", "--json"] in t.argvs
    assert ["python3", "tools/meta-dispatch-move", "land-first", "launch", "w-2", "--headless",
            "--json"] in t.argvs
    pokes = [a for a in out["session_actions"] if a["kind"] == "poke"]
    assert [p["signature"] for p in pokes] == ["1:card-rerouted:w"]
    assert "launched headless as w-2" in pokes[0]["text"]
    assert not [a for a in out["session_actions"] if a["kind"] == "reissue_card"]
    # The eligibility read that decides this tick's dispatch is debited for it.
    el = [a for a in t.argvs if Fake.key(a) == "eligible"]
    assert el[-1][el[-1].index("--headless-started") + 1] == "1"
    row = json.loads([a for a in t.argvs if a[2:3] == ["chip-row"]][0][-2])
    assert row["launch"] == "headless" and row["privileged"] is False


def test_step_1c_runs_after_recover_stalls_and_before_eligibility(tmp_path):
    t = _tick(tmp_path, _reroute_fake([_waiting()]), inflight={"w": CARD_FM})
    t.run()
    v = verbs(t)
    first_el = v.index("eligible")
    assert v.index("recover-stalls") < first_el < v.index("abandon") < v.index("launch")
    assert v.index("launch") < len(v) - 1 - v[::-1].index("eligible"), \
        "the dispatch eligibility read comes AFTER the re-route"
    assert v.index("probe") < v.index("abandon"), "login probed before the first start"


def test_step_1c_oldest_card_first_within_the_slot(tmp_path):
    fm2 = dict(CARD_FM, session="task_older")
    cards = [_waiting("young", age=7), _waiting("old", age=30)]
    fake = _reroute_fake(cards, eligible=[_elig(cards=cards, headless_slots=1),
                                          _elig(cards=[])],
                         abandon={"ok": True, "successor": "old-2"})
    t = _tick(tmp_path, fake, inflight={"young": CARD_FM, "old": fm2})
    out = t.run()
    assert [a[4] for a in t.argvs if a[2:4] == ["land-first", "abandon"]] == ["old"]
    assert "reroute: young — no headless slot left this tick" in out["lines"]


def test_step_1c_never_reroutes_a_privileged_card(tmp_path):
    fm = dict(CARD_FM, privileged="true")
    t = _tick(tmp_path, _reroute_fake([_waiting()]), inflight={"w": fm})
    out = t.run()
    assert "abandon" not in verbs(t) and "launch" not in verbs(t)
    reissued = [a for a in out["session_actions"] if a["kind"] == "reissue_card"]
    assert [a["id"] for a in reissued] == ["w"] and reissued[0]["privileged"] is True


def test_step_1c_goes_through_route_for(tmp_path, monkeypatch):
    """The guardrail is `route_for`, THE routing decision (D-PM1/D-PM2): if the step
    decided on its own reading of `privileged` this would re-route anyway."""
    calls = []
    monkeypatch.setattr(mt.mdh, "route_for",
                        lambda p, a: calls.append((p, a)) or mt.mdh.ROUTE_PREPARED)
    t = _tick(tmp_path, _reroute_fake([_waiting()]), inflight={"w": CARD_FM})
    t.run()
    assert calls == [(False, True)]
    assert "abandon" not in verbs(t)


@pytest.mark.parametrize("over,line", [
    ({"headless": {"available": False, "per_tick": 2}, "headless_slots": 0}, None),
    ({"headless_slots": 0}, "reroute: w — no headless slot left this tick"),
    ({"blocked_by": "back-pressure"}, "reroute: lane blocked (back-pressure) — nothing "
                                      "re-routed"),
    ({"headless": {"per_tick": 2}}, "reroute: headless availability unreadable — "
                                    "nothing re-routed"),
])
def test_step_1c_does_nothing_without_a_free_headless_slot(tmp_path, over, line):
    elig = _elig(cards=[_waiting()], **over)
    t = _tick(tmp_path, Fake(eligible=elig), inflight={"w": CARD_FM})
    out = t.run()
    assert "abandon" not in verbs(t) and "launch" not in verbs(t)
    if line:
        assert line in out["lines"]
    assert [a["id"] for a in out["session_actions"] if a["kind"] == "reissue_card"] \
        == ["w"], "a card that stays a card is re-issued in this session"


def test_step_1c_leaves_an_unknown_card_alone_and_names_it(tmp_path):
    card = _waiting(state="unknown", age=None)
    t = _tick(tmp_path, Fake(eligible=_elig(cards=[card])), inflight={"w": CARD_FM})
    out = t.run()
    assert "abandon" not in verbs(t)
    assert "reroute: w — card state unknown; left in place" in out["lines"]
    assert not [a for a in out["session_actions"] if a["kind"] == "reissue_card"]
    assert "not in this session: [w] state unknown — investigate" in out["report"]


def test_step_1c_on_a_dry_run_reports_and_moves_nothing(tmp_path):
    t = _tick(tmp_path, _reroute_fake([_waiting()]), inflight={"w": CARD_FM},
              dry_run=True)
    out = t.run()
    assert "would re-route card w to a headless launch" in out["lines"]
    assert not {"abandon", "launch", "probe"} & set(verbs(t))
    assert out["session_actions"] == []


def test_step_1c_is_free_on_a_tick_with_no_card(tmp_path):
    """No non-privileged card in this checkout: no extra eligibility read."""
    t = _tick(tmp_path, Fake(), inflight={"p": dict(CARD_FM, privileged="true")})
    t.run()
    assert verbs(t).count("eligible") == 1


def test_step_1c_refusal_is_a_line_and_the_card_is_reissued(tmp_path):
    fake = _reroute_fake([_waiting()], abandon=(2, "", "meta-dispatch-move: refused: "
                                                       "ledger row … tapped"),
                         eligible=[_elig(cards=[_waiting()])])
    t = _tick(tmp_path, fake, inflight={"w": CARD_FM})
    out = t.run()
    assert "launch" not in verbs(t)
    assert any(ln.startswith("reroute w refused:") for ln in out["lines"])
    assert [a["id"] for a in out["session_actions"] if a["kind"] == "reissue_card"] \
        == ["w"]


def test_a_waiting_card_is_reissued_verbatim_in_the_newest_session(tmp_path):
    """Item 2: the tray is the latest tick. The re-issued card is the entry body
    byte-for-byte under its ORIGINAL tray title (the operator matches it by eye), and
    nothing moves — the lane already records the card."""
    card = _waiting(state="prepared", age=2, action="tap")
    t = _tick(tmp_path, Fake(eligible=_elig(cards=[card],
                                            headless={"available": False,
                                                      "per_tick": 2})),
              inflight={"w": CARD_FM})
    out = t.run()
    [item] = [a for a in out["session_actions"] if a["kind"] == "reissue_card"]
    assert item == {"kind": "reissue_card", "id": "w", "aspect": "substrate",
                    "title_timed": card["tray_title"], "privileged": False, "age_h": 2,
                    "body": "body", "body_path": "internal/dispatch/inflight/w.md"}
    assert "then" not in item
    assert not {"abandon", "launch"} & set(verbs(t))


def test_the_tray_line_says_the_newest_session_has_every_waiting_card(tmp_path):
    """Item 3: the operator's count is what THIS session holds, not `prepared_cap`."""
    cards = [_waiting("w", state="prepared", age=2),
             _waiting("p", state="stale", age=9)]
    t = _tick(tmp_path, Fake(eligible=_elig(cards=cards, prepared_count=2,
                                            headless={"available": False,
                                                      "per_tick": 2})),
              inflight={"w": CARD_FM, "p": dict(CARD_FM, privileged="true")})
    report = t.run()["report"]
    [line] = [ln for ln in report.splitlines() if "waiting" in ln and "session" in ln]
    assert line.startswith("2 cards waiting — newest session has them: ")
    assert "[w] Make the widget frobnicate (2h)" in line
    assert "[p] Make the widget frobnicate (9h, privileged)" in line


def test_tray_line_zero_and_one():
    assert mt.tray_line([]) == "no cards waiting"
    one = mt.tray_line([{"id": "a", "title_timed": "[META:s] 01:00 [a] T", "age_h": 0}])
    assert one == "1 card waiting — newest session has them: [META:s] 01:00 [a] T (0h)"


# ── a repair whose `repair_pr` merged completes (brief a-repair-brief-completes-…) ──

_REPAIR = {"class": "repair", "repair_pr": 4424, "launch": "prepared",
           "session": "task_ab12cd34", "pr": "null"}


def _repair_tick(tmp_path, gh, inflight=None, dry_run=False, **answers):
    fake = Fake(gh=gh, complete={"ok": True, "outcome": "repaired"}, **answers)
    return _tick(tmp_path, fake, inflight=inflight or {"hold-fix-4424": _REPAIR},
                 dry_run=dry_run)


def test_a_merged_repair_is_completed_with_outcome_repaired(tmp_path):
    gh = [{"number": 4424, "state": "MERGED", "mergedAt": "2026-09-26T16:02:11Z"}]
    t = _repair_tick(tmp_path, gh)
    out = t.run()
    assert ["python3", "tools/meta-dispatch-move", "land-first", "complete", "hold-fix-4424",
            "--outcome", "repaired", "--repaired-pr", "4424", "--merged-prs", "4424",
            "--merged-at", "2026-09-26T16:02:11Z", "--json"] in t.argvs
    pokes = [a for a in out["session_actions"] if a["kind"] == "poke"]
    assert [p["signature"] for p in pokes] == ["1:repair-complete:hold-fix-4424"]
    # Never bound as an ordinary unbound session entry, never re-routed as a card.
    assert "abandon" not in verbs(t) and "bind" not in verbs(t)


def test_completion_runs_before_eligibility(tmp_path):
    gh = [{"number": 4424, "state": "MERGED", "mergedAt": "2026-09-26T16:02:11Z"}]
    t = _repair_tick(tmp_path, gh)
    t.run()
    v = verbs(t)
    assert v.index("complete") < v.index("eligible")
    assert v.index("complete") < v.index("recover-stalls")


def test_the_repair_complete_poke_is_sent_once(tmp_path):
    gh = [{"number": 4424, "state": "MERGED", "mergedAt": "2026-09-26T16:02:11Z"}]
    t = _repair_tick(tmp_path, gh)
    t.run()
    t2 = _repair_tick(tmp_path, gh)
    t2.last = {"poked": ["1:repair-complete:hold-fix-4424"]}
    out = t2.run()
    assert not [a for a in out["session_actions"]
                if a.get("signature") == "1:repair-complete:hold-fix-4424"]


def test_a_repair_whose_pr_is_open_is_untouched(tmp_path):
    t = _repair_tick(tmp_path, [{"number": 4424, "state": "OPEN"}])
    t.run()
    assert "complete" not in verbs(t)


def test_a_repair_merged_before_the_page_is_asked_for_by_number(tmp_path):
    fake = Fake(gh=[{"number": 1, "state": "OPEN"}], complete={"ok": True})

    def run(argv, cwd):
        if list(argv[:3]) == ["gh", "pr", "view"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                {"state": "MERGED", "mergedAt": "2026-09-01T00:00:00Z"}), stderr="")
        return fake(argv, cwd)
    t = _tick(tmp_path, run, inflight={"hold-fix-4424": _REPAIR})
    t.run()
    assert ["gh", "pr", "view", "4424", "--repo", t.repo, "--json",
            "state,mergedAt,headRefName"] in t.argvs
    assert "--merged-at" in next(a for a in t.argvs if a[2:4] == ["land-first", "complete"])


def test_a_non_repair_entry_is_never_completed_by_this_path(tmp_path):
    gh = [{"number": 4424, "state": "MERGED"}]
    plain = dict(_REPAIR, **{"class": "backlog"})
    del plain["repair_pr"]
    t = _repair_tick(tmp_path, gh, inflight={"ordinary": plain})
    t.run()
    assert "complete" not in verbs(t)


def test_a_repair_with_its_own_pr_takes_the_ordinary_path(tmp_path):
    gh = [{"number": 4424, "state": "MERGED"}, {"number": 4500, "state": "OPEN"}]
    t = _repair_tick(tmp_path, gh, inflight={"r": dict(_REPAIR, pr=4500)})
    t.run()
    assert "complete" not in verbs(t)


def test_dry_run_reports_the_completion_and_moves_nothing(tmp_path):
    gh = [{"number": 4424, "state": "MERGED"}]
    t = _repair_tick(tmp_path, gh, dry_run=True)
    out = t.run()
    assert "complete" not in verbs(t)
    assert any("would complete hold-fix-4424" in ln for ln in out.get("lines", t.lines))


def test_a_refused_completion_is_a_line_and_an_integrity_poke(tmp_path):
    gh = [{"number": 4424, "state": "MERGED"}]
    t = _tick(tmp_path, Fake(gh=gh, complete=(4, "", "refused: integrity")),
              inflight={"hold-fix-4424": _REPAIR})
    out = t.run()
    assert any(ln.startswith("complete hold-fix-4424 refused") for ln in t.lines)
    assert "3:integrity:hold-fix-4424" in {a.get("signature")
                                           for a in out["session_actions"]}


# ── `gh` without the binary (RULINGS 2026-09-28 08:40 PDT) ───────────────────
#
# The bridge VM has no `gh`, so step 1's `gh pr list` exited 127 and the tick reconciled
# nothing — for nine runs, with no line saying so. These pin the fallback: the lane's own
# API helper serves the SAME two granted shapes, in `gh`'s own JSON, and nothing else.


class _FakeGhPy:
    """Stands in for `internal/pm/gh.py`: `call(path)` over the REST shapes it returns."""

    def __init__(self, pulls, files=None):
        self.pulls = pulls
        self.files = files or {}
        self.paths = []

    def call(self, path, method="GET", data=None):
        self.paths.append(path)
        if path.startswith("/pulls/") and path.endswith("/files?per_page=100"):
            return self.files.get(int(path.split("/")[2]), [])
        if path.startswith("/pulls/"):
            n = int(path.split("/")[2].split("?")[0])
            return next((p for p in self.pulls if p["number"] == n),
                        {"_error": 404, "_body": "not found"})
        return list(self.pulls)


def _pull(n, ref, state="open", merged_at=None):
    return {"number": n, "head": {"ref": ref}, "state": state, "merged_at": merged_at}


def test_gh_py_pr_list_speaks_ghs_own_json(tmp_path, monkeypatch):
    fake = _FakeGhPy([_pull(4400, "a", "closed", "2026-09-28T01:02:03Z"),
                      _pull(4401, "b"),
                      _pull(4402, "c", "closed")])
    monkeypatch.setattr(mt, "_gh_py_module", lambda root: fake)
    code, out, err = mt._gh_py_call(
        ["gh", "pr", "list", "--repo", "o/r", "--state", "all", "--limit", "80",
         "--json", "number,headRefName,state,mergedAt"], tmp_path)
    assert (code, err) == (0, "")
    rows = json.loads(out)
    assert [r["state"] for r in rows] == ["MERGED", "OPEN", "CLOSED"]
    assert rows[0] == {"number": 4400, "headRefName": "a", "state": "MERGED",
                       "mergedAt": "2026-09-28T01:02:03Z"}
    assert "state=all" in fake.paths[0] and "per_page=80" in fake.paths[0]


def test_gh_py_pr_view_answers_one_number(tmp_path, monkeypatch):
    fake = _FakeGhPy([_pull(4401, "b", "closed", "2026-09-28T04:00:00Z")])
    monkeypatch.setattr(mt, "_gh_py_module", lambda root: fake)
    code, out, err = mt._gh_py_call(
        ["gh", "pr", "view", "4401", "--repo", "o/r", "--json",
         "state,mergedAt,headRefName"], tmp_path)
    assert code == 0
    assert json.loads(out) == {"state": "MERGED", "mergedAt": "2026-09-28T04:00:00Z",
                               "headRefName": "b"}


def test_gh_py_files_come_back_as_paths_for_bind_unbound(tmp_path, monkeypatch):
    fake = _FakeGhPy([_pull(4402, "c")],
                     files={4402: [{"filename": "internal/dispatch/inflight/x.md"}]})
    monkeypatch.setattr(mt, "_gh_py_module", lambda root: fake)
    code, out, _err = mt._gh_py_call(
        ["gh", "pr", "list", "--repo", "o/r", "--state", "open", "--limit", "80",
         "--json", "number,headRefName,files"], tmp_path)
    assert code == 0
    assert json.loads(out)[0]["files"] == [{"path": "internal/dispatch/inflight/x.md"}]


def test_gh_py_reports_an_api_error_as_a_failure_not_as_an_empty_page(tmp_path,
                                                                     monkeypatch):
    """An empty page reads as "no PRs" and reconciles nothing, silently — the defect's own
    shape. A refusal has to exit non-zero so `Call.why()` says what happened."""
    class _Broken(_FakeGhPy):
        def call(self, path, method="GET", data=None):
            return {"_error": 403, "_body": "forbidden"}

    monkeypatch.setattr(mt, "_gh_py_module", lambda root: _Broken([]))
    code, out, err = mt._gh_py_call(
        ["gh", "pr", "list", "--json", "number"], tmp_path)
    assert code != 0 and out == "" and "403" in err


def test_gh_py_missing_helper_is_127_with_the_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "_gh_py_module", lambda root: None)
    code, _out, err = mt._gh_py_call(["gh", "pr", "list", "--json", "number"], tmp_path)
    assert code == 127
    assert mt.GH_PY in err and mt._GH_PY_ENV in err


def test_the_fallback_serves_only_the_two_granted_shapes(monkeypatch, tmp_path):
    """Not a new grant (the claim in the code comment): the helper is reached for `gh pr
    list` / `gh pr view` and for nothing else, and only when `gh` is absent."""
    calls = []
    monkeypatch.setattr(mt.shutil, "which", lambda n: None)
    monkeypatch.setattr(mt, "_gh_py_call", lambda argv, cwd: (calls.append(list(argv))
                                                             or (0, "[]", "")))
    mt._run(["gh", "pr", "list", "--json", "number"], tmp_path)
    assert calls == [["gh", "pr", "list", "--json", "number"]]
    assert all(g[0] != "gh" or g[1] == "pr" for g in mt.GRANTED)
    assert {g for g in mt.GRANTED if g[0] == "gh"} == {("gh", "pr", "list"),
                                                       ("gh", "pr", "view")}


def test_a_present_gh_binary_still_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(mt.shutil, "which", lambda n: "/usr/bin/gh")
    monkeypatch.setattr(mt, "_gh_py_call",
                        lambda argv, cwd: pytest.fail("the fallback ran with gh present"))
    seen = {}
    monkeypatch.setattr(mt.subprocess, "run",
                        lambda argv, **kw: seen.setdefault("argv", argv) or
                        types.SimpleNamespace(returncode=0, stdout="[]", stderr=""))
    mt._run(["gh", "pr", "list", "--json", "number"], tmp_path)
    assert seen["argv"] == ["gh", "pr", "list", "--json", "number"]


# ── the card guard (RULINGS 2026-09-30 07:55 (1)) ────────────────────────────


def test_no_card_is_reissued_for_an_id_whose_dash_n_twin_has_an_open_pr(tmp_path):
    """The card storm: overnight the tick re-issued a card every hour for briefs its own
    headless sessions had already built, and the operator's taps were spent for nothing.
    An open PR on `w-2`'s head suppresses the card for `w` too — base and `-N` copy are
    one piece of work — and the tray line says why instead of going silent."""
    card = _waiting(state="prepared", age=2)
    t = _tick(tmp_path, Fake(eligible=_elig(cards=[card],
                                            headless={"available": False, "per_tick": 2}),
                             gh=[{"number": 4674, "headRefName": "claude/meta-w-2"}]),
              inflight={"w": CARD_FM})
    out = t.run()
    assert not [a for a in out["session_actions"] if a["kind"] == "reissue_card"]
    assert t.unreissued == [("w", "PR #4674 is open on it")]


def test_a_recorded_headless_session_on_the_twin_also_suppresses_the_card(tmp_path):
    """The other half of the same rule: no PR yet, but a sibling entry records a headless
    launch, so the work is out with a session and a card would buy a duplicate."""
    card = _waiting(state="prepared", age=2)
    t = _tick(tmp_path, Fake(eligible=_elig(cards=[card],
                                            headless={"available": False, "per_tick": 2})),
              inflight={"w": CARD_FM,
                        "w-2": {"launch": "headless", "started": "2026-09-30T06:00:00Z"}})
    out = t.run()
    assert not [a for a in out["session_actions"] if a["kind"] == "reissue_card"]
    assert t.unreissued == [("w", "a headless session is recorded for w-2")]


def test_an_unreadable_pr_list_does_not_suppress_the_card(tmp_path):
    """Fails toward issuing: a card wrongly issued costs one tap, a card never issued
    stalls the operator. The line says the guard could not read."""
    card = _waiting(state="prepared", age=2)
    t = _tick(tmp_path, Fake(eligible=_elig(cards=[card],
                                            headless={"available": False, "per_tick": 2}),
                             gh=(1, "", "gh: API rate limit")),
              inflight={"w": CARD_FM})
    out = t.run()
    assert [a["id"] for a in out["session_actions"] if a["kind"] == "reissue_card"] == ["w"]
    assert any(ln.startswith("card guard: open PRs unreadable") for ln in out["lines"])


def test_the_lanes_own_prs_never_suppress_a_card(tmp_path):
    """`lane/state` names every in-flight id by construction and `pm/*` discusses them;
    only a `claude/…` head is a chip's own PR."""
    assert mt.head_id("lane/state") == ""
    assert mt.head_id("pm/opus-review-4693") == ""
    assert mt.head_id("claude/meta-w-2") == "w"
    assert mt.head_id("claude/app-store-and-ledger-verbs") == "app-store-and-ledger-verbs"
    assert mt.base_id("w-12") == "w"


def test_ledger_n_rows_are_reported_and_never_rewritten(tmp_path):
    """RULINGS 2026-09-30 16:30 (1)/(2): the ledger half of the `-N` reconcile, report-only."""
    d = tmp_path / "meta-state"
    d.mkdir()
    a = json.dumps({"chips": [{"id": "x"}, {"id": "x-2", "pr": 7}, {"id": "y-3"}]})
    (d / "a.json").write_text(a)
    (d / "_skip.json").write_text(json.dumps({"chips": [{"id": "z-2"}]}))
    (d / "bad.json").write_text("{")
    rows = mt.ledger_n_rows(d)
    assert rows == [{"id": "x-2", "base": "x", "ledger": "a.json", "base_row": True},
                    {"id": "y-3", "base": "y", "ledger": "a.json", "base_row": False}]
    assert (d / "a.json").read_text() == a
    assert mt.ledger_n_rows(tmp_path / "absent") == []


def test_a_tick_names_its_ledger_n_rows_and_runs_the_same_steps(tmp_path):
    t = _tick(tmp_path, Fake())
    d = tmp_path / "no-ledgers"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"chips": [{"id": "x-2"}]}))
    t.run()
    assert "rig-preflight" in verbs(t) and "repair" in verbs(t)
    assert any(ln.startswith("ledger -N rows (report only") and "x-2 -> x" in ln
               and "base row absent" in ln for ln in t.lines)


# ── land-first: the tick lands its lane move before the checkout sees it ──────────────
#
# RULINGS 2026-09-29 21:15 PDT (5). These run the REAL mover against a real repo and a
# real `origin`, because the defect is what git does to the operator's checkout; every
# other verb is still the Fake. HOME is isolated so no ledger is reachable.

import os  # noqa: E402
import subprocess  # noqa: E402

LF_BRIEF = ("---\nid: {id}\naspect: substrate\ntitle: \"Make the widget frobnicate\"\n"
            "privileged: false\n---\nBODY of {id}, verbatim.\n")


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True,
                          text=True, check=check)


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


@pytest.fixture
def lf_repo(tmp_path, monkeypatch):
    """(checkout, origin): the operator's checkout on `main`, current with `origin`, one
    queued brief `lf-a` committed, and a `gh` whose one open PR is the lane PR."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env python3\nimport json, sys\na = sys.argv[1:]\n"
        "if a[:2] == ['pr', 'list']:\n"
        "    print(json.dumps([{'number': 9, 'url': 'https://x/pr/9'}]))\n"
        "elif a[:2] in (['pr', 'edit'], ['pr', 'create']):\n    print('https://x/pr/9')\n"
        "else:\n    sys.exit(9)\n")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", "%s:%s" % (bin_dir, os.environ.get("PATH", "")))
    origin, work = tmp_path / "origin", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git(work, "config", k, v)
    lane = work / "internal" / "dispatch"
    for d in ("queued", "inflight", "done"):
        (lane / d).mkdir(parents=True)
        (lane / d / ".gitkeep").write_text("")
    (lane / "queued" / "lf-a.md").write_text(LF_BRIEF.format(id="lf-a"))
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return work, origin


def _real_moves(fake):
    """The real mover for lane verbs and `sync`; the Fake for everything else."""
    def run(argv, cwd):
        if list(argv[:2]) == list(mt.MOVE) and argv[2] in ("land-first", "sync"):
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            return subprocess.run(list(argv), cwd=str(cwd), capture_output=True,
                                  text=True, env=env)
        return fake(argv, cwd)
    return run


def _lf_tick(work, tmp_path):
    ls = tmp_path / "last-seen.json"
    ls.write_text(json.dumps({"poked": []}))
    return mt.Tick(work, run=_real_moves(Fake()), last_seen_path=ls,
                   ledger_dir=tmp_path / "no-ledgers")


@pytest.fixture
def mover_on_path(monkeypatch, lf_repo):
    """The fixture checkout carries the real mover at the path the grant names."""
    work, _ = lf_repo
    (work / "tools").mkdir(exist_ok=True)
    for name in ("meta-dispatch-move", "meta_dispatch_integrity.py",
                 "meta_dispatch_headless.py", "meta_rig_preflight.py", "rig_line.py"):
        src = _REPO / "tools" / name
        if src.exists():
            (work / "tools" / name).symlink_to(src)
    # Untracked and ignored: the checkout's own status must read only lane state.
    (work / ".git" / "info" / "exclude").write_text("tools/\n")
    return lf_repo


def test_a_landed_transition_leaves_the_checkout_clean_and_fast_forwarded(
        tmp_path, mover_on_path):
    work, origin = mover_on_path
    t = _lf_tick(work, tmp_path)
    before = _snapshot(work / "internal")

    r = t.move("launch", "lf-a", "--session", "task_1", "--json")

    assert r.ok, r.err
    assert r.data["land_first"]["landed"] is True
    assert _snapshot(work / "internal") == before          # not written by the tick
    assert _git(work, "status", "--porcelain").stdout == ""
    # The reconciler merges the lane PR; the next tick's `sync` fast-forwards.
    _git(origin, "update-ref", "refs/heads/main", "refs/heads/lane/state")
    s = t.move("sync", "--json")
    assert s.ok and s.data["pulled"] is True, s.err
    assert (work / "internal/dispatch/inflight/lf-a.md").is_file()
    assert not (work / "internal/dispatch/queued/lf-a.md").exists()
    assert _git(work, "status", "--porcelain").stdout == ""


def test_a_failed_land_leaves_the_checkout_byte_identical_and_says_so(
        tmp_path, mover_on_path):
    work, origin = mover_on_path
    t = _lf_tick(work, tmp_path)
    before = _snapshot(work / "internal")
    origin.rename(tmp_path / "origin-gone")                # no network, in effect

    r = t.move("launch", "lf-a", "--session", "task_1", "--json")

    assert not r.ok
    assert _snapshot(work / "internal") == before
    assert _git(work, "status", "--porcelain").stdout == ""
    named = [ln for ln in t.lines if ln.startswith("land-first: launch lf-a not landed")]
    assert len(named) == 1, t.lines
    assert "the transition did not happen" in named[0]
    assert [a["signature"] for a in t.actions] == ["lane:land-first-refused:launch"]


def test_an_id_already_done_on_main_is_refused_not_landed(tmp_path, mover_on_path):
    work, origin = mover_on_path
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(origin), str(other)],
                   check=True)
    for k, v in (("user.email", "o@example.com"), ("user.name", "o")):
        _git(other, "config", k, v)
    _git(other, "mv", "internal/dispatch/queued/lf-a.md", "internal/dispatch/done/lf-a.md")
    _git(other, "commit", "-qm", "the chip PR merged")
    _git(other, "push", "-q", "origin", "HEAD:main")
    t = _lf_tick(work, tmp_path)                          # the checkout is behind
    before = _snapshot(work / "internal")

    r = t.move("launch", "lf-a", "--session", "task_1", "--json")

    assert not r.ok and "no queued brief" in r.err
    assert "lane/state" not in _git(work, "ls-remote", "--heads", "origin").stdout
    assert _snapshot(work / "internal") == before


def test_the_land_first_list_is_the_movers_not_a_copy():
    """The brief's "read the list from the mover, do not duplicate it": the tick's set is
    the mover's tuple, loaded from the mover at run time."""
    from importlib.machinery import SourceFileLoader
    import importlib.util
    loader = SourceFileLoader("mdm_for_tick_test", str(_REPO / "tools" / "meta-dispatch-move"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    assert mt.land_first_verbs() == tuple(mod.LAND_FIRST_VERBS)
    assert "LAND_FIRST_VERBS =" not in (_REPO / "tools" / "meta_tick.py").read_text()


def test_a_candidate_pending_on_the_lane_pr_is_not_dispatched_again(tmp_path):
    """The lane PR has not merged, so this checkout still reads `a` queued — but the PR
    already carries its launch. Neither a headless start nor a card is spent on it."""
    gh = [{"number": 9, "headRefName": "lane/state", "state": "OPEN",
           "files": [{"path": "internal/dispatch/inflight/a.md"},
                     {"path": "internal/dispatch/queued/a.md"}]}]
    t = _tick(tmp_path, Fake(gh=gh, eligible=_elig(candidates=[_cand("a"),
                                                                _cand("b", "prepared")])),
              queued=["a", "b"])
    out = t.run()
    assert "launch" not in verbs(t)
    assert "skip a: its transition is pending on lane PR #9" in t.lines
    assert [a["id"] for a in out["session_actions"] if a["kind"] == "prepare_card"] == ["b"]


# ── the caps count launches that are pending on the lane PR ──────────────────


class _CapFake(Fake):
    """An eligibility helper that applies the caps the way the real one does: `slots` and
    `prepared_slots` start at the cap and lose whatever `--pending-*` says. It reads the
    argv, so a tick that forgets to pass the counts is told it has the full cap."""

    def __init__(self, cands, cap=2, prepared_cap=2, **answers):
        super().__init__(**answers)
        self.cands, self.cap, self.prepared_cap = cands, cap, prepared_cap
        self.seen = []

    def __call__(self, argv, cwd):
        if self.key(argv) == "eligible":
            def flag(name):
                return int(argv[argv.index(name) + 1]) if name in argv else 0
            slots = max(0, self.cap - flag("--pending-in-motion"))
            prep = max(0, self.prepared_cap - flag("--pending-prepared"))
            hs = max(0, slots - flag("--headless-started"))
            self.seen.append((slots, prep))
            # the real helper's routing: a headless route needs a build slot, a prepared one
            # a prepared slot
            room = {"headless": slots, "prepared": prep}
            cands = []
            for c in self.cands:
                if room[c["route"]] > 0:
                    room[c["route"]] -= 1
                    cands.append(dict(c))
            self.answers["eligible"] = _elig(candidates=cands,
                                             slots=slots, prepared_slots=prep,
                                             headless_slots=min(2, hs), cap=self.cap)
        return super().__call__(argv, cwd)


def _lane_pr(*ids):
    return [{"number": 9, "headRefName": "lane/state", "state": "OPEN",
             "files": [{"path": "internal/dispatch/inflight/%s.md" % i} for i in ids]
                      + [{"path": "internal/dispatch/queued/%s.md" % i} for i in ids]}]


def _pending_launch_slots(fake):
    return fake.seen[-1]


def test_a_second_tick_does_not_refill_slots_the_open_lane_pr_already_holds(tmp_path):
    """Tick one launches `a` headless into one of two build slots. Its lane PR stays open,
    so tick two's checkout still reads `a` queued and `slots` would be 2 again: the tick
    must charge `a` against the cap and launch only what tick one left free."""
    cands = [_cand("a"), _cand("b"), _cand("c")]
    one = _CapFake(cands[:1], gh=[])
    t1 = _tick(tmp_path, one, queued=["a", "b", "c"])
    t1.run()
    assert "launch" in verbs(t1)                                  # a was launched
    kinds = state_written(t1)["launch_kinds"]
    assert kinds == {"a": "built"}

    two = _CapFake(cands[1:], gh=_lane_pr("a"))
    t2 = _tick(tmp_path, two, last={"poked": [], "launch_kinds": kinds},
               queued=["a", "b", "c"])
    t2.run()
    assert _pending_launch_slots(two)[0] == 1                      # 2 - the pending `a`
    launched = [a[4] for a in t2.argvs if Fake.key(a) == "launch"]
    assert launched == ["b"]                                       # one free slot, one launch
    assert "--pending-in-motion" in t2.argvs[[i for i, a in enumerate(t2.argvs)
                                              if Fake.key(a) == "eligible"][0]]


def test_pending_prepared_cards_are_charged_to_the_prepared_cap(tmp_path):
    fake = _CapFake([_cand("c", "prepared")], cap=2, prepared_cap=1,
                    gh=_lane_pr("a"))
    t = _tick(tmp_path, fake, last={"poked": [], "launch_kinds": {"a": "prepared"}},
              queued=["a", "c"])
    out = t.run()
    assert fake.seen[-1] == (2, 0)                  # prepared cap 1 - the pending card
    assert [a for a in out["session_actions"] if a["kind"] == "prepare_card"] == []


def test_an_unrecorded_pending_launch_is_charged_as_built(tmp_path):
    fake = _CapFake([], cap=2, gh=_lane_pr("a"))
    _tick(tmp_path, fake, queued=["a"]).run()
    assert fake.seen[-1] == (1, 2)


def test_a_merged_lane_pr_is_not_charged_twice(tmp_path):
    """After the merge the checkout holds `a` in inflight/ and the lane PR is gone: the
    eligibility read charges it from the entry, the tick charges nothing."""
    fake = _CapFake([], cap=2, gh=[])
    t = _tick(tmp_path, fake, last={"poked": [], "launch_kinds": {"a": "built"}},
              inflight={"a": {"launch": "headless", "session": "s"}})
    t.run()
    assert fake.seen[-1] == (2, 2)
    assert not any("--pending-in-motion" in a for a in t.argvs)
    assert state_written(t)["launch_kinds"] == {}                  # the record is pruned
    # and a lane PR still open while the checkout already holds the entry is not pending
    fake2 = _CapFake([], cap=2, gh=_lane_pr("a"))
    other = tmp_path / "other"
    other.mkdir()
    _tick(other, fake2, inflight={"a": {"launch": "headless", "session": "s"}}).run()
    assert fake2.seen[-1] == (2, 2)


def test_a_gated_candidate_fences_nothing_for_the_rest_of_the_tick(tmp_path):
    """RULINGS 2026-10-05 16:45 PDT: the tick asks eligibility once more with the refused
    brief marked duplicate, and launches the brief its files had deferred."""
    fake = Fake(eligible=[_elig(candidates=[_cand("a")]),
                          _elig(candidates=[_cand("b")])],
                inflight=[{"actionable_count": 1, "count": 1, "warnings": [],
                           "overlaps": [{"title": "same thing"}]},
                          {"actionable_count": 0, "count": 0, "warnings": []}])
    t = _tick(tmp_path, fake, queued=["a", "b"])
    out = t.run()
    again = [a for a in t.argvs if "--duplicates" in a]
    assert again and again[0][again[0].index("--duplicates") + 1] == "a"
    assert "launch" in verbs(t) and "duplicates_exhausted" not in out["report"]


# ── a QUEUED repair whose `repair_pr` merged is retired (RULINGS 2026-10-07 16:25 (1)) ──

def _queued_repair(tmp_path, bid="hold-fix-4674-x", rpr=4674):
    q = tmp_path / "internal" / "dispatch" / "queued"
    q.mkdir(parents=True, exist_ok=True)
    (q / ("%s.md" % bid)).write_text(
        "---\nid: %s\naspect: apps\ntitle: \"Fix it\"\nprivileged: false\n"
        "class: repair\nrepair_pr: %d\n---\nbody\n" % (bid, rpr))


def test_a_queued_repair_whose_pr_merged_is_completed_every_tick(tmp_path):
    _queued_repair(tmp_path)
    fake = Fake(gh=[{"number": 4674, "state": "MERGED"}], complete={"ok": True})
    t = _tick(tmp_path, fake)
    t.run()
    assert ["python3", "tools/meta-dispatch-move", "land-first", "complete",
            "hold-fix-4674-x", "--json"] in t.argvs
    v = verbs(t)
    assert v.index("complete") < v.index("eligible")
    el = next(a for a in t.argvs if Fake.key(a) == "eligible")
    assert "4674" in el[el.index("--merged-prs") + 1].split(",")


def test_a_queued_repair_merged_before_the_page_is_asked_for_by_number(tmp_path):
    _queued_repair(tmp_path)
    fake = Fake(gh=[{"number": 1, "state": "OPEN"}], complete={"ok": True})

    def run(argv, cwd):
        if list(argv[:3]) == ["gh", "pr", "view"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                {"state": "MERGED"}), stderr="")
        return fake(argv, cwd)
    t = _tick(tmp_path, run)
    t.run()
    assert "complete" in verbs(t)


def test_a_queued_repair_whose_pr_is_open_stays_queued(tmp_path):
    _queued_repair(tmp_path)
    t = _tick(tmp_path, Fake(gh=[{"number": 4674, "state": "OPEN"}]))
    t.run()
    assert "complete" not in verbs(t)


def test_a_dry_run_names_the_retirement_and_moves_nothing(tmp_path):
    _queued_repair(tmp_path)
    t = _tick(tmp_path, Fake(gh=[{"number": 4674, "state": "MERGED"}]), dry_run=True)
    t.run()
    assert "complete" not in verbs(t)
    assert any("would retire hold-fix-4674-x" in ln for ln in t.lines)
