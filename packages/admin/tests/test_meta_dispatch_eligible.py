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
