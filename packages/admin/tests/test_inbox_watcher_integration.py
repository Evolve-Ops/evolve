"""Tests for upstream_issues_watcher → intake.store.append_activity wiring.

Verifies:
  - When the watcher detects an event matching a filed intake, the
    intake's activity_log gains an ActivityEvent.
  - When no matching intake exists, the watcher does NOT crash and
    does NOT pollute anything — the cross-package miss is silent.
  - Bad event shapes (missing repo, non-numeric number) are noops.
  - The alert dispatch path is independent — an inbox failure doesn't
    block dispatch (covered indirectly by the watcher's try/except).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.intake import store  # noqa: E402
from evolve_admin.intake.envelope import Intake  # noqa: E402

import upstream_issues_watcher as uw  # noqa: E402


def _make_filed_intake(
    *, id_: str = "intake-watcher-test",
    repo: str = "openclaw/openclaw", number: int = 84820,
) -> Intake:
    ix = Intake(id=id_, kind="bug", body="Gateway crashes")
    ix.state = "filed"
    ix.promotion.github_issue_url = f"https://github.com/{repo}/issues/{number}"
    ix.promotion.github_issue_number = number
    return ix


def _event(**overrides) -> dict:
    """Default shape of a watcher event payload, override fields as needed."""
    base = {
        "repo": "openclaw/openclaw",
        "repo_number": "#84820",
        "event_kind": "new_comment",
        "actor": "oc-maintainer",
        "body_short": "Thanks for the report",
        "comment_id": "c12345",
    }
    base.update(overrides)
    return base


# ─── Happy path ───────────────────────────────────────────────────────────


def test_append_activity_matches_filed_intake(tmp_path):
    """A watcher event on a tracked issue should land an ActivityEvent
    on the matching intake."""
    ix = _make_filed_intake()
    store.write_intake(ix, tmp_path)

    uw._append_inbox_activity(shared_dir=tmp_path, event=_event())

    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert len(reloaded.activity_log) == 1
    ev = reloaded.activity_log[0]
    assert ev.kind == "new_comment"
    assert ev.actor == "oc-maintainer"
    assert "Thanks for the report" in ev.snippet
    assert ev.ref == "c12345"


def test_repo_number_with_or_without_hash_both_parse(tmp_path):
    """Watcher emits `#42` strings; some callers may pass `42`. Both
    must work — defensive."""
    ix = _make_filed_intake(number=42)
    store.write_intake(ix, tmp_path)

    uw._append_inbox_activity(
        shared_dir=tmp_path,
        event=_event(repo=ix.promotion.github_issue_url.split("/issues/")[0].split("github.com/")[1],
                     repo_number="42", comment_id="c-with-hash"),
    )
    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert len(reloaded.activity_log) == 1


def test_multiple_events_all_land(tmp_path):
    """Sequential events on the same issue accumulate in the log."""
    ix = _make_filed_intake()
    store.write_intake(ix, tmp_path)

    for i in range(3):
        uw._append_inbox_activity(
            shared_dir=tmp_path,
            event=_event(comment_id=f"c{i}"),
        )

    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert [e.ref for e in reloaded.activity_log] == ["c0", "c1", "c2"]


# ─── Event-kind handling ──────────────────────────────────────────────────


def test_each_recognized_event_kind_persists(tmp_path):
    ix = _make_filed_intake()
    store.write_intake(ix, tmp_path)

    for kind in ("new_comment", "state_change", "closed", "reopened"):
        uw._append_inbox_activity(
            shared_dir=tmp_path,
            event=_event(event_kind=kind, comment_id=f"ref-{kind}"),
        )

    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    kinds_seen = [e.kind for e in reloaded.activity_log]
    assert kinds_seen == ["new_comment", "state_change", "closed", "reopened"]


def test_unknown_event_kind_falls_back_to_new_comment(tmp_path):
    """Watcher's classifier emits kinds — if a new one shows up the
    activity logger should still record it, defaulting to new_comment
    so the operator sees SOMETHING rather than the event being lost."""
    ix = _make_filed_intake()
    store.write_intake(ix, tmp_path)
    uw._append_inbox_activity(
        shared_dir=tmp_path,
        event=_event(event_kind="totally_new_thing"),
    )
    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert reloaded.activity_log[0].kind == "new_comment"


# ─── Miss / defensive cases ───────────────────────────────────────────────


def test_no_matching_intake_is_silent_noop(tmp_path):
    """Activity on an issue we never filed via intake must NOT crash —
    the watcher tracks every issue under the author filter, only some
    of which came from us."""
    # No filed intakes at all.
    uw._append_inbox_activity(
        shared_dir=tmp_path,
        event=_event(repo="someone-else/repo", repo_number="#999"),
    )
    # Nothing to assert other than: no exception, no files written.
    intakes = list(store.iter_intakes(tmp_path))
    assert intakes == []


def test_malformed_event_is_silent_noop(tmp_path):
    """Bad event shapes — empty repo, non-numeric number, missing
    fields — should be noops, not exceptions."""
    ix = _make_filed_intake()
    store.write_intake(ix, tmp_path)
    before_count = len(ix.activity_log)

    uw._append_inbox_activity(shared_dir=tmp_path,
                              event=_event(repo=""))
    uw._append_inbox_activity(shared_dir=tmp_path,
                              event=_event(repo_number="not-a-number"))
    uw._append_inbox_activity(shared_dir=tmp_path,
                              event=_event(repo_number=""))
    uw._append_inbox_activity(shared_dir=tmp_path, event={})

    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert len(reloaded.activity_log) == before_count


def test_unfiled_intake_with_same_number_not_matched(tmp_path):
    """An open intake that hasn't been promoted yet (no github_issue_url)
    must NOT be matched even if a coincidentally-numbered event arrives."""
    open_ix = Intake(id="intake-open", kind="bug", body="not yet filed")
    # state stays "open", no promotion data
    store.write_intake(open_ix, tmp_path)

    uw._append_inbox_activity(
        shared_dir=tmp_path,
        event=_event(repo="openclaw/openclaw", repo_number="#84820"),
    )

    reloaded, _, _ = store.find_intake(tmp_path, "intake-open")
    assert reloaded.activity_log == []
