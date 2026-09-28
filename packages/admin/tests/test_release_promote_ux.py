"""tests/test_release_promote_ux.py — the web "Complete soak now" button maps
``release_promote()`` outcomes to truthful, self-contained operator messages.

Incident (2026-06-14, META:deploy D-1): the operator clicked "Complete soak
now" and got a red toast — ``Promote failed: tick ran but did not promote —
see steps above`` — but the web UI showed no steps and nothing had failed. The
cause was a **benign candidate-replacement race**: ``origin/main`` advanced
while promoting, so the normal tick under ``release_promote`` correctly
superseded the old candidate with the newer commit and restarted Gate 1,
returning an empty ``promoted_to`` and empty ``error``. The web handler then
fell through to a generic CLI-flavored failure string.

These tests pin the fix:
  * ``classify_promote_outcome`` distinguishes promoted / replaced (benign) /
    noop, and never references "steps above".
  * the background job for a benign race finishes as a NON-error completion
    (so the banner renders it neutrally, not red), carrying the info message.
  * a genuine error still finishes as a failure, with a self-contained message.
  * the overview.js wording no longer says "see steps above".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import release_manager as rm  # noqa: E402
from evolve_admin.web.release_routes import classify_promote_outcome  # noqa: E402

_OVERVIEW_JS = (_ADMIN_DIR / "evolve_admin" / "web" / "static"
                / "js" / "pages" / "overview.js")

_OLD_SHA = "aaaaaaaaaaaa1111111111111111111111111111"   # the candidate we click
_NEW_SHA = "bbbbbbbbbbbb2222222222222222222222222222"   # the one that arrives


# ── Pure decision helper ─────────────────────────────────────────────────────


def test_classify_promoted_is_success():
    rt = rm.ReleaseTickResult(promoted_to=_NEW_SHA)
    kind, msg = classify_promote_outcome(rt, _OLD_SHA[:12])
    assert kind == "promoted"
    assert _NEW_SHA[:12] in msg


def test_classify_benign_replacement_is_not_an_error():
    """The incident case: a NEWER candidate took over mid-promote. Empty
    promoted_to + empty error, but a different candidate_sha now in flight.
    This must classify as the non-error ``replaced`` kind — never ``noop``."""
    rt = rm.ReleaseTickResult(
        promoted_to="", error="", candidate_sha=_NEW_SHA, candidate_state="soaking",
        steps=[f"candidate {_OLD_SHA[:12]} superseded by {_NEW_SHA[:12]} (clocks reset)"],
    )
    kind, msg = classify_promote_outcome(rt, _OLD_SHA[:12])
    assert kind == "replaced"
    assert _NEW_SHA[:12] in msg
    assert "soaking" in msg
    # The message must stand alone — the web flow has no "steps above".
    assert "steps above" not in msg


def test_classify_replacement_still_checking_tells_operator_to_wait():
    rt = rm.ReleaseTickResult(candidate_sha=_NEW_SHA, candidate_state="checking")
    kind, msg = classify_promote_outcome(rt, _OLD_SHA[:12])
    assert kind == "replaced"
    assert "Gate 1" in msg and "once it begins soaking" in msg


def test_classify_replacement_that_failed_gate1_is_truthful():
    """A superseding candidate that fails Gate 1 is still not the operator's
    promote failing — but the wording must say it failed, not that it's
    'running checks'."""
    rt = rm.ReleaseTickResult(candidate_sha=_NEW_SHA, candidate_state="failed")
    kind, msg = classify_promote_outcome(rt, _OLD_SHA[:12])
    assert kind == "replaced"
    assert "failed its Gate 1 checks" in msg


def test_classify_real_error_is_surfaced_verbatim():
    rt = rm.ReleaseTickResult(promoted_to="", error="fleet move failed",
                              candidate_sha=_OLD_SHA, candidate_state="soaking")
    kind, msg = classify_promote_outcome(rt, _OLD_SHA[:12])
    assert kind == "noop"
    assert msg == "fleet move failed"


def test_classify_same_candidate_failed_is_truthful():
    """Same candidate (no replacement) that fails its checks at promote time:
    the generic 'already promoted / nothing in flight' wording would lie, so
    this gets a failure-specific self-contained message."""
    rt = rm.ReleaseTickResult(promoted_to="", error="", candidate_sha=_OLD_SHA,
                              candidate_state="failed")
    kind, msg = classify_promote_outcome(rt, _OLD_SHA[:12])
    assert kind == "noop"
    assert "failed its checks during promotion" in msg
    assert "no candidate is in flight" not in msg
    assert "steps above" not in msg


def test_classify_silent_noop_gets_self_contained_message():
    """No promote, no new candidate, no error string — the genuine no-op. The
    fallback message must be self-contained (no 'see steps above')."""
    rt = rm.ReleaseTickResult(promoted_to="", error="", candidate_sha="", candidate_state="")
    kind, msg = classify_promote_outcome(rt, _OLD_SHA[:12])
    assert kind == "noop"
    assert "No candidate was promoted" in msg
    assert "steps above" not in msg


# ── End-to-end wiring through /api/release/promote ───────────────────────────


class _SyncThread:
    """Stand-in for ``threading.Thread`` that runs the target on ``.start()``,
    so the background promote job reaches a terminal state synchronously within
    the request — no polling, no sleeps, no flakiness."""

    def __init__(self, *a, target=None, daemon=None, **k):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class _FakeThreadingModule:
    Thread = _SyncThread


@pytest.fixture()
def promote_client(tmp_path, monkeypatch):
    """A test client whose pre-flight always sees a soaking candidate, with an
    injectable ``release_promote``. Runs the job synchronously."""
    from evolve_admin.web import server
    from evolve_admin.web import release_routes

    net_file = tmp_path / "network.json"
    net_file.write_text('{"sharedDir": "%s"}' % (tmp_path / "shared"))
    (tmp_path / "shared").mkdir()

    # Pre-flight: canary mode + a soaking candidate (the one the operator clicks).
    monkeypatch.setattr(
        rm, "resolve_release_config",
        lambda net: rm.ReleaseConfig(mode="canary", canary_bot="canary_bot", soak_minutes=20),
    )
    monkeypatch.setattr(
        rm, "load_release_state",
        lambda shared_dir: rm.ReleaseState(
            stable={"sha": "0" * 40, "version": "2026.0614.0001"},
            candidate={"sha": _OLD_SHA, "state": "soaking",
                       "soak_started_at": "2026-06-14T00:00:00+00:00"},
        ),
    )
    # Run the promote job inline rather than on a real thread.
    monkeypatch.setattr(release_routes, "threading", _FakeThreadingModule)
    # No prior job may be active, or the route 409s.
    server._active_job_id.clear()

    app = server.create_app(network_path=net_file)
    app.config["TESTING"] = True

    def _run(fake_release_promote):
        monkeypatch.setattr(rm, "release_promote", fake_release_promote)
        with app.test_client() as c:
            resp = c.post("/api/release/promote")
        assert resp.status_code == 202, resp.get_json()
        job_id = resp.get_json()["jobId"]
        return server._jobs[job_id]

    return _run


def test_benign_replacement_job_is_not_a_failure(promote_client):
    """Before the fix this finished as ``status='failed'`` with the misleading
    'see steps above'. After: a non-error completion carrying ``result.info``,
    plus the step log the operator can actually read."""
    def fake_promote(repo, shared_dir):
        return rm.ReleaseTickResult(
            promoted_to="", error="", candidate_sha=_NEW_SHA, candidate_state="soaking",
            steps=[f"candidate {_OLD_SHA[:12]} superseded by {_NEW_SHA[:12]} (clocks reset)",
                   f"new candidate {_NEW_SHA[:12]}", "gate1: pass — ok"],
        )

    job = promote_client(fake_promote)
    assert job["status"] == "complete"           # NOT "failed"
    assert job["error"] is None                  # NOT a red error toast
    assert job["result"]["info"].startswith(f"A newer commit ({_NEW_SHA[:12]})")
    assert "steps above" not in job["result"]["info"]
    # The step log is captured on the job so the client can render it inline.
    logged = " ".join(entry["msg"] for entry in job["log"])
    assert "superseded by" in logged and "gate1: pass" in logged


def test_real_promote_finishes_complete_with_promoted_to(promote_client):
    def fake_promote(repo, shared_dir):
        return rm.ReleaseTickResult(promoted_to=_NEW_SHA, steps=["promoted"])

    job = promote_client(fake_promote)
    assert job["status"] == "complete"
    assert job["error"] is None
    assert job["result"]["promoted_to"] == _NEW_SHA
    assert "info" not in job["result"]           # so the client shows the success path


def test_genuine_error_still_finishes_failed_self_contained(promote_client):
    def fake_promote(repo, shared_dir):
        return rm.ReleaseTickResult(promoted_to="", error="", candidate_sha="",
                                    candidate_state="", steps=["tick: nothing to do"])

    job = promote_client(fake_promote)
    assert job["status"] == "failed"
    assert job["error"] and "steps above" not in job["error"]
    assert "No candidate was promoted" in job["error"]


# ── Frontend wording (static guard) ──────────────────────────────────────────


def test_overview_js_no_longer_references_phantom_steps():
    """The web promote flow has no 'steps above'; the dead phrase must be gone
    and the inline step-log renderer must be present."""
    src = _OVERVIEW_JS.read_text()
    assert "see steps above" not in src
    assert "_renderPromoteOutcome" in src       # step log surfaced inline
    assert "result.info" in src                 # benign-race branch handled
