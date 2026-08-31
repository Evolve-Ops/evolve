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
          pr=None, body="Build the thing.\n\nOpen the PR with `gh pr create`.", extra=""):
    fm = ["---", "id: %s" % ident, "aspect: %s" % aspect,
          "title: %s" % (title or ident.replace("-", " ")),
          "privileged: %s" % ("true" if privileged else "false")]
    if greenlight is not None:
        fm.append("operator_greenlight: %s" % ("true" if greenlight else "false"))
    if depends_on is not None:
        fm.append("depends_on: [%s]" % ", ".join(str(d) for d in depends_on))
    if pr is not None:
        fm.append("pr: %d" % pr)
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


# ── D-PM3: the cap ───────────────────────────────────────────────────────────


def test_cap_respected_at_three_in_flight(tmp_path):
    root = lane(tmp_path)
    for i in range(3):
        brief(root, "inflight", "busy-%d" % i, pr=100 + i)
        review(root, 100 + i, "Verdict: PASS — fine.\n")   # reviewed: no back-pressure
    brief(root, "queued", "waiting")
    r = mde.evaluate(root)
    assert r["inflight_count"] == 3 and r["slots"] == 0
    assert r["blocked_by"] == "cap"
    assert r["next"] is None and r["dispatchable"] is False
    # the entry itself is still eligible — the CAP is the lane's state, not the brief's
    assert [e["id"] for e in r["eligible"]] == ["waiting"]


def test_one_slot_free_dispatches_exactly_one(tmp_path):
    root = lane(tmp_path)
    for i in range(2):
        brief(root, "inflight", "busy-%d" % i, pr=100 + i)
        review(root, 100 + i, "Verdict: PASS — fine.\n")
    brief(root, "queued", "a-first", created="2026-08-01")
    brief(root, "queued", "b-second", created="2026-08-02")
    r = mde.evaluate(root)
    assert r["slots"] == 1
    assert r["next"]["id"] == "a-first"
    assert [e["id"] for e in r["eligible"]] == ["a-first", "b-second"]


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
    assert out["next"]["id"] == "chip-a" and out["cap"] == 3
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
    for i, pr in enumerate((1, 2, 3), start=1):
        brief(root, "inflight", "busy-%d" % i, pr=pr)
    brief(root, "done", "busy-1")
    r = mde.evaluate(root)
    assert r["back_pressure"]["paused"] is True and r["slots"] == 0
    assert r["blocked_by"] == "lane-conflict"


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
