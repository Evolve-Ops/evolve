"""tests/test_evo_app_create.py — Wave 3 `evo app create` wizard.

Covers the four user paths through the chat flow:
  * describe → draft → approve → commit (manifest + forge job)
  * describe → draft → iterate <feedback> → re-draft
  * describe → draft → cancel
  * describe with reply too short → re-prompt without burning a draft

LLM call (`_build_draft`) is monkeypatched everywhere — these are
contract tests on the engine + handlers, not integration tests against
Anthropic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def network(tmp_path):
    return {
        "sharedDir": str(tmp_path),
        "members": ["admin_bot", "evolve"],
        "bots": {"admin_bot": {"primary_user": {
            "external_ids": {"telegram": "12345"},
        }}},
    }


def _build_draft_stub(*, description, target_bots, shared_dir,
                      previous_draft=None, feedback=None, version=1):
    """Deterministic draft for tests."""
    name = "Iterated App" if previous_draft else "Test App"
    desc = description if not previous_draft else (
        f"{previous_draft.get('description', '')} (+ {feedback})"
    )
    return {
        "version": version,
        "display_name": name,
        "description": desc,
        "build_spec": "# Test\nDo a thing.",
        "application_tags": ["productivity"],
        "requirements": {"integrations": [], "python_packages": []},
        "app_dependencies": [],
        "conflicts": [],
        "suggestions": [],
        "created_at": "2026-05-13T00:00:00Z",
    }


@pytest.fixture
def patched_llm(monkeypatch):
    """Patch _build_draft to a deterministic stub on the import path the
    engine handler uses."""
    import evolve_admin.web.spec_routes as sr
    monkeypatch.setattr(sr, "_build_draft", _build_draft_stub)
    return sr


@pytest.fixture
def patched_commit(monkeypatch, tmp_path):
    """Patch the manifest / forge functions used by the approve path."""
    saved: list = []
    jobs: list[dict] = []
    dispatched: list[tuple[str, str]] = []

    class _FakeJob:
        def __init__(self, job_id):
            self.job_id = job_id

    def fake_save_manifest(manifest, shared_dir):
        saved.append(manifest)

    def fake_create_install_job(*, pkg_id, app_id, bot_id, gallery_version, shared_dir):
        job_id = f"j-{pkg_id}-{bot_id}"
        jobs.append({"pkg_id": pkg_id, "app_id": app_id, "bot_id": bot_id})
        return _FakeJob(job_id)

    def fake_dispatch(job_id, bot_id):
        dispatched.append((job_id, bot_id))
        return True, "ok"

    import evolve_admin.applications.manifest as mfst
    import evolve_admin.applications.forge_jobs as fj
    import evolve_admin.web.spec_routes as sr
    monkeypatch.setattr(mfst, "save_manifest", fake_save_manifest)
    monkeypatch.setattr(fj, "create_install_job", fake_create_install_job)
    monkeypatch.setattr(sr, "_dispatch_forge_job", fake_dispatch)
    return {"saved": saved, "jobs": jobs, "dispatched": dispatched}


def _dispatch(network: dict, raw_text: str, bot_id: str = "admin_bot"):
    from evolve_admin.evo.dispatch import dispatch
    return dispatch(
        network, bot_id=bot_id, channel="telegram",
        sender_external_id="12345", raw_text=raw_text,
    )


def _turn(network: dict, user_message: str, bot_id: str = "admin_bot"):
    from evolve_admin.evo.wizard.engine import process_turn
    from evolve_admin.evo.identity import derive_user_key
    shared_dir = Path(network["sharedDir"])
    user_key = derive_user_key(
        network, bot_id=bot_id, channel="telegram", sender_external_id="12345",
    )
    return process_turn(
        shared_dir, bot_id=bot_id, user_key=user_key,
        user_message=user_message, network=network,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch routing
# ─────────────────────────────────────────────────────────────────────────────


def test_bare_evo_app_returns_usage(network):
    r = _dispatch(network, "evo app")
    body = r.direct_send_message or ""
    assert "Usage" in body and "evo app create" in body
    assert r.wizard_session_id is None


def test_evo_app_create_from_evolve_refused(network):
    r = _dispatch(network, "evo app create", bot_id="evolve")
    body = r.direct_send_message or ""
    assert "sysadmin" in body or "evolve" in body.lower()
    assert r.wizard_session_id is None


def test_evo_app_create_starts_wizard(network, patched_llm):
    r = _dispatch(network, "evo app create")
    assert r.subcommand == "app"
    assert r.wizard_session_id  # session started


# ─────────────────────────────────────────────────────────────────────────────
# Describe path
# ─────────────────────────────────────────────────────────────────────────────


def test_describe_with_short_reply_reprompts(network, patched_llm):
    _dispatch(network, "evo app create")
    r = _turn(network, "ok")
    body = r.system_append or ""
    assert "describe" in body.lower() or "sentence" in body.lower()
    assert r.completed is False


def test_describe_drafts_and_advances_to_review(network, patched_llm):
    _dispatch(network, "evo app create")
    r = _turn(network, "Track my electrical panel inspections and repairs.")
    body = r.system_append or ""
    # Wizard advanced to the review phase (engine state) and rendered the draft
    assert r.phase == "app_create_review"
    # The verbatim draft body has the spec header and the approve/iterate/cancel gate
    assert "Drafted spec" in body or "Revised spec" in body
    assert "approve" in body.lower() and "iterate" in body.lower() and "cancel" in body.lower()
    # The draft summary should reference the test stub's name
    assert "Test App" in body


# ─────────────────────────────────────────────────────────────────────────────
# Iterate path
# ─────────────────────────────────────────────────────────────────────────────


def test_iterate_redrafts_and_stays_in_review(network, patched_llm):
    _dispatch(network, "evo app create")
    _turn(network, "Track my electrical panel inspections and repairs.")
    r = _turn(network, "iterate add quarterly safety checks")
    body = r.system_append or ""
    # Stub returns "Iterated App" when previous_draft is passed
    assert "Iterated App" in body
    assert "revised" in body.lower() or "iterate" in body.lower()
    assert r.completed is False


def test_iterate_with_empty_feedback_reprompts(network, patched_llm):
    _dispatch(network, "evo app create")
    _turn(network, "Track my electrical panel inspections and repairs.")
    r = _turn(network, "iterate")
    body = r.system_append or ""
    assert "what" in body.lower() or "feedback" in body.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Cancel path
# ─────────────────────────────────────────────────────────────────────────────


def test_cancel_terminates_without_commit(network, patched_llm, patched_commit):
    _dispatch(network, "evo app create")
    _turn(network, "Track my electrical panel inspections and repairs.")
    r = _turn(network, "cancel")
    assert r.completed is True
    assert r.wizard_session_id is None
    assert "cancel" in (r.system_append or "").lower() or "dropped" in (r.system_append or "").lower()
    assert not patched_commit["saved"]
    assert not patched_commit["jobs"]
    assert not patched_commit["dispatched"]


# ─────────────────────────────────────────────────────────────────────────────
# Approve path — commit
# ─────────────────────────────────────────────────────────────────────────────


def test_approve_queues_manifest_and_forge_job(network, patched_llm, patched_commit):
    _dispatch(network, "evo app create")
    _turn(network, "Track my electrical panel inspections and repairs.")
    r = _turn(network, "approve")
    assert r.completed is True
    assert r.wizard_session_id is None
    body = r.system_append or ""
    assert "queued" in body.lower() or "wrap" in body.lower()

    # Side effects: manifest saved, install job created + dispatched
    assert len(patched_commit["saved"]) == 1
    manifest = patched_commit["saved"][0]
    assert manifest.bot_id == "admin_bot"
    assert manifest.display_name == "Test App"
    assert manifest.build_spec
    assert len(patched_commit["jobs"]) == 1
    assert patched_commit["jobs"][0]["bot_id"] == "admin_bot"
    assert len(patched_commit["dispatched"]) == 1


@pytest.mark.parametrize("reply", ["yes", "ok", "build", "go", "do it"])
def test_approve_aliases(network, patched_llm, patched_commit, reply):
    """`yes`, `ok`, `build`, `go`, `do it` all approve."""
    _dispatch(network, "evo app create")
    _turn(network, "Some description here that is plenty long.")
    r = _turn(network, reply)
    assert r.completed is True, f"reply={reply!r} did not approve"
    assert len(patched_commit["saved"]) == 1


def test_ambiguous_reply_re_renders_review(network, patched_llm, patched_commit):
    _dispatch(network, "evo app create")
    _turn(network, "Some app description here that is plenty long.")
    r = _turn(network, "what?")
    body = r.system_append or ""
    # Stays in REVIEW; mentions the three options
    assert "approve" in body and "iterate" in body and "cancel" in body
    assert r.completed is False
    # No commit
    assert not patched_commit["saved"]


# ─────────────────────────────────────────────────────────────────────────────
# LLM failure paths
# ─────────────────────────────────────────────────────────────────────────────


def test_describe_handles_llm_failure_gracefully(network, monkeypatch):
    """When `_build_draft` raises, the wizard stays at DESCRIBE so the
    user can retry."""
    import evolve_admin.web.spec_routes as sr

    def boom(*a, **kw):
        raise RuntimeError("no API key")

    monkeypatch.setattr(sr, "_build_draft", boom)
    _dispatch(network, "evo app create")
    r = _turn(network, "Real description here that is long enough.")
    body = r.system_append or ""
    assert "hiccup" in body.lower() or "retry" in body.lower() or "again" in body.lower()
    assert r.completed is False
