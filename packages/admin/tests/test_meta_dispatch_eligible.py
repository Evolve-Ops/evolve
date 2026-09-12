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

import datetime
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

sys.path.insert(0, str(_TOOL.parent))
import meta_dispatch_integrity as mdi  # noqa: E402  (path insert must precede the import)


# ── fixtures ─────────────────────────────────────────────────────────────────


UNAVAILABLE = mde.mdh.Prereqs(ok=False, missing=(mde.mdh.MISSING_GRANT,),
                              detail="pinned by tests")
AVAILABLE = mde.mdh.Prereqs(ok=True, missing=(), detail="")


@pytest.fixture(autouse=True)
def _headless_unavailable_by_default(monkeypatch):
    """Pin the D-PM6' prerequisite answer for EVERY test in this file.

    Without it the suite reads the developer's own `~/.claude/settings.json` and
    `~/.claude.json`, and the lane's routing genuinely differs on that answer — with
    headless available a non-privileged brief is bounded by BUILD slots, without it by
    PREPARED slots. A test that passes on a machine where the operator has not yet added
    the grant and fails on one where they have is not pinning the rule, it is reporting
    the machine. Default UNAVAILABLE so every pre-D-PM6' test keeps asserting the
    prepare-and-tap behaviour it was written for; the headless tests below opt in.
    """
    monkeypatch.setattr(mde.mdh, "check_prereqs", lambda **kw: UNAVAILABLE)


def headless_available(monkeypatch):
    monkeypatch.setattr(mde.mdh, "check_prereqs", lambda **kw: AVAILABLE)


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
#                   optionally a `pr`: charged to `--cap` (default 6)
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


def test_the_cap_is_six_and_it_counts_work_in_motion(tmp_path):
    """D-PM3″: six STARTED chips fill the lane. The queued brief stays eligible — the
    cap is the lane's state, not the brief's."""
    root = lane(tmp_path)
    for i in range(6):
        started(root, "busy-%d" % i, pr=100 + i)
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["cap"] == 6 and r["in_motion_count"] == 6 and r["slots"] == 0
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
    assert r["slots"] == 3 and r["prepared_count"] == 1 and r["prepared_slots"] == 1
    assert r["blocked_by"] is None
    assert r["next"]["id"] == "a-first"
    assert [e["id"] for e in r["eligible"]] == ["a-first", "b-second"]


def test_two_prepared_cards_block_dispatch_even_with_free_build_slots(tmp_path):
    """The prepared cap is a SEPARATE budget, and it binds first here: 2/6 in motion says
    the lane could build more, but the dispatcher only ever prepares cards, and two are
    already waiting on the operator's click."""
    root = lane(tmp_path)
    for i in range(2):
        started(root, "busy-%d" % i, pr=100 + i)
    prepared(root, "unclicked-a")
    prepared(root, "unclicked-b")
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["in_motion_count"] == 2 and r["slots"] == 4        # in motion 2/6: room
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
    for i in range(6):
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
    assert r["slots"] == 3                    # the cap alone would have allowed a launch
    assert r["blocked_by"] == "back-pressure"


# ── D-PM6: a held PR is in motion, and it is NAMED ───────────────────────────


def test_a_held_pr_still_occupies_its_slot_and_is_named(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=3976, reviewed="Verdict: CONCERNS — one blocker.\n")
    started(root, "chip-b", pr=3977)
    r = mde.evaluate(root)
    assert r["in_motion_count"] == 2 and r["slots"] == 4   # NOT subtracted
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
    assert lines[1] == ("  in flight: 2/6 · prepared: 1/2 · slots: 4 in-motion, "
                        "1 prepared · 1 held: #3976 · headless: unavailable "
                        "· base: unknown")


def test_the_summary_line_omits_the_held_clause_when_nothing_is_held(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=3976)
    lines = mde.render_text(mde.evaluate(root)).splitlines()
    assert lines[1] == ("  in flight: 1/6 · prepared: 0/2 · slots: 5 in-motion, "
                        "2 prepared · headless: unavailable · base: unknown")


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


# ── D-PM6‴: ONE PR IS ONE BUILD SLOT ─────────────────────────────────────────
#
# The build cap charges DISTINCT PRs among the in-motion entries, not entries. A
# fix-forward chip (D-PM7) binds to the PR it repairs, so two lane entries on one `pr:`
# is the normal shape of that work — one line of work, one slot. Nothing else about the
# cap moves: its value is still 6, an entry with no `pr:` still costs its own slot, and an
# entry whose `pr:` cannot be read is charged its own rather than folded into anyone's.


_BLOCKING_REVIEW = "Verdict: CONCERNS — one blocker.\n"


def held_pr(root, ident, *, pr):
    """An in-motion entry whose PM review is BLOCKING — the 2026-09-08 lane was seven of
    these and nothing else."""
    started(root, ident, pr=pr, reviewed=_BLOCKING_REVIEW)


def test_the_lane_that_could_not_clear_itself_on_2026_09_08(tmp_path):
    """THE REGRESSION THIS RULE EXISTS FOR — `internal/finding-the-lane-cannot-clear-
    itself-2026-09-08.md`.

    On the evening of 2026-09-08 the reader showed `blocked_by: "cap"`, `in_motion 7 /
    cap 6`, `slots: 0`, `next: null` and 31 eligible briefs, and every one of the seven
    in-motion entries was a HELD PR — six DISTINCT numbers, because #4079 was charged
    twice (the fix-forward chip repairing it is bound to the PR it repairs; the companion
    finding is `internal/finding-a-completed-chip-reads-as-an-unclicked-card-2026-09-08.md`).

    D-PM6‴ un-charges the duplicate: seven entries now cost SIX slots and `shared_prs`
    names #4079 as the PR carrying two of them.

    HONEST ARITHMETIC, pinned here because this chip's own brief asserted otherwise (it
    asked for `slots == 1` on this shape): six distinct held PRs against a build cap of
    six is still exactly full, so this shape reports `slots: 0` and `blocked_by: "cap"`
    after the fix too. What the rule buys back is the seventh CHARGE, not a seventh slot —
    the lane was over its own cap and is now at it. The very next test pins the shape one
    merge away, where the corrected count is what lets `next` select.
    """
    root = lane(tmp_path)
    for ident, pr in (("hold-fix-4036-firewall-daemon-python", 4036),
                      ("pm-mini-probe-readonly-mcp", 4079),
                      ("hold-fix-4079-probe-sudoers-wrapper", 4079),   # the second entry
                      ("heartbeat-cron-decides-without-a-model", 4135),
                      ("dispatch-three-cards-per-tick-cap-four", 4137),
                      ("oc-compatibility-contract", 4146),
                      ("prompt-cache-shape-and-one-hour-tier", 4153)):
        held_pr(root, ident, pr=pr)
    brief(root, "queued", "hold-fix-4088-headless-launch-fix-forward", created="2026-08-19")
    brief(root, "queued", "hold-fix-4141-tier-routing-live-evidence", created="2026-08-20")

    r = mde.evaluate(root)
    assert r["inflight_count"] == 7 and r["in_motion_count"] == 7
    assert r["in_motion_slots"] == 6                      # was 7 — over its own cap
    assert r["shared_prs"] == [{"pr": 4079, "slots": 1, "entry_count": 2,
                                "entries": ["hold-fix-4079-probe-sudoers-wrapper",
                                            "pm-mini-probe-readonly-mcp"]}]
    assert len(r["held_prs"]) == 6                        # all six, still named
    # …and the honest consequence: six distinct held PRs fill a cap of six exactly.
    assert r["slots"] == 0 and r["blocked_by"] == "cap"
    assert "the cap is full (6 in motion of 6)" in mde.render_text(r)


def test_the_corrected_count_is_what_lets_next_select_on_a_lane_that_looks_full(tmp_path):
    """The 2026-09-08 shape one merge later: SEVEN in-motion entries — more than the cap —
    over FIVE distinct PRs. Under the old count that is `in_motion 7 / 6`, `slots: 0`,
    `blocked_by: "cap"`, `next: null`. Under D-PM6‴ it is five slots charged, one free,
    and `next` is the oldest eligible brief.

    `next` is asserted, not just `slots`: the failure this test forbids is a selection
    loop short-circuiting on a count taken before the grouping, which would report a free
    slot and still hand back `null`.
    """
    root = lane(tmp_path)
    for ident, pr in (("chip-a", 4036), ("chip-b", 4079), ("chip-c", 4079),
                      ("chip-d", 4135), ("chip-e", 4135), ("chip-f", 4137),
                      ("chip-g", 4146)):
        held_pr(root, ident, pr=pr)
    brief(root, "queued", "younger-brief", created="2026-08-20")
    brief(root, "queued", "oldest-brief", created="2026-08-19")

    r = mde.evaluate(root)
    assert r["in_motion_count"] == 7 and r["in_motion_count"] > r["cap"]   # the old count
    assert r["in_motion_slots"] == 5 and r["slots"] == 1                   # the new one
    assert not r["blocked_by"]
    assert r["dispatchable"] is True and r["next"]["id"] == "oldest-brief"
    assert [g["pr"] for g in r["shared_prs"]] == [4079, 4135]


def test_two_entries_on_one_pr_cost_one_slot_and_the_summary_names_the_sharing_pr(tmp_path):
    root = lane(tmp_path)
    started(root, "pm-mini-probe", pr=4079)
    started(root, "hold-fix-4079-probe-sudoers-wrapper", pr=4079)
    r = mde.evaluate(root)
    assert r["in_motion_count"] == 2 and r["in_motion_slots"] == 1 and r["slots"] == 5
    line = mde.render_text(r).splitlines()[1]
    # The `base:` field is appended by the stale-checkout reporter (#4181) and is
    # unconditional, so it rides every summary line including this one. `unknown` is the
    # fixture's value: a lane dir outside a git checkout, which must keep working.
    assert line == ("  in flight: 1/6 · prepared: 0/2 · slots: 5 in-motion, 2 prepared "
                    "· shared: #4079: 2 entries, 1 slot · headless: unavailable "
                    "· base: unknown")
    assert ("SHARED    #4079" in mde.render_text(r)
            and "2 entries, 1 slot (hold-fix-4079-probe-sudoers-wrapper, pm-mini-probe)"
            in mde.render_text(r))


def test_two_entries_on_two_prs_still_cost_two_slots(tmp_path):
    """The cap is not weakened. Two chips on two PRs are two lines of work."""
    root = lane(tmp_path)
    started(root, "chip-a", pr=4079)
    started(root, "chip-b", pr=4080)
    r = mde.evaluate(root)
    assert r["in_motion_count"] == r["in_motion_slots"] == 2 and r["slots"] == 4
    assert r["shared_prs"] == []
    assert "shared:" not in mde.render_text(r).splitlines()[1]


def test_the_cap_still_stops_dispatch_at_six_distinct_prs(tmp_path):
    """Guardrail: the cap's VALUE does not move and it still binds."""
    root = lane(tmp_path)
    for i in range(6):
        started(root, "busy-%d" % i, pr=100 + i)
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["cap"] == 6 and r["in_motion_slots"] == 6 and r["slots"] == 0
    assert r["blocked_by"] == "cap" and r["next"] is None


def test_an_entry_with_no_pr_is_charged_its_own_slot(tmp_path):
    """Unchanged from today: a started chip that has not opened a PR yet is its own line
    of work, and two of them are two slots — there is no `pr:` to share."""
    root = lane(tmp_path)
    started(root, "chip-a")
    started(root, "chip-b")
    started(root, "chip-c", pr=4079)
    r = mde.evaluate(root)
    assert r["in_motion_count"] == r["in_motion_slots"] == 3 and r["slots"] == 3
    assert r["shared_prs"] == []


def test_an_unparseable_pr_is_charged_its_own_slot_and_never_folded(tmp_path):
    """FAIL CLOSED, both halves.

    `build_slot_key` is the second line of defence and is pinned directly: anything that
    is not a positive int — a string the writer would refuse, `0`, a negative, and `True`
    (an `int` in Python, which must never key on 1) — gets its OWN key, so it can never be
    folded into a real PR's group and hand back a slot that is not free.

    The first line of defence is `load_brief`, and the lane-level half pins that too: a
    `pr:` the shared parser cannot read makes the entry INVALID, which is reported and
    never dispatched. It costs no slot there — an unparseable entry is not silently
    charged to #4079 either, which is the direction that matters.
    """
    key = mde.build_slot_key
    assert key({"id": "a", "pr": 4079, "path": "a.md"}) == ("pr", 4079)
    for bad in ("4079-ish", "", None, 0, -4079, True, 4079.0, [4079]):
        k = key({"id": "x", "pr": bad, "path": "x.md"})
        assert k == ("entry", "x.md"), (bad, k)
        assert k != ("pr", 4079) and k != ("pr", 1)
    # two unreadable ones are two slots, never one shared "bad pr" bucket
    slots, shared = mde.charge_build_slots([{"id": "x", "pr": "bogus", "path": "x.md"},
                                            {"id": "y", "pr": "bogus", "path": "y.md"},
                                            {"id": "z", "pr": 4079, "path": "z.md"}])
    assert (slots, shared) == (3, [])

    root = lane(tmp_path)
    started(root, "good", pr=4079)
    brief(root, "inflight", "bad-pr", launch="started", branch="claude/bad-pr",
          extra="pr: 4079-ish")
    r = mde.evaluate(root)
    assert r["in_motion_slots"] == 1                       # NOT folded into #4079
    assert [Path(bad["path"]).stem for bad in r["invalid"]] == ["bad-pr"]
    assert "4079-ish" in r["invalid"][0]["error"]


def test_both_counts_are_published_and_in_motion_count_keeps_its_meaning(tmp_path):
    """`in_motion_count` is a PUBLISHED field: it still says how many entries are in
    motion, and the count that gates moved to a new name that says what it is."""
    root = lane(tmp_path)
    started(root, "chip-a", pr=4079)
    started(root, "chip-b", pr=4079)
    prepared(root, "unclicked")
    r = mde.evaluate(root)
    assert r["inflight_count"] == 3
    assert r["in_motion_count"] == 2
    assert r["in_motion_slots"] == 1
    assert r["slots"] == r["cap"] - r["in_motion_slots"]
    # the one-directional invariant the three numbers must satisfy
    assert r["inflight_count"] >= r["in_motion_count"] >= r["in_motion_slots"]
    assert r["in_motion_count"] + r["prepared_count"] == r["inflight_count"]


def test_the_shared_report_is_in_the_json_payload(tmp_path, capsys):
    root = lane(tmp_path)
    started(root, "chip-a", pr=4079)
    started(root, "chip-b", pr=4079)
    mde.main(["--dir", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["in_motion_slots"] == 1 and payload["in_motion_count"] == 2
    assert payload["shared_prs"][0]["pr"] == 4079


# ── D-PM6‴ guardrails: every other block still blocks ────────────────────────
#
# Each fixture below frees a slot through the corrected count and then asserts the OTHER
# guard still stops the lane — so none of them can be passing merely because the cap was
# binding first.


def test_back_pressure_still_blocks_with_the_corrected_count(tmp_path):
    root = lane(tmp_path)
    for i in range(3):
        started(root, "busy-%d" % i, pr=200 + i, reviewed=None)      # unreviewed
    started(root, "fix-forward-200", pr=200, reviewed=None)          # shares #200
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["in_motion_count"] == 4 and r["in_motion_slots"] == 3 and r["slots"] == 3
    assert r["blocked_by"] == "back-pressure" and r["next"] is None


def test_depends_on_still_blocks_with_the_corrected_count(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=4079)
    started(root, "chip-b", pr=4079)
    brief(root, "queued", "waiting", depends_on=["never-done"])
    r = mde.evaluate(root)
    assert r["slots"] == 5 and r["blocked_by"] is None       # capacity is NOT the reason
    assert r["eligible"] == [] and r["next"] is None
    assert r["held"][0]["reason"] == "depends-on-unmet"


def test_privileged_without_greenlight_still_blocks_with_the_corrected_count(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=4079)
    started(root, "chip-b", pr=4079)
    brief(root, "queued", "priv-chip", privileged=True)
    r = mde.evaluate(root)
    assert r["slots"] == 5 and r["next"] is None
    assert r["held"][0]["reason"] == "privileged-without-greenlight"


def test_a_lane_conflict_still_blocks_with_the_corrected_count(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=4079)
    started(root, "chip-b", pr=4079)
    brief(root, "queued", "two-places", created="2026-08-01", body="One body.")
    brief(root, "done", "two-places", created="2026-08-01", body="Another body entirely.")
    r = mde.evaluate(root)
    assert r["slots"] == 5
    assert r["blocked_by"] == "lane-conflict" and r["next"] is None


def test_the_prepared_cap_still_blocks_with_the_corrected_count(tmp_path):
    """The prepared cap is untouched by this rule — a card is charged to it whether or not
    some other entry shares a PR, and two unclicked cards still stop the prepared route."""
    root = lane(tmp_path)
    started(root, "chip-a", pr=4079)
    started(root, "chip-b", pr=4079)
    prepared(root, "unclicked-a")
    prepared(root, "unclicked-b")
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["in_motion_slots"] == 1 and r["slots"] == 5      # build capacity to spare
    assert r["prepared_count"] == 2 and r["prepared_slots"] == 0
    assert r["blocked_by"] == "prepared-cap" and r["next"] is None


def test_the_body_integrity_gate_still_blocks_with_the_corrected_count(tmp_path):
    root = lane(tmp_path)
    started(root, "chip-a", pr=4079)
    started(root, "chip-b", pr=4079)
    path = brief(root, "queued", "stamped", body="The real brief body.")
    path.write_text(mdi.stamp(path.read_text(encoding="utf-8"))[0], encoding="utf-8")
    path.write_text(path.read_text(encoding="utf-8").replace("The real brief body.",
                                                             "Truncated."),
                    encoding="utf-8")
    r = mde.evaluate(root)
    assert r["slots"] == 5 and r["eligible"] == [] and r["next"] is None
    assert len(r["invalid"]) == 1


# ── D-PM6‴: pm_verdict() has a DEFINED answer when two entries share a PR ────


def test_pm_verdict_prefers_the_entry_whose_done_move_the_pr_carries(tmp_path):
    """Two entries legitimately name one PR (the D-PM7 shape), so "whichever the directory
    scan reached first" is not an answer — this lookup gates MERGING. `done/` wins: a
    chip's closing act is the `queued/ -> done/` rename it commits to the PR's own branch,
    so a `done/` entry carrying this `pr:` is the entry the PR head records as complete.
    """
    root = lane(tmp_path)
    brief(root, "inflight", "aaa-still-running", pr=4079, launch="started")
    brief(root, "done", "zzz-finished-on-the-pr-head", pr=4079)
    review(root, 4079, "Verdict: PASS — fine.\n")
    v = mde.pm_verdict(root, 4079)
    assert v["chip"] == "zzz-finished-on-the-pr-head"       # not the alphabetical first
    assert sorted(v["lane_entries"]) == ["aaa-still-running",
                                         "zzz-finished-on-the-pr-head"]
    assert v["qualifies"] is True


def test_pm_verdict_breaks_a_same_tier_tie_on_the_most_recent_dispatch(tmp_path):
    """Both entries in `inflight/`: the most recently dispatched answers, and `id` is only
    the final tiebreak — so the answer cannot be directory order wearing a disguise."""
    root = lane(tmp_path)
    brief(root, "inflight", "aaa-newer", pr=4079, launch="started",
          extra="dispatched_at: 2026-09-08T20:57:00Z")
    brief(root, "inflight", "bbb-older", pr=4079, launch="started",
          extra="dispatched_at: 2026-09-07T19:45:00Z")
    review(root, 4079, "Verdict: PASS — fine.\n")
    assert mde.pm_verdict(root, 4079)["chip"] == "aaa-newer"


def test_pm_verdict_is_stable_whatever_order_the_entries_are_scanned_in(tmp_path):
    """The selection is TOTAL: the same lane answers the same way however the ids sort."""
    for first, second in (("aaa", "zzz"), ("zzz", "aaa")):
        root = lane(tmp_path / first)
        brief(root, "inflight", first, pr=4079, launch="started",
              extra="dispatched_at: 2026-09-07T10:00:00Z")
        brief(root, "done", second, pr=4079)
        review(root, 4079, "Verdict: PASS — fine.\n")
        assert mde.pm_verdict(root, 4079)["chip"] == second      # the done/ one, always


def test_pm_verdict_privileged_is_the_or_across_every_entry_on_the_pr(tmp_path):
    """FAIL CLOSED where the answer gates merging: a non-privileged fix-forward chip bound
    to a privileged chip's PR must not talk that PR through D-PM2's gate, whichever entry
    the selection picks."""
    root = lane(tmp_path)
    brief(root, "inflight", "privileged-original", pr=4079, launch="started",
          privileged=True, greenlight=True)
    brief(root, "done", "non-privileged-fix-forward", pr=4079)
    review(root, 4079, "Verdict: PASS — fine.\n")
    v = mde.pm_verdict(root, 4079)
    assert v["chip"] == "non-privileged-fix-forward"      # the defined selection …
    assert v["privileged"] is True                        # … but the gate is the OR
    assert v["qualifies"] is False and "privileged" in v["reason"]
    assert v["hold_fix_forward"] is False


def test_pm_verdict_on_a_single_entry_is_unchanged(tmp_path):
    """Guardrail: the ordering only ever decides between MATCHES, so the one-entry answer
    — including a PR recorded only in `queued/` — is exactly what it was."""
    root = lane(tmp_path)
    brief(root, "queued", "only-entry", pr=4079)
    review(root, 4079, "Verdict: PASS — fine.\n")
    v = mde.pm_verdict(root, 4079)
    assert (v["chip"], v["lane"], v["qualifies"]) == ("only-entry", True, True)
    assert v["lane_entries"] == ["only-entry"]


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
    assert out["next"]["id"] == "chip-a" and out["cap"] == 6 and out["prepared_cap"] == 2
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
    for i, pr in enumerate((1, 2, 3, 4, 5, 6), start=1):
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


# ── the THIRD shape: a merged chip's leftover `inflight/` marker (2026-09-10) ──
#
# `inflight/` is working-tree-local by construction, and the chip's own PR moves
# `queued/ -> done/` and merges — so every SUCCESSFUL chip merge leaves one id in
# `inflight/` AND `done/`, and until now that stopped the whole lane, for every id, until
# a human ran `meta-dispatch-move done <id>` on the machine holding the marker. Observed
# live on 2026-09-10 after #4183 merged; the better the lane works the more often it
# fires. `internal/finding-inflight-done-after-merge-2026-09-10.md`.
#
# The release is NARROW: the `done/` entry must name a PR, and this run must have
# positively observed that PR merged (the number arrives in `--merged-prs`, which is the
# only merged-ness source — the tool never calls the network). Everything else is the
# conflict it always was.


def test_a_merged_chips_leftover_marker_is_repairable(tmp_path):
    """The 2026-09-10 shape: the merge is the evidence, and it is stronger than either
    shape above — the brief's text is on `main`, carried there by the PR the `done/`
    entry names."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "merged-chip", body=BODY))
    brief(root, "done", "merged-chip", body=BODY, pr=4183)

    r = mde.evaluate(root, facts={"merged_prs": [4183]})

    assert r["conflicts"] == [] and r["blocked_by"] is None
    (rp,) = r["repairable"]
    assert rp["id"] == "merged-chip"
    assert rp["shape"] == mde.mdi.REPAIR_MERGED_CHIP_MARKER
    assert rp["evidence"] == "merged-pr" and rp["pr"] == 4183
    assert rp["delete"].endswith("inflight/merged-chip.md")
    assert rp["keeper"].endswith("done/merged-chip.md")
    assert rp["command"] == "python3 tools/meta-dispatch-move done merged-chip"
    assert rp["log"] == ("dropped the in-flight marker for merged-chip "
                         "(chip merged as PR #4183; done/ carries the brief)")


def test_a_merged_chips_leftover_marker_does_not_stop_the_queue_head(tmp_path):
    """THE REGRESSION THIS IS FOR. On 2026-09-10 merging #4183 put one id in two dirs and
    the very next lane read answered `NEXT: none` — dispatching nothing at all, including
    a queue head with no anomaly of its own."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "merged-chip", body=BODY))
    brief(root, "done", "merged-chip", body=BODY, pr=4183)
    brief(root, "queued", "next-chip")

    r = mde.evaluate(root, facts={"merged_prs": [4183]})

    assert r["blocked_by"] is None
    assert r["next"]["id"] == "next-chip"
    assert "NEXT: none" not in mde.render_text(r)


def test_a_leftover_marker_whose_done_entry_has_no_pr_stays_a_conflict(tmp_path):
    """`pr:` is what says a PR carried this brief to `done/` at all. Without one there is
    nothing to check merged-ness of, and the marker is not provably the stale half."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "unbound-twin", body=BODY))
    brief(root, "done", "unbound-twin", body=BODY)

    r = mde.evaluate(root, facts={"merged_prs": [4183]})

    assert r["repairable"] == []
    assert [c["id"] for c in r["conflicts"]] == ["unbound-twin"]
    assert r["blocked_by"] == "lane-conflict"
    # Pinned at the predicate too, so the refusal is the predicate's and not an artefact
    # of the shape never having reached it — the lane-level assertion alone would still
    # hold if this pair were routed nowhere at all.
    assert mde.mdi.classify_merged_chip_marker(
        marker_body=BODY, done_body=BODY, done_pr=None, pr_merged=True) is None


def test_a_leftover_marker_whose_pr_is_still_OPEN_stays_a_conflict(tmp_path):
    """An open PR means the chip is still running, and its marker is holding a build slot
    correctly. The run observed #4200 merged and this entry's #4201 not, so #4201 is not
    in the injected set — the same path a closed-unmerged PR takes, because the injection
    carries merged numbers and nothing else."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "running-twin", body=BODY))
    brief(root, "done", "running-twin", body=BODY, pr=4201)

    r = mde.evaluate(root, facts={"merged_prs": [4200]})

    assert r["repairable"] == []
    assert [c["id"] for c in r["conflicts"]] == ["running-twin"]
    assert r["blocked_by"] == "lane-conflict"
    assert mde.mdi.classify_merged_chip_marker(
        marker_body=BODY, done_body=BODY, done_pr=4201, pr_merged=False) is None


def test_a_leftover_marker_whose_pr_CLOSED_UNMERGED_stays_a_conflict(tmp_path):
    """Abandoned work. Which of the two copies is canonical is then a judgment call — the
    `done/` entry records a completion that never happened — so the guard keeps it."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "abandoned-twin", body=BODY))
    brief(root, "done", "abandoned-twin", body=BODY, pr=4202)

    r = mde.evaluate(root, facts={"merged_prs": [4200, 4183]})

    assert r["repairable"] == []
    assert [c["id"] for c in r["conflicts"]] == ["abandoned-twin"]
    assert r["blocked_by"] == "lane-conflict"
    assert mde.mdi.classify_merged_chip_marker(
        marker_body=BODY, done_body=BODY, done_pr=4202, pr_merged=False) is None


def test_undeterminable_merged_ness_is_treated_as_NOT_merged(tmp_path):
    """FAIL CLOSED. When the run's `gh` page never happened — no network, an API error,
    an unattended tick that injected nothing — `merged_prs[]` is absent, and an absent
    fact is not an assumption. The thing being deleted is the lane's only local record
    that a chip is in flight, so uncertainty keeps the stop rather than releasing it."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "unknown-twin", body=BODY))
    brief(root, "done", "unknown-twin", body=BODY, pr=4183)

    r = mde.evaluate(root)                      # no facts at all

    assert r["repairable"] == []
    assert [c["id"] for c in r["conflicts"]] == ["unknown-twin"]
    assert r["blocked_by"] == "lane-conflict"
    assert mde.evaluate(root, facts={})["blocked_by"] == "lane-conflict"
    assert mde.mdi.classify_merged_chip_marker(
        marker_body=BODY, done_body=BODY, done_pr=4183, pr_merged=None) is None


def test_a_leftover_marker_the_done_entry_does_not_carry_stays_a_conflict(tmp_path):
    """The reader promises exactly what `meta-dispatch-move done` will perform, and that
    verb refuses to unlink a marker whose body is not inside the durable copy
    (`_check_durable_carries_the_brief`). A merged PR does not make a divergent `done/`
    body safe to drop text against."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "diverged-twin", body="WHY: the text as dispatched."))
    brief(root, "done", "diverged-twin", body="WHY: something else entirely.", pr=4183)

    r = mde.evaluate(root, facts={"merged_prs": [4183]})

    assert r["repairable"] == []
    assert [c["id"] for c in r["conflicts"]] == ["diverged-twin"]
    assert r["blocked_by"] == "lane-conflict"
    assert mde.mdi.classify_merged_chip_marker(
        marker_body="WHY: the text as dispatched.",
        done_body="WHY: something else entirely.",
        done_pr=4183, pr_merged=True) is None


def test_an_appended_outcome_note_on_the_done_entry_still_repairs(tmp_path):
    """Containment, not equality — a chip or the operator legitimately APPENDS an outcome
    note to a `done/` entry, and appending must not turn a healed lane back into a stopped
    one. (Unlike the `queued/`+`done/` shape, containment is safe here: the marker is not
    a brief anyone might have re-queued, it is the local trace of a chip that has now
    merged.)"""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "noted-chip", body=BODY))
    brief(root, "done", "noted-chip", body=BODY + "\n\nOUTCOME: merged.", pr=4183)

    r = mde.evaluate(root, facts={"merged_prs": [4183]})

    assert [rp["id"] for rp in r["repairable"]] == ["noted-chip"]
    assert r["blocked_by"] is None


def test_the_marker_repair_reaches_the_operator_in_the_text_render(tmp_path):
    """The REPAIR block names the right file and the right verb — the old line said
    "queued/ copy is redundant" for every shape, which would have pointed the reader at a
    file this shape does not have."""
    root = lane(tmp_path)
    _stamp(brief(root, "inflight", "merged-chip", body=BODY))
    brief(root, "done", "merged-chip", body=BODY, pr=4183)

    out = mde.render_text(mde.evaluate(root, facts={"merged_prs": [4183]}),
                          dry_run=True)

    assert "REPAIR" in out and "inflight/ marker is redundant" in out
    assert "python3 tools/meta-dispatch-move done merged-chip" in out


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


# ── D-PM11: dispatcher-authored lane state is reversible by construction ─────
#
# WHY. `tools/meta-dispatch-move land` opens a PR carrying the dispatcher's own lane
# bookkeeping. It has no chip row, so nothing about it carries `reversible` or
# `operator_merge`, and the reconciler's headless unledgered-orphan rule refuses to merge
# rather than guess. Right in general, wrong for this one shape, and the cost compounds:
# 19 byte-identical one-line PRs over eleven sweeps (#4046-#4065, 2026-09-05/06). These
# pin the qualifier that replaces the guess with a rule — and, in every direction, that it
# fails toward NOT merging.

import subprocess  # noqa: E402
import sys as _sys_lane  # noqa: E402

_sys_lane.path.insert(0, str(_TOOL.parent))
import meta_dispatch_integrity as _mdi  # noqa: E402


def _lane_repo(tmp_path: Path):
    """A repo whose `origin/main` and `origin/lane/state` are both real refs."""
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
    root = work / "internal" / "dispatch"
    for sub in ("queued", "inflight", "done", "reviews"):
        (root / sub).mkdir(parents=True)
        (root / sub / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(origin)],
                   check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "main"],
                   check=True)
    return work, root, origin


_LANE_BODY = "WHY: pinned by the lane-state qualifier tests."


def _entry(ident, *, state="queued", extra="", body=_LANE_BODY):
    return ("---\nid: %s\naspect: substrate\ntitle: t\nprivileged: false\n"
            "created: 2026-09-06\npm: fable-cowork\nbody_sha256: %s\n%s---\n%s\n"
            % (ident, _mdi.body_digest(body), extra, body))


def _commit_push(work: Path, msg: str, branch: str = "main") -> str:
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", msg], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin",
                    "HEAD:refs/heads/%s" % branch], check=True)
    subprocess.run(["git", "-C", str(work), "fetch", "-q", "origin"], check=True)
    return subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _lane_pr_json(head, *, paths, branch=None, trailer=True, number=4100):
    branch = _mdi.LANE_STATE_BRANCH if branch is None else branch
    body = ("Lane state.\n\n" + _mdi.LAND_TRAILER) if trailer else "Lane state.\n"
    return json.dumps({
        "number": number, "headRefName": branch,
        "files": [{"path": p} for p in paths],
        "commits": [{"oid": head, "messageHeadline": "lane: state — 1 lane change",
                     "messageBody": body}],
    })


def _land_like(work: Path, root: Path, mutate) -> str:
    """Apply a lane change and push it to `lane/state`, then restore main's worktree."""
    main_sha = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-B", "landing",
                    main_sha], check=True)
    mutate(root)
    head = _commit_push(work, "lane: state", branch=_mdi.LANE_STATE_BRANCH)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "main"], check=True)
    return head


def test_a_front_matter_only_lane_pr_qualifies(tmp_path):
    work, root, _ = _lane_repo(tmp_path)
    (root / "queued" / "chip-x.md").write_text(_entry("chip-x"))
    _commit_push(work, "queue chip-x")
    head = _land_like(work, root, lambda r: (r / "queued" / "chip-x.md").write_text(
        _entry("chip-x", extra="operator_greenlight: true\n")))

    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/queued/chip-x.md"]))
    assert v["qualifies"] is True, v["reason"]
    assert v["dispatcher_authored"] and v["front_matter_only"]
    # ...and the reconciler is told, in the payload, what this does NOT answer.
    assert any("CI green" in c for c in v["reconciler_must_also_check"])
    assert any("--match-head-commit" in c for c in v["reconciler_must_also_check"])


def test_a_lane_move_qualifies_because_the_body_survives_it(tmp_path):
    """queued/ -> inflight/ is a delete plus an add. What makes it safe is that the
    BODY survived the move, not that the path did."""
    work, root, _ = _lane_repo(tmp_path)
    (root / "queued" / "chip-m.md").write_text(_entry("chip-m"))
    _commit_push(work, "queue chip-m")

    def move(r):
        (r / "inflight" / "chip-m.md").write_text(
            _entry("chip-m", extra="launch: prepared\n"))
        (r / "queued" / "chip-m.md").unlink()
    head = _land_like(work, root, move)

    # GitHub reports a rename as ONE file entry (the new path, plus a previousFilename
    # gh does not surface), so this is the realistic `files[]` for a move.
    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/inflight/chip-m.md"]))
    assert v["qualifies"] is True, v["reason"]
    # ...and the diff, read with --no-renames, carries BOTH sides — which is what lets
    # "the body survived the move" be checkable at all.
    assert v["paths"] == ["internal/dispatch/inflight/chip-m.md",
                          "internal/dispatch/queued/chip-m.md"]


def test_a_pure_entry_deletion_is_refused_as_an_entry_loss_not_a_body_change(tmp_path):
    """The #4113 shape: `inflight/<id>` dropped with nothing left under any lane dir.
    Refusing is right — an entry loss is not front matter — but the reason must SAY
    that, not "the body differs", which sends the reconciler hunting an edit that never
    happened."""
    work, root, _ = _lane_repo(tmp_path)
    (root / "inflight" / "chip-gone.md").write_text(_entry("chip-gone"))
    _commit_push(work, "inflight chip-gone")
    head = _land_like(work, root,
                      lambda r: (r / "inflight" / "chip-gone.md").unlink())

    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/inflight/chip-gone.md"]))
    assert v["qualifies"] is False
    assert v["reason"].startswith("entry-deleted: ")
    assert "chip-gone" in v["reason"] and "entry loss" in v["reason"]
    # ...and specifically NOT the body reason, which is what it used to say.
    assert "body" not in v["reason"]


def test_a_deletion_whose_counterpart_already_sits_in_done_is_a_move(tmp_path):
    """`done <id>` deletes the tracked `inflight/<id>` while `done/<id>` is ALREADY on
    origin/main from a chip PR that raced the lane PR. The diff shows one side only, so
    the digest comparison sees an empty head list — but the entry did not go anywhere,
    and refusing parks the standing PR on a deletion it re-pushes every tick."""
    work, root, _ = _lane_repo(tmp_path)
    (root / "inflight" / "chip-raced.md").write_text(_entry("chip-raced"))
    (root / "done" / "chip-raced.md").write_text(
        _entry("chip-raced", extra="bucket: merged\n"))
    _commit_push(work, "chip-raced landed in done by the chip PR")
    head = _land_like(work, root,
                      lambda r: (r / "inflight" / "chip-raced.md").unlink())

    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/inflight/chip-raced.md"]))
    assert v["qualifies"] is True, v["reason"]
    assert v["front_matter_only"] is True


def test_a_deletion_whose_counterpart_carries_a_different_body_is_still_a_loss(tmp_path):
    """Same shape as the move above, but the surviving `done/<id>` is a different brief
    under the same id — the body did not survive, so this is not a move."""
    work, root, _ = _lane_repo(tmp_path)
    (root / "inflight" / "chip-swap.md").write_text(_entry("chip-swap"))
    (root / "done" / "chip-swap.md").write_text(
        _entry("chip-swap", body="WHY: an entirely different brief."))
    _commit_push(work, "inflight + an unrelated done entry")
    head = _land_like(work, root,
                      lambda r: (r / "inflight" / "chip-swap.md").unlink())

    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/inflight/chip-swap.md"]))
    assert v["qualifies"] is False
    assert v["reason"].startswith("entry-deleted: ")


def test_it_refuses_a_pr_that_touches_a_body(tmp_path):
    work, root, _ = _lane_repo(tmp_path)
    (root / "queued" / "chip-b.md").write_text(_entry("chip-b"))
    _commit_push(work, "queue chip-b")
    head = _land_like(work, root, lambda r: (r / "queued" / "chip-b.md").write_text(
        _entry("chip-b", body="WHY: an entirely different brief.")))

    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/queued/chip-b.md"]))
    assert v["qualifies"] is False
    assert "body" in v["reason"] and "chip-b" in v["reason"]


def test_it_refuses_a_pr_that_touches_reviews(tmp_path):
    """A review file is the PM's verdict. A bookkeeping rule must never merge one."""
    work, root, _ = _lane_repo(tmp_path)
    head = _land_like(work, root, lambda r: (r / "reviews" / "pr-4100.md").write_text(
        "Verdict: PASS — looks fine.\n"))
    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/reviews/pr-4100.md"]))
    assert v["qualifies"] is False
    assert "review" in v["reason"].lower()


def test_it_refuses_a_pr_that_is_not_dispatcher_authored(tmp_path):
    """The trailer is the identity. The git AUTHOR is the operator's in every checkout
    that runs the lane, so an author check would qualify a hand-push to this branch."""
    work, root, _ = _lane_repo(tmp_path)
    (root / "queued" / "chip-h.md").write_text(_entry("chip-h"))
    _commit_push(work, "queue chip-h")
    head = _land_like(work, root, lambda r: (r / "queued" / "chip-h.md").write_text(
        _entry("chip-h", extra="launch: prepared\n")))

    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/queued/chip-h.md"], trailer=False))
    assert v["qualifies"] is False
    assert v["dispatcher_authored"] is False
    assert _mdi.LAND_TRAILER in v["reason"]


def test_it_refuses_a_path_outside_the_dispatch_dir(tmp_path):
    work, root, _ = _lane_repo(tmp_path)
    head = _land_like(work, root, lambda r: (r.parent.parent / "README.md")
                      .write_text("a real content change\n"))
    v = mde.lane_state_pr(root, 4100, _lane_pr_json(head, paths=["README.md"]))
    assert v["qualifies"] is False
    assert "README.md" in v["reason"]


# ── the path scope is read from the DIFF, never from the PR's own account ────
#
# THE BYPASS THIS CLOSES. `files[]` is injected by the caller and caps at 100 entries
# with no truncation signal, and a `startswith("internal/dispatch/queued/")` prefix test
# is not the lane-entry rule. Together they accepted arbitrary content: for a path the
# body index never sees (non-`.md`, or one directory deeper), both sides of the body
# comparison are the empty list, they compare equal, and the function fell through to
# `qualifies: True`. `lane/state` is not a protected branch and the trailer string is in
# the repo, so that was a review bypass for anyone with push access — precisely what the
# trailer check exists to prevent.


@pytest.mark.parametrize("name,payload", [
    ("a non-.md file under a lane dir", "evil.sh"),
    ("a .md one directory deeper", "sub/payload.md"),
])
def test_arbitrary_content_under_a_lane_dir_never_qualifies(tmp_path, name, payload):
    work, root, _ = _lane_repo(tmp_path)

    def plant(r):
        target = r / "queued" / payload
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\narbitrary content\n")
    head = _land_like(work, root, plant)

    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/queued/%s" % payload]))
    assert v["qualifies"] is False, "%s must not qualify: %s" % (name, v["reason"])
    assert payload in v["reason"]


def test_a_path_the_pr_metadata_hides_is_still_seen(tmp_path):
    """`files[]` under-reporting (the 100-entry cap, or a caller passing a short list)
    must not shrink what is checked: the diff is the authority."""
    work, root, _ = _lane_repo(tmp_path)
    (root / "queued" / "chip-hidden.md").write_text(_entry("chip-hidden"))
    _commit_push(work, "queue chip-hidden")

    def sneak(r):
        (r / "queued" / "chip-hidden.md").write_text(
            _entry("chip-hidden", extra="launch: prepared\n"))
        (r.parent.parent / "packages" / "admin").mkdir(parents=True, exist_ok=True)
        (r.parent.parent / "packages" / "admin" / "x.py").write_text("import os\n")
    head = _land_like(work, root, sneak)

    # The PR's own account names only the innocuous path.
    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/queued/chip-hidden.md"]))
    assert v["qualifies"] is False
    assert "packages/admin/x.py" in v["reason"]


def test_metadata_naming_a_path_the_diff_lacks_is_a_refusal(tmp_path):
    """Corroboration cuts both ways: if `files[]` and the diff disagree, neither is
    trusted and nothing is merged on the friendlier reading."""
    work, root, _ = _lane_repo(tmp_path)
    (root / "queued" / "chip-p.md").write_text(_entry("chip-p"))
    _commit_push(work, "queue chip-p")
    head = _land_like(work, root, lambda r: (r / "queued" / "chip-p.md").write_text(
        _entry("chip-p", extra="launch: prepared\n")))

    v = mde.lane_state_pr(root, 4100, _lane_pr_json(head, paths=[
        "internal/dispatch/queued/chip-p.md",
        "internal/dispatch/queued/never-in-the-diff.md"]))
    assert v["qualifies"] is False
    assert "never-in-the-diff.md" in v["reason"]


def test_a_commit_with_no_oid_is_a_refusal_not_a_fallback_ref(tmp_path):
    """A missing `oid` used to fall back to the LOCAL `origin/lane/state`, which after a
    force-push is a different revision than the PR head — so the body check vetted a tree
    that is not the one being merged, and could still say yes."""
    work, root, _ = _lane_repo(tmp_path)
    payload = json.loads(_lane_pr_json("x" * 40, paths=["internal/dispatch/queued/a.md"]))
    payload["commits"][0].pop("oid")
    v = mde.lane_state_pr(root, 4100, json.dumps(payload))
    assert v["qualifies"] is False and "oid" in v["reason"]


def test_the_payload_tells_the_reconciler_to_pin_the_head_it_vetted(tmp_path):
    """This answers about a SHA; the reconciler merges a PR NUMBER; and `land`
    force-pushes that branch every 30 minutes — including during the reconciler's own
    checks poll. Without `--match-head-commit` the merge can land a tree this rule never
    saw and would have refused."""
    work, root, _ = _lane_repo(tmp_path)
    (root / "queued" / "chip-pin.md").write_text(_entry("chip-pin"))
    _commit_push(work, "queue chip-pin")
    head = _land_like(work, root, lambda r: (r / "queued" / "chip-pin.md").write_text(
        _entry("chip-pin", extra="launch: prepared\n")))
    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/queued/chip-pin.md"]))
    assert v["qualifies"] is True
    assert v["head"] == head
    assert any("--match-head-commit" in c for c in v["reconciler_must_also_check"])

    # Not "somewhere in the file": the D-PM11 block and HARD RULES both carry the pin
    # in prose, so a whole-file grep passes while the ONE line the headless reconciler
    # actually executes — step 4's lane-state merge bullet — merges unpinned.
    proc = _TOOL.parent.parent / "internal" / "meta-reconcile-procedure.md"
    bullet = [ln for ln in proc.read_text(encoding="utf-8").splitlines()
              if "MERGE the dispatcher's LANE-STATE PR" in ln]
    assert len(bullet) == 1, "step 4's lane-state merge bullet is missing or doubled"
    assert "--match-head-commit" in bullet[0], \
        "step 4's merge bullet is what the reconciler runs; it must pass the pin"


# ── closing the superseded per-tick PRs ──────────────────────────────────────


def test_a_legacy_pr_whose_paths_the_merge_carries_is_superseded():
    v = mde.lane_state_supersedes(["a/x.md", "a/y.md"], ["a/x.md"])
    assert v["superseded"] is True


def test_a_legacy_pr_carrying_an_extra_path_is_not_superseded():
    """Not a subset is not tidy-up: it is a diff nobody has looked at."""
    v = mde.lane_state_supersedes(["a/x.md"], ["a/x.md", "a/z.md"])
    assert v["superseded"] is False and v["extra"] == ["a/z.md"]


def test_an_empty_candidate_is_never_closed_as_superseded():
    assert mde.lane_state_supersedes(["a/x.md"], [])["superseded"] is False


def test_the_superseded_cli_exits_zero_only_on_a_subset(tmp_path):
    work, root, _ = _lane_repo(tmp_path)
    cand = json.dumps({"files": [{"path": "internal/dispatch/queued/a.md"}]})
    ok = subprocess.run([sys.executable, str(_TOOL), "--lane-state-superseded", "4046",
                         "--dir", str(root), "--pr-json", cand, "--merged-files",
                         "internal/dispatch/queued/a.md,internal/dispatch/done/b.md",
                         "--json"], capture_output=True, text=True, cwd=str(work))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert json.loads(ok.stdout)["superseded"] is True

    no = subprocess.run([sys.executable, str(_TOOL), "--lane-state-superseded", "4046",
                         "--dir", str(root), "--pr-json", cand, "--merged-files",
                         "internal/dispatch/done/b.md", "--json"],
                        capture_output=True, text=True, cwd=str(work))
    assert no.returncode == 1
    assert json.loads(no.stdout)["superseded"] is False


def test_it_refuses_a_head_branch_that_is_not_the_standing_lane_branch(tmp_path):
    work, root, _ = _lane_repo(tmp_path)
    head = _land_like(work, root, lambda r: (r / "queued" / "chip-z.md").write_text(
        _entry("chip-z")))
    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        head, paths=["internal/dispatch/queued/chip-z.md"],
        branch="lane/state-20260905T000000Z"))
    assert v["qualifies"] is False
    assert "lane/state-20260905T000000Z" in v["reason"]


def test_it_refuses_pr_json_describing_a_different_pr(tmp_path):
    work, root, _ = _lane_repo(tmp_path)
    head = _land_like(work, root, lambda r: (r / "queued" / "chip-q.md").write_text(
        _entry("chip-q")))
    v = mde.lane_state_pr(root, 4101, _lane_pr_json(
        head, paths=["internal/dispatch/queued/chip-q.md"], number=4100))
    assert v["qualifies"] is False and "4100" in v["reason"]


def test_a_head_sha_this_checkout_never_fetched_is_a_refusal_naming_the_fetch(tmp_path):
    """Read-only and offline: it says `git fetch`, it never runs one."""
    work, root, _ = _lane_repo(tmp_path)
    v = mde.lane_state_pr(root, 4100, _lane_pr_json(
        "0" * 40, paths=["internal/dispatch/queued/chip-x.md"]))
    assert v["qualifies"] is False
    assert "fetch" in v["reason"]


@pytest.mark.parametrize("payload,needle", [
    (None, "no PR metadata"),
    ("{not json", "not the JSON object"),
    ("[]", "not an object"),
    (json.dumps({"headRefName": "lane/state", "files": [{"path": "x"}]}), "commits"),
    (json.dumps({"headRefName": "lane/state",
                 "commits": [{"oid": "a", "messageBody": _mdi.LAND_TRAILER}]}), "files"),
])
def test_missing_or_malformed_metadata_is_a_refusal_not_a_guess(tmp_path, payload,
                                                                needle):
    work, root, _ = _lane_repo(tmp_path)
    v = mde.lane_state_pr(root, 4100, payload)
    assert v["qualifies"] is False
    assert needle in v["reason"]


def test_the_cli_exits_one_when_it_does_not_qualify_and_zero_when_it_does(tmp_path):
    work, root, _ = _lane_repo(tmp_path)
    (root / "queued" / "chip-c.md").write_text(_entry("chip-c"))
    _commit_push(work, "queue chip-c")
    head = _land_like(work, root, lambda r: (r / "queued" / "chip-c.md").write_text(
        _entry("chip-c", extra="launch: prepared\n")))
    payload = _lane_pr_json(head, paths=["internal/dispatch/queued/chip-c.md"])

    ok = subprocess.run([sys.executable, str(_TOOL), "--lane-state-pr", "4100",
                         "--dir", str(root), "--pr-json", payload, "--json"],
                        capture_output=True, text=True, cwd=str(work))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert json.loads(ok.stdout)["qualifies"] is True

    bad = subprocess.run([sys.executable, str(_TOOL), "--lane-state-pr", "4100",
                          "--dir", str(root), "--pr-json",
                          _lane_pr_json(head, paths=["README.md"]), "--json"],
                         capture_output=True, text=True, cwd=str(work))
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["qualifies"] is False


def test_the_qualifier_and_the_lander_share_one_definition_of_the_branch():
    """Two strings that must agree or the writer's PRs silently stop matching the
    reader's rule — in the direction that refuses every merge, which is the flood."""
    move = _TOOL.parent / "meta-dispatch-move"
    src = move.read_text(encoding="utf-8")
    assert "LANE_STATE_BRANCH = mdi.LANE_STATE_BRANCH" in src
    assert "LAND_TRAILER = mdi.LAND_TRAILER" in src


# ── the standing "cards waiting for a tap" line (2026-09-07) ─────────────────
#
# WHY IT EXISTS (operator): a prepared card is announced exactly once, by the
# edge-triggered poke on the tick that prepared it. With the dispatcher ticking round the
# clock, a card missed in that moment had no surface that would ever mention it again and
# simply aged in a tray of quiet tick sessions. This line is the state of the tray,
# printed on EVERY tick — including the ticks that have no news, which is the whole point.


def _card(root, ident, *, at=None, session=None, privileged=False, title=None,
          greenlight=None):
    """A PREPARED card: in `inflight/` with `launch: prepared` and no start evidence,
    which is exactly the partition the prepared cap gates on."""
    extra = ["launch: prepared"]
    if at is not None:
        extra.append("dispatched_at: %s" % at)
        extra.append("dispatched: %s" % at[:10])
    if session is not None:
        extra.append("session: %s" % session)
    return brief(root, "inflight", ident, title=title, privileged=privileged,
                 greenlight=greenlight, extra="\n".join(extra))


NOW = datetime.datetime(2026, 9, 7, 18, 30, 0, tzinfo=datetime.timezone.utc)


def test_waiting_line_says_none_when_no_card_is_waiting(tmp_path):
    """Printed on a quiet tick too. A line that disappears at zero is a line whose
    absence the operator has to interpret — and the token they skim for changes."""
    root = lane(tmp_path)
    r = mde.evaluate(root)
    assert r["cards_waiting"] == []
    assert r["cards_waiting_line"] == "cards waiting for a tap: none"
    assert "  cards waiting for a tap: none" in mde.render_text(r).splitlines()


def test_waiting_line_names_one_card_with_its_time_age_and_session(tmp_path):
    """The three facts that let an operator find a tile an hour later: when it
    appeared (their wall clock), how long it has sat, and the handle that names it."""
    root = lane(tmp_path)
    _card(root, "one-card", at="2026-09-07T16:05:00Z", session="task_ab12cd34",
          title="Widen the drift check")

    cards = mde.cards_waiting(
        [e for e in mde.scan_lane(root)[0]["inflight"] if not mde.in_motion(e)], NOW)
    line = mde.render_cards_waiting(cards)

    assert line.startswith("cards waiting for a tap: 1 — Widen the drift check (")
    assert "created %s" % mdi.local_hhmm("2026-09-07T16:05:00Z") in line
    assert "2h" in line                       # 16:05Z -> 18:30Z, floored
    assert "session task_ab12cd34" in line
    assert "privileged" not in line


def test_waiting_line_is_oldest_first_and_marks_privileged(tmp_path):
    """Ordering is `dispatched_at` ascending — the buried card is the old one, so it
    leads. `privileged` is marked because those cards cannot be cleared by anything but
    the operator (D-PM1), so a mixed pile is not a uniform pile."""
    root = lane(tmp_path)
    _card(root, "card-newest", at="2026-09-07T17:50:00Z", session="task_c3", title="Third")
    _card(root, "card-oldest", at="2026-09-07T09:14:00Z", session="task_a1", title="First",
          privileged=True, greenlight=True)
    _card(root, "card-middle", at="2026-09-07T12:00:00Z", session="task_b2", title="Second")

    cards = mde.cards_waiting(
        [e for e in mde.scan_lane(root)[0]["inflight"] if not mde.in_motion(e)], NOW)

    assert [c["id"] for c in cards] == ["card-oldest", "card-middle", "card-newest"]
    assert [c["age"] for c in cards] == ["9h", "6h", "0h"]     # floored, not rounded
    line = mde.render_cards_waiting(cards)
    assert line.startswith("cards waiting for a tap: 3 — First (")
    assert line.index("First") < line.index("Second") < line.index("Third")
    assert "privileged" in line.split("Second")[0]     # marked on the first card only
    assert line.count("privileged") == 1


def test_waiting_line_floors_the_age_so_this_hour_reads_zero(tmp_path):
    """A card prepared 40 minutes ago is `0h`, not `1h`. Flooring is the honest answer
    to "did this hour's tick make it?" — rounding up would make every fresh card look
    like it had already been missed once."""
    root = lane(tmp_path)
    _card(root, "fresh-card", at="2026-09-07T17:50:00Z", session="task_x")
    cards = mde.cards_waiting(
        [e for e in mde.scan_lane(root)[0]["inflight"] if not mde.in_motion(e)], NOW)
    assert cards[0]["age_hours"] == 0 and cards[0]["age"] == "0h"


def test_waiting_line_survives_a_card_with_no_stamp_and_sorts_it_first(tmp_path):
    """Every card written before 2026-09-07 has no `dispatched_at`, and so does one
    whose bookkeeping went wrong. Both read `?`/`?h` rather than vanishing — the card
    the operator has lost is precisely the one whose bookkeeping failed — and both sort
    FIRST, where the eye starts, rather than at the end where it stops."""
    root = lane(tmp_path)
    _card(root, "stamped-card", at="2026-09-07T16:05:00Z", session="task_new")
    _card(root, "legacy-card", session="task_old")

    cards = mde.cards_waiting(
        [e for e in mde.scan_lane(root)[0]["inflight"] if not mde.in_motion(e)], NOW)

    assert [c["id"] for c in cards] == ["legacy-card", "stamped-card"]
    assert cards[0]["created_local"] is None and cards[0]["age"] == "?h"
    assert "created ?, ?h, session task_old" in mde.render_cards_waiting(cards)


def test_waiting_line_counts_exactly_what_the_prepared_cap_counts(tmp_path):
    """One partition, two consumers. If the line and `prepared_count` were derived
    separately they could disagree about how many cards are waiting — and the operator
    would have no way to tell which of the two was lying."""
    root = lane(tmp_path)
    _card(root, "waiting-a", at="2026-09-07T10:00:00Z", session="task_a")
    _card(root, "waiting-b", at="2026-09-07T11:00:00Z", session="task_b")
    brief(root, "inflight", "moving-chip", launch="started", branch="claude/x")

    r = mde.evaluate(root)

    assert r["prepared_count"] == 2 == len(r["cards_waiting"])
    assert r["in_motion_count"] == 1
    assert "moving-chip" not in r["cards_waiting_line"]


def test_waiting_line_is_reported_on_a_blocked_tick_too(tmp_path):
    """The tick that most needs it is the one with nothing else to say. A lane at its
    prepared cap dispatches nothing and pokes nothing — and that is exactly the state
    where a card is sitting unclicked."""
    root = lane(tmp_path)
    _card(root, "waiting-a", at="2026-09-07T10:00:00Z", session="task_a")
    _card(root, "waiting-b", at="2026-09-07T11:00:00Z", session="task_b")
    brief(root, "queued", "ready-work")

    r = mde.evaluate(root)

    assert r["blocked_by"] == "prepared-cap" and r["next"] is None
    assert r["cards_waiting_line"].startswith("cards waiting for a tap: 2 — ")
    assert any(ln.strip() == r["cards_waiting_line"]
               for ln in mde.render_text(r).splitlines())


def test_waiting_line_clips_a_long_title_but_never_the_session_or_time(tmp_path):
    """Lane titles are a chip's whole scope in one sentence — the corpus runs past 200
    characters and three of those on one line is 700. The line's job is recognition;
    the fields that IDENTIFY the card are never clipped, and the full title stays in
    `cards_waiting[].title` for any caller that wants it."""
    root = lane(tmp_path)
    long_title = "Widen the drift check " * 12
    _card(root, "verbose-card", at="2026-09-07T16:05:00Z", session="task_zz",
          title=long_title.strip())

    cards = mde.cards_waiting(
        [e for e in mde.scan_lane(root)[0]["inflight"] if not mde.in_motion(e)], NOW)
    line = mde.render_cards_waiting(cards)

    assert cards[0]["title"] == long_title.strip()          # untruncated in the data
    assert "…" in line and len(line) < 200
    assert "session task_zz" in line and "created" in line


# ── the timed chip title (2026-09-07) ────────────────────────────────────────


def test_next_carries_a_chip_title_with_the_local_time(tmp_path):
    """The spawn title. `HH:MM` is the operator's wall clock, 24-hour, so the tray tile
    is self-describing and the tray sorts chronologically. The run cannot compose it:
    it has no `date` grant and the harness gives it a date with no time."""
    root = lane(tmp_path)
    brief(root, "queued", "ready-work", aspect="substrate", title="Widen the drift check")

    r = mde.evaluate(root)

    timed = r["next"]["chip_title_timed"]
    assert re.fullmatch(r"\[META:substrate\] \d{2}:\d{2} Widen the drift check", timed)
    assert timed == "[META:substrate] %s Widen the drift check" % r["now_local"]
    # the untimed form is still reported — it is what internal/dispatch/README.md calls
    # the chip title, and only the SPAWN title carries the time
    assert r["next"]["chip_title"] == "[META:substrate] Widen the drift check"


def test_now_local_is_the_same_instant_as_now(tmp_path):
    """One sample, not two. Sampling twice lets the title say 10:59 while the stamp
    beside it says 11:00 — a one-minute lie about a field whose entire job is to say
    which tile."""
    root = lane(tmp_path)
    brief(root, "queued", "ready-work")

    r = mde.evaluate(root)

    expected = (datetime.datetime.strptime(r["now"], "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=datetime.timezone.utc).astimezone().strftime("%H:%M"))
    assert r["now_local"] == expected
    assert r["now_tz"]


# ── D-PM6′: the headless launch path ─────────────────────────────────────────
#
# The defect this whole path exists for, reproduced first: on 2026-09-06 the lane sat at
# `blocked_by: "prepared-cap"` with ONE free build slot and 24 eligible briefs, because
# two untapped cards had frozen dispatch outright. The tap protects nothing on a
# non-privileged brief — it is on main, the PM reviewed it, and the chip's PR still goes
# through the reconciler's gates — so the fix is to stop making one.


def headless(root, ident, *, session="agent_abc123", started_at="2026-09-06T10:00:00Z",
             privileged=False):
    """An in-flight entry the dispatcher started headless: a session, a start stamp, and
    deliberately NO branch — the chip writes that itself at branch-cut."""
    brief(root, "inflight", ident, privileged=privileged, launch="headless",
          extra="session: %s\nstarted: %s" % (session, started_at))


def test_a_headless_entry_is_in_motion_from_the_moment_it_is_stamped(tmp_path):
    """It has no branch and no PR — it is running anyway, and it costs a BUILD slot from
    the instant `launch --headless` returns, because there is no click between the stamp
    and the work."""
    root = lane(tmp_path)
    headless(root, "running-now")
    r = mde.evaluate(root)
    assert r["in_motion_count"] == 1
    assert r["prepared_count"] == 0
    assert r["slots"] == 5


def test_a_headless_entry_is_never_charged_to_the_prepared_cap(tmp_path):
    """Two invisible running sessions must not freeze the gate this path was built to
    remove — the exact inversion that would make the fix reintroduce the bug."""
    root = lane(tmp_path)
    headless(root, "one")
    headless(root, "two")
    r = mde.evaluate(root)
    assert r["prepared_count"] == 0
    assert r["prepared_slots"] == 2
    assert r["blocked_by"] is None


def test_a_full_tray_no_longer_freezes_a_non_privileged_brief(tmp_path, monkeypatch):
    """THE regression test for 2026-09-06. Two unclicked cards, a free build slot, one
    eligible non-privileged brief: dispatch proceeds, on the headless route."""
    headless_available(monkeypatch)
    root = lane(tmp_path)
    prepared(root, "card-one")
    prepared(root, "card-two")
    brief(root, "queued", "ready-work", created="2026-08-21")
    r = mde.evaluate(root)
    assert r["prepared_slots"] == 0, "the tray is genuinely full"
    assert r["blocked_by"] is None
    assert r["next"]["id"] == "ready-work"
    assert r["next"]["route"] == "headless"


def test_a_full_tray_still_freezes_a_privileged_brief(tmp_path, monkeypatch):
    """The other half of the same rule: a privileged brief still needs a card, so a full
    tray still stops it. D-PM1/D-PM2 keep the operator at both ends."""
    headless_available(monkeypatch)
    root = lane(tmp_path)
    prepared(root, "card-one")
    prepared(root, "card-two")
    brief(root, "queued", "priv-work", privileged=True, greenlight=True)
    r = mde.evaluate(root)
    assert r["next"] is None
    assert r["blocked_by"] == "prepared-cap"


def test_a_full_tray_is_walked_past_to_reach_a_headless_candidate(tmp_path, monkeypatch):
    """Oldest-first still orders the walk; it no longer lets a full tray outrank it. The
    older privileged brief cannot go, so the run takes the younger non-privileged one
    rather than reporting the whole lane blocked."""
    headless_available(monkeypatch)
    root = lane(tmp_path)
    prepared(root, "card-one")
    prepared(root, "card-two")
    brief(root, "queued", "older-priv", privileged=True, greenlight=True,
          created="2026-08-01")
    brief(root, "queued", "younger-open", created="2026-08-15")
    r = mde.evaluate(root)
    assert [e["id"] for e in r["eligible"]] == ["older-priv", "younger-open"]
    assert r["next"]["id"] == "younger-open"
    assert r["next"]["route"] == "headless"


def test_the_build_cap_still_stops_everything(tmp_path, monkeypatch):
    """`cap` outranks the route split. A headless session is work in motion like any
    other, so a full build cap is still a whole-lane stop — the one thing the D-PM6′
    change must NOT relax."""
    headless_available(monkeypatch)
    root = lane(tmp_path)
    for i in range(6):
        started(root, "chip-%d" % i)
    brief(root, "queued", "ready-work")
    r = mde.evaluate(root)
    assert r["blocked_by"] == "cap"
    assert r["next"] is None
    assert r["headless_slots"] == 0


def test_back_pressure_still_stops_everything(tmp_path, monkeypatch):
    headless_available(monkeypatch)
    root = lane(tmp_path)
    for i in range(3):
        started(root, "chip-%d" % i, pr=4000 + i, reviewed=None)
    brief(root, "queued", "ready-work")
    r = mde.evaluate(root)
    assert r["blocked_by"] == "back-pressure"
    assert r["next"] is None
    assert r["headless_slots"] == 0


def test_a_lane_conflict_still_stops_everything(tmp_path, monkeypatch):
    headless_available(monkeypatch)
    root = lane(tmp_path)
    brief(root, "queued", "two-places", body="One body.")
    # A DIFFERENT body, deliberately: equal bodies are the D-PM8 self-healing shape
    # (`repairable[]`, which never blocks), and this test is about the shape that does.
    brief(root, "inflight", "two-places", launch="prepared", body="A different body.")
    brief(root, "queued", "ready-work")
    r = mde.evaluate(root)
    assert r["blocked_by"] == "lane-conflict"
    assert r["next"] is None
    assert r["headless_slots"] == 0


# ── caps arithmetic: ≤2 headless per tick ────────────────────────────────────


def test_headless_slots_are_two_per_tick_not_the_whole_build_budget(tmp_path, monkeypatch):
    headless_available(monkeypatch)
    root = lane(tmp_path)
    assert mde.evaluate(root)["slots"] == 6
    assert mde.evaluate(root)["headless_slots"] == 2


def test_each_launch_this_tick_must_be_declared_and_debits_the_ceiling(tmp_path, monkeypatch):
    """A run that launches one and calls again passes `--headless-started 1`. It cannot be
    re-derived: a session THIS tick started and one the last tick started are the same
    bytes on disk."""
    headless_available(monkeypatch)
    root = lane(tmp_path)
    brief(root, "queued", "ready-work")
    assert mde.evaluate(root, headless_started=1)["headless_slots"] == 1
    r = mde.evaluate(root, headless_started=2)
    assert r["headless_slots"] == 0
    assert r["next"] is None, "the third headless start of a tick is not offered"


def test_the_build_cap_binds_headless_slots_when_it_is_tighter(tmp_path, monkeypatch):
    headless_available(monkeypatch)
    root = lane(tmp_path)
    for i in range(5):
        started(root, "chip-%d" % i)
    assert mde.evaluate(root)["headless_slots"] == 1


# ── the prerequisite fallback ────────────────────────────────────────────────


def test_without_the_prerequisites_every_brief_routes_to_a_card(tmp_path):
    """The autouse fixture pins UNAVAILABLE, which is this machine's honest state until
    the operator does the two steps. The lane degrades to prepare-and-tap; it never goes
    dark for want of a grant."""
    root = lane(tmp_path)
    brief(root, "queued", "ready-work")
    r = mde.evaluate(root)
    assert r["next"]["route"] == "prepared"
    assert r["headless"]["available"] is False
    assert r["headless"]["note"] == "headless: unavailable (grant)"


def test_without_the_prerequisites_a_full_tray_blocks_again(tmp_path):
    """Deliberate, and the reason the fallback counts against the prepared cap: a card is
    a card whoever routed it there. If fallback cards escaped the cap, a lane with the
    grant missing would queue one unclicked card per tick forever — the §18 pile-up, in a
    new costume."""
    root = lane(tmp_path)
    prepared(root, "card-one")
    prepared(root, "card-two")
    brief(root, "queued", "ready-work")
    r = mde.evaluate(root)
    assert r["blocked_by"] == "prepared-cap"
    assert r["next"] is None


def test_the_fallback_note_names_which_prerequisite_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mde.mdh, "check_prereqs",
                        lambda **kw: mde.mdh.Prereqs(
                            ok=False, missing=(mde.mdh.MISSING_DISCLAIMER,),
                            detail="never accepted"))
    root = lane(tmp_path)
    text = mde.render_text(mde.evaluate(root))
    assert "headless: unavailable (disclaimer)" in text
    assert "prepare-and-tap" in text


# ── liveness for a session nobody can see ────────────────────────────────────


def test_a_headless_chip_with_no_branch_after_45_minutes_is_reported_stalled(tmp_path):
    root = lane(tmp_path)
    headless(root, "gone-quiet", started_at="2020-01-01T00:00:00Z")
    (live,) = mde.evaluate(root)["headless_liveness"]
    assert live["id"] == "gone-quiet"
    assert live["state"] == "stalled"
    assert live["session"] == "agent_abc123"


def test_a_fresh_headless_chip_is_not_reported_stalled(tmp_path):
    root = lane(tmp_path)
    headless(root, "just-started", started_at=mde._utc_now_iso())
    (live,) = mde.evaluate(root)["headless_liveness"]
    assert live["state"] == "alive"


def test_a_stalled_headless_chip_is_named_in_the_text_report(tmp_path):
    root = lane(tmp_path)
    headless(root, "gone-quiet", started_at="2020-01-01T00:00:00Z")
    text = mde.render_text(mde.evaluate(root))
    assert "STALLED" in text and "gone-quiet" in text and "agent_abc123" in text
    assert "output of record" in text, "the PR is the output of record, never a log file"


def test_a_prepared_card_gets_no_liveness_row(tmp_path):
    """Only headless entries. A tray card is visible in the app and has its own story —
    group (E) — and reporting it here would say a card nobody clicked had died."""
    root = lane(tmp_path)
    prepared(root, "unclicked")
    assert mde.evaluate(root)["headless_liveness"] == []


def test_an_injected_last_commit_switches_to_the_two_hour_window(tmp_path):
    root = lane(tmp_path)
    headless(root, "committing", started_at="2020-01-01T00:00:00Z")
    facts = {"last_commit": {"committing": mde._utc_now_iso()}}
    (live,) = mde.evaluate(root, facts=facts)["headless_liveness"]
    assert live["state"] == "alive", ("a chip that committed a minute ago is alive however "
                                      "long ago it launched")


# ── the checkout the order is computed FROM ──────────────────────────────────
#
# 2026-09-09: the lane's one free build slot went to a brief the PM had retired three and
# a half hours earlier, while the operator's stated priority sat at the head of a queue
# the dispatcher could not see. One cause: this tool sorts `queued/` by `created:` as read
# from the working tree of whatever checkout it runs in, and that tree was 11 commits
# behind `main`. Priority in this lane IS a `created:` edit landed on `main`, and
# retirement IS a deletion there, so both were no-ops in the tree that decided — and the
# summary line named nine facts about the lane and not the one that ordered them.
# `internal/finding-dispatcher-sorts-a-stale-checkout-2026-09-09.md`.
#
# The fixtures below are REAL git checkouts rather than a patched distance: the defect was
# about what a tree actually holds versus what `main` holds, and a stubbed number cannot
# reproduce the brief that is present in one and absent in the other.


def _git(repo, *args):
    import subprocess
    r = subprocess.run(
        ["git", "-c", "user.name=lane-test", "-c", "user.email=lane@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(repo), capture_output=True, text=True)
    assert r.returncode == 0, "git %s failed: %s" % (" ".join(args), r.stderr)
    return r.stdout.strip()


def checkout(tmp_path, name="clone"):
    """A real git checkout with one commit, and no remote-tracking ref yet."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("lane fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def set_base_ref(repo, rev="HEAD", remote="origin", base="main"):
    """Point `refs/remotes/<remote>/<base>` at `rev` — the same ref a fetch would write,
    written locally so the fixture needs no network and this tool needs no fetch grant."""
    _git(repo, "update-ref", "refs/remotes/%s/%s" % (remote, base), _git(repo, "rev-parse", rev))


def test_a_lane_dir_outside_a_checkout_reports_base_unknown_and_still_selects(tmp_path):
    """The generous direction, and the one every other test in this file depends on: a
    lane dir is readable on its own, so a tool that refused to answer without a clone and
    a remote would have traded a real defect for a broken tool."""
    root = lane(tmp_path)
    brief(root, "queued", "ready-chip")
    r = mde.evaluate(root)
    assert r["base"]["known"] is False and r["base"]["behind"] is None
    assert r["blocked_by"] is None and r["next"]["id"] == "ready-chip"
    assert "base: unknown" in mde.render_text(r).splitlines()[1]


def test_a_checkout_level_with_base_reports_plus_zero_and_selects(tmp_path):
    repo = checkout(tmp_path)
    set_base_ref(repo)
    root = lane(repo)
    brief(root, "queued", "ready-chip")
    r = mde.evaluate(root)
    assert r["base"] == dict(r["base"], known=True, behind=0, ref="origin/main")
    assert r["blocked_by"] is None and r["next"]["id"] == "ready-chip"
    assert "base: origin/main +0" in mde.render_text(r).splitlines()[1]


def test_a_checkout_behind_base_selects_nothing_and_names_the_distance(tmp_path):
    """Fail closed. A lane that cannot see current priority selects nothing rather than
    selecting from an ordering it knows is wrong."""
    repo = checkout(tmp_path)
    here = _git(repo, "rev-parse", "HEAD")
    for n in range(3):
        (repo / ("later-%d.md" % n)).write_text("landed on main\n", encoding="utf-8")
        _git(repo, "add", "later-%d.md" % n)
        _git(repo, "commit", "-m", "main moves on %d" % n)
    set_base_ref(repo)
    _git(repo, "reset", "--hard", here)

    root = lane(repo)
    brief(root, "queued", "ready-chip")
    r = mde.evaluate(root)
    assert r["base"]["known"] is True and r["base"]["behind"] == 3
    assert r["blocked_by"] == "stale-checkout"
    assert r["next"] is None and r["dispatchable"] is False
    assert r["headless_slots"] == 0, "a refused run offers no headless ceiling either"

    text = mde.render_text(r)
    assert "base: origin/main -3 (queue order may be stale)" in text.splitlines()[1]
    assert "3 commit(s) behind origin/main" in text
    assert "tools/meta-dispatch-move sync" in text, "the refusal names its remedy"


def test_the_stale_winner_is_not_dispatched_when_the_orderings_disagree(tmp_path):
    """THE REGRESSION. Two briefs whose order in this checkout and on `main` disagree —
    the 2026-09-09 shape exactly: the queue head re-dated on `main` and the other brief
    RETIRED there, neither visible here. The stale sort puts the retired brief first. The
    assertion is that nothing is selected, not that the current head is: this tool cannot
    read `main`, so the only honest answer is a refusal."""
    repo = checkout(tmp_path)
    root = lane(repo)
    # As the stale checkout holds them: the head still carries its pre-re-dating date, and
    # the brief the PM retired is still present and still sorts first.
    brief(root, "queued", "queue-head", created="2026-08-24")
    brief(root, "queued", "retired-on-main", created="2026-08-19")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "the lane, as this checkout holds it")
    stale_head = _git(repo, "rev-parse", "HEAD")

    # What actually landed on `main`: the head re-dated ahead of it, the other retired.
    brief(root, "queued", "queue-head", created="2026-08-17")
    (root / "queued" / "retired-on-main.md").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "re-date the head; retire the other brief")
    set_base_ref(repo)
    _git(repo, "reset", "--hard", stale_head)

    r = mde.evaluate(root)
    assert [e["id"] for e in r["eligible"]] == ["retired-on-main", "queue-head"], (
        "the stale ordering is what this tree computes — that is the premise")
    assert r["next"] is None, "and it is refused, rather than dispatched"
    assert r["blocked_by"] == "stale-checkout"


def test_stale_checkout_outranks_the_lane_conflict_and_every_cap(tmp_path):
    """It is the only condition that says the READ may be wrong rather than the lane —
    the briefs, their dates and their very presence are all as of a tree that has not seen
    what landed — so nothing computed below it is known to be about the lane as it is."""
    repo = checkout(tmp_path)
    here = _git(repo, "rev-parse", "HEAD")
    (repo / "later.md").write_text("landed on main\n", encoding="utf-8")
    _git(repo, "add", "later.md")
    _git(repo, "commit", "-m", "main moves on")
    set_base_ref(repo)
    _git(repo, "reset", "--hard", here)

    root = lane(repo)
    for i, pr in enumerate((1, 2, 3, 4, 5, 6), start=1):
        started(root, "busy-%d" % i, pr=pr, reviewed=None)
    brief(root, "done", "busy-1")
    r = mde.evaluate(root)
    assert r["conflicts"] and r["back_pressure"]["paused"] is True and r["slots"] == 0
    assert r["blocked_by"] == "stale-checkout"


def test_the_base_field_and_the_remote_are_overridable(tmp_path):
    """`--remote` / `--base` already existed for `--lane-state-pr`; the queue mode measures
    its distance from the same pair rather than a second hardcoded one."""
    repo = checkout(tmp_path)
    set_base_ref(repo, remote="upstream", base="trunk")
    root = lane(repo)
    brief(root, "queued", "ready-chip")
    r = mde.evaluate(root, remote="upstream", base="trunk")
    assert r["base"]["ref"] == "upstream/trunk" and r["base"]["behind"] == 0
    unknown = mde.evaluate(root)
    assert unknown["base"]["known"] is False, "origin/main was never fetched here"
    assert unknown["next"]["id"] == "ready-chip", "and unknown still selects"


def test_the_base_check_leaves_the_tool_read_only(tmp_path, capsys):
    """The report is a REPORT. It never pulls, fetches, checks out or writes a ref — the
    fast-forward belongs to `tools/meta-dispatch-move sync`, and a read-only decider that
    quietly moved the operator's HEAD would be a different tool."""
    repo = checkout(tmp_path)
    set_base_ref(repo)
    root = lane(repo)
    brief(root, "queued", "chip-a")
    before = (_git(repo, "rev-parse", "HEAD"),
              _git(repo, "rev-parse", "refs/remotes/origin/main"),
              _git(repo, "status", "--porcelain"),
              _git(repo, "reflog", "--format=%H %gs"))
    assert mde.main(["--dir", str(root), "--json"]) == 0
    capsys.readouterr()
    assert (_git(repo, "rev-parse", "HEAD"),
            _git(repo, "rev-parse", "refs/remotes/origin/main"),
            _git(repo, "status", "--porcelain"),
            _git(repo, "reflog", "--format=%H %gs")) == before


def test_the_base_field_is_on_the_summary_line_of_every_run(tmp_path):
    """Present or absent news, in the same column. An unreported staleness is what let a
    retired brief outrank the queue head for four hours; a field that only appears on the
    tick that has news is a field nobody learns to read."""
    root = lane(tmp_path)
    for state in ("empty queue", "one queued", "one in flight"):
        if state == "one queued":
            brief(root, "queued", "chip-a")
        if state == "one in flight":
            started(root, "chip-b", pr=3771)
        line = mde.render_text(mde.evaluate(root)).splitlines()[1]
        assert line.endswith(" · base: unknown"), "%s: %r" % (state, line)


def test_the_base_state_is_in_the_json_payload(tmp_path, capsys):
    root = lane(tmp_path)
    brief(root, "queued", "chip-a")
    assert mde.main(["--dir", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base"]["known"] is False
    assert payload["base_line"] == "base: unknown"
