"""Deploy-resilience against a starved box (A + C1, incident 2026-06-24).

The 2026-06-24 mini incident: an operator upgrade raced the scheduled repo-
puller redeploy sweep on a memory-starved box. Every deploy subprocess stalled
past its hardcoded timeout, and the recursive chown/chmod perm-passes storm
Spotlight (mds_stores held 582 MB). Two gaps are closed here:

Part A — plant ``.metadata_never_index`` markers so Spotlight stops INDEXING
the churny bot state trees. macOS-only; Linux no-ops.

Part C1 — a single pod-wide flock (``{shared_dir}/deploy.lock``) so a manual
web upgrade and the puller redeploy sweep cannot run concurrently.

Each test fails against current main (the marker/lock didn't exist) and passes
with the change.
"""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402
from evolve_admin import deploy_resilience as dres  # noqa: E402
from evolve_admin import repo_puller  # noqa: E402


# ── Part A: Spotlight-exclusion marker ────────────────────────────────────────


def test_marker_pod_wide_direct_write_plants_idempotently(tmp_path):
    """via_sudo=False (the {shared_dir} chokepoint) writes the marker directly
    and is idempotent: a second call when it already exists is a no-op."""
    planted = dres.plant_never_index_marker(tmp_path, via_sudo=False)
    marker = tmp_path / ".metadata_never_index"
    assert planted is True
    assert marker.exists()

    # Second call: marker present → skip (idempotent, no rewrite).
    again = dres.plant_never_index_marker(tmp_path, via_sudo=False)
    assert again is False


def test_marker_per_bot_uses_sudo_touch_when_missing(tmp_path, monkeypatch):
    """via_sudo=True (the bot-owned .openclaw/ chokepoint, only r-x to evolve)
    plants via `sudo /usr/bin/touch <marker>` exactly when the marker is
    missing."""
    calls: list[list[str]] = []

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **kw):
        calls.append(list(cmd))
        return _Ok()

    monkeypatch.setattr(dres.subprocess, "run", _run)

    planted = dres.plant_never_index_marker(tmp_path, via_sudo=True)
    assert planted is True
    assert calls == [
        ["sudo", "/usr/bin/touch", str(tmp_path / ".metadata_never_index")]
    ]


def test_marker_per_bot_skips_sudo_when_already_present(tmp_path, monkeypatch):
    """Idempotent: an existing marker short-circuits before any sudo spawn."""
    (tmp_path / ".metadata_never_index").touch()

    def _boom(*a, **k):  # pragma: no cover — must not be reached
        raise AssertionError(f"unexpected subprocess spawn: {a!r}")

    monkeypatch.setattr(dres.subprocess, "run", _boom)

    assert dres.plant_never_index_marker(tmp_path, via_sudo=True) is False


def test_marker_sudo_failure_is_non_fatal(tmp_path, monkeypatch):
    """A sudo failure (e.g. sudoers not yet refreshed) returns False, never
    raises — the marker must never block a deploy."""

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "sudo: a password is required"

    monkeypatch.setattr(dres.subprocess, "run", lambda *a, **k: _Fail())
    assert dres.plant_never_index_marker(tmp_path, via_sudo=True) is False


def test_marker_is_noop_on_non_macos_profile(tmp_path, monkeypatch):
    """Linux has no Spotlight — the platform-profile guard hard no-ops, writing
    nothing and spawning nothing (covers BOTH the direct and sudo paths)."""
    import platform_profile as pp

    monkeypatch.setattr(dres, "_PROFILE", pp.LINUX)

    def _boom(*a, **k):  # pragma: no cover — must not be reached on Linux
        raise AssertionError(f"unexpected subprocess spawn on Linux: {a!r}")

    monkeypatch.setattr(dres.subprocess, "run", _boom)

    assert dres.plant_never_index_marker(tmp_path, via_sudo=False) is False
    assert dres.plant_never_index_marker(tmp_path, via_sudo=True) is False
    assert not (tmp_path / ".metadata_never_index").exists()


def test_set_evolve_read_acl_wires_per_bot_marker(tmp_path, monkeypatch):
    """The per-bot chokepoint (set_evolve_read_acl) plants the marker in the
    bot's .openclaw/ via sudo — wiring proof for Part A."""
    from evolve_admin.runtime import FakePerms, set_perms

    oc = tmp_path / ".openclaw"
    oc.mkdir()

    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id, *a, **k: "testbot")
    monkeypatch.setattr(deploy, "_user_home", lambda user: tmp_path)
    # Stub the heavy ACL contract + verify so the function reaches the plant.
    monkeypatch.setattr(deploy, "_apply_openclaw_read_contract", lambda *a, **k: True)
    monkeypatch.setattr(deploy._secret_perms, "verify_evolve_access", lambda *a, **k: None)
    set_perms(FakePerms())

    recorded: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        dres, "plant_never_index_marker",
        lambda parent, *, via_sudo, enabled=True: recorded.append((Path(parent), via_sudo)) or True,
    )
    try:
        deploy.set_evolve_read_acl("anybot")
    finally:
        set_perms(None)

    assert (oc, True) in recorded


# ── Part C1: pod-wide deploy lock ─────────────────────────────────────────────


def test_deploy_lock_mutual_exclusion(tmp_path):
    """A second concurrent acquirer takes the 'already running' branch (None) —
    it does NOT block or raise. Releasing frees the lock for the next acquirer."""
    h1 = dres.try_acquire_deploy_lock(tmp_path)
    assert h1 is not None and h1 is not dres._DEPLOY_LOCK_UNLOCKED

    h2 = dres.try_acquire_deploy_lock(tmp_path)
    assert h2 is None  # held → non-blocking miss, not a crash

    dres.release_deploy_lock(h1)

    h3 = dres.try_acquire_deploy_lock(tmp_path)
    assert h3 is not None and h3 is not dres._DEPLOY_LOCK_UNLOCKED
    dres.release_deploy_lock(h3)


def test_deploy_lock_fail_open_when_unopenable(tmp_path):
    """If the lock FILE can't be opened (misconfigured shared_dir), fail OPEN —
    return the sentinel (truthy, ≠ None) so a transient glitch can't wedge every
    deploy. release on the sentinel is a safe no-op."""
    bad = tmp_path / "does-not-exist"  # parent missing → open() raises
    handle = dres.try_acquire_deploy_lock(bad)
    assert handle is dres._DEPLOY_LOCK_UNLOCKED
    assert handle is not None
    dres.release_deploy_lock(handle)  # must not raise


def test_deploy_lock_context_manager_held_branch(tmp_path):
    """deploy_lock() yields None when another holder has the lock."""
    external = open(tmp_path / "deploy.lock", "a+")
    fcntl.flock(external, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with dres.deploy_lock(tmp_path) as lk:
            assert lk is None
    finally:
        fcntl.flock(external, fcntl.LOCK_UN)
        external.close()

    # Lock released → context manager now acquires.
    with dres.deploy_lock(tmp_path) as lk:
        assert lk is not None and lk is not dres._DEPLOY_LOCK_UNLOCKED


def test_redeploy_sweep_skips_when_lock_held(tmp_path, monkeypatch):
    """The repo-puller redeploy sweep skips (and logs a legible step) when a
    manual upgrade holds the pod deploy lock — it must NOT run the deploy."""
    ran: list[bool] = []
    monkeypatch.setattr(
        repo_puller, "_redeploy_lagging_bots",
        lambda repo, shared_dir: ran.append(True) or ([], {}),
    )

    external = open(tmp_path / "deploy.lock", "a+")
    fcntl.flock(external, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = repo_puller.PullResult(success=True)
        repo_puller._run_lagging_bot_redeploy_sweep(result, tmp_path, tmp_path)
    finally:
        fcntl.flock(external, fcntl.LOCK_UN)
        external.close()

    assert ran == []  # deploy never ran while the lock was held
    assert any("skipped redeploy sweep" in s for s in result.steps)


def test_redeploy_sweep_runs_when_lock_free(tmp_path, monkeypatch):
    """Control: with the lock free, the sweep proceeds and decorates result."""
    monkeypatch.setattr(
        repo_puller, "_redeploy_lagging_bots",
        lambda repo, shared_dir: (["bot1"], {}),
    )
    result = repo_puller.PullResult(success=True)
    repo_puller._run_lagging_bot_redeploy_sweep(result, tmp_path, tmp_path)

    assert result.lagging_bots_redeployed == ["bot1"]
    assert any("redeployed 1 lagging bot" in s for s in result.steps)


def test_manual_upgrade_reports_already_in_progress_when_lock_held(tmp_path, monkeypatch):
    """POST /api/upgrade returns a legible 409 (not a queued/silent deploy) when
    the scheduled redeploy sweep already holds the pod deploy lock."""
    from evolve_admin.web import server as srv

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network = {
        "networkId": "pod-test",
        "sharedDir": str(shared_dir),
        "bots": {},
        "members": [],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Direct (non-canary) release mode → canary guard returns None; patch to be
    # robust regardless of the test pod's release config.
    import evolve_admin.release_manager as rm
    monkeypatch.setattr(rm, "canary_upgrade_block", lambda net: None)
    srv._active_job_id.clear()  # no stale in-process job guard

    app = srv.create_app(network_path)
    client = app.test_client()

    # Simulate the scheduled redeploy sweep holding the lock.
    external = open(shared_dir / "deploy.lock", "a+")
    fcntl.flock(external, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        resp = client.post("/api/upgrade", json={})
        assert resp.status_code == 409
        assert "already in progress" in resp.get_json()["error"]
    finally:
        fcntl.flock(external, fcntl.LOCK_UN)
        external.close()
