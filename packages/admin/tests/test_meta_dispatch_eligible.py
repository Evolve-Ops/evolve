"""Unit tests for tools/meta-dispatch-eligible.

`tools/meta-dispatch-eligible` is the executable half of the PM dispatch lane
(internal/spec-pm-lane-2026-08-23.md, D-PM1/2/3): the `meta-dispatch` scheduled task is
markdown a model executes, so the deterministic part of the decision — front-matter
parsing, the privileged gate, `depends_on`, the cap, the back-pressure pause, and the
oldest-first order — lives here where it can be pinned.

These tests pin each rule against crafted fixture dirs so none can silently drift, and
they pin the two FAIL-SAFE directions that matter most: a brief whose schema does not
parse is held (never dispatched on a guess), and a `privileged` field that is not a real
boolean is an error rather than a falsy default.

The tool is an extensionless script under tools/, so we load it by path (it in turn loads
tools/meta-verdict-check, which loads tools/meta-queue, to reuse the ONE canonical verdict
parser). No network: `gh` is never invoked by this tool.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "meta-dispatch-eligible"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("meta_dispatch_eligible", str(_TOOL))
    spec = importlib.util.spec_from_loader("meta_dispatch_eligible", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_dispatch_eligible"] = mod
    loader.exec_module(mod)
    return mod


mde = _load_tool()


# ── fixtures ─────────────────────────────────────────────────────────────────


def lane(tmp_path):
    root = tmp_path / "dispatch"
    for sub in ("queued", "inflight", "done", "reviews"):
        (root / sub).mkdir(parents=True)
    return root


def brief(root, state, ident, *, aspect="substrate", title=None, privileged=False,
          greenlight=None, depends_on=None, created="2026-08-20", pm="fable-cowork",
          pr=None, launch=None, branch=None,
          body="Build the thing.\n\nOpen the PR with `gh pr create`.", extra=""):
    fm = ["---", "id: %s" % ident, "aspect: %s" % aspect,
          "title: %s" % (title or ident.replace("-", " ")),
          "privileged: %s" % ("true" if privileged else "false")]
    if greenlight is not None:
        fm.append("operator_greenlight: %s" % ("true" if greenlight else "false"))
    if depends_on is not None:
        fm.append("depends_on: [%s]" % ", ".join(str(d) for d in depends_on))
    if pr is not None:
        fm.append("pr: %d" % pr)
    if launch is not None:
        fm.append("launch: %s" % launch)
    if branch is not None:
        fm.append("branch: %s" % branch)
    fm += ["created: %s" % created, "pm: %s" % pm]
    if extra:
        fm.append(extra)
    fm.append("---")
    path = root / state / ("%s.md" % ident)
    path.write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
    return path


def review(root, pr, text):
    (root / "reviews" / ("pr-%d.md" % pr)).write_text(text, encoding="utf-8")


# ── D-PM1: the privileged gate ───────────────────────────────────────────────


def test_privileged_without_greenlight_is_held(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "priv-chip", privileged=True)
    r = mde.evaluate(root)
    assert r["next"] is None and r["eligible"] == []
    (held,) = r["held"]
    assert held["id"] == "priv-chip"
    assert held["reason"] == "privileged-without-greenlight"


def test_privileged_with_greenlight_false_is_still_held(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "priv-chip", privileged=True, greenlight=False)
    r = mde.evaluate(root)
    assert r["next"] is None
    assert r["held"][0]["reason"] == "privileged-without-greenlight"


def test_privileged_with_operator_greenlight_is_eligible(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "priv-chip", privileged=True, greenlight=True)
    r = mde.evaluate(root)
    assert r["next"]["id"] == "priv-chip"


def test_non_boolean_privileged_is_an_error_not_a_falsy_default(tmp_path):
    """The fail-safe direction: a typo'd flag must HOLD the brief, never dispatch it as
    non-privileged. Neither a truthiness read (`privileged: maybe` → True) nor a
    `bool(fm.get("privileged"))` default (missing → False, silently skipping the D-PM1
    gate) is safe, so anything that is not a real boolean is invalid."""
    root = lane(tmp_path)
    p = brief(root, "queued", "typo-chip")
    p.write_text(p.read_text().replace("privileged: false", "privileged: maybe"))
    r = mde.evaluate(root)
    assert r["eligible"] == [] and r["held"] == [] and r["next"] is None
    assert "privileged must be true or false" in r["invalid"][0]["error"]


# ── depends_on ───────────────────────────────────────────────────────────────


def test_depends_on_unmet_chip_is_held(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "second", depends_on=["first"])
    brief(root, "inflight", "first")
    r = mde.evaluate(root)
    assert r["next"] is None
    assert r["held"][0]["reason"] == "depends-on-unmet"
    assert r["held"][0]["detail"] == "chip:first"


def test_depends_on_satisfied_when_the_dependency_is_done(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "second", depends_on=["first"])
    brief(root, "done", "first")
    assert mde.evaluate(root)["next"]["id"] == "second"


def test_depends_on_pr_is_unmet_until_observed_merged(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "after-pr", depends_on=["#3763"])
    assert mde.evaluate(root)["held"][0]["detail"] == "pr:3763"
    assert mde.evaluate(root, facts={"merged_prs": [3763]})["next"]["id"] == "after-pr"


@pytest.mark.parametrize("form", ["3763", "#3763", "pr:3763"])
def test_pr_dependency_accepts_the_three_documented_forms(tmp_path, form):
    root = lane(tmp_path)
    brief(root, "queued", "after-pr", depends_on=[form])
    assert mde.evaluate(root, facts={"merged_prs": [3763]})["next"]["id"] == "after-pr"


def test_the_display_form_of_a_dep_is_not_valid_input(tmp_path):
    """`chip:<id>` is what every JSON VIEW renders (the entry's own `depends_on`, and a
    `held[]` entry's `detail`), and it is NOT accepted input — `_dep_key` takes a bare
    kebab id, an int, or a PR ref, and nothing else. Reading the tool's own output back
    into a brief is the natural mistake and it is a live one: the first draft of
    `lane-launch-field-has-no-writeback` was written `depends_on: [chip:...]` that way and
    was held `invalid` on 2026-08-27. Pinned so the asymmetry is deliberate rather than
    incidental, and documented in `internal/dispatch/README.md`'s schema table."""
    root = lane(tmp_path)
    brief(root, "queued", "second", depends_on=["chip:first"])
    brief(root, "done", "first")
    r = mde.evaluate(root)
    assert r["next"] is None
    assert "neither a kebab chip id nor a PR number" in r["invalid"][0]["error"]


# ── D-PM3′ / D-PM6: the two caps ─────────────────────────────────────────────
#
# Slots measure WORK IN MOTION. Every fixture below therefore has to say which kind of
# entry it is building, and the two kinds are the whole point:
#
#   started(...)  — `launch: started` + a branch (what `meta-dispatch-move start` writes),
#                   optionally a `pr`: charged to `--cap` (default 4)
#   prepared(...) — `launch: prepared`, no branch, no pr (all a dispatcher can write):
#                   charged to `--prepared-cap` (default 2)


def started(root, ident, *, pr=None, reviewed="Verdict: PASS — fine.\n"):
    """An in-flight entry a chip has actually started. Reviewed by default so the fixture
    exercises the CAP and not §7 back-pressure."""
    brief(root, "inflight", ident, pr=pr, launch="started", branch="claude/%s" % ident)
    if pr is not None and reviewed is not None:
        review(root, pr, reviewed)


def prepared(root, ident):
    """A card the dispatcher prepared and nobody has clicked: no branch, no pr."""
    brief(root, "inflight", ident, launch="prepared")


def test_the_cap_is_four_and_it_counts_work_in_motion(tmp_path):
    """D-PM3′: four STARTED chips fill the lane. The queued brief stays eligible — the
    cap is the lane's state, not the brief's."""
    root = lane(tmp_path)
    for i in range(4):
        started(root, "busy-%d" % i, pr=100 + i)
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["cap"] == 4 and r["in_motion_count"] == 4 and r["slots"] == 0
    assert r["prepared_count"] == 0 and r["prepared_slots"] == 2
    assert r["blocked_by"] == "cap"
    assert r["next"] is None and r["dispatchable"] is False
    assert [e["id"] for e in r["eligible"]] == ["waiting"]


def test_three_started_plus_one_prepared_still_allows_one_more_launch(tmp_path):
    """The shape the old single count got wrong: four entries in `inflight/`, but only
    three of them are building, so there is room for a fourth."""
    root = lane(tmp_path)
    for i in range(3):
        started(root, "busy-%d" % i, pr=100 + i)
    prepared(root, "unclicked")
    brief(root, "queued", "a-first", created="2026-08-01")
    brief(root, "queued", "b-second", created="2026-08-02")
    r = mde.evaluate(root)
    assert r["inflight_count"] == 4           # the DIR holds four …
    assert r["in_motion_count"] == 3          # … three of which are in motion
    assert r["slots"] == 1 and r["prepared_count"] == 1 and r["prepared_slots"] == 1
    assert r["blocked_by"] is None
    assert r["next"]["id"] == "a-first"
    assert [e["id"] for e in r["eligible"]] == ["a-first", "b-second"]


def test_two_prepared_cards_block_dispatch_even_with_free_build_slots(tmp_path):
    """The prepared cap is a SEPARATE budget, and it binds first here: 2/4 in motion says
    the lane could build more, but the dispatcher only ever prepares cards, and two are
    already waiting on the operator's click."""
    root = lane(tmp_path)
    for i in range(2):
        started(root, "busy-%d" % i, pr=100 + i)
    prepared(root, "unclicked-a")
    prepared(root, "unclicked-b")
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["in_motion_count"] == 2 and r["slots"] == 2        # in motion 2/4: room
    assert r["prepared_count"] == 2 and r["prepared_slots"] == 0  # prepared 2/2: full
    assert r["blocked_by"] == "prepared-cap"
    assert r["next"] is None and r["dispatchable"] is False
    assert "prepared and unclicked (prepared cap 2)" in mde.render_text(r)


def test_a_prepared_card_that_started_stops_costing_a_prepared_slot(tmp_path):
    """`meta-dispatch-move start` moves an entry between the two budgets and nowhere
    else — the partition is total, so the entry is always charged exactly once."""
    root = lane(tmp_path)
    prepared(root, "chip-a")
    before = mde.evaluate(root)
    assert (before["in_motion_count"], before["prepared_count"]) == (0, 1)

    p = root / "inflight" / "chip-a.md"
    p.write_text(p.read_text().replace("launch: prepared",
                                       "launch: started\nbranch: claude/chip-a"))
    after = mde.evaluate(root)
    assert (after["in_motion_count"], after["prepared_count"]) == (1, 0)
    assert after["inflight_count"] == before["inflight_count"] == 1


def test_a_branch_without_a_started_stamp_still_reads_as_in_motion(tmp_path):
    """Start evidence is start evidence. `tools/meta-queue._has_start_evidence` counts a
    recorded `branch` on its own, and if this count disagreed, group (E) would invite a
    click on a card whose build slot this tool had already handed to someone else."""
    root = lane(tmp_path)
    brief(root, "inflight", "half-stamped", branch="claude/half-stamped")
    r = mde.evaluate(root)
    assert r["in_motion_count"] == 1 and r["prepared_count"] == 0
    assert r["inflight"][0]["in_motion"] is True


def test_prepared_and_in_motion_partition_the_whole_dir(tmp_path):
    """No entry may fall outside BOTH counts — that is a slot silently handed back."""
    root = lane(tmp_path)
    started(root, "s1", pr=101)
    started(root, "s2")                       # started, no PR yet
    prepared(root, "p1")
    brief(root, "inflight", "bare")           # no launch field at all (pre-D-PM6 entry)
    r = mde.evaluate(root, cap=9, prepared_cap=9)
    assert r["in_motion_count"] + r["prepared_count"] == r["inflight_count"] == 4
    assert r["in_motion_count"] == 2 and r["prepared_count"] == 2


def test_the_cap_outranks_the_prepared_cap_when_both_are_full(tmp_path):
    root = lane(tmp_path)
    for i in range(4):
        started(root, "busy-%d" % i, pr=100 + i)
    prepared(root, "unclicked-a")
    prepared(root, "unclicked-b")
    brief(root, "queued", "waiting")
    assert mde.evaluate(root)["blocked_by"] == "cap"


def test_back_pressure_still_outranks_both_caps(tmp_path):
    """Guardrail: §7 is unchanged and still pre-empts the cap decision."""
    root = lane(tmp_path)
    for i in range(3):
        started(root, "busy-%d" % i, pr=200 + i, reviewed=None)   # unreviewed
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["slots"] == 1                    # the cap alone would have allowed a launch
    assert r["blocked_by"] == "back-pressure"


# ── D-PM6: a held PR is in motion, and it is NAMED ───────────────────────────


def test_a_held_pr_still_occupies_its_slot_and_is_named(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=3976, reviewed="Verdict: CONCERNS — one blocker.\n")
    started(root, "chip-b", pr=3977)
    r = mde.evaluate(root)
    assert r["in_motion_count"] == 2 and r["slots"] == 2   # NOT subtracted
    assert r["held_prs"] == [3976]
    text = mde.render_text(r)
    assert "· 1 held: #3976" in text
    assert "still counts as in motion" in text


def test_a_passing_review_is_not_a_hold(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=3976)                       # PASS
    started(root, "chip-b", pr=3977, reviewed=None)        # not yet reviewed
    prepared(root, "unclicked")                            # no PR at all
    r = mde.evaluate(root)
    assert r["held_prs"] == []
    assert "held:" not in mde.render_text(r)


def test_a_review_naming_another_pr_never_marks_this_one_held(tmp_path):
    """The held list reads reviews through the SAME guard `--pm-verdict` does, so a
    mismatched front-matter `pr:` cannot make the summary accuse an innocent PR."""
    root = lane(tmp_path)
    started(root, "chip-a", pr=3976,
            reviewed="---\npr: 3999\n---\nVerdict: FAIL — broken.\n")
    assert mde.evaluate(root)["held_prs"] == []


# ── the summary line (pinned) ────────────────────────────────────────────────


def test_the_summary_line_states_both_counts_both_caps_and_the_holds(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=3976, reviewed="Verdict: CONCERNS — one blocker.\n")
    started(root, "chip-b", pr=3977)
    prepared(root, "unclicked")
    lines = mde.render_text(mde.evaluate(root)).splitlines()
    assert lines[1] == ("  in flight: 2/4 · prepared: 1/2 · slots: 2 in-motion, "
                        "1 prepared · 1 held: #3976")


def test_the_summary_line_omits_the_held_clause_when_nothing_is_held(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=3976)
    lines = mde.render_text(mde.evaluate(root)).splitlines()
    assert lines[1] == "  in flight: 1/4 · prepared: 0/2 · slots: 3 in-motion, 2 prepared"


def test_the_two_caps_are_overridable_and_reported(tmp_path):
    root = lane(tmp_path)
    prepared(root, "unclicked")
    brief(root, "queued", "waiting")
    r = mde.evaluate(root, cap=1, prepared_cap=1)
    assert (r["cap"], r["prepared_cap"]) == (1, 1)
    assert r["blocked_by"] == "prepared-cap"
    assert mde.evaluate(root, cap=1, prepared_cap=2)["next"]["id"] == "waiting"


def test_the_prepared_cap_default_has_one_definition(tmp_path):
    """The gate is here; `tools/meta-queue` renders group (E) against the same number.
    Two copies of `2` that must agree is the drift the shared import exists to prevent."""
    path = _TOOL.parent / "meta-queue"
    loader = importlib.machinery.SourceFileLoader("meta_queue_for_cap_pin", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mq = importlib.util.module_from_spec(spec)
    loader.exec_module(mq)
    assert mde.DEFAULT_PREPARED_CAP == mq.PM_PREPARED_CAP == 2


# ── duplicates (tools/meta-inflight) ─────────────────────────────────────────


def test_duplicate_reported_by_meta_inflight_is_held(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "dup-chip", created="2026-08-01")
    brief(root, "queued", "clean-chip", created="2026-08-02")
    r = mde.evaluate(root, facts={"duplicates": {"dup-chip": "overlaps open PR #3700"}})
    assert r["next"]["id"] == "clean-chip"
    (held,) = r["held"]
    assert held["id"] == "dup-chip" and held["reason"] == "duplicate"
    assert "3700" in held["detail"]


def test_an_id_already_in_flight_is_held_as_a_duplicate_id(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "same-id")
    brief(root, "inflight", "same-id")
    r = mde.evaluate(root)
    assert r["next"] is None and r["held"][0]["reason"] == "duplicate-id"


# ── ordering ─────────────────────────────────────────────────────────────────


def test_oldest_first_order(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "newest", created="2026-08-22")
    brief(root, "queued", "oldest", created="2026-08-01")
    brief(root, "queued", "middle", created="2026-08-10")
    r = mde.evaluate(root)
    assert [e["id"] for e in r["eligible"]] == ["oldest", "middle", "newest"]
    assert r["next"]["id"] == "oldest"


def test_same_day_ties_break_on_id_so_the_order_is_total(tmp_path):
    root = lane(tmp_path)
    for ident in ("zeta", "alpha", "mid"):
        brief(root, "queued", ident, created="2026-08-05")
    assert [e["id"] for e in mde.evaluate(root)["eligible"]] == ["alpha", "mid", "zeta"]


# ── back-pressure (spec §7) ──────────────────────────────────────────────────


def test_three_unreviewed_pm_lane_prs_pause_dispatch(tmp_path):
    root = lane(tmp_path)
    for i in range(3):
        brief(root, "inflight", "busy-%d" % i, pr=200 + i)
    brief(root, "queued", "waiting")
    r = mde.evaluate(root, cap=9)          # cap out of the way: this is back-pressure
    assert r["back_pressure"]["paused"] is True
    assert r["back_pressure"]["unreviewed_prs"] == [200, 201, 202]
    assert r["blocked_by"] == "back-pressure" and r["next"] is None


def test_a_review_file_of_any_verdict_drains_back_pressure(tmp_path):
    """§7 counts the REVIEW, not its outcome — a CONCERNS verdict is drained through
    /queue, so it must not also wedge the dispatcher."""
    root = lane(tmp_path)
    for i in range(3):
        brief(root, "inflight", "busy-%d" % i, pr=200 + i)
    review(root, 200, "Verdict: CONCERNS — one blocker.\n")
    brief(root, "queued", "waiting")
    r = mde.evaluate(root, cap=9)
    assert r["back_pressure"]["paused"] is False
    assert r["next"]["id"] == "waiting"


def test_an_inflight_entry_without_a_pr_is_not_counted_unreviewed(tmp_path):
    root = lane(tmp_path)
    for i in range(3):
        brief(root, "inflight", "busy-%d" % i)      # prepared, no PR yet
    r = mde.evaluate(root, cap=9)
    assert r["back_pressure"]["unreviewed_prs"] == []
    assert r["back_pressure"]["paused"] is False


# ── schema validation (held, never guessed at) ───────────────────────────────


@pytest.mark.parametrize("mutate,expected", [
    (lambda t: t.replace("id: good-chip\n", ""), "missing required front-matter field"),
    (lambda t: t.replace("pm: fable-cowork\n", ""), "missing required front-matter field"),
    (lambda t: t.replace("id: good-chip", "id: Good_Chip"), "not strict kebab-case"),
    (lambda t: t.replace("id: good-chip", "id: other-chip"), "does not match the filename stem"),
    (lambda t: t.replace("created: 2026-08-20", "created: last tuesday"), "is not YYYY-MM-DD"),
    (lambda t: t.replace("aspect: substrate", "aspect: Substrate"), "not a strict-kebab"),
    (lambda t: t.split("---", 2)[2].strip(), "no YAML front matter"),
])
def test_a_malformed_brief_is_invalid_not_dispatched(tmp_path, mutate, expected):
    root = lane(tmp_path)
    p = brief(root, "queued", "good-chip")
    p.write_text(mutate(p.read_text()))
    r = mde.evaluate(root)
    assert r["next"] is None and r["eligible"] == []
    assert expected in r["invalid"][0]["error"]


def test_an_empty_body_is_invalid_because_the_body_is_the_prompt(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "hollow", body="")
    r = mde.evaluate(root)
    assert "body is empty" in r["invalid"][0]["error"]


def test_one_malformed_brief_does_not_block_the_rest_of_the_queue(tmp_path):
    root = lane(tmp_path)
    p = brief(root, "queued", "broken", created="2026-08-01")
    p.write_text(p.read_text().replace("privileged: false", "privileged: maybe"))
    brief(root, "queued", "fine", created="2026-08-02")
    r = mde.evaluate(root)
    assert r["next"]["id"] == "fine"
    assert len(r["invalid"]) == 1


def test_block_list_depends_on_parses(tmp_path):
    root = lane(tmp_path)
    path = root / "queued" / "blocky.md"
    path.write_text(
        "---\nid: blocky\naspect: substrate\ntitle: Blocky\nprivileged: false\n"
        "depends_on:\n  - first\n  - 3763\ncreated: 2026-08-20\npm: fable-cowork\n---\n\nBody.\n")
    r = mde.evaluate(root)
    assert r["held"][0]["detail"] == "chip:first, pr:3763"


# ── D-PM2: the PM review verdict ─────────────────────────────────────────────


def test_pm_pass_on_a_lane_pr_qualifies(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, "---\npr: 3771\nreviewer: fable-cowork\n---\n"
                       "Verdict: PASS — brief followed.\n\n## Findings\n1. none\n")
    v = mde.pm_verdict(root, 3771)
    assert v["lane"] and v["verdict_is_pass"] and v["qualifies"]


def test_a_pr_outside_the_lane_never_qualifies(tmp_path):
    root = lane(tmp_path)
    review(root, 3771, "Verdict: PASS — looks fine.\n")
    v = mde.pm_verdict(root, 3771)
    assert v["lane"] is False and v["qualifies"] is False
    assert "not a PM-lane PR" in v["reason"]


def test_a_privileged_lane_pr_holds_for_the_operator_despite_a_pass(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-p", pr=3772, privileged=True, greenlight=True)
    review(root, 3772, "Verdict: PASS — all good.\n")
    v = mde.pm_verdict(root, 3772)
    assert v["verdict_is_pass"] is True and v["qualifies"] is False
    assert "non-privileged" in v["reason"]


def test_no_review_file_does_not_qualify(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    v = mde.pm_verdict(root, 3771)
    assert v["qualifies"] is False and "no PM review file" in v["reason"]


def test_a_blocking_verdict_does_not_qualify(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, "Verdict: FAIL — the helper is untested.\n")
    v = mde.pm_verdict(root, 3771)
    assert v["verdict_is_blocking"] and not v["qualifies"]


def test_a_review_naming_a_different_pr_is_refused(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, "---\npr: 3999\n---\nVerdict: PASS — fine.\n")
    v = mde.pm_verdict(root, 3771)
    assert v["qualifies"] is False and "not 3771" in v["reason"]


@pytest.mark.parametrize("text,why", [
    ("Verdict: PASS pending QA\n", "a not-done marker disqualifies unconditionally"),
    ("Verdict: PASS — some concerns\n", "un-de-fanged concerns read as blocking"),
    ("```\nVerdict: PASS\n```\n", "a fenced verdict is not seen at all"),
    ("Verdict: Passing this to the operator\n", "a prose opener is narrative, not a verdict"),
])
def test_the_four_standing_disqualifiers_apply_to_review_files_too(tmp_path, text, why):
    """The review file is read through the SAME parser as a PR-body review section, so
    the disqualifiers cannot drift apart between the two surfaces."""
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, text)
    assert mde.pm_verdict(root, 3771)["qualifies"] is False, why


def test_a_de_fanged_concerns_pass_still_qualifies(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, "Verdict: PASS — 2 non-blocking concerns.\n")
    assert mde.pm_verdict(root, 3771)["qualifies"] is True


def test_a_done_entry_still_resolves_the_lane(tmp_path):
    """The chip moves its own brief to done/ in the same PR the reconciler is about to
    merge, so by merge time the entry is usually in done/, not inflight/."""
    root = lane(tmp_path)
    brief(root, "done", "chip-a", pr=3771)
    review(root, 3771, "Verdict: PASS — fine.\n")
    assert mde.pm_verdict(root, 3771)["qualifies"] is True


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_json_and_exit_codes(tmp_path, capsys):
    root = lane(tmp_path)
    brief(root, "queued", "chip-a")
    assert mde.main(["--dir", str(root), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["next"]["id"] == "chip-a" and out["cap"] == 4 and out["prepared_cap"] == 2
    assert "_next_body" not in out          # private field never leaks into the payload


def test_cli_pm_verdict_exit_codes(tmp_path, capsys):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    assert mde.main(["--dir", str(root), "--pm-verdict", "3771"]) == 1
    review(root, 3771, "Verdict: PASS — fine.\n")
    assert mde.main(["--dir", str(root), "--pm-verdict", "3771"]) == 0
    assert "qualifies : True" in capsys.readouterr().out


def test_cli_missing_lane_dir_is_a_usage_error(tmp_path, capsys):
    assert mde.main(["--dir", str(tmp_path / "nope")]) == 2
    assert "no lane dir" in capsys.readouterr().err


def test_cli_facts_from_stdin(tmp_path, capsys, monkeypatch):
    root = lane(tmp_path)
    brief(root, "queued", "after-pr", depends_on=["#3763"])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"merged_prs": [3763]}'))
    assert mde.main(["--dir", str(root), "--facts", "-", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["next"]["id"] == "after-pr"


def test_cli_dry_run_prints_the_launch_plan_with_the_verbatim_body(tmp_path, capsys):
    root = lane(tmp_path)
    brief(root, "queued", "chip-a", aspect="substrate", title="Do the thing",
          body="WHY: roadmap:spec-pm-lane §4\nBuild it.")
    assert mde.main(["--dir", str(root), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "launch plan (dry run — nothing below was executed)" in out
    assert "[META:substrate] Do the thing" in out
    assert "| WHY: roadmap:spec-pm-lane §4" in out
    # read-only by construction: the entry has not moved
    assert (root / "queued" / "chip-a.md").exists()
    assert not (root / "inflight" / "chip-a.md").exists()


# ── the run's clock ──────────────────────────────────────────────────────────
#
# An unattended `meta-dispatch` run has no `date` grant and the harness gives it a date
# with no time, so every timestamp it wrote used to be a guess: 2026-08-26.jsonl's first
# line read `18:56:00Z` for a run that fired at `01:55:28Z` (local time wearing a Z), and
# the log's `<date>.jsonl` shard key came off the same non-clock. Reporting the time is
# not mutating the lane, so the read-only decider is where the run gets it.


def test_queue_json_carries_a_utc_now(tmp_path, capsys):
    import datetime

    root = lane(tmp_path)
    brief(root, "queued", "chip-a")
    assert mde.main(["--dir", str(root), "--json"]) == 0
    now = json.loads(capsys.readouterr().out)["now"]

    stamped = datetime.datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")   # exact shape
    real = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert abs((real - stamped).total_seconds()) < 120, "not a real clock: %s" % now


def test_now_is_reported_even_when_the_queue_is_empty(tmp_path):
    """Step 7 writes `last_run` on EVERY run, including the no-op runs that are most of
    them — so the clock cannot be conditional on there being something to dispatch."""
    result = mde.evaluate(lane(tmp_path))
    assert result["next"] is None and result["now"].endswith("Z")


def test_reporting_the_clock_leaves_the_tool_read_only(tmp_path, capsys):
    """The whole reason `now` lives here and the append lives on meta-dispatch-move: this
    tool must still touch nothing. Snapshot every path under the lane, not just the entry.
    """
    root = lane(tmp_path)
    brief(root, "queued", "chip-a")
    brief(root, "inflight", "chip-b", pr=3771)
    review(root, 3771, "Verdict: PASS\n")

    def snapshot():
        return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(root.rglob("*"))}

    before = snapshot()
    assert mde.main(["--dir", str(root), "--json"]) == 0
    assert mde.main(["--dir", str(root), "--dry-run"]) == 0
    assert mde.main(["--dir", str(root), "--pm-verdict", "3771"]) == 0
    capsys.readouterr()
    assert snapshot() == before


# ── integrity: a brief that lost its body must never be dispatched ───────────
#
# On 2026-08-25 a truncated `alpha-7-price-from-catalog.md` reached a chip because nothing
# between the PM's write and the launch ever re-read the body. These pin the three shapes
# that failure can take. The empty-body case is covered above
# (`test_an_empty_body_is_invalid_because_the_body_is_the_prompt`); the two below are the
# ones a body-length check alone cannot see.


def _stamped(root, state, ident, **kw):
    """A brief carrying a correct `body_sha256:` — what the mover leaves behind."""
    path = brief(root, state, ident, **kw)
    text = path.read_text(encoding="utf-8")
    path.write_text(mde.mdi.stamp(text)[0], encoding="utf-8")
    return path


def test_a_stamped_brief_is_eligible_and_reports_both_hashes(tmp_path):
    root = lane(tmp_path)
    _stamped(root, "queued", "intact")
    r = mde.evaluate(root)
    assert r["invalid"] == [] and r["next"]["id"] == "intact"
    assert r["next"]["body_sha256"] == r["next"]["body_sha256_recorded"]


def test_a_truncated_body_is_ineligible_not_merely_smaller(tmp_path):
    """The partial loss: a body still present, still parsing, and no longer the brief. No
    length or shape heuristic can see this — only the recorded hash can."""
    root = lane(tmp_path)
    p = _stamped(root, "queued", "half-gone",
                 body="WHY: the tag.\n\nBuild: the first half.\n\nAnd the second half.")
    p.write_text(p.read_text(encoding="utf-8").replace("\n\nAnd the second half.", ""),
                 encoding="utf-8")
    r = mde.evaluate(root)
    assert r["eligible"] == [] and r["next"] is None
    assert "truncated or edited outside the lane's mover" in r["invalid"][0]["error"]


def test_a_body_that_survives_but_a_stamp_that_does_not_is_ineligible(tmp_path):
    """Fail-safe direction: a corrupted stamp holds the entry rather than disabling the
    check, because "the hash is unreadable" is not evidence the body is fine."""
    root = lane(tmp_path)
    p = _stamped(root, "queued", "bad-stamp")
    p.write_text(re.sub(r"body_sha256: sha256:[0-9a-f]+", "body_sha256: sha256:oops",
                        p.read_text(encoding="utf-8")), encoding="utf-8")
    r = mde.evaluate(root)
    assert r["eligible"] == []
    assert "is not 'sha256:<64 hex>'" in r["invalid"][0]["error"]


def test_a_hollow_brief_is_reported_as_the_2026_08_25_shape(tmp_path):
    """Empty body, no stamp — nothing to compare against, and it still must not dispatch."""
    root = lane(tmp_path)
    brief(root, "queued", "hollowed", body="")
    r = mde.evaluate(root)
    assert r["eligible"] == [] and r["next"] is None
    assert "only the front matter survived" in r["invalid"][0]["error"]


def test_a_front_matter_write_after_stamping_leaves_the_entry_eligible(tmp_path):
    """The lane writes `dispatched`/`session`/`pr` at transitions. If those invalidated
    the stamp, every in-flight entry would read as corrupt and the check would be off
    within a week."""
    root = lane(tmp_path)
    p = _stamped(root, "queued", "still-fine")
    text = p.read_text(encoding="utf-8")
    for k, v in (("dispatched", "2026-08-27"), ("session", "task_x"), ("pr", 4001)):
        text = mde.mdi.set_front_matter_field(text, k, v)
    p.write_text(text, encoding="utf-8")
    assert mde.evaluate(root)["next"]["id"] == "still-fine"


# ── lane conflicts: one id, two lane dirs ────────────────────────────────────
#
# Live residual, not hypothetical: #3816 and #3824 each move their own brief
# `queued/ -> done/` in their diff, but #3828 committed those same briefs into `inflight/`
# on main afterwards — so git cannot see the change as a rename and applies it as a plain
# ADD, leaving one id in two dirs with no conflict and no warning.


def test_an_id_in_two_lane_dirs_is_flagged_and_stops_dispatch(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "twin")
    brief(root, "done", "twin")
    brief(root, "queued", "unrelated")
    r = mde.evaluate(root)
    assert [c["id"] for c in r["conflicts"]] == ["twin"]
    assert r["conflicts"][0]["states"] == ["inflight", "done"]
    assert r["blocked_by"] == "lane-conflict"
    assert r["next"] is None, "an ambiguous lane gets no new work"


def test_a_conflicting_copy_too_corrupt_to_parse_is_still_flagged(tmp_path):
    """By filename stem — the copy most worth flagging is the one that no longer parses,
    and the lane's schema pins stem == id anyway."""
    root = lane(tmp_path)
    brief(root, "inflight", "twin")
    (root / "done" / "twin.md").write_text("not front matter at all\n", encoding="utf-8")
    r = mde.evaluate(root)
    assert [c["id"] for c in r["conflicts"]] == ["twin"]
    assert r["blocked_by"] == "lane-conflict"


def test_a_clean_lane_reports_no_conflicts(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "a-chip")
    brief(root, "inflight", "b-chip")
    brief(root, "done", "c-chip")
    r = mde.evaluate(root)
    assert r["conflicts"] == [] and r["blocked_by"] is None


def test_lane_conflict_outranks_the_cap_and_back_pressure(tmp_path):
    """It is the only one of the three that means "the lane is wrong" rather than "the
    lane is busy", and dispatching onto an ambiguous lane is how the ambiguity gets a chip.
    """
    root = lane(tmp_path)
    for i, pr in enumerate((1, 2, 3, 4), start=1):
        started(root, "busy-%d" % i, pr=pr, reviewed=None)
    prepared(root, "unclicked-a")
    prepared(root, "unclicked-b")
    brief(root, "done", "busy-1")
    r = mde.evaluate(root)
    # every other blocker is armed too — cap, prepared cap AND back-pressure
    assert r["back_pressure"]["paused"] is True
    assert r["slots"] == 0 and r["prepared_slots"] == 0
    assert r["blocked_by"] == "lane-conflict"


# ── orphan done/ entries: ONE CHIP under TWO IDS ────────────────────────
#
# The inverse of the conflicts[] defect above, and structurally invisible to it: #3849
# closed out `dossier-module-synthesis` by adding `done/dossier-modules.md`, and two
# DIFFERENT ids never collide. conflicts[] reported empty for the whole window while a
# `depends_on` on `chip:dossier-module-synthesis` could never clear and the unbindable
# marker held one of three slots. `bind` can see it at PR-open (exit 3); this is the same
# finding on the surface an operator already reads, every tick, for a lane already
# carrying one.


# The one brief body both entries carry — the #3849 fixture is "the brief verbatim,
# under a different name".
BODY = "The dossier modules learn to speak.\n\nOpen the PR with `gh pr create`."


def test_a_done_entry_holding_an_inflight_brief_under_another_id_is_reported(tmp_path):
    """The #3849 shape. The predicate is the BODY, because a chip closing out under a
    coined id writes the brief verbatim — that is what made it a broken key rather than
    data loss."""
    root = lane(tmp_path)
    brief(root, "inflight", "dossier-module-synthesis", body=BODY)
    brief(root, "done", "dossier-modules", body=BODY)

    r = mde.evaluate(root)

    (o,) = r["orphan_done"]
    assert o["id"] == "dossier-modules"
    assert o["inflight_id"] == "dossier-module-synthesis"
    assert o["evidence"] == "body-sha256"
    assert o["path"].endswith("done/dossier-modules.md")
    assert o["inflight_path"].endswith("inflight/dossier-module-synthesis.md")


def test_the_orphan_report_does_not_block_dispatch(tmp_path):
    """Halting all dispatch outranks the cap and back-pressure and is reserved for a lane
    whose state is AMBIGUOUS. Here it is unambiguous and wrong under one key, and the
    refuted "flag briefs untracked at dispatch" design shows what over-blocking costs: a
    guard that halts on a healthy chip stops the whole lane."""
    root = lane(tmp_path)
    brief(root, "inflight", "dossier-module-synthesis", body=BODY)
    brief(root, "done", "dossier-modules", body=BODY)
    brief(root, "queued", "next-chip")

    r = mde.evaluate(root)

    assert r["orphan_done"] and r["blocked_by"] is None
    assert r["next"]["id"] == "next-chip"


def test_an_appended_outcome_note_does_not_hide_the_orphan(tmp_path):
    """Containment, not equality — a chip or the operator legitimately APPENDS an outcome
    note to a `done/` entry, and two entries in the lane already carry one. Appending must
    not be a way to launder the broken key."""
    root = lane(tmp_path)
    brief(root, "inflight", "dossier-module-synthesis", body=BODY)
    brief(root, "done", "dossier-modules",
          body=BODY + "\n\nOUTCOME: merged as #3849.")

    (o,) = mde.evaluate(root)["orphan_done"]

    assert o["evidence"] == "body-contains"


def test_a_healthy_lane_reports_no_orphans(tmp_path):
    """The false-positive direction, and the one that matters: this runs every 30-minute
    tick against the whole lane, so a predicate that fires on unrelated briefs is a report
    nobody reads."""
    root = lane(tmp_path)
    brief(root, "inflight", "a-chip", body="Do A.")
    brief(root, "done", "b-chip", body="Do B.")
    brief(root, "done", "a-chip-successor", body="Do A again, differently.")

    assert mde.evaluate(root)["orphan_done"] == []


def test_the_same_id_in_two_dirs_stays_a_conflict_not_an_orphan(tmp_path):
    """The two findings must not double-report: one id in two dirs is `conflicts[]` (and
    blocks); one chip under two ids is `orphan_done[]` (and does not)."""
    root = lane(tmp_path)
    brief(root, "inflight", "twin", body=BODY)
    brief(root, "done", "twin", body=BODY)

    r = mde.evaluate(root)

    assert [c["id"] for c in r["conflicts"]] == ["twin"]
    assert r["orphan_done"] == []


def test_the_orphan_names_both_ids_in_the_text_render(tmp_path):
    """The operator reads the text form. A report naming only the coined id is not
    actionable — the id that can never clear is the other one."""
    root = lane(tmp_path)
    brief(root, "inflight", "dossier-module-synthesis", body=BODY)
    brief(root, "done", "dossier-modules", body=BODY)

    out = mde.render_text(mde.evaluate(root))

    assert "ORPHAN" in out
    assert "dossier-modules" in out and "dossier-module-synthesis" in out
    assert "never auto-repair" in out


def test_orphan_done_is_in_the_json_payload(tmp_path, capsys):
    """Step 2 of the procedure reads the JSON, so the key must be there on every run —
    including an empty lane, where an ABSENT key and an empty list read the same to a
    model and differently to a `.get()`."""
    root = lane(tmp_path)
    brief(root, "queued", "a-chip")

    assert mde.main(["--dir", str(root), "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["orphan_done"] == []


# ── D-PM2 must answer the same from any checkout ─────────────────────────────
#
# `lane: false` is what licenses the reconciler to AUTO-REVIEW a PR — dispatching a review
# chip at a PR whose own PM is its reviewer, which then writes a `two_pass` verdict.
# Answering it from working-tree-only state made it a per-checkout answer: run from a clean
# clone, #3824 read `lane: false` (meta-reconcile sweep 169 pre-registered exactly this).


def _ledger(tmp_path, aspect, chips):
    d = tmp_path / "meta-state"
    d.mkdir(exist_ok=True)
    (d / ("%s.json" % aspect)).write_text(json.dumps({"chips": chips}), encoding="utf-8")
    return d


def test_pm_verdict_finds_a_pr_recorded_only_in_queued(tmp_path):
    """Under the lane-of-record model a brief carrying a `pr:` can still be sitting in
    `queued/` on main — the move to `inflight/` is a working-tree edit only one checkout
    has. Skipping `queued/` was a silent `lane: false` for every such PR."""
    root = lane(tmp_path)
    brief(root, "queued", "still-queued", pr=3900)
    v = mde.pm_verdict(root, 3900, ledger_dir=str(tmp_path / "absent"))
    assert v["lane"] is True and v["lane_source"] == "entry"
    assert v["chip"] == "still-queued"


def test_pm_verdict_falls_back_to_the_ledger_when_no_checkout_records_the_pr(tmp_path):
    """The clean-clone case, and also BRIEF-NEVER-STAMPED (sweeps 165 and 167): the ledger
    row carries `pr` even when the brief never got one."""
    root = lane(tmp_path)
    led = _ledger(tmp_path, "apps", [{"id": "corpus-mining", "pr": 3824,
                                      "privileged": False,
                                      "note": "PM lane: internal/dispatch/inflight/corpus-mining.md"}])
    v = mde.pm_verdict(root, 3824, ledger_dir=str(led))
    assert v["lane"] is True and v["lane_source"] == "ledger"
    assert v["chip"] == "corpus-mining" and v["aspect"] == "apps"
    assert v["privileged"] is False


def test_the_ledger_fallback_ignores_rows_that_are_not_pm_lane(tmp_path):
    """Strictly protective, and strictly narrow: only a chip row whose note names the lane
    counts. A model-tiers chip with a PR is not a PM-lane PR."""
    root = lane(tmp_path)
    led = _ledger(tmp_path, "model-tiers", [{"id": "some-chip", "pr": 3822,
                                             "privileged": False,
                                             "note": "ordinary aspect chip"}])
    v = mde.pm_verdict(root, 3822, ledger_dir=str(led))
    assert v["lane"] is False and v["lane_source"] is None
    assert "no meta-state chip row does either" in v["reason"]


def test_a_ledger_row_without_a_real_privileged_boolean_holds_for_the_operator(tmp_path):
    """Fail-closed: D-PM2 covers non-privileged PRs only, so an unknown holds rather than
    auto-merging on a guess."""
    root = lane(tmp_path)
    led = _ledger(tmp_path, "deploy", [{"id": "risky", "pr": 3901,
                                        "note": "PM lane: internal/dispatch/inflight/risky.md"}])
    review(root, 3901, "Verdict: PASS — clean.\n")
    v = mde.pm_verdict(root, 3901, ledger_dir=str(led))
    assert v["lane"] is True and v["privileged"] is True
    assert v["qualifies"] is False and "privileged" in v["reason"]


def test_the_ledger_fallback_still_requires_a_pass_review(tmp_path):
    """It supplies the `lane` fact only. The permissive half — the verdict — is unchanged,
    and its absence still fails closed."""
    root = lane(tmp_path)
    led = _ledger(tmp_path, "apps", [{"id": "c", "pr": 3902, "privileged": False,
                                      "note": "PM lane: internal/dispatch/inflight/c.md"}])
    assert mde.pm_verdict(root, 3902, ledger_dir=str(led))["qualifies"] is False
    review(root, 3902, "## Two-pass review\n\nVerdict: PASS — brief followed.\n")
    v = mde.pm_verdict(root, 3902, ledger_dir=str(led))
    assert v["qualifies"] is True and v["lane_source"] == "ledger"


def test_a_lane_entry_beats_the_ledger_fallback(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "real-entry", pr=3903, privileged=False)
    led = _ledger(tmp_path, "apps", [{"id": "stale-row", "pr": 3903, "privileged": True,
                                      "note": "PM lane: internal/dispatch/inflight/x.md"}])
    v = mde.pm_verdict(root, 3903, ledger_dir=str(led))
    assert v["lane_source"] == "entry" and v["chip"] == "real-entry"


def test_a_corrupt_ledger_is_no_evidence_rather_than_a_crash(tmp_path):
    root = lane(tmp_path)
    d = tmp_path / "meta-state"
    d.mkdir()
    (d / "apps.json").write_text("{ this is not json", encoding="utf-8")
    v = mde.pm_verdict(root, 3904, ledger_dir=str(d))
    assert v["lane"] is False


# ── the #3813 false hold, on the real review file (2026-08-31) ───────────────


def test_the_real_pr3813_review_file_reports_the_pms_pass(tmp_path):
    """The incident, end to end: on 2026-08-27 `--pm-verdict 3813` answered
    "PM verdict is blocking: fail-toward-doing gate and the
    spy-not-raising-sentinel rule." — line 84 of the review file, the soft-wrapped
    CONTINUATION of a sentence PRAISING the work. Line 3 says
    `**Verdict: PASS** (2 non-blocking findings)` and the Disposition section says
    `Merge.`

    Pinned against the real hand-wrapped file rather than a synthesised string,
    because the wrap is the defect and a synthesised string cannot reproduce
    how a ~90-column reviewer actually wraps.
    """
    src = Path(__file__).resolve().parents[3] / "internal/dispatch/reviews/pr-3813.md"
    if not src.is_file():                   # synced consumer checkout: nothing to pin
        pytest.skip("%s not present in this checkout" % src)
    root = lane(tmp_path)
    brief(root, "done", "alpha-8-stranger-install-docs", aspect="deploy", pr=3813)
    review(root, 3813, src.read_text(encoding="utf-8"))
    v = mde.pm_verdict(root, 3813, ledger_dir=str(tmp_path / "absent"))
    assert v["lane"] is True
    assert v["verdict_is_blocking"] is False, v["verdict"]
    assert v["verdict_is_pass"] is True, v["verdict"]
    assert v["qualifies"] is True, v["reason"]


# ── --merged-prs: the inline injection that replaces the stalling scratch file ──
#
# `facts.json` existed only to bridge "the procedure forbids pipes": the run wrote the
# merged-PR numbers to ~/.claude/meta-dispatch/facts.json with the `Write` tool and
# pointed --facts at it. That write is OUT-OF-CWD, and an out-of-cwd Write in a
# default-mode scheduled run raises the workspace-boundary approval prompt an unattended
# run cannot answer (measured 2026-09-01: 8 of 27 blocked, median 4.1h). These pin the
# inline form as an exact substitute for the file, so the file can stop being written.

def test_merged_prs_inline_resolves_a_dependency_like_the_facts_file(tmp_path, capsys):
    root = lane(tmp_path)
    brief(root, "queued", "after-pr", depends_on=["#3763"])
    assert mde.main(["--dir", str(root), "--merged-prs", "3763", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["next"]["id"] == "after-pr"


def test_merged_prs_accepts_commas_and_whitespace(tmp_path, capsys):
    root = lane(tmp_path)
    brief(root, "queued", "after-pr", depends_on=["#3765"])
    assert mde.main(["--dir", str(root), "--merged-prs", "3763, 3764  3765", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["next"]["id"] == "after-pr"


def test_merged_prs_unions_with_the_facts_file_rather_than_replacing_it(tmp_path, capsys):
    """Both given means "these too" — a caller must not lose the file's `duplicates{}`
    or its own merged_prs by adding one number on the command line."""
    root = lane(tmp_path)
    brief(root, "queued", "after-two", depends_on=["#3763", "#3999"])
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps({"merged_prs": [3763], "duplicates": {}}))
    assert mde.main(["--dir", str(root), "--facts", str(facts),
                     "--merged-prs", "3999", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["next"]["id"] == "after-two"


def test_merged_prs_refuses_a_non_number_instead_of_dropping_it(tmp_path):
    """A silently-skipped token reads as "that PR is not merged" — the direction that
    holds a brief forever. A typo must be an error, never a quiet unmet dependency."""
    root = lane(tmp_path)
    with pytest.raises(SystemExit) as e:
        mde.main(["--dir", str(root), "--merged-prs", "3763,#3764"])
    assert "not one" in str(e.value)


def test_merged_prs_absent_leaves_dependencies_unmet(tmp_path, capsys):
    """The fail-safe direction: no injection is not an assumption that it merged."""
    root = lane(tmp_path)
    brief(root, "queued", "after-pr", depends_on=["#3763"])
    assert mde.main(["--dir", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["next"] is None


# ── repairable[]: the two duplicate shapes the lane produces ITSELF (D-PM8) ───
#
# `conflicts[]` halting the lane is right for a duplicate whose two copies are two answers
# to "where is this work". But the dispatcher's launch is a working-tree DELETION of
# `queued/<id>.md` plus an untracked `inflight/` marker, so the queued copy is still on
# `main` until the chip's PR renames it — and every pull or branch switch materializes it
# again. That stopped the lane four times between 2026-09-01 and 09-03, each costing an
# operator paste, and a guard that fires on its own design is a guard that gets switched
# off. Both self-healing shapes are REPORTED with the command that clears them; every
# other two-dir shape is unchanged.


def _stamp(path):
    """Write the `body_sha256` the mover's `launch` would have stamped in."""
    text, _ = mde.mdi.stamp(path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")
    return path


def test_a_queued_copy_restored_over_an_inflight_marker_is_repairable(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "restored-chip")                       # main's copy, unstamped
    _stamp(brief(root, "inflight", "restored-chip"))             # launch stamped this

    r = mde.evaluate(root)

    assert r["conflicts"] == [] and r["blocked_by"] is None
    (rp,) = r["repairable"]
    assert rp["id"] == "restored-chip"
    assert rp["shape"] == mde.mdi.REPAIR_RESTORED
    assert rp["delete"].endswith("queued/restored-chip.md")
    assert rp["keeper"].endswith("inflight/restored-chip.md")
    assert rp["log"] == ("re-deleted queued copy of restored-chip "
                         "(restored by checkout/pull)")


def test_a_repairable_duplicate_does_not_stop_dispatch(tmp_path):
    """The whole point: the lane keeps working while the stale copy is cleared."""
    root = lane(tmp_path)
    brief(root, "queued", "restored-chip")
    _stamp(brief(root, "inflight", "restored-chip"))
    brief(root, "queued", "next-chip")

    r = mde.evaluate(root)

    assert r["blocked_by"] is None
    assert r["next"]["id"] == "next-chip"


def test_the_restored_queued_copy_is_still_held_never_dispatched(tmp_path):
    """Repairable is not eligible. The stale copy stays `duplicate-id` in `held[]`, so
    even a tick that never runs the repair cannot dispatch the work a second time."""
    root = lane(tmp_path)
    brief(root, "queued", "restored-chip")
    _stamp(brief(root, "inflight", "restored-chip"))

    r = mde.evaluate(root)

    assert r["eligible"] == [] and r["next"] is None
    assert [(h["id"], h["reason"]) for h in r["held"]] == [("restored-chip",
                                                            "duplicate-id")]


def test_a_queued_copy_with_a_different_body_stays_a_conflict(tmp_path):
    """The amend flow depends on this mismatch staying visible: a brief edited after
    dispatch has genuinely diverged from the chip running against the older text."""
    root = lane(tmp_path)
    brief(root, "queued", "amended-chip", body="The AMENDED brief.")
    _stamp(brief(root, "inflight", "amended-chip", body="The brief as dispatched."))

    r = mde.evaluate(root)

    assert r["repairable"] == []
    assert [c["id"] for c in r["conflicts"]] == ["amended-chip"]
    assert r["blocked_by"] == "lane-conflict"


def test_a_queued_copy_superseded_by_a_merged_done_entry_is_repairable(tmp_path):
    """The #3964 shape: identical bodies, because `complete` moves the file verbatim."""
    root = lane(tmp_path)
    brief(root, "queued", "merged-chip", body=BODY)
    brief(root, "done", "merged-chip", body=BODY, pr=3964)

    r = mde.evaluate(root)

    (rp,) = r["repairable"]
    assert rp["shape"] == mde.mdi.REPAIR_SUPERSEDED
    assert rp["evidence"] == "body-sha256" and rp["pr"] == 3964
    assert r["conflicts"] == [] and r["blocked_by"] is None


def test_a_done_entry_that_only_CONTAINS_the_queued_body_pokes_instead(tmp_path):
    """F1 on #3982, with the reviewer's own fixture.

    An APPENDED outcome note (what containment was written for) and a NEW brief queued
    under a completed id are the same bytes, so containment repaired both — deleting the
    new brief unattended every 30 minutes and reporting one log line. Prefix-matching does
    not separate them either: this fixture is a prefix. So the shape is reported, blocks
    the lane exactly as it did before #3982, and the poke names the two readings.
    """
    root = lane(tmp_path)
    brief(root, "queued", "shrunk", body="WHY: do part one.")
    brief(root, "done", "shrunk", body="WHY: do part one.\nAND part two.", pr=99)

    r = mde.evaluate(root)

    assert r["repairable"] == []
    (c,) = r["conflicts"]
    assert c["id"] == "shrunk" and c["shape"] == mde.mdi.POKE_DONE_DIVERGED
    assert c["note"] == ("queued copy of shrunk needs an operator (done/ (PR #99) "
                         "contains this body but is NOT equal to it — either the done/ "
                         "entry was appended to and this queued copy is stale, or a NEW "
                         "brief was queued under a completed id; a human deletes the "
                         "stale copy or renames the new brief)")
    assert r["blocked_by"] == "lane-conflict"


def test_the_poke_reaches_the_operator_in_the_text_render(tmp_path):
    """A finding nobody reads is the defect F1 describes — the misfire's only trace was
    one log line in an unattended run. The note rides the CONFLICT block the dispatcher
    already pastes."""
    root = lane(tmp_path)
    brief(root, "queued", "shrunk", body="WHY: do part one.")
    brief(root, "done", "shrunk", body="WHY: do part one.\nAND part two.", pr=99)

    out = mde.render_text(mde.evaluate(root))

    assert "CONFLICT" in out and "needs an operator" in out
    assert "renames the new brief" in out


def test_a_done_entry_without_a_pr_stays_a_conflict(tmp_path):
    """`pr` is what says a PR actually carried the brief to `done/`; without one the
    queued copy is not provably the stale half."""
    root = lane(tmp_path)
    brief(root, "queued", "unbound-chip", body=BODY)
    brief(root, "done", "unbound-chip", body=BODY)

    r = mde.evaluate(root)

    assert r["repairable"] == []
    assert [c["id"] for c in r["conflicts"]] == ["unbound-chip"]
    assert r["blocked_by"] == "lane-conflict"


def test_inflight_plus_done_is_never_repairable(tmp_path):
    """The #3828 residual. Neither copy is a `queued/` copy, so there is nothing this
    repair could delete — it stays the operator's call, exactly as before."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "twin"))
    _stamp(brief(root, "done", "twin", pr=3828))

    r = mde.evaluate(root)

    assert r["repairable"] == []
    assert [c["id"] for c in r["conflicts"]] == ["twin"]
    assert r["blocked_by"] == "lane-conflict"


def test_an_id_in_all_three_dirs_stays_a_conflict(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "triple-chip")
    _stamp(brief(root, "inflight", "triple-chip"))
    brief(root, "done", "triple-chip", pr=1)

    r = mde.evaluate(root)

    assert r["repairable"] == [] and r["blocked_by"] == "lane-conflict"


def test_a_copy_that_does_not_parse_is_never_repairable(tmp_path):
    """The body comparison IS the safety argument, so a copy whose body cannot be read is
    never one whose body can be shown to survive elsewhere."""
    root = lane(tmp_path)
    (root / "queued" / "broken-chip.md").write_text("not front matter at all\n",
                                                    encoding="utf-8")
    _stamp(brief(root, "inflight", "broken-chip"))

    r = mde.evaluate(root)

    assert r["repairable"] == [] and r["blocked_by"] == "lane-conflict"


def test_a_repairable_shape_and_a_real_conflict_coexist(tmp_path):
    """One healable duplicate must not launder an ambiguous one standing beside it."""
    root = lane(tmp_path)
    brief(root, "queued", "restored-chip")
    _stamp(brief(root, "inflight", "restored-chip"))
    _stamp(brief(root, "inflight", "twin"))
    _stamp(brief(root, "done", "twin", pr=1))

    r = mde.evaluate(root)

    assert [rp["id"] for rp in r["repairable"]] == ["restored-chip"]
    assert [c["id"] for c in r["conflicts"]] == ["twin"]
    assert r["blocked_by"] == "lane-conflict"


def test_the_text_render_names_the_repair_and_says_it_does_not_block(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "restored-chip")
    _stamp(brief(root, "inflight", "restored-chip"))

    out = mde.render_text(mde.evaluate(root))

    assert "REPAIR" in out and "restored-chip" in out
    assert "does NOT stop dispatch" in out


def test_dry_run_reports_the_repair_plan_and_writes_nothing(tmp_path):
    """A reader must not write. `--dry-run` names the command, the file it would delete
    and the line it would log — and the lane is byte-for-byte unchanged afterwards."""
    root = lane(tmp_path)
    queued = brief(root, "queued", "restored-chip")
    marker = _stamp(brief(root, "inflight", "restored-chip"))
    before = {p: p.read_bytes() for p in (queued, marker)}

    out = mde.render_text(mde.evaluate(root), dry_run=True)

    assert "repair plan" in out
    assert "python3 tools/meta-dispatch-move repair-queued-copy restored-chip" in out
    assert "re-deleted queued copy of restored-chip (restored by checkout/pull)" in out
    assert {p: p.read_bytes() for p in (queued, marker)} == before
    assert sorted(p.name for p in (root / "queued").iterdir()) == ["restored-chip.md"]


def test_the_cli_dry_run_writes_nothing(tmp_path):
    """Through `main()` as the dispatcher actually calls it, not just the renderer."""
    root = lane(tmp_path)
    queued = brief(root, "queued", "restored-chip")
    _stamp(brief(root, "inflight", "restored-chip"))

    assert mde.main(["--dir", str(root), "--dry-run"]) == 0

    assert queued.is_file()


def test_the_json_payload_carries_repairable(tmp_path, capsys):
    """The dispatcher reads JSON, not prose."""
    root = lane(tmp_path)
    brief(root, "queued", "restored-chip")
    _stamp(brief(root, "inflight", "restored-chip"))

    assert mde.main(["--dir", str(root), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked_by"] is None
    assert payload["repairable"][0]["command"].endswith("repair-queued-copy restored-chip")


def test_a_clean_lane_reports_nothing_repairable(tmp_path):
    root = lane(tmp_path)
    brief(root, "queued", "a-chip")
    brief(root, "inflight", "b-chip")
    brief(root, "done", "c-chip")

    assert mde.evaluate(root)["repairable"] == []


# ── D-PM7: the hold decision table ───────────────────────────────────────────
#
# `--pm-verdict` answers two questions on one read: may this PR MERGE (D-PM2),
# and may the reconciler send ONE fix-forward chip at its hold (D-PM7). The rows
# below are the whole table, and every one of them except the last fails toward
# the operator rather than toward a chip. The two conjuncts the tool deliberately
# does not answer — `reversible` and the fix budget, both ledger facts — stay in
# internal/meta-reconcile-procedure.md and are pinned by the last test here.

_HOLD = "## Hold\nH1. `census.py:103-113` — count the stubs; restate the number.\n"
_CONCERNS = "Verdict: CONCERNS — one number is overstated; correct it and this is a PASS.\n"


def _held_review(concerns=_CONCERNS, hold=_HOLD, pr=3771):
    return "---\npr: %d\nreviewer: fable-cowork\n---\n\n## Two-pass review\n%s\n%s" % (
        pr, concerns, hold)


def test_concerns_with_a_hold_on_a_non_privileged_pr_dispatches_one_chip(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, _held_review())
    v = mde.pm_verdict(root, 3771)
    assert v["verdict_is_concerns"] is True and v["hold_present"] is True
    assert v["hold_fix_forward"] is True
    assert "census.py:103-113" in v["hold"]
    assert v["qualifies"] is False, "a held PR still never merges"


def test_concerns_without_a_hold_is_malformed_and_dispatches_nothing(tmp_path):
    """The verdict blocks but states no remedy, so there is no prompt to give a
    chip. Red zone, poked at the PM."""
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, _held_review(hold="## Findings\n1. **BLOCKS** — `demo.py:147`.\n"))
    v = mde.pm_verdict(root, 3771)
    assert v["verdict_is_concerns"] is True and v["hold_present"] is False
    assert v["hold_fix_forward"] is False
    assert "MALFORMED" in v["hold_reason"]


@pytest.mark.parametrize("verdict", [
    "Verdict: FAIL — the migration is unsafe.\n",
    "Verdict: DO NOT MERGE — this needs a different design.\n",
    "Verdict: REQUEST CHANGES — start over on the auth path.\n",
])
def test_a_rejection_never_dispatches_a_chip_even_with_a_hold(tmp_path, verdict):
    """A rejection is a human's judgment about the shape of the change; a chip sent
    at one would be arguing with the reviewer."""
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, _held_review(concerns=verdict))
    v = mde.pm_verdict(root, 3771)
    assert v["verdict_is_blocking"] is True and v["verdict_is_concerns"] is False
    assert v["hold_present"] is True, "the hold is still parsed and reported"
    assert v["hold_fix_forward"] is False
    assert "human hold" in v["hold_reason"]


def test_a_privileged_lane_pr_never_dispatches_a_chip(tmp_path):
    """D-PM1 × D-PM7 meet exactly where D-PM1 and D-PM2 do."""
    root = lane(tmp_path)
    brief(root, "inflight", "chip-p", pr=3771, privileged=True, greenlight=True)
    review(root, 3771, _held_review())
    v = mde.pm_verdict(root, 3771)
    assert v["hold_present"] is True and v["hold_fix_forward"] is False
    assert "privileged" in v["hold_reason"]


def test_a_privileged_row_from_the_LEDGER_fallback_also_blocks_the_chip(tmp_path):
    """The fallback treats an unstated `privileged` as privileged; that fail-closed
    default must gate the chip too, or a lane PR the checkout cannot see would be
    fixed forward on a guess."""
    root = lane(tmp_path)
    led = tmp_path / "meta-state"
    led.mkdir()
    (led / "apps.json").write_text(json.dumps({"chips": [
        {"id": "chip-x", "pr": 3771, "note": "PM lane: internal/dispatch/inflight/chip-x.md"},
    ]}), encoding="utf-8")
    review(root, 3771, _held_review())
    v = mde.pm_verdict(root, 3771, ledger_dir=str(led))
    assert v["lane"] is True and v["privileged"] is True
    assert v["hold_fix_forward"] is False and "privileged" in v["hold_reason"]


def test_a_pass_verdict_dispatches_nothing(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, "Verdict: PASS — brief followed.\n")
    v = mde.pm_verdict(root, 3771)
    assert v["qualifies"] is True
    assert v["hold_fix_forward"] is False and "not blocking" in v["hold_reason"]


def test_a_pr_outside_the_lane_dispatches_nothing(tmp_path):
    """D-PM7 is a PM-lane rule; a hold in a file about a non-lane PR is not one."""
    root = lane(tmp_path)
    review(root, 3771, _held_review())
    v = mde.pm_verdict(root, 3771, ledger_dir=str(tmp_path / "absent"))
    assert v["lane"] is False and v["hold_fix_forward"] is False


def test_a_review_naming_a_different_pr_yields_no_hold(tmp_path):
    """The hold and the verdict come from a file that passed the SAME front-matter
    guard — otherwise a chip could be dispatched at a remedy written for another PR."""
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, _held_review(pr=3999))
    v = mde.pm_verdict(root, 3771)
    assert v["hold"] is None and v["hold_fix_forward"] is False
    assert "not 3771" in v["hold_reason"]


def test_no_review_file_yields_no_hold(tmp_path):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    v = mde.pm_verdict(root, 3771)
    assert v["hold_present"] is False and v["hold_fix_forward"] is False


def test_a_held_pr_is_still_named_in_motion_while_its_hold_is_fixed(tmp_path):
    """D-PM6 and D-PM7 agree: the fix chip does not free the slot — the PM's
    re-review does. A hold under repair is still stopped work occupying a slot."""
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771, launch="started")
    review(root, 3771, _held_review())
    r = mde.evaluate(root)
    assert r["held_prs"] == [3771] and r["in_motion_count"] == 1


def test_cli_pm_verdict_reports_the_hold_decision(tmp_path, capsys):
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=3771)
    review(root, 3771, _held_review())
    assert mde.main(["--dir", str(root), "--pm-verdict", "3771"]) == 1
    out = capsys.readouterr().out
    assert "hold      : present" in out and "fix-fwd   : True" in out
    assert mde.main(["--dir", str(root), "--pm-verdict", "3771", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["hold_fix_forward"] is True and payload["hold_present"] is True


def test_the_procedure_states_the_conjuncts_the_tool_cannot_see():
    """`reversible` and the fix budget are ledger facts this tool never sees, so
    they must be stated where the reconciler reads. A rule split across two
    documents is a rule that loses half of itself; this pins the half that lives
    in prose."""
    doc = (Path(__file__).resolve().parents[3]
           / "internal" / "meta-reconcile-procedure.md").read_text(encoding="utf-8")
    assert "hold_fix_forward" in doc
    assert "fix_count<1" in doc
    assert "reversible==true" in doc


def test_the_lane_readme_states_the_hold_contract_once():
    readme = (Path(__file__).resolve().parents[3]
              / "internal" / "dispatch" / "README.md").read_text(encoding="utf-8")
    assert "## Hold` contract" in readme
    assert "tools/meta-verdict-check --hold" in readme


@pytest.mark.parametrize("pr,expect_chip", [
    (3976, True),    # CONCERNS + `## Hold (anti-vacuity — …)` — the live case
    (3964, True),    # CONCERNS + `## Hold` — H1, the lane conflict
    # pr-3959 is the row the corpus contributes that the rule must NOT act on, and
    # it lands there twice over: it has no `## Hold` (its blocking item sits under
    # `## Findings` marked "BLOCKS"), AND its verdict — "…; everything else holds
    # and the rest is non-blocking." — reads as not-blocking at all, because the
    # canonical predicate treats a trailing "non-blocking" as de-fanging the whole
    # verdict. Either row alone withholds the chip. The predicate is out of scope
    # here (D-PM7 changes no verdict reading); what is in scope is that a review
    # this ambiguous never gets a chip sent at it.
    (3959, False),
])
def test_the_real_review_corpus_dispatches_where_it_should(tmp_path, pr, expect_chip):
    """Synthesised strings prove the rule; the actual review files this was built
    from prove it on hand-wrapped prose written before the rule existed."""
    src = (Path(__file__).resolve().parents[3]
           / "internal" / "dispatch" / "reviews" / ("pr-%d.md" % pr))
    if not src.is_file():                   # synced consumer checkout: nothing to pin
        pytest.skip("%s not present in this checkout" % src)
    root = lane(tmp_path)
    brief(root, "inflight", "chip-a", pr=pr)
    review(root, pr, src.read_text(encoding="utf-8"))
    v = mde.pm_verdict(root, pr)
    assert v["hold_fix_forward"] is expect_chip, v["hold_reason"]
