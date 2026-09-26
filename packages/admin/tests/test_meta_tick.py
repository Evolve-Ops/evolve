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
    return mt.Tick(tmp_path, run=fake, last_seen_path=ls, dry_run=dry_run)


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
                        "recover-stalls", "eligible", "cards", "land", "heartbeat", "state"]
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
    report verbatim, on a quiet tick."""
    line = "cards waiting for a tap: 1 — [x] 10:00 (2h)"
    t = _tick(tmp_path, Fake(eligible=_elig(cards_waiting_line=line),
                             repair={"line": "repair: 1 executed — r1 (req pm)"}))
    report = t.run()["report"]
    assert line in report.splitlines()
    assert "repair: 1 executed — r1 (req pm)" in report.splitlines()
    assert "lane PRs open: 0" in report.splitlines()


# ── stop rules ──────────────────────────────────────────────────────────────


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
    assert ["python3", "tools/meta-dispatch-move", "launch", "a", "--headless",
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
    launched = [a[3] for a in t.argvs if a[2:3] == ["launch"]]
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
    assert [a[3] for a in t.argvs if a[2:3] == ["launch"]] == ["a"]
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
    assert ["python3", "tools/meta-dispatch-move", "launch", "a", "--session", "task_9f",
            "--json"] in t.argvs
    text = out["poke"]["text"]
    assert text == ("[META:substrate] [a] %s is prepared in the tray — created 11:00 PDT, "
                    "session `task_9f` — one tap to start it." % ("x" * 63 + "…"))
    assert len(text) <= mt.POKE_BUDGET
    merge = [a for a in t.argvs if "--merge" in a][0]
    assert json.loads(merge[-1]) == {"poked": ["1:card:a:0h"]}
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
    assert "[p] Title prepared 12h — tap or decline" in t.actions[0]["text"]
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
    assert ["python3", "tools/meta-dispatch-move", "done", "m", "--json"] in t.argvs
    row = [a for a in t.argvs if a[2:3] == ["chip-row"]][0]
    assert json.loads(row[row.index("--set") + 1]) == {"bucket": "merged", "pr": 7}
    el = [a for a in t.argvs if Fake.key(a) == "eligible"][0]
    assert el[el.index("--merged-prs") + 1] == "7"


def test_closed_inflight_is_abandoned_with_its_pr(tmp_path):
    gh = [{"number": 8, "state": "CLOSED", "headRefName": "b"}]
    t = _tick(tmp_path, Fake(gh=gh, abandon={"ok": True}), inflight={"c": {"pr": 8}})
    t.run()
    assert ["python3", "tools/meta-dispatch-move", "abandon", "c", "--pr", "8",
            "--json"] in t.argvs


def test_dry_run_mutates_nothing(tmp_path):
    t = _tick(tmp_path, Fake(eligible=_elig(candidates=[_cand("a")]),
                             gh=[{"number": 7, "state": "MERGED"}]),
              inflight={"m": {"pr": 7}}, queued=["a"], dry_run=True)
    t.run()
    mutating = {"launch", "done", "abandon", "bind", "cards", "chip-row", "heartbeat",
                "state", "probe", "repair-queued-copy", "recover-stalls"}
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
