"""Phase 3f — forge_engine._notify_operator routes through alerts.dispatcher.

Pins:

  - _notify_operator hands off to dispatcher.send with source="forge_engine"
  - dedup_key is per-job (forge/<job_id>) — every job is a unique event
  - severity=INFO — these are review-ready notifications, not alarms
  - on dispatcher SENT, no on-disk fallback file is written (the chat
    delivery is sufficient)
  - on any non-SENT outcome (suppression, no recipient, failure), the
    discoverable on-disk fallback file IS written — preserves the
    original resilience contract
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.dispatcher import (
        DispatchOutcome, DispatchResult, Severity,
    )
    from evolve_admin.applications import forge_engine

    captured: list = []
    next_result = {"value": DispatchResult.SENT}

    def fake_send(*, shared_dir, network, source, severity,
                  message=None, payload=None,
                  dedup_key=None, catalog_event=None, **_kw):
        captured.append({
            "source": source, "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        return DispatchOutcome(
            result=next_result["value"], source=source, severity=severity,
            dedup_key=dedup_key, channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    def fail_run(*a, **kw):
        raise AssertionError(
            f"forge_engine must not subprocess.run after Phase 3f: {a}"
        )
    monkeypatch.setattr(forge_engine.subprocess, "run", fail_run)

    # forge_engine resolves shared_dir/.. for network.json — point a real
    # network at the right relative spot so its ``cfg = json.loads(...)``
    # block doesn't no-op.
    parent = tmp_path
    shared_dir = parent / "evolve"
    shared_dir.mkdir()
    (parent / "network.json").write_text(json.dumps({
        "alerts": {"channel": "telegram", "chatId": "12345"}
    }))

    return {
        "captured": captured,
        "next_result": next_result,
        "shared_dir": shared_dir,
        "forge_engine": forge_engine,
        "DispatchResult": DispatchResult,
        "Severity": Severity,
    }


def _job():
    """Minimal ForgeJob shape — only the fields _notify_operator reads."""
    class _J:
        job_id = "forge_abc12345"
        app_id = "my-app"
        pkg_id = "my-app@1.0.0"
        bot_id = "team_bot_a"
        job_type = "create"
        critique_rounds_done = 2
        issues_found = 5
        issues_resolved = 5
        test_exit_code = 0
    return _J()


def _notify_path(shared_dir, job):
    return shared_dir / "forge" / "logs" / f"{job.job_id}.notify"


def test_notify_routes_through_dispatcher_with_per_job_dedup(env):
    job = _job()
    env["forge_engine"]._notify_operator(job, env["shared_dir"])
    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["source"] == "forge_engine"
    assert call["dedup_key"] == f"forge/{job.job_id}"
    assert call["catalog_event"] == "decisions.forge_job_ready"
    assert call["severity"] == env["Severity"].INFO
    # Phase F5: payload-driven; catalog body_template renders.
    assert call["message"] is None
    assert call["payload"] == {
        "app_id": job.app_id,
        "pkg_id": job.pkg_id,
        "bot_id": job.bot_id,
        "job_type": job.job_type,
    }


def test_no_fallback_file_when_dispatcher_sent(env):
    """Successful dispatch makes the fallback file unnecessary — chat
    delivery is the operator's signal."""
    job = _job()
    env["forge_engine"]._notify_operator(job, env["shared_dir"])
    notify_file = _notify_path(env["shared_dir"], job)
    assert not notify_file.exists()


def test_fallback_file_written_when_dispatcher_disabled(env):
    """Operator muted forge_engine via alerts.forge_engine.enabled=false:
    chat is silent BUT we still want the job discoverable on disk so
    the operator (or a future review tool) can find pending work."""
    env["next_result"]["value"] = env["DispatchResult"].SUPPRESSED_DISABLED
    job = _job()
    env["forge_engine"]._notify_operator(job, env["shared_dir"])
    notify_file = _notify_path(env["shared_dir"], job)
    assert notify_file.exists()
    assert "ready for review" in notify_file.read_text()


def test_fallback_file_written_when_dispatcher_failed(env):
    env["next_result"]["value"] = env["DispatchResult"].FAILED
    job = _job()
    env["forge_engine"]._notify_operator(job, env["shared_dir"])
    notify_file = _notify_path(env["shared_dir"], job)
    assert notify_file.exists()


def test_fallback_file_written_when_no_recipient(env):
    """No alerts channel configured → dispatcher returns NO_RECIPIENT;
    fallback ensures the job is still discoverable."""
    env["next_result"]["value"] = env["DispatchResult"].NO_RECIPIENT
    job = _job()
    env["forge_engine"]._notify_operator(job, env["shared_dir"])
    notify_file = _notify_path(env["shared_dir"], job)
    assert notify_file.exists()
