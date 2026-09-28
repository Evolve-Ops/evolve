"""Regression: the web UPGRADE orchestrator must heal evolve→.openclaw access
BEFORE the per-bot deploy loop.

Incident 2026-06-23 (DigitalOcean Ubuntu VPS, bots darwin+evo): a version
upgrade failed with "Upgrade failed — 0 of 2 bot(s) upgraded", EACCES on
``/home/<bot>/.openclaw/plugins/installs.json``. Root cause: the OC gateway
hardens each bot's ``~/.openclaw`` to 0700; on Linux that chmod recalculates the
POSIX ACL mask to ``mask::---``, clamping evolve's named-user ``rX`` ACE to
``#effective:---`` so evolve cannot even *traverse* ``.openclaw``. The fresh
INSTALL flow runs the access heal (``ensure_pod_perms`` →
``check_evolve_access`` → ``heal_evolve_access`` re-asserts the mask via
``setfacl -m m::rwX`` and re-verifies); the web upgrade orchestrator did NOT —
it called ``ensure_plugin_config`` / ``install_oc_plugin`` / ``deploy_bot``
directly with no pre-deploy heal. This pins the ordering invariant so the
regression cannot recur silently.

The test is platform-agnostic: the heal itself is mocked, so we assert only the
ORCHESTRATOR ORDER (heal before per-bot work), which is the regression. The
real on-Linux ACL behaviour is exercised by the e2e harness
(``tests/e2e_linux/test_ubuntu_e2e.py`` install→upgrade cycle).
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def upgrade_client(tmp_path, monkeypatch):
    from evolve_admin.web import server

    shared = tmp_path / "shared"
    shared.mkdir()
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps({
        "networkId": "test-net",
        "bots": {"darwin": {"role": "member", "port": 19031}},
        "members": ["darwin"],
        "sharedDir": str(shared),
        "repoPath": str(tmp_path / "repo"),
    }))

    # Module-global job state persists across apps — reset so the concurrency
    # guard (``if _active_job_id``) doesn't 409 us from a prior test.
    server._active_job_id.clear()
    server._jobs.clear()

    calls: list[str] = []

    def _record(name, ret=None):
        def _fn(*_a, **_k):
            calls.append(name)
            return ret
        return _fn

    # The per-bot deploy primitives + the heal, recording call order.
    monkeypatch.setattr(
        server, "ensure_pod_perms",
        _record("ensure_pod_perms", SimpleNamespace(applied=[], errors=[])),
    )
    monkeypatch.setattr(server, "ensure_plugin_config", _record("ensure_plugin_config"))
    monkeypatch.setattr(server, "install_oc_plugin", _record("install_oc_plugin"))
    monkeypatch.setattr(
        server, "deploy_bot",
        _record("deploy_bot", SimpleNamespace(success=True, errors=[])),
    )
    monkeypatch.setattr(server, "record_bot_deploy", _record("record_bot_deploy"))

    # Neuter the surrounding upgrade machinery (orphan sweep / git pull /
    # install.json write / admin restart) so the thread completes cleanly
    # without touching the real system.
    monkeypatch.setattr(server, "find_orphaned_plists", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "write_install_json", _record("write_install_json"))
    monkeypatch.setattr(server, "read_install_json", lambda *_a, **_k: {})
    # git pull / chown run via stdlib subprocess inside the closure.
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    import evolve_admin.service as _svc
    monkeypatch.setattr(_svc, "restart", lambda *_a, **_k: (True, "restart skipped (test)"))

    app = server.create_app(network_path=net_file)
    app.config["TESTING"] = True
    client = app.test_client()
    return client, calls


def _wait_for_job(client, job_id, timeout=10.0):
    """Poll the job until it leaves the running state. The job is marked
    complete BEFORE the (mocked) admin restart, so this returns promptly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        body = resp.get_json()
        if body and body.get("status") in ("complete", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_upgrade_heals_access_before_per_bot_deploy(upgrade_client):
    client, calls = upgrade_client

    # skipPlugin avoids the TypeScript rebuild step; the per-bot loop (and the
    # heal that must precede it) still runs.
    resp = client.post("/api/upgrade", json={"botId": "darwin", "skipPlugin": True})
    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    job_id = resp.get_json()["jobId"]

    job = _wait_for_job(client, job_id)
    assert job["status"] == "complete", job

    # The heal ran...
    assert "ensure_pod_perms" in calls, calls
    # ...and it ran BEFORE any per-bot work that touches .openclaw as evolve.
    heal_at = calls.index("ensure_pod_perms")
    for downstream in ("ensure_plugin_config", "install_oc_plugin", "deploy_bot"):
        assert downstream in calls, calls
        assert heal_at < calls.index(downstream), (
            f"{downstream} ran before the access heal — the 2026-06-23 "
            f"upgrade-path EACCES regression. order={calls}"
        )


def test_upgrade_dry_run_skips_heal(upgrade_client):
    """Dry-run mutates nothing — including the heal (which runs setfacl/chmod).
    Confirms the heal is gated on ``not dry_run`` so a dry-run upgrade stays a
    pure no-op."""
    client, calls = upgrade_client
    resp = client.post("/api/upgrade", json={"botId": "darwin", "dryRun": True})
    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    job = _wait_for_job(client, resp.get_json()["jobId"])
    assert job["status"] == "complete", job
    assert "ensure_pod_perms" not in calls, calls
