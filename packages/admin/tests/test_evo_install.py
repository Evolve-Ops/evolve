"""tests/test_evo_install.py — `evo install` handler.

Mocks the gallery + forge functions so we test the handler's shape
(arg parsing, lookup, preflight gating, OAuth deferral, already-installed
short-circuit) without spinning up forge_engine.
"""

from __future__ import annotations

import json
import sys
from collections import namedtuple
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for path in (str(_ADMIN_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


_PwResult = namedtuple("_PwResult", ["pw_dir"])


@dataclass
class FakeJob:
    job_id: str = "j-fake-001"
    run_id: str = "r-fake-001"
    context_snapshot: dict = field(default_factory=dict)


@pytest.fixture
def patched(tmp_path, monkeypatch):
    """Wire up fake gallery + forge + pwd lookups.

    Catalog is set by tests via ``patched["catalog"]``. Default empty.
    """
    state = {
        "catalog": [],
        "homes": {},  # bot_user -> Path
        "oauth_missing": [],  # list of dicts to return for prereq checks
        "preflight_blockers": [],  # list of dicts to return as blockers
        "jobs_created": [],
        "saved": [],
        "dispatched": [],
    }

    # Mock pwd
    def fake_pwnam(user: str):
        if user in state["homes"]:
            return _PwResult(pw_dir=str(state["homes"][user]))
        raise KeyError(user)

    from evolve_admin.evo.handlers import install as ih
    monkeypatch.setattr(ih.pwd, "getpwnam", fake_pwnam)

    # Mock gallery
    def fake_load(pkg_id, shared_dir):
        for p in state["catalog"]:
            if p.get("pkg_id") == pkg_id:
                return p
        return None

    def fake_list(shared_dir, bot_ids=None):
        return list(state["catalog"])

    def fake_preflight(pkg_id, bot_id, shared_dir):
        return {
            "app_dependencies": [],
            "requirements": state["preflight_blockers"],
        }

    import evolve_admin.applications.gallery as gallery_mod
    monkeypatch.setattr(gallery_mod, "load_gallery_package", fake_load)
    monkeypatch.setattr(gallery_mod, "list_gallery_packages", fake_list)
    monkeypatch.setattr(gallery_mod, "preflight_check", fake_preflight)

    # Mock forge_jobs
    def fake_create_install_job(*, pkg_id, app_id, bot_id, gallery_version,
                                shared_dir):
        job = FakeJob(job_id=f"j-{pkg_id}-{bot_id}")
        state["jobs_created"].append(
            {"pkg_id": pkg_id, "app_id": app_id, "bot_id": bot_id}
        )
        return job

    def fake_save_job(job, shared_dir):
        state["saved"].append(job)

    import evolve_admin.applications.forge_jobs as fj_mod
    monkeypatch.setattr(fj_mod, "create_install_job", fake_create_install_job)
    monkeypatch.setattr(fj_mod, "save_job", fake_save_job)

    # Mock OAuth orchestrator
    def fake_eval_prereqs(bot_id, requirements, *, shared_dir):
        if state["oauth_missing"]:
            return {"satisfied": False, "missing": state["oauth_missing"]}
        return {"satisfied": True, "missing": []}

    import evolve_admin.applications.oauth_orchestrator as oo_mod
    monkeypatch.setattr(oo_mod, "evaluate_install_prerequisites",
                        fake_eval_prereqs)

    # The gallery handler's _load_catalog reads the real catalog.json on
    # disk for `evo install <n>`. Stub it so number-form tests resolve
    # against the test's fake catalog, not the real one.
    import evolve_admin.evo.handlers.gallery as gallery_handler
    monkeypatch.setattr(gallery_handler, "_load_catalog",
                        lambda: list(state["catalog"]))

    # Stub the async dispatch so threads don't run
    monkeypatch.setattr(ih, "_dispatch_forge_job_async", lambda *a, **kw: None)

    return state, tmp_path


def _call(patched, bot_id: str, args: str):
    from evolve_admin.evo.handlers.install import render
    network = {"sharedDir": str(patched[1]),
               "members": [bot_id, "evolve"]}
    return render(role="primary", bot_id=bot_id, args=args, network=network)


# ─────────────────────────────────────────────────────────────────────────────
# Arg / usage
# ─────────────────────────────────────────────────────────────────────────────


def test_no_args_shows_usage(patched):
    r = _call(patched, "admin_bot", "")
    body = r.direct_send_message or ""
    assert "Usage" in body
    assert "evo gallery" in body


def test_pod_caller_refused(patched):
    r = _call(patched, "evolve", "anything")
    body = r.direct_send_message or ""
    # Body should redirect the operator to call from the target bot.
    assert "primary" in body.lower() or "sysadmin" in body.lower()
    assert "from the bot you want to install on" in body


def test_unknown_package(patched):
    r = _call(patched, "admin_bot", "nonexistent-app")
    body = r.direct_send_message or ""
    assert "No gallery app matching" in body


def test_ambiguous_prefix(patched):
    state, _ = patched
    state["catalog"] = [
        {"pkg_id": "p1", "name": "Daily Tracker", "requirements": {}},
        {"pkg_id": "p2", "name": "Daily Logger", "requirements": {}},
    ]
    r = _call(patched, "admin_bot", "Daily")
    body = r.direct_send_message or ""
    assert "matches more than one" in body
    assert "Daily Tracker" in body
    assert "Daily Logger" in body


# ─────────────────────────────────────────────────────────────────────────────
# Already installed
# ─────────────────────────────────────────────────────────────────────────────


def test_already_installed_short_circuit(patched):
    state, tmp_path = patched
    state["catalog"] = [
        {"pkg_id": "p1", "name": "Daily Tracker", "requirements": {}},
    ]
    admin_bot_home = tmp_path / "admin_bot"
    state["homes"]["admin_bot"] = admin_bot_home
    manifests = admin_bot_home / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "app_daily.json").write_text(json.dumps({
        "name": "Daily Tracker", "id": "app_daily",
    }))
    r = _call(patched, "admin_bot", "Daily Tracker")
    body = r.direct_send_message or ""
    assert "already installed" in body.lower()
    assert not state["jobs_created"]  # nothing queued


# ─────────────────────────────────────────────────────────────────────────────
# OAuth deferral
# ─────────────────────────────────────────────────────────────────────────────


def test_oauth_required_punts_to_admin_ui(patched):
    state, _ = patched
    state["catalog"] = [
        {"pkg_id": "p1", "name": "Gmail Companion", "requirements": {}},
    ]
    state["oauth_missing"] = [
        {"integration": "Google (GOG)", "reason": "no token"},
    ]
    r = _call(patched, "admin_bot", "Gmail Companion")
    body = r.direct_send_message or ""
    assert "OAuth" in body
    assert "Google (GOG)" in body
    assert "admin Apps page" in body
    assert not state["jobs_created"]


# ─────────────────────────────────────────────────────────────────────────────
# Preflight blockers
# ─────────────────────────────────────────────────────────────────────────────


def test_preflight_blockers_reported(patched):
    state, _ = patched
    state["catalog"] = [
        {"pkg_id": "p1", "name": "Tool", "requirements": {}},
    ]
    state["preflight_blockers"] = [
        {"id": "git", "display_name": "git", "severity": "build_blocker",
         "state": "missing", "message": "git not installed",
         "type": "system"},
    ]
    r = _call(patched, "admin_bot", "Tool")
    body = r.direct_send_message or ""
    assert "can't be installed yet" in body
    assert "git" in body
    assert not state["jobs_created"]


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_happy_path_queues_forge_job(patched):
    state, _ = patched
    state["catalog"] = [
        {"pkg_id": "p1", "name": "Tool", "requirements": {},
         "pkg_version": "1.0", "build_spec": "build it"},
    ]
    r = _call(patched, "admin_bot", "Tool")
    body = r.direct_send_message or ""
    assert "Queued" in body and "Tool" in body
    assert "evo apps" in body
    assert len(state["jobs_created"]) == 1
    job = state["jobs_created"][0]
    assert job["pkg_id"] == "p1"
    assert job["bot_id"] == "admin_bot"
    assert job["app_id"] == "tool"


# ─────────────────────────────────────────────────────────────────────────────
# Number form: `evo install <n>`
# ─────────────────────────────────────────────────────────────────────────────


def test_number_form_picks_nth_available(patched):
    """`evo install 1` resolves to the same first item the gallery
    handler renders (alphabetical by name)."""
    state, _ = patched
    state["catalog"] = [
        {"pkg_id": "p-zeta", "name": "Zeta App", "requirements": {}},
        {"pkg_id": "p-alpha", "name": "Alpha App", "requirements": {}},
        {"pkg_id": "p-beta", "name": "Beta App", "requirements": {}},
    ]
    r = _call(patched, "admin_bot", "1")
    body = r.direct_send_message or ""
    assert "Queued" in body and "Alpha App" in body  # alphabetical first
    assert len(state["jobs_created"]) == 1
    assert state["jobs_created"][0]["pkg_id"] == "p-alpha"


def test_number_form_accepts_hash_prefix(patched):
    """`evo install #2` works just like `evo install 2`."""
    state, _ = patched
    state["catalog"] = [
        {"pkg_id": "p-a", "name": "Alpha App", "requirements": {}},
        {"pkg_id": "p-b", "name": "Beta App", "requirements": {}},
    ]
    r = _call(patched, "admin_bot", "#2")
    body = r.direct_send_message or ""
    assert "Beta App" in body


def test_number_form_out_of_range(patched):
    state, _ = patched
    state["catalog"] = [
        {"pkg_id": "p-a", "name": "Alpha App", "requirements": {}},
    ]
    r = _call(patched, "admin_bot", "99")
    body = r.direct_send_message or ""
    assert "Pick a number between 1 and 1" in body
    assert not state["jobs_created"]


def test_number_form_zero_is_invalid(patched):
    state, _ = patched
    state["catalog"] = [
        {"pkg_id": "p-a", "name": "A", "requirements": {}},
    ]
    r = _call(patched, "admin_bot", "0")
    body = r.direct_send_message or ""
    assert "Pick a number" in body
    assert not state["jobs_created"]


def test_number_form_when_nothing_installable(patched):
    state, tmp_path = patched
    state["catalog"] = [
        {"pkg_id": "p-a", "name": "A", "requirements": {}},
    ]
    # Pre-install the only app so available list is empty
    admin_bot = tmp_path / "admin_bot"
    state["homes"]["admin_bot"] = admin_bot
    manifests = admin_bot / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "app_a.json").write_text(json.dumps({"name": "A", "id": "a"}))
    r = _call(patched, "admin_bot", "1")
    body = r.direct_send_message or ""
    assert "No installable apps" in body
    assert not state["jobs_created"]


def test_no_args_mentions_number_form(patched):
    r = _call(patched, "admin_bot", "")
    body = r.direct_send_message or ""
    assert "evo install 1" in body or "<n>" in body
