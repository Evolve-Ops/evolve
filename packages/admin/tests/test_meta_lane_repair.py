"""Unit tests for the PM-lane repair queue and its executor —
`tools/meta-dispatch-move repair` (`internal/decision-pm-operating-model-2026-09-10.md`
D-OM2, operator-ratified).

WHAT THIS FILE IS DEFENDING. Four of the five operator interventions of 2026-09-09 were
lane-state repairs the PM could see precisely and could not perform, because the state
lives where a Cowork session cannot reach: `~/.claude` is invisible from `device_bash`, and
the connected folder forbids `unlink`. The repair queue is the workaround — a committed
request file the PM *can* land, executed by a step in a scheduled task that already has the
access. Its entire value is its narrowness, so the tests that matter most here are the ones
that pin what it REFUSES:

  * an unknown verb, a malformed request, and arguments outside the verb's declared shape
    are each refused with their own reason, and the rest of the queue still runs;
  * a verb spelled like a shell command or carrying a path traversal is refused BEFORE any
    table lookup, with a reason distinct from a plain typo;
  * **a request naming a target that does not exist is refused, never rendered as
    success** — "there was nothing to move, so the move is complete" is the one failure
    mode that reads as done, and a queue that reports it as done both retires the request
    and tells the PM the state was fixed;
  * re-running a completed request changes nothing and says `already-done`.

The tool is an extensionless script under tools/, so it is loaded by path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "meta-dispatch-move"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("meta_dispatch_move_repair", str(_TOOL))
    spec = importlib.util.spec_from_loader("meta_dispatch_move_repair", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_dispatch_move_repair"] = mod
    loader.exec_module(mod)
    return mod


MOD = _load_tool()


BRIEF = """---
id: {id}
aspect: apps
title: "A brief"
privileged: false
created: 2026-08-23
pm: fable-cowork
launch: {launch}
---
WHY: pinned by tests.
"""


def _lane(tmp_path: Path) -> Path:
    root = tmp_path / "dispatch"
    for sub in ("queued", "inflight", "done", "repairs"):
        (root / sub).mkdir(parents=True)
    return root


def _entry(root: Path, brief_id: str, *, state: str = "inflight",
           launch: str = "prepared", extra: str = "") -> Path:
    p = root / state / ("%s.md" % brief_id)
    text = BRIEF.format(id=brief_id, launch=launch)
    if extra:
        text = text.replace("---\nWHY:", "%s\n---\nWHY:" % extra)
    p.write_text(text)
    return p


def _request(root: Path, req_id: str, verb: str, args=None, **over) -> Path:
    obj = {"verb": verb, "requested_by": "tick-2026-09-10T14:00:00Z",
           "requested_at": "2026-09-10T14:03:11Z",
           "why": "pinned by tests"}
    if args is not None:
        obj["args"] = args
    obj.update(over)
    p = root / "repairs" / ("%s.json" % req_id)
    p.write_text(json.dumps(obj, indent=1) + "\n")
    return p


def _ledger(tmp_path: Path, chips) -> Path:
    """A meta-state ledger dir holding one aspect file with `chips`."""
    d = tmp_path / "meta-state"
    d.mkdir(exist_ok=True)
    (d / "apps.json").write_text(json.dumps(
        {"aspect": "apps", "updated": "2026-09-10", "chips": chips}, indent=1) + "\n")
    return d


def _outcome(root: Path, req_id: str) -> dict:
    return json.loads((root / "repairs" / "done" / ("%s.json" % req_id)).read_text())


def _by_id(report: dict) -> dict:
    return {r["id"]: r for r in report["results"]}


# ── the queue schema ─────────────────────────────────────────────────────────


def test_the_request_schema_is_the_five_fields_and_a_closed_arg_shape(tmp_path):
    """The schema, pinned: `verb` + `args` + `requested_by` + `requested_at` + `why`.

    `requested_by` is the field that makes the audit run in BOTH directions (D-OM2's last
    clause) — a repair with nothing to attribute it to is a repair that happened quietly,
    which is the same failure as one nobody could perform.
    """
    root = _lane(tmp_path)
    path = _request(root, "r-1", "sync-checkout", {})
    req = MOD.repair_load(path)
    assert req == {"id": "r-1", "path": path, "verb": "sync-checkout", "args": {},
                   "requested_by": "tick-2026-09-10T14:00:00Z",
                   "requested_at": "2026-09-10T14:03:11Z", "why": "pinned by tests"}

    assert sorted(MOD.REPAIR_VERBS) == ["abandon-entry", "dismiss-card",
                                        "mark-row-terminal", "sync-checkout"]
    assert MOD.REPAIR_REQUIRED_FIELDS == ("verb", "requested_by", "requested_at", "why")
    assert MOD.REPAIR_VERBS["dismiss-card"] == {"required": ("id",), "optional": ()}
    assert MOD.REPAIR_VERBS["mark-row-terminal"] == {"required": ("id", "bucket"),
                                                     "optional": ()}
    assert MOD.REPAIR_VERBS["abandon-entry"] == {"required": ("id",), "optional": ("pr",)}
    assert MOD.REPAIR_VERBS["sync-checkout"] == {"required": (), "optional": ()}


def test_there_is_no_fifth_verb_and_no_generic_escape_hatch(tmp_path):
    """The guardrail, stated as a test: a generic "run this" verb would make a committed
    file into a remote shell on the operator's machine with extra steps."""
    assert set(MOD.REPAIR_EXECUTORS) == set(MOD.REPAIR_VERBS)
    assert len(MOD.REPAIR_VERBS) == 4
    for name in ("run", "bash", "exec", "shell", "eval", "apply", "any"):
        assert name not in MOD.REPAIR_VERBS


def test_a_declared_id_must_equal_the_filename_stem(tmp_path):
    root = _lane(tmp_path)
    good = _request(root, "r-ok", "sync-checkout", {}, id="r-ok")
    assert MOD.repair_load(good)["id"] == "r-ok"
    bad = _request(root, "r-bad", "sync-checkout", {}, id="something-else")
    with pytest.raises(MOD.RepairRefused) as e:
        MOD.repair_load(bad)
    assert e.value.reason == "malformed"


# ── the four verbs, each performing its effect and recording an outcome ───────


def test_dismiss_card_frees_the_prepared_cap_slot_and_retires_the_row(tmp_path):
    """Both halves, because only doing the ledger half leaves the slot spent.

    A prepared card is charged to the prepared cap by its `inflight/` entry (D-PM6 —
    `meta-dispatch-eligible` partitions `inflight/` on `in_motion`), so retiring only the
    ledger row leaves the lane holding a slot for a card nobody will ever click. That is
    the accounting error that sat the lane on `blocked_by: prepared-cap` for most of
    2026-09-06.
    """
    root = _lane(tmp_path)
    src = _entry(root, "stale-card", launch="prepared")
    led = _ledger(tmp_path, [{"id": "stale-card", "bucket": "click-pending",
                              "task_id": "task_abc"}])
    _request(root, "r-dismiss", "dismiss-card", {"id": "stale-card"})

    report = MOD.repair_run(root, ledger_dir=str(led))
    rec = _by_id(report)["r-dismiss"]
    assert rec["ok"] and rec["reason"] == "ok", rec

    # the prepared-cap slot: the inflight entry is gone, the brief survives in done/
    assert not src.exists()
    done_text = (root / "done" / "stale-card.md").read_text()
    assert "outcome: dismissed" in done_text
    assert "WHY: pinned by tests." in done_text
    # the tray/ledger side: the row is terminal, so group (E) stops inviting a tap
    row = json.loads((led / "apps.json").read_text())["chips"][0]
    assert row["bucket"] == "dismissed"

    out = _outcome(root, "r-dismiss")
    assert out["verb"] == "dismiss-card"
    assert out["requested_by"] == "tick-2026-09-10T14:00:00Z"
    assert any("bucket" in c for c in out["changed"])
    assert any("done/stale-card.md" in c for c in out["changed"])
    assert any("apps.json" in r for r in out["read"])


def test_dismiss_card_refuses_an_entry_that_is_already_in_motion(tmp_path):
    """A `launch: started` entry is a running chip, not a card. Dismissing it would
    discard a live session's record, and that is a judgement call."""
    root = _lane(tmp_path)
    src = _entry(root, "running-chip", launch="started", extra="branch: claude/x")
    _request(root, "r-nope", "dismiss-card", {"id": "running-chip"})

    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    rec = _by_id(report)["r-nope"]
    assert not rec["ok"] and rec["reason"] == "refused"
    assert "in motion" in rec["message"]
    assert src.exists() and not (root / "done" / "running-chip.md").exists()


def test_dismiss_card_that_cannot_move_the_entry_leaves_the_ledger_row_alone(tmp_path):
    """A refusal must change NOTHING — not even the half that would have succeeded.

    Review pr-4194 H1. `_terminal_move` has four realistic refusal paths and the likeliest
    is the one pinned here: `done/<id>.md` already exists, which is exactly the state a
    stale local marker for an already-merged chip is in — this verb's first customer. With
    the ledger written first, that refusal left the row flipped to `dismissed`, the record
    saying `changed: []  reason: refused`, and the outcome file marking it terminal so it
    never retried. The module promises it is never partially applied; this is the test that
    holds it to that.
    """
    root = _lane(tmp_path)
    src = _entry(root, "stale-card", launch="prepared")
    # the collision: the brief is already at done/ (its chip merged and the durable entry
    # landed upstream), and only the local inflight/ marker is left over
    (root / "done" / "stale-card.md").write_text(BRIEF.format(id="stale-card",
                                                              launch="prepared"))
    led = _ledger(tmp_path, [{"id": "stale-card", "bucket": "click-pending",
                              "task_id": "task_abc"}])
    _request(root, "r-collide", "dismiss-card", {"id": "stale-card"})

    report = MOD.repair_run(root, ledger_dir=str(led))
    rec = _by_id(report)["r-collide"]
    assert not rec["ok"] and rec["reason"] == "refused", rec
    assert rec["changed"] == []

    # NOTHING moved and NOTHING was written: the marker is still there, and the row still
    # reads `click-pending` so a later, successful repair can still do both halves.
    assert src.exists()
    row = json.loads((led / "apps.json").read_text())["chips"][0]
    assert row["bucket"] == "click-pending"


def test_mark_row_terminal_stops_a_row_gating_its_own_brief(tmp_path):
    """The 2026-09-09 instance: a `returned_to_queue` row with no PR and no branch counted
    as work in flight indefinitely (R4 — a recorded condition with no expiry becomes
    permanent by accident). Nothing about it was hard; it lived in `~/.claude`."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "gating-row", "bucket": "returned_to_queue"}])
    _request(root, "r-term", "mark-row-terminal",
             {"id": "gating-row", "bucket": "closed_superseded"})

    report = MOD.repair_run(root, ledger_dir=str(led))
    rec = _by_id(report)["r-term"]
    assert rec["ok"] and rec["reason"] == "ok", rec
    row = json.loads((led / "apps.json").read_text())["chips"][0]
    assert row["bucket"] == "closed_superseded"
    # and it now reads as terminal through the ONE canonical predicate
    assert MOD.is_terminal_bucket(row["bucket"])
    assert _outcome(root, "r-term")["changed"]


def test_mark_row_terminal_refuses_a_bucket_that_would_claim_a_ship(tmp_path):
    """`merged` / `live` / `done` are terminal too and are NOT allowed: they assert that
    work shipped, and an automated repair asserting a ship is a false entry in the record
    the throughput accounting reads."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "gating-row", "bucket": "returned_to_queue"}])
    for bucket in ("merged", "live", "done"):
        assert MOD.is_terminal_bucket(bucket)
        assert bucket not in MOD.REPAIR_TERMINAL_BUCKETS
    _request(root, "r-ship", "mark-row-terminal",
             {"id": "gating-row", "bucket": "merged"})

    report = MOD.repair_run(root, ledger_dir=str(led))
    rec = _by_id(report)["r-ship"]
    assert not rec["ok"] and rec["reason"] == "bad-args"
    assert json.loads((led / "apps.json").read_text())["chips"][0][
        "bucket"] == "returned_to_queue"


def test_abandon_entry_delegates_to_the_existing_lane_verb(tmp_path):
    """Delegated, not reimplemented: `abandon` already performs the transition as
    copy-verify-delete, and a second hand-written copy of that order is the 2026-08-25
    destruction of `alpha-7-price-from-catalog.md` with a different verb name on it."""
    root = _lane(tmp_path)
    src = _entry(root, "dropped-chip", launch="started")
    _request(root, "r-ab", "abandon-entry", {"id": "dropped-chip", "pr": 3900})

    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    rec = _by_id(report)["r-ab"]
    assert rec["ok"] and rec["reason"] == "ok", rec
    assert not src.exists()
    text = (root / "done" / "dropped-chip.md").read_text()
    assert "outcome: abandoned" in text and "pr: 3900" in text
    assert "WHY: pinned by tests." in text
    assert _outcome(root, "r-ab")["detail"]["method"].startswith("copy-verify")


def test_sync_checkout_fast_forwards_and_records_the_move(tmp_path):
    """A real `--ff-only` round trip, because the preconditions and the pull are the
    behaviour under test — stubbing git would pin the stub."""
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(seed), "config", k, v], check=True)
    (seed / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "one"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
    behind = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()

    (seed / "a.txt").write_text("two\n")
    subprocess.run(["git", "-C", str(seed), "commit", "-qam", "two"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

    root = _lane(work)
    _request(root, "r-sync", "sync-checkout", {})
    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    rec = _by_id(report)["r-sync"]
    assert rec["ok"] and rec["reason"] == "ok", rec

    after = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    assert after != behind
    assert _outcome(root, "r-sync")["detail"]["pulled"] is True


def test_sync_checkout_defers_rather_than_completing_when_a_precondition_fails(tmp_path):
    """`deferred`, not `refused` and never `ok`: "this checkout is somebody's working tree
    right now" is a fact about this minute, not about the request. NO outcome file is
    written, so the next tick retries — marking it done would retire the very request that
    exists because the checkout keeps falling behind."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "someones-branch"],
                   check=True)

    root = _lane(repo)
    _request(root, "r-sync", "sync-checkout", {})
    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    rec = _by_id(report)["r-sync"]
    assert not rec["ok"] and rec["reason"] == "deferred"
    assert rec["outcome_file"] is None
    assert not (root / "repairs" / "done" / "r-sync.json").exists()
    assert report["counts"]["deferred"] == 1


# ── refuse, never interpret ──────────────────────────────────────────────────


def test_an_unknown_verb_is_refused_recorded_and_does_not_stop_the_queue(tmp_path):
    """The queue continues past a refusal, and the refusal is recorded with its reason.

    Stopping the queue on the first bad request would let one malformed file the PM landed
    block every repair behind it — the same shape as R4, a gate with no expiry.
    """
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "gating-row", "bucket": "returned_to_queue"}])
    # oldest first: the bad request is FIRST, so a stop would swallow the good one
    _request(root, "r-bad", "reticulate-splines", {"id": "gating-row"},
             requested_at="2026-09-10T10:00:00Z")
    _request(root, "r-good", "mark-row-terminal",
             {"id": "gating-row", "bucket": "dismissed"},
             requested_at="2026-09-10T11:00:00Z")

    report = MOD.repair_run(root, ledger_dir=str(led))
    recs = _by_id(report)
    assert [r["id"] for r in report["results"]] == ["r-bad", "r-good"]
    assert recs["r-bad"]["reason"] == "unknown-verb" and not recs["r-bad"]["ok"]
    assert recs["r-good"]["ok"] and recs["r-good"]["reason"] == "ok"
    assert _outcome(root, "r-bad")["reason"] == "unknown-verb"
    assert json.loads((led / "apps.json").read_text())["chips"][0][
        "bucket"] == "dismissed"
    assert report["counts"] == {"executed": 1, "refused": 1, "deferred": 0,
                                "already_done": 0, "dry_run": 0}


@pytest.mark.parametrize("payload,reason", [
    ('{"requested_by": "t", "requested_at": "z", "why": "w"}', "malformed"),
    ('{"verb": "sync-checkout", "requested_at": "z", "why": "w"}', "malformed"),
    ('{"verb": "sync-checkout", "requested_by": "t", "why": "w"}', "malformed"),
    ('{"verb": "sync-checkout", "requested_by": "t", "requested_at": "z"}', "malformed"),
    ('{"verb": 7, "requested_by": "t", "requested_at": "z", "why": "w"}', "malformed"),
    ('["not", "an", "object"]', "malformed"),
    ('not json at all', "malformed"),
])
def test_a_malformed_request_is_refused_with_a_reason(tmp_path, payload, reason):
    root = _lane(tmp_path)
    (root / "repairs" / "r-mal.json").write_text(payload)
    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    rec = _by_id(report)["r-mal"]
    assert not rec["ok"] and rec["reason"] == reason, rec
    assert _outcome(root, "r-mal")["reason"] == reason


@pytest.mark.parametrize("verb,args,reason", [
    ("dismiss-card", {}, "bad-args"),
    ("dismiss-card", {"id": "x", "force": True}, "bad-args"),
    ("mark-row-terminal", {"id": "x"}, "bad-args"),
    ("mark-row-terminal", {"id": "x", "bucket": "dispatched"}, "bad-args"),
    ("sync-checkout", {"remote": "elsewhere"}, "bad-args"),
    ("abandon-entry", {"id": "x", "pr": "3900"}, "bad-args"),
    ("abandon-entry", {"id": "Not Kebab"}, "bad-args"),
])
def test_arguments_outside_the_declared_shape_are_refused(tmp_path, verb, args, reason):
    """An argument the executor silently dropped would be a request the PM believes it
    made — so an unexpected key is a refusal, not an ignore."""
    root = _lane(tmp_path)
    _request(root, "r-args", verb, args)
    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    rec = _by_id(report)["r-args"]
    assert not rec["ok"] and rec["reason"] == reason, rec


@pytest.mark.parametrize("verb", [
    "sync-checkout; rm -rf /",
    "sync-checkout && echo pwned",
    "../../../etc/passwd",
    "../abandon-entry",
    "dismiss-card/../sync-checkout",
    "$(whoami)",
    "sync checkout",
    "SYNC-CHECKOUT",
])
def test_a_verb_spelled_like_a_command_or_a_path_is_refused_before_any_lookup(tmp_path,
                                                                             verb):
    """`unsafe-verb`, a DIFFERENT reason from `unknown-verb` on purpose. "the PM typoed a
    verb" and "something tried to smuggle a command through the queue" are different
    findings, and a refusal that means "I found something" must not be readable as one
    that means "not my business" (`feedback_distinguish_tooling_failure_from_findings`)."""
    root = _lane(tmp_path)
    _request(root, "r-unsafe", verb, {})
    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    rec = _by_id(report)["r-unsafe"]
    assert not rec["ok"] and rec["reason"] == "unsafe-verb", rec
    assert _outcome(root, "r-unsafe")["reason"] == "unsafe-verb"


def test_an_unsafe_verb_is_named_in_the_outcome_but_never_echoed_into_the_line(tmp_path):
    """The line is pasted verbatim into a tick report and a poke, so a rejected value
    spelled like a shell command must not reach an operator-facing string as if it were a
    verb this lane knows. The quoted repr survives in the outcome file, which is where a
    human looks to fix the request."""
    root = _lane(tmp_path)
    _request(root, "r-unsafe", "sync-checkout; rm -rf /", {})
    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    assert "rm -rf" not in report["line"]
    assert "r-unsafe ? REFUSED:unsafe-verb" in report["line"]
    assert "rm -rf" in _outcome(root, "r-unsafe")["message"]


@pytest.mark.parametrize("verb,args", [
    ("dismiss-card", {"id": "never-existed"}),
    ("abandon-entry", {"id": "never-existed"}),
    ("mark-row-terminal", {"id": "never-existed", "bucket": "dismissed"}),
])
def test_a_target_that_does_not_exist_is_refused_not_reported_as_done(tmp_path, verb,
                                                                     args):
    """THE ANTI-VACUITY CASE. "Nothing to do" must never render as "done".

    An absent target is the one failure a naive executor reports as success — there was
    nothing to move, so the move is complete — and a queue that says `done` when it found
    nothing both retires the request AND tells the PM the state was fixed. So it is its
    own reason, `no-such-target`, and `ok` is false.
    """
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "some-other-chip", "bucket": "dispatched"}])
    _request(root, "r-ghost", verb, args)
    report = MOD.repair_run(root, ledger_dir=str(led))
    rec = _by_id(report)["r-ghost"]
    assert not rec["ok"], rec
    assert rec["reason"] == "no-such-target", rec
    assert rec["changed"] == []
    out = _outcome(root, "r-ghost")
    assert out["ok"] is False and out["reason"] == "no-such-target"
    assert "REFUSED:no-such-target" in report["line"]


def test_a_refusal_never_touches_the_pod_the_grants_or_any_code(tmp_path):
    """Stated as a test of the surface: every executor is one of four, and the only paths
    a run writes are the lane dir and the meta-state ledger."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "stale-card", "bucket": "click-pending"}])
    _entry(root, "stale-card")
    _request(root, "r-dismiss", "dismiss-card", {"id": "stale-card"})
    report = MOD.repair_run(root, ledger_dir=str(led))
    written = [p for r in report["results"] for p in r["changed"]]
    assert written
    for path in written:
        assert str(root) in path or str(led) in path, path


# ── idempotency ──────────────────────────────────────────────────────────────


def test_re_running_a_completed_request_changes_nothing_and_says_already_done(tmp_path):
    """The request file lives on `main` and the executor cannot commit, so it never
    deletes one — the outcome file's presence is the idempotency anchor."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "gating-row", "bucket": "returned_to_queue"}])
    _request(root, "r-term", "mark-row-terminal",
             {"id": "gating-row", "bucket": "dismissed"})

    first = MOD.repair_run(root, ledger_dir=str(led))
    assert _by_id(first)["r-term"]["reason"] == "ok"
    outcome_before = (root / "repairs" / "done" / "r-term.json").read_text()

    # someone re-opens the row by hand; a re-run must NOT re-close it — the request is done
    data = json.loads((led / "apps.json").read_text())
    data["chips"][0]["bucket"] = "returned_to_queue"
    (led / "apps.json").write_text(json.dumps(data, indent=1) + "\n")

    second = MOD.repair_run(root, ledger_dir=str(led))
    rec = _by_id(second)["r-term"]
    assert rec["ok"] and rec["reason"] == "already-done"
    assert rec["changed"] == []
    assert json.loads((led / "apps.json").read_text())["chips"][0][
        "bucket"] == "returned_to_queue"
    assert (root / "repairs" / "done" / "r-term.json").read_text() == outcome_before
    assert second["counts"] == {"executed": 0, "refused": 0, "deferred": 0,
                                "already_done": 1, "dry_run": 0}


def test_an_already_terminal_row_is_no_change_rather_than_a_rewrite(tmp_path):
    """The second layer of idempotency, under the outcome file: the verb itself reports
    "nothing to do" honestly instead of restating a value that is already right."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "settled-row", "bucket": "merged"}])
    _request(root, "r-noop", "mark-row-terminal",
             {"id": "settled-row", "bucket": "dismissed"})
    report = MOD.repair_run(root, ledger_dir=str(led))
    rec = _by_id(report)["r-noop"]
    assert rec["ok"] and rec["reason"] == "ok"
    assert rec["changed"] == []
    assert rec["detail"]["already_terminal"] is True
    assert json.loads((led / "apps.json").read_text())["chips"][0]["bucket"] == "merged"


# ── reporting, both ways ─────────────────────────────────────────────────────


def test_every_execution_and_every_refusal_names_the_requesting_tick(tmp_path):
    """D-OM2's last clause. The PM reads the line to learn its repair ran; the operator
    reads it to learn something changed his lane state and on whose word. A count serves
    neither, so each item is named with its `requested_by`."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "gating-row", "bucket": "returned_to_queue"}])
    _request(root, "r-ok", "mark-row-terminal",
             {"id": "gating-row", "bucket": "dismissed"},
             requested_by="tick-A", requested_at="2026-09-10T10:00:00Z")
    _request(root, "r-no", "not-a-verb", {}, requested_by="tick-B",
             requested_at="2026-09-10T11:00:00Z")

    line = MOD.repair_run(root, ledger_dir=str(led))["line"]
    assert "r-ok mark-row-terminal ok (req tick-A)" in line
    assert "r-no not-a-verb REFUSED:unknown-verb (req tick-B)" in line


def test_the_line_is_a_standing_line_even_on_an_empty_queue(tmp_path):
    """Same reason as step 7b's waiting line: a standing line costs nothing, and its
    absence is indistinguishable from a step that never ran."""
    root = _lane(tmp_path)
    report = MOD.repair_run(root, ledger_dir=str(_ledger(tmp_path, [])))
    assert report["results"] == []
    assert report["line"] == "repair: queue empty"


def test_the_queue_is_executed_oldest_first(tmp_path):
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [])
    for req_id, stamp in (("r-third", "2026-09-10T12:00:00Z"),
                          ("r-first", "2026-09-10T10:00:00Z"),
                          ("r-second", "2026-09-10T11:00:00Z")):
        _request(root, req_id, "sync-checkout", {}, requested_at=stamp)
    report = MOD.repair_run(root, ledger_dir=str(led))
    assert [r["id"] for r in report["results"]] == ["r-first", "r-second", "r-third"]


def test_dry_run_validates_and_writes_nothing(tmp_path):
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "gating-row", "bucket": "returned_to_queue"}])
    _request(root, "r-term", "mark-row-terminal",
             {"id": "gating-row", "bucket": "dismissed"})
    report = MOD.repair_run(root, ledger_dir=str(led), dry_run=True)
    assert _by_id(report)["r-term"]["reason"] == "dry-run"
    # a validation is NOT an execution: a report that counted it as one would say a
    # repair happened
    assert report["counts"] == {"executed": 0, "refused": 0, "deferred": 0,
                                "already_done": 0, "dry_run": 1}
    assert not (root / "repairs" / "done").exists()
    assert json.loads((led / "apps.json").read_text())["chips"][0][
        "bucket"] == "returned_to_queue"


# ── the CLI surface ──────────────────────────────────────────────────────────


def test_the_cli_verb_runs_the_queue_and_prints_the_line(tmp_path, monkeypatch):
    """It rides `meta-dispatch-move`'s existing grant rather than needing a new one —
    widening a grant to install a repair path is the one thing this path must not do."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "gating-row", "bucket": "returned_to_queue"}])
    monkeypatch.setattr(MOD, "resolve_ledger_dir", lambda _d=None: str(led))
    _request(root, "r-term", "mark-row-terminal",
             {"id": "gating-row", "bucket": "dismissed"})
    assert MOD.main(["repair", "--dir", str(root)]) == 0
    assert json.loads((led / "apps.json").read_text())["chips"][0][
        "bucket"] == "dismissed"


def test_the_cli_limit_bounds_one_run(tmp_path, monkeypatch):
    root = _lane(tmp_path)
    led = _ledger(tmp_path, [{"id": "a-row", "bucket": "returned_to_queue"},
                             {"id": "b-row", "bucket": "returned_to_queue"}])
    monkeypatch.setattr(MOD, "resolve_ledger_dir", lambda _d=None: str(led))
    _request(root, "r-a", "mark-row-terminal", {"id": "a-row", "bucket": "dismissed"},
             requested_at="2026-09-10T10:00:00Z")
    _request(root, "r-b", "mark-row-terminal", {"id": "b-row", "bucket": "dismissed"},
             requested_at="2026-09-10T11:00:00Z")
    report = MOD.repair_run(root, ledger_dir=str(led), limit=1)
    assert [r["id"] for r in report["results"]] == ["r-a"]
    rows = {c["id"]: c["bucket"] for c in
            json.loads((led / "apps.json").read_text())["chips"]}
    assert rows == {"a-row": "dismissed", "b-row": "returned_to_queue"}
