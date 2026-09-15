"""tests/test_evo_post_review_patches.py — Pass-2 review fix-ups.

Covers the four bundled fixes:

  1. ``is_pod_wide_caller`` is case-insensitive + None-tolerant
  2. ``evo install`` pre-validates ``forge_engine`` import before
     claiming the job was queued (so no silent thread failures)
  3. ``evo app create`` caps iterations per session
  4. ``_classify_app_create_review`` accepts a broader synonym set
     for approve / cancel without misclassifying ambiguous replies
"""

from __future__ import annotations

import json
import sys
from collections import namedtuple
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1 — is_pod_wide_caller is case-insensitive + None-tolerant
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bot_id", [
    "evolve", "Evolve", "EVOLVE", "  evolve  ", "eVoLvE",
])
def test_is_pod_wide_caller_case_insensitive(bot_id):
    from evolve_admin.evo.handlers._shared import is_pod_wide_caller
    assert is_pod_wide_caller(bot_id) is True


@pytest.mark.parametrize("bot_id", [
    "admin_bot", "team_bot_a", "evolve-test", "evolveX", None, "", 0, 42, [],
])
def test_is_pod_wide_caller_rejects_non_sysadmin(bot_id):
    from evolve_admin.evo.handlers._shared import is_pod_wide_caller
    assert is_pod_wide_caller(bot_id) is False


def test_is_pod_wide_caller_resolves_primary_field():
    """When network.primary is set, it determines the pod-wide caller."""
    from evolve_admin.evo.handlers._shared import is_pod_wide_caller
    network = {"primary": "evo", "bots": {"evo": {"user": "evolve"}}}
    assert is_pod_wide_caller("evo", network) is True


def test_is_pod_wide_caller_rejects_legacy_evolve_when_primary_renamed():
    """A pod that migrated to primary='evo' must NOT treat a residual bot
    literally named 'evolve' as the pod-wide caller — that would route
    sysadmin reports to the wrong identity."""
    from evolve_admin.evo.handlers._shared import is_pod_wide_caller
    network = {
        "primary": "evo",
        "bots": {"evo": {"user": "evolve"}, "evolve": {}},
    }
    assert is_pod_wide_caller("evolve", network) is False


def test_is_pod_wide_caller_resolves_role_flag():
    """Existing-bot-as-primary scenario: bot 'team_bot_a' has role='primary' but
    no top-level network.primary field set."""
    from evolve_admin.evo.handlers._shared import is_pod_wide_caller
    network = {"bots": {"team_bot_a": {"role": "primary"}, "admin_bot": {}}}
    assert is_pod_wide_caller("team_bot_a", network) is True
    assert is_pod_wide_caller("admin_bot", network) is False


def test_is_pod_wide_caller_legacy_evolve_fallback():
    """Pods that pre-date network.primary still resolve when the legacy
    bot literally named 'evolve' exists — covers the migration window."""
    from evolve_admin.evo.handlers._shared import is_pod_wide_caller
    network = {"bots": {"evolve": {}, "team_bot_a": {}}}
    assert is_pod_wide_caller("evolve", network) is True
    assert is_pod_wide_caller("team_bot_a", network) is False


def test_dispatch_evo_app_create_uses_case_insensitive_check(tmp_path):
    """`evo app create` from a bot named "Evolve" should still be refused."""
    from evolve_admin.evo.dispatch import dispatch
    network = {
        "sharedDir": str(tmp_path),
        "members": ["Evolve", "admin_bot"],
        "bots": {"admin_bot": {"primary_user": {
            "external_ids": {"telegram": "12345"},
        }}},
    }
    r = dispatch(
        network, bot_id="Evolve", channel="telegram",
        sender_external_id="12345", raw_text="evo app create",
    )
    body = r.direct_send_message or ""
    assert "sysadmin" in body.lower() or "evolve" in body.lower()
    assert "from the bot you want" in body
    assert r.wizard_session_id is None


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2 — evo install pre-validates forge_engine import
# ─────────────────────────────────────────────────────────────────────────────


_PwResult = namedtuple("_PwResult", ["pw_dir"])


@pytest.fixture
def install_patched(tmp_path, monkeypatch):
    state = {
        "catalog": [
            {"pkg_id": "p1", "name": "Tool", "requirements": {},
             "pkg_version": "1.0", "build_spec": "build it"},
        ],
        "homes": {},
        "jobs_created": [],
    }

    def fake_pwnam(user: str):
        if user in state["homes"]:
            return _PwResult(pw_dir=str(state["homes"][user]))
        raise KeyError(user)

    from evolve_admin.evo.handlers import install as ih
    monkeypatch.setattr(ih.pwd, "getpwnam", fake_pwnam)

    def fake_load(pkg_id, shared_dir):
        for p in state["catalog"]:
            if p.get("pkg_id") == pkg_id or p.get("name") == pkg_id:
                return p
        return None

    def fake_list(shared_dir, bot_ids=None):
        return list(state["catalog"])

    def fake_preflight(pkg_id, bot_id, shared_dir):
        return {"app_dependencies": [], "requirements": []}

    import evolve_admin.applications.gallery as gallery_mod
    monkeypatch.setattr(gallery_mod, "load_gallery_package", fake_load)
    monkeypatch.setattr(gallery_mod, "list_gallery_packages", fake_list)
    monkeypatch.setattr(gallery_mod, "preflight_check", fake_preflight)

    class _FakeJob:
        def __init__(self, job_id="j-fake"):
            self.job_id = job_id
            self.context_snapshot = {}

    def fake_create_install_job(*, pkg_id, app_id, bot_id, gallery_version, shared_dir):
        state["jobs_created"].append({"pkg_id": pkg_id, "bot_id": bot_id})
        return _FakeJob()

    import evolve_admin.applications.forge_jobs as fj_mod
    monkeypatch.setattr(fj_mod, "create_install_job", fake_create_install_job)
    monkeypatch.setattr(fj_mod, "save_job", lambda *a, **kw: None)

    import evolve_admin.applications.oauth_orchestrator as oo_mod
    monkeypatch.setattr(oo_mod, "evaluate_install_prerequisites",
                        lambda *a, **kw: {"satisfied": True, "missing": []})

    monkeypatch.setattr(ih, "_dispatch_forge_job_async", lambda *a, **kw: None)
    return state, tmp_path


def test_install_pre_validates_forge_engine_importable(install_patched):
    """Happy path: forge_engine import works, install reports 'Queued'."""
    state, tmp_path = install_patched
    from evolve_admin.evo.handlers.install import render
    network = {"sharedDir": str(tmp_path), "members": ["admin_bot"]}
    r = render(role="primary", bot_id="admin_bot", args="Tool", network=network)
    body = r.direct_send_message or ""
    assert "Queued" in body
    assert len(state["jobs_created"]) == 1


def test_install_reports_forge_engine_missing(install_patched, monkeypatch):
    """When forge_engine isn't importable, install refuses with a real
    error message — does NOT say "Queued" then silently drop the work.

    Simulating a real ImportError is tricky because `forge_engine` may
    already be cached on the `applications` package's namespace from
    earlier imports in the test run. Need to both wipe the attribute
    and poison sys.modules.
    """
    state, tmp_path = install_patched

    import sys as _sys
    import evolve_admin.applications as _apps_pkg
    # Wipe both caches so `from ...applications import forge_engine`
    # actually runs the import machinery and hits the None entry.
    monkeypatch.delattr(_apps_pkg, "forge_engine", raising=False)
    monkeypatch.setitem(_sys.modules, "evolve_admin.applications.forge_engine",
                        None)

    from evolve_admin.evo.handlers.install import render
    network = {"sharedDir": str(tmp_path), "members": ["admin_bot"]}
    r = render(role="primary", bot_id="admin_bot", args="Tool", network=network)
    body = r.direct_send_message or ""
    assert "Forge engine unavailable" in body
    assert "Queued" not in body
    # No job was created because we bailed before that step
    assert state["jobs_created"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3 — evo app create iteration cap
# ─────────────────────────────────────────────────────────────────────────────


def _build_draft_stub(*, description, target_bots, shared_dir,
                      previous_draft=None, feedback=None, version=1):
    return {
        "version": version,
        "display_name": "Test App",
        "description": description,
        "build_spec": "spec",
        "application_tags": [],
        "requirements": {},
        "app_dependencies": [],
        "conflicts": [],
        "suggestions": [],
        "created_at": "2026-05-13T00:00:00Z",
    }


@pytest.fixture
def app_create_patched(tmp_path, monkeypatch):
    network = {
        "sharedDir": str(tmp_path),
        "members": ["admin_bot", "evolve"],
        "bots": {"admin_bot": {"primary_user": {
            "external_ids": {"telegram": "12345"},
        }}},
    }
    import evolve_admin.web.spec_routes as sr
    monkeypatch.setattr(sr, "_build_draft", _build_draft_stub)
    return network


def _dispatch_then_turn(network, raw, bot_id="admin_bot"):
    from evolve_admin.evo.dispatch import dispatch
    return dispatch(
        network, bot_id=bot_id, channel="telegram",
        sender_external_id="12345", raw_text=raw,
    )


def _wizard_turn(network, user_message, bot_id="admin_bot"):
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


def test_iteration_cap_hits_after_five_iterations(app_create_patched):
    """Confirm the cap fires on the 6th iteration attempt."""
    from evolve_admin.evo.wizard.engine import _APP_CREATE_MAX_ITERATIONS
    network = app_create_patched

    _dispatch_then_turn(network, "evo app create")
    _wizard_turn(network, "A description that's long enough to draft from.")

    # Iterate exactly _APP_CREATE_MAX_ITERATIONS times — each should redraft.
    for i in range(_APP_CREATE_MAX_ITERATIONS):
        r = _wizard_turn(network, f"iterate change number {i}")
        assert r.completed is False
        body = r.system_append or ""
        assert "cap" not in body.lower(), (
            f"cap fired prematurely at iteration {i + 1}"
        )

    # The next iterate attempt should hit the cap, NOT redraft.
    r = _wizard_turn(network, "iterate one more thing")
    body = r.system_append or ""
    assert "cap" in body.lower() or "iteration" in body.lower()
    assert r.completed is False  # user can still approve / cancel


def test_iteration_cap_does_not_block_approve_or_cancel(app_create_patched):
    """After cap is hit, user should still be able to approve or cancel."""
    from evolve_admin.evo.wizard.engine import _APP_CREATE_MAX_ITERATIONS
    network = app_create_patched

    _dispatch_then_turn(network, "evo app create")
    _wizard_turn(network, "A description that's long enough to draft from.")
    for i in range(_APP_CREATE_MAX_ITERATIONS):
        _wizard_turn(network, f"iterate change {i}")
    # Hit cap once
    r = _wizard_turn(network, "iterate one more")
    assert "cap" in (r.system_append or "").lower() or \
           "iteration" in (r.system_append or "").lower()

    # Now cancel works
    r = _wizard_turn(network, "cancel")
    assert r.completed is True


# ─────────────────────────────────────────────────────────────────────────────
# Fix 4 — expanded classifier
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reply", [
    "approve", "yes", "y", "ok", "okay", "go", "build", "ship", "do it",
    "yep", "yeah", "sure", "let's do it", "lets do it", "go ahead", "ship it",
    # Case + whitespace normalization
    "YES", "  yes  ", "Yes",
])
def test_classifier_approve_synonyms(reply):
    from evolve_admin.evo.wizard.engine import _classify_app_create_review
    intent, _ = _classify_app_create_review(reply)
    assert intent == "approve", f"reply={reply!r} was {intent!r}"


@pytest.mark.parametrize("reply", [
    "cancel", "no", "n", "stop", "nevermind", "never mind", "abort",
    "nope", "no thanks", "forget it", "drop it", "scrap it",
    "don't", "dont", "don't build", "dont build",
    "Cancel", "  no  ",
])
def test_classifier_cancel_synonyms(reply):
    from evolve_admin.evo.wizard.engine import _classify_app_create_review
    intent, _ = _classify_app_create_review(reply)
    assert intent == "cancel", f"reply={reply!r} was {intent!r}"


@pytest.mark.parametrize("reply,expected_feedback", [
    ("iterate add a feature", "add a feature"),
    ("iterate: drop the test exemption", "drop the test exemption"),
    ("edit add support for X", "add support for X"),
    ("change the test command", "the test command"),
    ("revise the build spec", "the build spec"),
    ("iterate", ""),  # bare iterate — empty feedback
])
def test_classifier_iterate_prefixes(reply, expected_feedback):
    from evolve_admin.evo.wizard.engine import _classify_app_create_review
    intent, feedback = _classify_app_create_review(reply)
    assert intent == "iterate", f"reply={reply!r} was {intent!r}"
    assert feedback == expected_feedback


@pytest.mark.parametrize("reply", [
    # Ambiguous — intentionally NOT classified as approve/iterate/cancel
    "ok but make it faster",   # "ok" with trailing context — too risky to ship
    "yes please drop X",       # "yes" with iterate-like text — re-prompt instead
    "no drop the test command",  # "no" not exact-match
    "maybe",
    "?",
    "add quarterly checks",   # no iterate prefix → unknown (re-prompt)
])
def test_classifier_ambiguous_returns_unknown(reply):
    from evolve_admin.evo.wizard.engine import _classify_app_create_review
    intent, _ = _classify_app_create_review(reply)
    assert intent == "unknown", f"reply={reply!r} was {intent!r}"
