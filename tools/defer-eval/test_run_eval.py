"""Mechanical tests for the defer-eval harness.

Covers the scoring + cleanup logic; doesn't invoke openclaw or the LLM.
The actual eval against a real bot is operator-run on the mini.

Run with:
  cd tools/defer-eval && python3 -m pytest test_run_eval.py -v
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import run_eval as r


# ── load_prompts ─────────────────────────────────────────────────────────


def test_load_prompts_returns_all_40_with_expected_categories():
    p = Path(__file__).parent / "prompts.json"
    cases = r.load_prompts(p)
    assert len(cases) == 40
    should = [c for c in cases if c.category == "should_defer"]
    shouldnt = [c for c in cases if c.category == "should_not_defer"]
    assert len(should) == 20
    assert len(shouldnt) == 20
    # IDs are unique
    assert len({c.id for c in cases}) == 40


def test_load_prompts_carries_expected_fields():
    p = Path(__file__).parent / "prompts.json"
    cases = r.load_prompts(p)
    by_id = {c.id: c for c in cases}
    c = by_id["should-defer-001"]
    assert c.expected_called_defer is True
    assert c.expected_mode == "message"
    assert c.expected_due_at_offset_minutes == 5
    assert c.expected_due_at_tolerance_minutes == 1


# ── score_case ───────────────────────────────────────────────────────────


def _case(
    id_: str,
    cat: str,
    called: bool,
    mode: str | None = None,
    offset: float | None = None,
    tol: float | None = None,
) -> r.PromptCase:
    return r.PromptCase(
        id=id_, category=cat, subcategory="t", text=f"prompt {id_}",
        expected_called_defer=called,
        expected_mode=mode,
        expected_due_at_offset_minutes=offset,
        expected_due_at_tolerance_minutes=tol,
    )


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.timezone.utc)


class TestScoreCase:

    def test_should_defer_and_did_passes_overall(self):
        case = _case("should-defer-x", "should_defer", True, mode="message",
                     offset=5, tol=1)
        prompt_at = _now()
        new_row = {
            "defer_id": "abc",
            "mode": "message",
            "fires_at": (prompt_at + dt.timedelta(minutes=5)).isoformat(),
        }
        res, ids = r.score_case(case, set(), [new_row], prompt_at, True, "ok", 100)
        assert res.pass_overall is True
        assert res.actual_called_defer is True
        assert res.pass_called_defer is True
        assert res.pass_mode is True
        assert res.pass_due_at is True
        assert ids == {"abc"}

    def test_should_defer_and_did_not_fails(self):
        case = _case("missed", "should_defer", True)
        res, ids = r.score_case(case, set(), [], _now(), True, "ok", 100)
        assert res.pass_overall is False
        assert res.actual_called_defer is False
        assert ids == set()

    def test_should_not_defer_and_did_not_passes(self):
        case = _case("clean", "should_not_defer", False)
        res, ids = r.score_case(case, set(), [], _now(), True, "ok", 100)
        assert res.pass_overall is True

    def test_should_not_defer_but_did_fails(self):
        case = _case("false-pos", "should_not_defer", False)
        new_row = {"defer_id": "x", "mode": "message", "fires_at": _now().isoformat()}
        res, ids = r.score_case(case, set(), [new_row], _now(), True, "ok", 100)
        assert res.pass_overall is False
        assert res.actual_called_defer is True
        assert ids == {"x"}

    def test_wrong_mode_fails_overall(self):
        case = _case("mode-fail", "should_defer", True, mode="message")
        new_row = {
            "defer_id": "y", "mode": "action",
            "fires_at": _now().isoformat(),
        }
        res, _ = r.score_case(case, set(), [new_row], _now(), True, "ok", 100)
        assert res.pass_overall is False
        assert res.pass_called_defer is True
        assert res.pass_mode is False

    def test_due_at_within_tolerance_passes(self):
        case = _case("time-ok", "should_defer", True, mode="message", offset=5, tol=1)
        prompt_at = _now()
        # 5min 30s — within ±1 minute tolerance
        new_row = {
            "defer_id": "z", "mode": "message",
            "fires_at": (prompt_at + dt.timedelta(minutes=5, seconds=30)).isoformat(),
        }
        res, _ = r.score_case(case, set(), [new_row], prompt_at, True, "ok", 100)
        assert res.pass_due_at is True
        assert res.actual_due_at_offset_minutes == pytest.approx(5.5, abs=0.05)

    def test_due_at_outside_tolerance_fails(self):
        case = _case("time-fail", "should_defer", True, mode="message", offset=5, tol=1)
        prompt_at = _now()
        new_row = {
            "defer_id": "z", "mode": "message",
            "fires_at": (prompt_at + dt.timedelta(minutes=10)).isoformat(),
        }
        res, _ = r.score_case(case, set(), [new_row], prompt_at, True, "ok", 100)
        assert res.pass_due_at is False
        assert res.pass_overall is False

    def test_unparseable_fires_at_marks_due_at_failed(self):
        case = _case("garbage-time", "should_defer", True, mode="message", offset=5, tol=1)
        new_row = {"defer_id": "z", "mode": "message", "fires_at": "not-an-iso"}
        res, _ = r.score_case(case, set(), [new_row], _now(), True, "ok", 100)
        assert res.pass_due_at is False

    def test_no_offset_assertion_means_due_at_unscored(self):
        case = _case("no-time", "should_defer", True, mode="action", offset=None, tol=None)
        new_row = {
            "defer_id": "z", "mode": "action",
            "fires_at": (_now() + dt.timedelta(hours=8)).isoformat(),
        }
        res, _ = r.score_case(case, set(), [new_row], _now(), True, "ok", 100)
        assert res.pass_due_at is True   # unasserted = pass
        assert res.pass_overall is True

    def test_existing_queue_rows_ignored(self):
        """A row that was already in the queue before the prompt is not
        attributed to this prompt — only NEW rows count."""
        case = _case("ignore-old", "should_not_defer", False)
        existing = {"old-id"}
        # New result includes both the old row and (no) new row
        old_row = {"defer_id": "old-id", "mode": "message",
                   "fires_at": _now().isoformat()}
        res, ids = r.score_case(case, existing, [old_row], _now(), True, "ok", 100)
        assert res.actual_called_defer is False
        assert ids == set()

    def test_agent_run_failure_propagates_error(self):
        case = _case("oops", "should_defer", True)
        res, ids = r.score_case(case, set(), [], _now(), False, "agent timed out", 90000)
        assert res.pass_overall is False
        assert res.error == "agent timed out"
        assert ids == set()


# ── remove_rows_by_id ────────────────────────────────────────────────────


@pytest.fixture()
def temp_queue(tmp_path, monkeypatch):
    """Create a temporary per-bot queue file. Patches the path resolvers
    on the run_eval module so tests work without /Users/<bot>/."""
    bot_id = "testbot"
    workspace = tmp_path / "Users" / bot_id / ".openclaw" / "workspace" / "evolve"
    workspace.mkdir(parents=True)
    qp = workspace / "defer-queue.jsonl"
    lp = workspace / "defer-queue.jsonl.lock"

    monkeypatch.setattr(r, "queue_path", lambda b: qp)
    monkeypatch.setattr(r, "lock_path", lambda b: lp)
    return bot_id, qp


class TestRemoveRowsById:

    def test_removes_matching_rows(self, temp_queue):
        bot_id, qp = temp_queue
        rows = [
            {"defer_id": "a", "msg": "keep"},
            {"defer_id": "b", "msg": "remove"},
            {"defer_id": "c", "msg": "keep"},
        ]
        qp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        removed = r.remove_rows_by_id(bot_id, {"b"})
        assert removed == 1
        kept_rows = [json.loads(l) for l in qp.read_text().splitlines() if l.strip()]
        kept_ids = {x["defer_id"] for x in kept_rows}
        assert kept_ids == {"a", "c"}

    def test_removes_multiple_at_once(self, temp_queue):
        bot_id, qp = temp_queue
        rows = [
            {"defer_id": "a", "msg": "x"},
            {"defer_id": "b", "msg": "x"},
            {"defer_id": "c", "msg": "x"},
        ]
        qp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        removed = r.remove_rows_by_id(bot_id, {"a", "c"})
        assert removed == 2
        kept_rows = [json.loads(l) for l in qp.read_text().splitlines() if l.strip()]
        assert {x["defer_id"] for x in kept_rows} == {"b"}

    def test_empty_target_set_is_noop(self, temp_queue):
        bot_id, qp = temp_queue
        original = '{"defer_id":"x"}\n'
        qp.write_text(original)
        removed = r.remove_rows_by_id(bot_id, set())
        assert removed == 0
        assert qp.read_text() == original

    def test_missing_queue_file_is_noop(self, temp_queue):
        bot_id, qp = temp_queue
        # qp does not exist
        removed = r.remove_rows_by_id(bot_id, {"a"})
        assert removed == 0

    def test_preserves_malformed_lines(self, temp_queue):
        """A malformed line in the queue file isn't ours to delete; keep it
        so the operator can investigate."""
        bot_id, qp = temp_queue
        qp.write_text(
            json.dumps({"defer_id": "good"}) + "\n" +
            "this is not json\n" +
            json.dumps({"defer_id": "bad"}) + "\n"
        )
        r.remove_rows_by_id(bot_id, {"bad"})
        text = qp.read_text()
        assert "this is not json" in text
        assert '"defer_id": "good"' in text
        assert '"defer_id": "bad"' not in text


# ── aggregate ────────────────────────────────────────────────────────────


class TestAggregate:

    def test_counts_tp_fn_fp_tn_correctly(self):
        report = r.EvalReport(started_at="t0")
        report.cases = [
            r.CaseResult(
                prompt_id="a", category="should_defer", text="x",
                actual_called_defer=True, agent_run_ok=True,
                pass_overall=True, pass_called_defer=True,
            ),
            r.CaseResult(
                prompt_id="b", category="should_defer", text="x",
                actual_called_defer=False, agent_run_ok=True,
                pass_overall=False, pass_called_defer=False,
            ),
            r.CaseResult(
                prompt_id="c", category="should_not_defer", text="x",
                actual_called_defer=False, agent_run_ok=True,
                pass_overall=True, pass_called_defer=True,
            ),
            r.CaseResult(
                prompt_id="d", category="should_not_defer", text="x",
                actual_called_defer=True, agent_run_ok=True,
                pass_overall=False, pass_called_defer=False,
            ),
        ]
        r.aggregate(report)
        assert report.total == 4
        assert report.should_defer_total == 2
        assert report.should_not_defer_total == 2
        assert report.true_positives == 1
        assert report.false_negatives == 1
        assert report.true_negatives == 1
        assert report.false_positives == 1
        assert report.agent_run_failures == 0

    def test_counts_agent_run_failures(self):
        report = r.EvalReport(started_at="t0")
        report.cases = [
            r.CaseResult(prompt_id="a", category="should_defer", text="x", agent_run_ok=False),
        ]
        r.aggregate(report)
        assert report.agent_run_failures == 1


# ── render_summary smoke test ────────────────────────────────────────────


def test_render_summary_does_not_crash_on_full_report():
    report = r.EvalReport(
        started_at="2026-05-06T00:00:00+00:00",
        finished_at="2026-05-06T00:10:00+00:00",
        bot_id="admin_bot",
        prompt_set="prompts.json",
        total=2,
        should_defer_total=1,
        should_not_defer_total=1,
        true_positives=1,
        true_negatives=1,
        cases=[
            r.CaseResult(
                prompt_id="a", category="should_defer", text="x",
                pass_overall=True, agent_run_ok=True,
            ),
        ],
    )
    out = r.render_summary(report)
    assert "admin_bot" in out
    assert "should-defer" in out
    assert "should-NOT-defer" in out


# ── session-key construction ─────────────────────────────────────────────


class TestSessionKeyFor:

    def test_default_agent(self):
        assert r.session_key_for("main", "defer-eval-admin_bot-20260506") \
            == "agent:main:explicit:defer-eval-admin_bot-20260506"

    def test_non_default_agent(self):
        assert r.session_key_for("email-reader", "test-1") \
            == "agent:email-reader:explicit:test-1"


# ── in-place rewrite preserves file ownership ────────────────────────────


class TestRemoveRowsByIdPreservesOwnership:
    """The legacy tempfile+rename rewrite changed file ownership to the
    invoking user (evolve), which silently broke the bot's defer plugin
    on the next call. The current implementation truncates and rewrites
    in place under flock — same inode, same owner."""

    def test_inode_preserved_after_rewrite(self, tmp_path, monkeypatch):
        bot_id = "testbot"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        qp = workspace / "defer-queue.jsonl"
        lp = workspace / "defer-queue.jsonl.lock"
        rows = [
            {"defer_id": "a"}, {"defer_id": "b"}, {"defer_id": "c"},
        ]
        qp.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
        before_inode = os.stat(qp).st_ino

        monkeypatch.setattr(r, "queue_path", lambda b: qp)
        monkeypatch.setattr(r, "lock_path", lambda b: lp)
        r.remove_rows_by_id(bot_id, {"b"})

        # If the implementation regresses to tempfile+rename, the inode
        # changes (new file, even if name is reused).
        after_inode = os.stat(qp).st_ino
        assert before_inode == after_inode, (
            "queue file inode changed — implementation may have reverted to "
            "tempfile+rename, which would break bot-owned-file invariant"
        )
