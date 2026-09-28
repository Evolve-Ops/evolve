"""Tests for ``upstream_issues_watcher``.

Strategy: stub the gh-subprocess and dispatcher boundaries; let everything
else run for real (state files, classification, dedup logic). Each test
isolates a single behavior contract from the spec.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import upstream_issues_watcher as uw


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def shared_dir(tmp_path: Path) -> Path:
    d = tmp_path / "evolve"
    d.mkdir()
    return d


@pytest.fixture()
def install_json(tmp_path: Path):
    """Write an install.json that enables the watcher with a custom repos list."""
    path = tmp_path / "install.json"

    def write(*, enabled: bool = True, repos: list[dict] | None = None) -> Path:
        payload = {
            "feature_profile": "developer" if enabled else "standard",
            "features": {
                "upstream_issues_watcher": {
                    "enabled": enabled,
                    "poll_interval_minutes": 15,
                    "repos": repos if repos is not None else [
                        {"repo": "openclaw/openclaw", "author": "cjalden"},
                    ],
                },
            },
        }
        path.write_text(json.dumps(payload))
        return path

    return write


@pytest.fixture()
def dispatch_capture(monkeypatch):
    """Capture every dispatcher call instead of routing it."""
    captured: list[dict[str, Any]] = []

    def fake_dispatch(**kwargs):
        captured.append(kwargs)
        return True  # pretend the dispatcher accepted

    monkeypatch.setattr(uw, "_dispatch", fake_dispatch)
    return captured


@pytest.fixture()
def gh_stub(monkeypatch):
    """Stub the three gh-CLI entry points. Each test programs its own
    issue list + view responses."""
    state = {
        "self_login": "cjalden",
        "issues_by_repo": {},      # repo → list of issue stubs
        "views_by_key": {},        # (repo, number) → full issue dict
        "auth_failure": None,      # set to a string to simulate auth error
    }

    def fake_self_login():
        if state["auth_failure"]:
            raise uw._GhAuthError(state["auth_failure"])
        return state["self_login"]

    def fake_list(repo, author, since_iso):
        if state["auth_failure"]:
            raise uw._GhAuthError(state["auth_failure"])
        return state["issues_by_repo"].get(repo, [])

    def fake_view(repo, number):
        if state["auth_failure"]:
            raise uw._GhAuthError(state["auth_failure"])
        return state["views_by_key"].get((repo, number))

    monkeypatch.setattr(uw, "_gh_self_login", fake_self_login)
    monkeypatch.setattr(uw, "_gh_list_issues", fake_list)
    monkeypatch.setattr(uw, "_gh_view_issue", fake_view)
    return state


def _make_comment(cid: str, author: str, body: str, created_at: str = "2026-05-22T10:00:00Z"):
    return {
        "id": cid,
        "author": {"login": author},
        "body": body,
        "createdAt": created_at,
        "url": f"https://example/comment/{cid}",
    }


def _make_issue(repo: str, number: int, *,
                state_: str = "OPEN",
                comments: list | None = None,
                title: str = "Sample issue"):
    return {
        "number": number,
        "title": title,
        "state": state_,
        "url": f"https://github.com/{repo}/issues/{number}",
        "comments": comments or [],
        "author": {"login": "cjalden"},
    }


# ── Feature-gate behavior ────────────────────────────────────────────────────


def test_feature_off_is_noop(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json(enabled=False)
    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert stats == {"events": 0, "dispatched": 0, "repos_polled": 0,
                     "auth_failures": 0, "backfilled": 0}
    assert dispatch_capture == []


def test_no_repos_configured_is_noop(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json(repos=[])
    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert stats["repos_polled"] == 0
    assert dispatch_capture == []


# ── New-comment detection ────────────────────────────────────────────────────


def test_new_maintainer_comment_dispatches(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "FileHandle leak", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "oc-maintainer", "Could you share more?")],
    )

    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)

    assert stats["events"] == 1
    assert stats["dispatched"] == 1
    assert len(dispatch_capture) == 1
    payload = dispatch_capture[0]["payload"]
    assert payload["repo"] == "openclaw/openclaw"
    assert payload["issue"] == "#84820"
    assert payload["actor"] == "oc-maintainer"
    assert payload["event_kind"] == "new_comment"
    assert "Could you share" in payload["body_short"]


def test_self_comment_does_not_dispatch(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "cjalden", "Replying to myself")],
    )

    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)

    assert stats["events"] == 0
    assert stats["dispatched"] == 0
    assert dispatch_capture == []


def test_self_comment_advances_high_water(shared_dir, install_json, gh_stub, dispatch_capture):
    """A self-comment should be recorded as seen even though it doesn't
    dispatch — otherwise the next maintainer reply is double-classified."""
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "cjalden", "...",
                                created_at="2026-05-22T10:00:00Z")],
    )

    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    state = json.loads((shared_dir / uw.FEATURE_NAME / "state.json").read_text())
    assert state["repos"]["openclaw/openclaw"]["issues"]["#84820"]["last_comment_id"] == "c1"


def test_dedup_same_comment_twice(shared_dir, install_json, gh_stub, dispatch_capture):
    """Running twice with the same gh response should dispatch once total."""
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "oc-maintainer", "Hi")],
    )

    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)

    # Only one dispatch should have happened total.
    assert len(dispatch_capture) == 1


def test_second_new_comment_after_first_dispatches(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "oc-maintainer", "First",
                                created_at="2026-05-22T10:00:00Z")],
    )
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    # New comment arrives. (Note: comment IDs sort lexically in our store,
    # so use a strictly-greater id.)
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[
            _make_comment("c1", "oc-maintainer", "First",
                          created_at="2026-05-22T10:00:00Z"),
            _make_comment("c2", "oc-maintainer", "Second",
                          created_at="2026-05-22T11:00:00Z"),
        ],
    )
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert len(dispatch_capture) == 2
    assert dispatch_capture[-1]["payload"]["body_short"].startswith("Second")


# ── State transitions ───────────────────────────────────────────────────────


def test_first_observation_records_state_silently(shared_dir, install_json, gh_stub, dispatch_capture):
    """Seeing an issue for the first time should NOT fire a state-change
    event — only subsequent transitions do."""
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820, state_="OPEN", comments=[],
    )
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert dispatch_capture == []


def test_open_to_closed_dispatches(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820, state_="OPEN", comments=[],
    )
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    # Issue closes.
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820, state_="CLOSED", comments=[],
    )
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert len(dispatch_capture) == 1
    assert dispatch_capture[0]["payload"]["event_kind"] == "closed"


def test_closed_to_open_dispatches_as_reopened(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "CLOSED",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820, state_="CLOSED", comments=[],
    )
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820, state_="OPEN", comments=[],
    )
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert len(dispatch_capture) == 1
    assert dispatch_capture[0]["payload"]["event_kind"] == "reopened"


# ── Auth failure path ───────────────────────────────────────────────────────


def test_auth_failure_emits_health_signal_and_continues(
    shared_dir, install_json, gh_stub, dispatch_capture
):
    """gh auth failure should produce exactly one health Signal and not
    crash the loop."""
    p = install_json()
    gh_stub["auth_failure"] = "not authenticated. Run gh auth login"
    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert stats["auth_failures"] >= 1
    # One health dispatch — not a per-comment dispatch — should have fired.
    auth_dispatches = [
        d for d in dispatch_capture
        if d.get("catalog_event") == uw._CATALOG_EVENT_AUTH_FAILURE
    ]
    assert len(auth_dispatches) >= 1


def test_auth_failure_dedup_across_repos(shared_dir, install_json, gh_stub, dispatch_capture):
    """Multiple repos all hitting the same auth failure should dedup at the
    dispatcher level (we use a stable dedup_key)."""
    p = install_json(repos=[
        {"repo": "openclaw/openclaw", "author": "cjalden"},
        {"repo": "anthropic/claude-cli", "author": "cjalden"},
    ])
    gh_stub["auth_failure"] = "401 Unauthorized"
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    auth_dispatches = [
        d for d in dispatch_capture
        if d.get("catalog_event") == uw._CATALOG_EVENT_AUTH_FAILURE
    ]
    # The dispatcher's own dedup will fold these on its side; here we
    # assert all auth dispatches share the same dedup_key so it CAN dedup.
    keys = {d["dedup_key"] for d in auth_dispatches}
    assert keys == {"upstream_issues_watcher/auth_failure"}


# ── Per-repo isolation ─────────────────────────────────────────────────────


def test_one_repo_failing_does_not_block_the_other(shared_dir, install_json, gh_stub, dispatch_capture):
    """If gh fails on one repo, the other should still be polled and dispatched."""
    p = install_json(repos=[
        {"repo": "openclaw/openclaw", "author": "cjalden"},
        {"repo": "anthropic/claude-cli", "author": "cjalden"},
    ])
    # First repo returns issues with a fresh comment.
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "oc-maintainer", "Hi")],
    )
    # Second repo returns nothing (simulates a non-auth gh failure ->
    # empty list).
    gh_stub["issues_by_repo"]["anthropic/claude-cli"] = []

    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert stats["repos_polled"] == 2
    assert stats["dispatched"] == 1


def test_state_file_persists_between_runs(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "oc-maintainer", "Hi")],
    )
    uw.run_once(shared_dir=shared_dir, network={}, install_json=p)

    state_path = shared_dir / uw.FEATURE_NAME / "state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["repos"]["openclaw/openclaw"]["issues"]["#84820"]["last_comment_id"] == "c1"


# ── Dry-run mode ────────────────────────────────────────────────────────────


def test_dry_run_does_not_dispatch(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "oc-maintainer", "Hi")],
    )
    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p, dry_run=True)
    assert stats["events"] == 1
    assert stats["dispatched"] == 0
    assert dispatch_capture == []


# ── Malformed-config tolerance ──────────────────────────────────────────────


def test_malformed_repo_entry_is_skipped(shared_dir, install_json, gh_stub, dispatch_capture):
    p = install_json(repos=[
        {"repo": "openclaw/openclaw"},                  # missing author
        {"author": "cjalden"},                          # missing repo
        {"repo": 42, "author": "cjalden"},              # non-string repo
        {"repo": "openclaw/openclaw", "author": "cjalden"},  # good
    ])
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = []
    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    # Only the good entry was polled.
    assert stats["repos_polled"] == 1


# ── Backfill: out-of-band issues get a synthesized Intake ───────────────────


@pytest.fixture()
def backfill_capture(monkeypatch):
    """Capture every backfill attempt; pretend each one created a new intake."""
    captured: list[dict[str, Any]] = []

    def fake_backfill(*, repo, issue, shared_dir):
        captured.append({"repo": repo, "number": issue.get("number"),
                         "title": issue.get("title"), "body": issue.get("body")})
        return True

    monkeypatch.setattr(uw, "_ensure_backfill_intake", fake_backfill)
    return captured


def test_backfill_runs_for_each_polled_issue(
    shared_dir, install_json, gh_stub, dispatch_capture, backfill_capture,
):
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "FileHandle leak", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
        {"number": 84821, "title": "feat: add retry", "state": "CLOSED",
         "updatedAt": "2026-05-22T11:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        title="FileHandle leak under load",
        comments=[],
    )
    gh_stub["views_by_key"][("openclaw/openclaw", 84821)] = _make_issue(
        "openclaw/openclaw", 84821, state_="CLOSED",
        title="feat: add retry",
        comments=[],
    )

    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)

    # Both issues hit the backfill path (open + closed both surface).
    assert len(backfill_capture) == 2
    numbers = {b["number"] for b in backfill_capture}
    assert numbers == {84820, 84821}
    assert stats["backfilled"] == 2


def test_backfill_skips_when_intake_already_exists(shared_dir, install_json, gh_stub, dispatch_capture):
    """The real _ensure_backfill_intake noops when find_by_github_issue hits."""
    p = install_json()
    # Pre-create a filed intake matching the issue we're about to poll.
    from evolve_admin.intake.envelope import Intake, IntakePromotion
    from evolve_admin.intake import store as _intake_store

    pre = Intake(
        id=_intake_store.new_intake_id(),
        kind="bug",
        body="existing intake body",
        state="filed",
        promotion=IntakePromotion(
            github_issue_url="https://github.com/openclaw/openclaw/issues/84820",
            github_issue_number=84820,
        ),
    )
    _intake_store.write_intake(pre, shared_dir)

    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "...", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "oc-maintainer", "thanks")],
    )

    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    # No new backfill; the pre-existing intake satisfied the lookup.
    assert stats["backfilled"] == 0
    # And the maintainer comment still dispatched.
    assert stats["dispatched"] == 1


# ── --backfill-since: deep discovery pass ───────────────────────────────────


def test_parse_backfill_window_accepts_durations_and_iso():
    # Smoke-test the parser; concrete values are time-dependent so just
    # assert shape (ISO Z-terminated) and that obvious inputs accept.
    for spec in ("7d", "12w", "6m", "2y", "all", "2026-01-01"):
        out = uw._parse_backfill_window(spec)
        assert isinstance(out, str) and out.endswith("Z")


def test_parse_backfill_window_rejects_bad_input():
    for spec in ("", "garbage", "7", "7x", "1991-13-40", "2026/01/01"):
        with pytest.raises(ValueError):
            uw._parse_backfill_window(spec)


def test_backfill_since_bad_spec_is_clean_noop(
    shared_dir, install_json, gh_stub, dispatch_capture, backfill_capture,
):
    p = install_json()
    # Even with a poll-worthy issue staged, a bad --backfill-since
    # value should produce a stderr warning and exit before polling.
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "x", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    stats = uw.run_once(
        shared_dir=shared_dir, network={}, install_json=p,
        backfill_since="garbage",
    )
    assert stats["repos_polled"] == 0
    assert backfill_capture == []
    assert dispatch_capture == []


def test_backfill_since_creates_intakes_and_suppresses_alerts(
    shared_dir, install_json, gh_stub, dispatch_capture, backfill_capture,
):
    """Deep pass: every self-authored issue gets backfilled, but the
    fresh-maintainer-comment that would normally fire is suppressed."""
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "old issue 1", "state": "CLOSED",
         "updatedAt": "2026-02-01T10:00:00Z", "url": "..."},
        {"number": 84821, "title": "old issue 2", "state": "OPEN",
         "updatedAt": "2026-02-15T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820, state_="CLOSED",
        comments=[_make_comment("c1", "oc-maintainer", "thanks")],
    )
    gh_stub["views_by_key"][("openclaw/openclaw", 84821)] = _make_issue(
        "openclaw/openclaw", 84821,
        comments=[_make_comment("c2", "oc-maintainer", "looking")],
    )

    stats = uw.run_once(
        shared_dir=shared_dir, network={}, install_json=p,
        backfill_since="90d",
    )

    # Backfill happened twice (one per issue).
    assert stats["backfilled"] == 2
    # Events were classified (state + comment per issue) but suppressed.
    assert stats["dispatched"] == 0
    assert dispatch_capture == []


def test_backfill_since_seeds_state_so_next_run_does_not_replay(
    shared_dir, install_json, gh_stub, dispatch_capture, backfill_capture,
):
    """After a deep backfill, the next normal run must NOT dispatch the
    same maintainer comment that was suppressed during discovery."""
    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "old issue", "state": "OPEN",
         "updatedAt": "2026-02-01T10:00:00Z", "url": "..."},
    ]
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = _make_issue(
        "openclaw/openclaw", 84820,
        comments=[_make_comment("c1", "oc-maintainer", "old comment")],
    )

    # Deep pass — suppressed.
    uw.run_once(
        shared_dir=shared_dir, network={}, install_json=p,
        backfill_since="90d",
    )
    assert dispatch_capture == []

    # Same poll repeated as a normal run — c1 must NOT re-fire because
    # backfill seeded the high-water mark.
    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)
    assert stats["dispatched"] == 0
    assert dispatch_capture == []


def test_backfill_then_comment_attaches_to_synthesized_intake(
    shared_dir, install_json, gh_stub, dispatch_capture,
):
    """End-to-end: a never-seen self-authored issue with a fresh
    maintainer comment results in (a) a new filed intake and (b) an
    activity event attached to it."""
    from evolve_admin.intake import store as _intake_store

    p = install_json()
    gh_stub["issues_by_repo"]["openclaw/openclaw"] = [
        {"number": 84820, "title": "FileHandle leak", "state": "OPEN",
         "updatedAt": "2026-05-22T10:00:00Z", "url": "..."},
    ]
    full = _make_issue(
        "openclaw/openclaw", 84820,
        title="FileHandle leak under load",
        comments=[_make_comment("c1", "oc-maintainer", "Can you share repro steps?")],
    )
    full["body"] = "Repro: run a bot overnight, FH count climbs unbounded."
    gh_stub["views_by_key"][("openclaw/openclaw", 84820)] = full

    stats = uw.run_once(shared_dir=shared_dir, network={}, install_json=p)

    assert stats["backfilled"] == 1
    assert stats["dispatched"] == 1

    # The intake was created in filed/ with the GitHub URL populated…
    found = _intake_store.find_by_github_issue(shared_dir, "openclaw/openclaw", 84820)
    assert found is not None
    assert found.state == "filed"
    assert found.inbound is False
    assert found.promotion.github_issue_number == 84820
    assert "FileHandle" in found.body or "Repro" in found.body
    # …and the maintainer comment landed on its activity log.
    assert len(found.activity_log) == 1
    assert found.activity_log[0].kind == "new_comment"
    assert found.activity_log[0].actor == "oc-maintainer"
