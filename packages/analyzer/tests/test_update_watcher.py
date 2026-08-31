"""Phase E2 — update_watcher daemon polls upstream sources and emits
via the alerts dispatcher.

Tests:

  - semver comparison handles release-name shapes (2026.4.29, etc.)
  - openclaw check emits when latest > installed and not yet notified
  - openclaw check no-ops when already notified for that exact version
  - openclaw check no-ops when installed = latest
  - openclaw check no-ops when registry / install path unreachable
  - evolve_repo check emits on first observation of a new HEAD
  - evolve_repo check no-ops when last_notified_sha == HEAD
  - state file persists across runs (atomic temp+rename)
  - update_watcher source is registered in the dispatcher
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import update_watcher  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.dispatcher import (
        DispatchOutcome, DispatchResult, Severity,
    )

    captured: list = []

    def fake_send(*, shared_dir, network, source, severity,
                  message=None, payload=None,
                  dedup_key=None, catalog_event=None, **_kw):
        captured.append({
            "source": source, "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        return DispatchOutcome(
            result=DispatchResult.SENT, source=source, severity=severity,
            dedup_key=dedup_key, catalog_event=catalog_event,
            channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    # Keep the openclaw-update checks hermetic: never shell out to a real
    # `openclaw update status --json` (which would run — and could return a
    # live version that defeats the patched npm poll — on any host where OC is
    # installed, e.g. the mini). Tests that exercise the update-status path
    # override this explicitly.
    monkeypatch.setattr(update_watcher, "fetch_oc_update_status",
                        lambda timeout=20: None)

    shared = tmp_path / "evolve"
    shared.mkdir()
    return {
        "captured": captured,
        "shared_dir": shared,
        "network": {"alerts": {"channel": "telegram", "chatId": "12345"}},
        "Severity": Severity,
    }


# ── Semver comparison ──────────────────────────────────────────────────────


@pytest.mark.parametrize("latest,current,expected", [
    ("2026.5.7",  "2026.4.29", True),
    ("2026.4.29", "2026.5.7",  False),
    ("2026.4.29", "2026.4.29", False),     # equal → not newer
    ("2026.5.0",  "2026.4.29", True),
    ("1.10.0",    "1.9.99",    True),      # numeric, not lexicographic
    ("2.0.0-rc.1", "2.0.0-rc.0", True),    # rc.1 > rc.0 — numeric trailing chunk wins
    ("2.0.0",      "2.0.0-rc.1", False),   # equal stems → not newer; suffix ignored
])
def test_is_newer_version(latest, current, expected):
    assert update_watcher.is_newer_version(latest, current) is expected


def test_version_tuple_handles_short_strings():
    """Pin that the lenient parser doesn't blow up on odd shapes."""
    assert update_watcher._version_tuple("1") == (1,)
    assert update_watcher._version_tuple("1.2") == (1, 2)
    assert update_watcher._version_tuple("") == (0,)


# ── State file ─────────────────────────────────────────────────────────────


def test_state_round_trip(env):
    update_watcher._save_state(
        env["shared_dir"],
        {"openclaw_last_notified_key": "2026.5.7:safe",
         "evolve_repo_last_notified_sha": "abc1234567890def"},
    )
    state = update_watcher._load_state(env["shared_dir"])
    assert state["openclaw_last_notified_key"] == "2026.5.7:safe"
    assert state["evolve_repo_last_notified_sha"] == "abc1234567890def"
    assert state.get("updated_at")   # auto-stamped


def test_state_missing_returns_default(env):
    state = update_watcher._load_state(env["shared_dir"])
    assert state == {"version": 1}


def test_state_corrupt_returns_default(env):
    p = env["shared_dir"] / "update_watcher" / "state.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not valid json")
    state = update_watcher._load_state(env["shared_dir"])
    assert state == {"version": 1}


# ── openclaw check ─────────────────────────────────────────────────────────


def _stub_preflight(env, monkeypatch, *, ok: bool, blockers: list[str] | None = None):
    """Bypass the live safe-upgrade preflight in tests."""
    blockers = blockers or []
    monkeypatch.setattr(
        update_watcher,
        "_run_preflight_safe",
        lambda target_version, *, network, shared_dir: {
            "ok": ok,
            "requirements": [
                {"summary": b, "blocking": True} for b in blockers
            ],
        },
    )


def test_openclaw_emits_when_latest_is_newer_and_preflight_safe(env, monkeypatch):
    """Preflight passes → updates.openclaw_available with no blocker payload."""
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: "2026.4.29")
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: "2026.5.7")
    _stub_preflight(env, monkeypatch, ok=True)
    state = {"version": 1}
    result = update_watcher.check_openclaw_update(state, env["network"], env["shared_dir"])
    assert result == "2026.5.7"
    assert state["openclaw_last_notified_key"] == "2026.5.7:safe"

    call = env["captured"][0]
    assert call["source"] == "update_watcher"
    assert call["catalog_event"] == "updates.openclaw_available"
    assert call["dedup_key"] == "update_watcher/openclaw/2026.5.7/safe"
    assert call["severity"] == env["Severity"].INFO
    assert call["message"] is None
    assert call["payload"] == {"new_version": "2026.5.7", "current_version": "2026.4.29"}


def test_openclaw_emits_blocked_when_preflight_finds_blockers(env, monkeypatch):
    """Preflight fails → updates.openclaw_blocked carries a rendered
    blocker_summary so the operator sees the why up front."""
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: "2026.4.29")
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: "2026.5.7")
    _stub_preflight(env, monkeypatch, ok=False, blockers=[
        "Node 25.9 below required 26.0",
        "rogue ai.openclaw.gateway.plist in team_bot_a's LaunchAgents",
    ])
    state = {"version": 1}
    result = update_watcher.check_openclaw_update(state, env["network"], env["shared_dir"])
    assert result == "2026.5.7"
    assert state["openclaw_last_notified_key"] == "2026.5.7:blocked"

    call = env["captured"][0]
    assert call["catalog_event"] == "updates.openclaw_blocked"
    assert call["dedup_key"] == "update_watcher/openclaw/2026.5.7/blocked"
    summary = call["payload"]["blocker_summary"]
    assert "Node 25.9 below required 26.0" in summary
    assert "rogue ai.openclaw.gateway.plist" in summary


def test_openclaw_re_emits_when_verdict_flips_for_same_version(env, monkeypatch):
    """Operator resolves blockers → next tick re-fires with the safe
    variant for the same version. Without this, they'd never hear the
    "now safe to upgrade" alert."""
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: "2026.4.29")
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: "2026.5.7")
    _stub_preflight(env, monkeypatch, ok=True)
    state = {"openclaw_last_notified_key": "2026.5.7:blocked"}
    result = update_watcher.check_openclaw_update(state, env["network"], env["shared_dir"])
    assert result == "2026.5.7"
    assert state["openclaw_last_notified_key"] == "2026.5.7:safe"
    assert env["captured"][0]["catalog_event"] == "updates.openclaw_available"


def test_openclaw_no_dispatch_when_already_notified_same_verdict(env, monkeypatch):
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: "2026.4.29")
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: "2026.5.7")
    _stub_preflight(env, monkeypatch, ok=True)
    state = {"openclaw_last_notified_key": "2026.5.7:safe"}
    result = update_watcher.check_openclaw_update(state, env["network"], env["shared_dir"])
    assert result is None
    assert env["captured"] == []


def test_openclaw_falls_back_to_available_when_preflight_unavailable(env, monkeypatch):
    """If the preflight can't run (import error, exception), still notify
    about the version — bare available alert, no verdict claim."""
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: "2026.4.29")
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: "2026.5.7")
    monkeypatch.setattr(
        update_watcher, "_run_preflight_safe",
        lambda target_version, *, network, shared_dir: None,
    )
    state = {"version": 1}
    result = update_watcher.check_openclaw_update(state, env["network"], env["shared_dir"])
    assert result == "2026.5.7"
    assert env["captured"][0]["catalog_event"] == "updates.openclaw_available"


def test_openclaw_no_dispatch_when_at_latest(env, monkeypatch):
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: "2026.5.7")
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: "2026.5.7")
    state = {"version": 1}
    result = update_watcher.check_openclaw_update(state, env["network"], env["shared_dir"])
    assert result is None
    assert env["captured"] == []


def test_openclaw_no_dispatch_when_registry_unreachable(env, monkeypatch):
    """Network down → fetch returns None → no crash, no dispatch, no
    state change. Next daily tick retries."""
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: "2026.4.29")
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: None)
    state = {"version": 1}
    result = update_watcher.check_openclaw_update(state, env["network"], env["shared_dir"])
    assert result is None
    assert env["captured"] == []
    assert "openclaw_last_notified_version" not in state


def test_openclaw_no_dispatch_when_not_installed(env, monkeypatch):
    """If openclaw isn't installed locally, we can't compare; skip."""
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: None)
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: "2026.5.7")
    result = update_watcher.check_openclaw_update(
        {"version": 1}, env["network"], env["shared_dir"],
    )
    assert result is None
    assert env["captured"] == []


# ── evolve_repo check ─────────────────────────────────────────────────────


def test_evolve_repo_emits_on_first_observation(env, monkeypatch, tmp_path):
    """First-run state has no last_notified_sha. ls-remote returns a
    head; we emit with the "new updates available" summary."""
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    monkeypatch.setattr(update_watcher, "git_ls_remote_head",
                        lambda r, b="main": "abc1234567890abcdef1234567890abcdef1234")
    state = {"version": 1}
    result = update_watcher.check_evolve_repo_update(
        state, env["network"], env["shared_dir"], repo=repo,
    )
    assert result == "abc1234567890abcdef1234567890abcdef1234"
    assert state["evolve_repo_last_notified_sha"] == result

    call = env["captured"][0]
    assert call["catalog_event"] == "updates.evolve_repo"
    # Phase F: payload-only; catalog body_template renders.
    assert call["message"] is None
    assert "new updates available" in call["payload"]["commits_summary"]
    # Operator-facing copy must not leak a git sha.
    assert "abc12345" not in call["payload"]["commits_summary"]


def test_evolve_repo_uses_commit_count_when_baseline_known(env, monkeypatch, tmp_path):
    """If last_notified_sha is known, we count commits between it and
    HEAD and report N new updates in the body."""
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    monkeypatch.setattr(update_watcher, "git_ls_remote_head",
                        lambda r, b="main": "head_sha_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(update_watcher, "git_count_commits_between",
                        lambda r, base, head: 7)
    state = {"evolve_repo_last_notified_sha": "base_sha_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
    update_watcher.check_evolve_repo_update(
        state, env["network"], env["shared_dir"], repo=repo,
    )
    assert "7 new updates since the last check" in env["captured"][0]["payload"]["commits_summary"]


def test_evolve_repo_no_dispatch_when_head_unchanged(env, monkeypatch, tmp_path):
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    head = "head_sha_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    monkeypatch.setattr(update_watcher, "git_ls_remote_head",
                        lambda r, b="main": head)
    state = {"evolve_repo_last_notified_sha": head}
    result = update_watcher.check_evolve_repo_update(
        state, env["network"], env["shared_dir"], repo=repo,
    )
    assert result is None
    assert env["captured"] == []


def test_evolve_repo_no_dispatch_when_ls_remote_fails(env, monkeypatch, tmp_path):
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    monkeypatch.setattr(update_watcher, "git_ls_remote_head",
                        lambda r, b="main": None)   # network/auth failure
    state = {"version": 1}
    result = update_watcher.check_evolve_repo_update(
        state, env["network"], env["shared_dir"], repo=repo,
    )
    assert result is None
    assert env["captured"] == []


def test_evolve_repo_no_dispatch_when_repo_path_missing(env, monkeypatch, tmp_path):
    """ls-remote internally returns None for a missing repo path, so
    the higher-level check no-ops cleanly."""
    repo = tmp_path / "nonexistent-repo"
    state = {"version": 1}
    result = update_watcher.check_evolve_repo_update(
        state, env["network"], env["shared_dir"], repo=repo,
    )
    assert result is None
    assert env["captured"] == []


# ── Tracked upstream issues ────────────────────────────────────────────────


def _issue(state: str, *, title="t", closed_at=None,
           url="https://github.com/openclaw/openclaw/issues/82301") -> dict:
    return {"state": state, "title": title, "closed_at": closed_at, "html_url": url}


def test_tracked_issues_falls_back_to_default_seed(env):
    """No config file present → use the embedded _DEFAULT_TRACKED_ISSUES list."""
    issues = update_watcher._load_tracked_issues(env["shared_dir"])
    assert len(issues) >= 1
    # Pre-seeded with the openclaw catch-22 issue.
    assert any(i["repo"] == "openclaw/openclaw" and i["number"] == 82301 for i in issues)


def test_tracked_issues_reads_operator_override(env):
    """Operator-written config file takes priority over the seed."""
    p = env["shared_dir"] / "update_watcher" / "tracked_upstream_issues.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"issues": [
        {"repo": "octocat/hello", "number": 1, "hint": "test"},
    ]}))
    issues = update_watcher._load_tracked_issues(env["shared_dir"])
    assert issues == [{"repo": "octocat/hello", "number": 1, "hint": "test"}]


def test_tracked_issues_first_observation_stale_closure_does_not_notify(env, monkeypatch):
    """First-time observation of an issue closed long ago: silently
    record, no notification. (Catches the operator-adds-an-ancient-issue
    case — we don't want to spam years of stale closures into the
    alerts channel.)"""
    monkeypatch.setattr(update_watcher, "_load_tracked_issues",
                        lambda sd: [{"repo": "o/r", "number": 1, "hint": "h"}])
    monkeypatch.setattr(update_watcher, "_fetch_github_issue",
                        lambda repo, number, timeout=10: _issue(
                            "closed", closed_at="2026-01-01T00:00:00Z"))

    state = {}
    notified = update_watcher.check_tracked_upstream_issues(
        state, env["network"], env["shared_dir"],
    )
    assert notified == []
    assert env["captured"] == []
    # State was recorded so the next tick can detect a real transition.
    assert state["tracked_upstream_issues"]["o/r#1"] == "closed"


def test_tracked_issues_first_observation_fresh_closure_DOES_notify(env, monkeypatch):
    """First-time observation of an issue that closed within the fresh
    window: emit the notification anyway. Catches the regression where
    issue closure happens between tracker deployment and the first poll
    (which bit openclaw/openclaw#82301 on 2026-05-16: PR #1143 merged
    21:31Z, issue closed 04:04Z next morning, first poll 16:32Z that
    afternoon → without this rule, the close notification was silently
    swallowed)."""
    from datetime import datetime, timedelta, timezone
    just_now = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    monkeypatch.setattr(update_watcher, "_load_tracked_issues",
                        lambda sd: [{"repo": "o/r", "number": 1,
                                     "hint": "freshly resolved"}])
    monkeypatch.setattr(update_watcher, "_fetch_github_issue",
                        lambda repo, number, timeout=10: _issue(
                            "closed", title="Recent fix", closed_at=just_now))

    state = {}
    notified = update_watcher.check_tracked_upstream_issues(
        state, env["network"], env["shared_dir"],
    )
    assert notified == ["o/r#1"]
    assert len(env["captured"]) == 1
    assert env["captured"][0]["catalog_event"] == "updates.upstream_issue_resolved"
    assert env["captured"][0]["payload"]["title"] == "Recent fix"
    # State advanced — a second tick won't re-notify.
    assert state["tracked_upstream_issues"]["o/r#1"] == "closed"


def test_tracked_issues_first_observation_open_does_not_notify(env, monkeypatch):
    """First-time observation of an open issue: silently record, never
    notify. Closure-window logic doesn't apply to still-open issues."""
    monkeypatch.setattr(update_watcher, "_load_tracked_issues",
                        lambda sd: [{"repo": "o/r", "number": 1, "hint": "h"}])
    monkeypatch.setattr(update_watcher, "_fetch_github_issue",
                        lambda repo, number, timeout=10: _issue("open"))

    state = {}
    notified = update_watcher.check_tracked_upstream_issues(
        state, env["network"], env["shared_dir"],
    )
    assert notified == []
    assert env["captured"] == []
    assert state["tracked_upstream_issues"]["o/r#1"] == "open"


def test_tracked_issues_first_observation_missing_closed_at_does_not_notify(env, monkeypatch):
    """Defensive: GitHub claims `state=closed` but omits `closed_at`. Treat
    as uncertain → don't notify."""
    monkeypatch.setattr(update_watcher, "_load_tracked_issues",
                        lambda sd: [{"repo": "o/r", "number": 1, "hint": "h"}])
    monkeypatch.setattr(update_watcher, "_fetch_github_issue",
                        lambda repo, number, timeout=10: _issue(
                            "closed", closed_at=None))

    state = {}
    notified = update_watcher.check_tracked_upstream_issues(
        state, env["network"], env["shared_dir"],
    )
    assert notified == []
    assert env["captured"] == []
    assert state["tracked_upstream_issues"]["o/r#1"] == "closed"


def test_closed_within_fresh_window_parses_z_suffix():
    """The standard GitHub timestamp shape is `2026-05-16T04:04:17Z`.
    Make sure both 'Z' and explicit +00:00 forms parse cleanly."""
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert update_watcher._closed_within_fresh_window({"closed_at": fresh}) is True

    stale = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert update_watcher._closed_within_fresh_window({"closed_at": stale}) is False

    # Boundary: just under 24h → fresh
    near_edge = (datetime.now(timezone.utc) - timedelta(hours=23)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert update_watcher._closed_within_fresh_window({"closed_at": near_edge}) is True

    # Boundary: just over 24h → stale
    past_edge = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert update_watcher._closed_within_fresh_window({"closed_at": past_edge}) is False


def test_closed_within_fresh_window_handles_garbage():
    """Malformed/missing closed_at returns False (don't notify on uncertainty)."""
    assert update_watcher._closed_within_fresh_window({}) is False
    assert update_watcher._closed_within_fresh_window({"closed_at": None}) is False
    assert update_watcher._closed_within_fresh_window({"closed_at": ""}) is False
    assert update_watcher._closed_within_fresh_window({"closed_at": "not-a-date"}) is False
    assert update_watcher._closed_within_fresh_window({"closed_at": 12345}) is False


def test_tracked_issues_open_to_closed_notifies(env, monkeypatch):
    """The interesting transition: an issue we were watching while open
    becomes closed → emit one notification."""
    monkeypatch.setattr(update_watcher, "_load_tracked_issues",
                        lambda sd: [{"repo": "o/r", "number": 1,
                                     "hint": "unblocks 5.12 upgrade"}])
    monkeypatch.setattr(update_watcher, "_fetch_github_issue",
                        lambda repo, number, timeout=10: _issue(
                            "closed", title="Fixed: catch-22",
                            closed_at="2026-05-20T14:32:00Z",
                            url="https://github.com/o/r/issues/1"))

    state = {"tracked_upstream_issues": {"o/r#1": "open"}}
    notified = update_watcher.check_tracked_upstream_issues(
        state, env["network"], env["shared_dir"],
    )
    assert notified == ["o/r#1"]
    assert len(env["captured"]) == 1
    sent = env["captured"][0]
    assert sent["catalog_event"] == "updates.upstream_issue_resolved"
    assert sent["payload"]["repo"] == "o/r"
    assert sent["payload"]["number"] == 1
    assert sent["payload"]["title"] == "Fixed: catch-22"
    assert sent["payload"]["hint"] == "unblocks 5.12 upgrade"
    assert sent["dedup_key"] == "update_watcher/upstream_issue/o/r/1/closed"
    # State advanced so a second tick won't re-notify.
    assert state["tracked_upstream_issues"]["o/r#1"] == "closed"


def test_tracked_issues_no_change_no_notify(env, monkeypatch):
    """State stays 'open' across ticks → no notification, no state churn."""
    monkeypatch.setattr(update_watcher, "_load_tracked_issues",
                        lambda sd: [{"repo": "o/r", "number": 1, "hint": "h"}])
    monkeypatch.setattr(update_watcher, "_fetch_github_issue",
                        lambda repo, number, timeout=10: _issue("open"))

    state = {"tracked_upstream_issues": {"o/r#1": "open"}}
    notified = update_watcher.check_tracked_upstream_issues(
        state, env["network"], env["shared_dir"],
    )
    assert notified == []
    assert env["captured"] == []


def test_tracked_issues_closed_to_open_silent(env, monkeypatch):
    """Reopens are a real signal upstream cares about, but for the
    operator the relevant action ('try the upgrade again') has already
    happened. Track for next time, but no alert."""
    monkeypatch.setattr(update_watcher, "_load_tracked_issues",
                        lambda sd: [{"repo": "o/r", "number": 1, "hint": "h"}])
    monkeypatch.setattr(update_watcher, "_fetch_github_issue",
                        lambda repo, number, timeout=10: _issue("open"))

    state = {"tracked_upstream_issues": {"o/r#1": "closed"}}
    notified = update_watcher.check_tracked_upstream_issues(
        state, env["network"], env["shared_dir"],
    )
    assert notified == []
    assert env["captured"] == []
    assert state["tracked_upstream_issues"]["o/r#1"] == "open"


def test_tracked_issues_fetch_failure_preserves_prior_state(env, monkeypatch):
    """Network or API blip → no notification, no state mutation. Next
    tick will retry with the same prior_state."""
    monkeypatch.setattr(update_watcher, "_load_tracked_issues",
                        lambda sd: [{"repo": "o/r", "number": 1, "hint": "h"}])
    monkeypatch.setattr(update_watcher, "_fetch_github_issue",
                        lambda repo, number, timeout=10: None)

    state = {"tracked_upstream_issues": {"o/r#1": "open"}}
    notified = update_watcher.check_tracked_upstream_issues(
        state, env["network"], env["shared_dir"],
    )
    assert notified == []
    assert env["captured"] == []
    # Prior state untouched so the next tick can still detect open→closed.
    assert state["tracked_upstream_issues"]["o/r#1"] == "open"


# ── Dispatcher source registration ─────────────────────────────────────────


def test_dispatcher_recognizes_update_watcher():
    from evolve_admin.alerts.dispatcher import known_sources
    assert "update_watcher" in set(known_sources())


# ── `openclaw update status --json` adoption ─────────────────────────────────


def test_resolve_latest_prefers_update_status(monkeypatch):
    """OC's own CLI is primary: when `update status --json` yields a version,
    use it and don't fall back to the npm poll."""
    monkeypatch.setattr(update_watcher, "fetch_oc_update_status",
                        lambda timeout=20: {"registry": {"latestVersion": "2026.6.9"},
                                            "channel": {"value": "stable"}})

    def _boom(_pkg):
        raise AssertionError("npm poll should not be called when update status wins")

    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version", _boom)
    version, status = update_watcher.resolve_latest_openclaw_version()
    assert version == "2026.6.9"
    assert status["channel"]["value"] == "stable"


def test_resolve_latest_falls_back_to_npm(monkeypatch):
    """CLI absent / no version → fall back to the npm registry poll."""
    monkeypatch.setattr(update_watcher, "fetch_oc_update_status", lambda timeout=20: None)
    monkeypatch.setattr(update_watcher, "fetch_npm_latest_version",
                        lambda pkg: "2026.6.5")
    version, status = update_watcher.resolve_latest_openclaw_version()
    assert version == "2026.6.5"
    assert status is None


# ── OpenClaw surface drift-diff ──────────────────────────────────────────────


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def _make_oc_tree(tmp_path: Path, name: str, *, extensions: dict, version: str) -> Path:
    """Build a minimal OC package root. ``extensions`` maps ext-dir → channel-id."""
    root = tmp_path / name
    _write_json(root / "package.json", {"name": "openclaw", "version": version})
    for ext_dir, cid in extensions.items():
        _write_json(
            root / "dist" / "extensions" / ext_dir / "package.json",
            {"name": f"@openclaw/{ext_dir}",
             "openclaw": {"channel": {"id": cid, "label": cid.title()}}},
        )
    _write_json(root / "dist" / "channel-catalog.json", {"entries": []})
    return root


def _tarball_from_tree(root: Path) -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                data = path.read_bytes()
                ti = tarfile.TarInfo(name=f"package/{rel}")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _firing_signals(shared_dir: Path) -> list:
    d = shared_dir / "signals" / "firing"
    if not d.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


def _wire_surface_drift(
    env, monkeypatch, tmp_path, *, installed_exts, candidate_exts,
    installed_version="2026.6.1", candidate_version="2026.6.5",
):
    """Common monkeypatch harness: real oc_surface, fake installed root +
    candidate tarball, no CLI probe."""
    from evolve_admin import oc_surface as real_ocs

    installed_root = _make_oc_tree(
        tmp_path, "installed", extensions=installed_exts, version=installed_version)
    candidate_root = _make_oc_tree(
        tmp_path, "candidate", extensions=candidate_exts, version=candidate_version)
    tarball = _tarball_from_tree(candidate_root)

    monkeypatch.setattr(real_ocs, "installed_oc_root", lambda: installed_root)
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: installed_version)
    monkeypatch.setattr(update_watcher, "resolve_latest_openclaw_version",
                        lambda: (candidate_version, None))
    monkeypatch.setattr(update_watcher, "_find_openclaw_cli", lambda: None)

    def fake_fetch_meta(spec):
        return {"version": spec, "dist": {"tarball": "https://example/openclaw.tgz"}}

    def fake_download(metadata, timeout=30):
        return tarball, None

    monkeypatch.setattr(update_watcher, "_import_surface_tools",
                        lambda: (real_ocs, fake_fetch_meta, fake_download))
    return real_ocs


def test_surface_drift_emits_info_without_oc_deps(env, monkeypatch, tmp_path):
    """A surface changed but Evolve declares no dependencies (oc_deps absent)
    → INFO Signal, full diff artifact written."""
    monkeypatch.setitem(sys.modules, "oc_deps", None)  # oc_deps not present
    _wire_surface_drift(
        env, monkeypatch, tmp_path,
        installed_exts={"signal": "signal", "imessage": "imessage"},
        candidate_exts={"signal": "signal", "whatsapp": "whatsapp"},
    )
    state = {"version": 1}
    drifted = update_watcher.check_openclaw_surface_drift(
        state, env["network"], env["shared_dir"])

    assert drifted == "2026.6.5"
    sigs = _firing_signals(env["shared_dir"])
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig["producer"] == "oc_surface_drift"
    assert sig["type"] == "updates.openclaw_surface_drift"
    assert sig["severity"] == "info"
    # Dev-facing artifact written under the diffs dir.
    artifact = env["shared_dir"] / "update_watcher" / "oc-surface-diffs" / "2026.6.5.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text())
    assert "extension:whatsapp" in payload["diff"]["changed_surface_items"]
    # State records the reviewed candidate so the tarball isn't re-fetched daily.
    assert state["surface_last_diffed_candidate"] == "2026.6.5"


def test_surface_drift_emits_warning_when_depended_surface_changes(env, monkeypatch, tmp_path):
    """A changed surface that Evolve declares it depends on → WARNING."""
    fake_deps = types.ModuleType("oc_deps")
    fake_deps.OC_DEPENDENCIES = ["extension:imessage"]  # Evolve depends on imessage
    monkeypatch.setitem(sys.modules, "oc_deps", fake_deps)

    _wire_surface_drift(
        env, monkeypatch, tmp_path,
        installed_exts={"signal": "signal", "imessage": "imessage"},
        candidate_exts={"signal": "signal"},  # imessage REMOVED — a depended surface
    )
    state = {"version": 1}
    drifted = update_watcher.check_openclaw_surface_drift(
        state, env["network"], env["shared_dir"])

    assert drifted == "2026.6.5"
    sig = _firing_signals(env["shared_dir"])[0]
    assert sig["severity"] == "warn"
    assert sig["flavor"] == "maintenance"
    assert "extension:imessage" in sig["details"]["depended_changed"]


def test_surface_drift_no_signal_when_no_change(env, monkeypatch, tmp_path):
    """Version bumped but no surface changed → no Signal, but the candidate is
    still recorded so we don't re-download daily."""
    monkeypatch.setitem(sys.modules, "oc_deps", None)
    _wire_surface_drift(
        env, monkeypatch, tmp_path,
        installed_exts={"signal": "signal", "telegram": "telegram"},
        candidate_exts={"signal": "signal", "telegram": "telegram"},  # identical
    )
    state = {"version": 1}
    drifted = update_watcher.check_openclaw_surface_drift(
        state, env["network"], env["shared_dir"])

    assert drifted is None
    assert _firing_signals(env["shared_dir"]) == []
    assert state["surface_last_diffed_candidate"] == "2026.6.5"


def test_surface_drift_failed_emit_does_not_burn_dedup(env, monkeypatch, tmp_path):
    """A transient signal-store failure must NOT record the candidate as
    reviewed — otherwise the drift review is silently swallowed forever."""
    monkeypatch.setitem(sys.modules, "oc_deps", None)
    _wire_surface_drift(
        env, monkeypatch, tmp_path,
        installed_exts={"signal": "signal", "imessage": "imessage"},
        candidate_exts={"signal": "signal", "whatsapp": "whatsapp"},
    )
    # Force the emit to fail (store unavailable).
    monkeypatch.setattr(update_watcher, "_import_signal_store", lambda: (None, None))

    state = {"version": 1}
    drifted = update_watcher.check_openclaw_surface_drift(
        state, env["network"], env["shared_dir"])

    assert drifted is None
    # Dedup NOT committed → next tick retries instead of swallowing the drift.
    assert "surface_last_diffed_candidate" not in state
    # The artifact is still written (dev-facing record) even when chat emit fails.
    assert (env["shared_dir"] / "update_watcher" / "oc-surface-diffs"
            / "2026.6.5.json").exists()


def test_surface_drift_noop_when_up_to_date(env, monkeypatch, tmp_path):
    """Installed == latest → no diff attempted, no Signal."""
    from evolve_admin import oc_surface as real_ocs
    installed_root = _make_oc_tree(
        tmp_path, "installed", extensions={"signal": "signal"}, version="2026.6.5")
    monkeypatch.setattr(real_ocs, "installed_oc_root", lambda: installed_root)
    monkeypatch.setattr(update_watcher, "read_installed_openclaw_version",
                        lambda: "2026.6.5")
    monkeypatch.setattr(update_watcher, "resolve_latest_openclaw_version",
                        lambda: ("2026.6.5", None))

    def _no_download(metadata, timeout=30):
        raise AssertionError("must not download a tarball when up to date")

    monkeypatch.setattr(update_watcher, "_import_surface_tools",
                        lambda: (real_ocs, lambda s: {}, _no_download))
    state = {"version": 1}
    drifted = update_watcher.check_openclaw_surface_drift(
        state, env["network"], env["shared_dir"])
    assert drifted is None
    assert _firing_signals(env["shared_dir"]) == []


def test_surface_drift_signal_producer_is_loud_by_default():
    """oc_surface_drift must NOT be on the direct-dispatch deny-list (it would
    silence it); chat routing flows through signal_notifier by default."""
    from evolve_admin.alerts.signal_notifier import _DIRECT_DISPATCH_PRODUCERS
    assert "oc_surface_drift" not in _DIRECT_DISPATCH_PRODUCERS


# ── Platform-aware install/CLI/deploy-checkout paths (Linux port) ─────────────
#
# The macOS-only literals here read OpenClaw's version as None on the Linux VPS
# (openclaw lives at /usr/lib/node_modules there, missing from the candidate
# list) and pointed the deploy-checkout poll at a /Users path that does not
# exist on Linux. Both now derive from platform_profile.


def _with_profile(prof):
    """Context-manager-ish helper: pin a profile, return a restore callable."""
    from platform_profile import get_profile, set_profile
    prev = get_profile()
    set_profile(prof)
    return lambda: set_profile(prev)


def test_install_candidates_linux_includes_nodesource_path():
    from platform_profile import LINUX
    restore = _with_profile(LINUX)
    try:
        cands = [str(p) for p in update_watcher._resolve_openclaw_install_candidates()]
    finally:
        restore()
    assert "/usr/lib/node_modules/openclaw/package.json" in cands
    assert all("homebrew" not in c for c in cands)


def test_install_candidates_macos_keeps_homebrew():
    from platform_profile import MACOS
    restore = _with_profile(MACOS)
    try:
        cands = [str(p) for p in update_watcher._resolve_openclaw_install_candidates()]
    finally:
        restore()
    assert "/opt/homebrew/lib/node_modules/openclaw/package.json" in cands


def test_cli_candidates_linux_includes_usr_bin():
    from platform_profile import LINUX
    restore = _with_profile(LINUX)
    try:
        cands = [str(p) for p in update_watcher._resolve_openclaw_cli_candidates()]
    finally:
        restore()
    assert "/usr/bin/openclaw" in cands
