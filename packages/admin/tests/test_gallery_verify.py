"""Unit tests for the gallery install-verify harness.

Everything runs against in-memory fakes — no pod, no socket, no wall clock.
``FakeApi`` is the mutating admin-API surface (install / poll / delete) and
``FakeProbe`` is the read-only pod inspector; the two share a ``phase`` so a
committed uninstall flips the pod to a torn-down state (units gone, manifest
archived) exactly as the real teardown would. That linkage is what lets a
single test assert both "install born-healthy" and "teardown residue-free".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolve_admin.gallery_verify import model
from evolve_admin.gallery_verify.api import (
    InstallOutcome,
    parse_install_response,
    summarize_scheduled_failures,
)
from evolve_admin.gallery_verify.assertions import scheduled_unit_target
from evolve_admin.gallery_verify.model import (
    BLOCKED_OAUTH,
    FAIL,
    PASS,
    SKIPPED,
)
from evolve_admin.gallery_verify.pipeline import run_sweep, verify_one_app

PKG_ID = "p-9bfa1c84"
BOT = "test_bot"
UNIT_LABEL = "ai.evolve.test_bot.task-check"


def _pkg(pkg_id: str = PKG_ID, name: str = "Task Manager", tags=None) -> dict:
    return {
        "pkg_id": pkg_id,
        "display_name": name,
        "name": name,
        "app_id": name.lower().replace(" ", "-"),
        "application_tags": tags or ["personal-productivity", "task-management"],
        "description": "A task manager.",
    }


def _manifest(pkg_id: str = PKG_ID, *, scheduled=True) -> dict:
    m = {
        "id": "task-manager",
        "name": "Task Manager",
        "display_name": "Task Manager",
        "pkg_id": pkg_id,
        "spec_id": pkg_id,
        "status": "approved",
        "definition_status": "defined",
        "manifest_shape": "v7-arc",
        "classification": {},
        "scheduled_actions": [],
    }
    if scheduled:
        m["scheduled_actions"] = [{
            "id": "task-check",
            "mechanism": "launchd_python_signal",
            "installed_artifact": f"/Users/{BOT}/Library/LaunchAgents/{UNIT_LABEL}.plist",
            "install": {"plist_label": UNIT_LABEL},
        }]
    return m


def _preflight_ok() -> dict:
    return {
        "pkg_id": PKG_ID, "bot_id": BOT, "error": None,
        "ready_to_build": True, "ready_to_run": True,
        "app_dependencies": [], "requirements": [],
    }


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeClock:
    """Deterministic monotonic clock; sleep advances it (no real waiting)."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeProbe:
    def __init__(
        self, *,
        manifest=None, manifest_after=None, packages=None, preflight=None,
        recon=None, covered=None, sched=None, delivery=None,
        installed_md="Task Manager", heartbeat=None, platform="macos",
        visible=None,
    ) -> None:
        self.platform = platform
        self.shared_dir = Path("/fake/shared")
        self.phase = "installed"  # flipped to "torn_down" by a committed delete
        self._manifest = manifest if manifest is not None else _manifest()
        self._manifest_after = manifest_after  # default None ⇒ archived
        self._packages = packages if packages is not None else [_pkg()]
        self._preflight = preflight if preflight is not None else _preflight_ok()
        self._recon = recon if recon is not None else {
            "owned_ok": [{"spec_id": PKG_ID, "path": "scripts/task.py",
                          "reason": "claimed_and_marked", "bucket": "owned_ok"}],
        }
        self._covered = covered if covered is not None else {PKG_ID}
        self._sched = sched or {}
        self._delivery = delivery if delivery is not None else {
            "entries": {PKG_ID: {"app_id": PKG_ID, "state": "delivered"}},
        }
        self._installed_md = installed_md
        self._heartbeat = heartbeat or []
        self._visible = visible or []

    def gallery_packages(self):
        return list(self._packages)

    def gallery_package(self, pkg_id):
        for p in self._packages:
            if p.get("pkg_id") == pkg_id:
                return p
        return None

    def preflight(self, pkg_id, bot_id):
        return self._preflight

    def app_manifest(self, pkg_id, bot_id):
        return self._manifest_after if self.phase == "torn_down" else self._manifest

    def recon_buckets(self, bot_id):
        return self._recon

    def covered_ids(self, bot_id):
        return set(self._covered)

    def scheduler_status(self, label):
        st = dict(self._sched.get(
            label, {"installed": True, "managed": True, "running": True, "status_error": None},
        ))
        if self.phase == "torn_down":
            st["installed"] = False
            st["managed"] = False
        return st

    def delivery_state(self):
        return self._delivery

    def installed_apps_md(self, bot_id):
        return "" if self.phase == "torn_down" else self._installed_md

    def visible_manifests(self, bot_id):
        return list(self._visible)

    def heartbeat_sections(self, bot_id):
        return [] if self.phase == "torn_down" else list(self._heartbeat)


class FakeApi:
    def __init__(
        self, *,
        probe=None,
        install=None,          # (code, body) returned by install()
        job=None,              # terminal job dict returned by get_job()
        job_never_terminal=False,
        delete_flips_phase=True,
        delete_exec=None,
    ) -> None:
        self.probe = probe
        self._install = install
        self._job = job if job is not None else {
            "job_id": "j-1", "status": "complete", "completed_with_errors": False,
            "steps": [], "context_snapshot": {},
        }
        self._job_never_terminal = job_never_terminal
        self._delete_flips_phase = delete_flips_phase
        self._delete_exec = delete_exec
        self.installs = []
        self.job_calls = 0
        self.deletes = []

    def install(self, pkg_id, bot_id, *, force, confirmed):
        self.installs.append((pkg_id, bot_id, force, confirmed))
        if self._install is not None:
            return self._install
        return 200, {"jobs": [{"job_id": "j-1", "run_id": "r-1", "bot_id": bot_id}]}

    def get_job(self, job_id):
        self.job_calls += 1
        if self._job_never_terminal:
            return 200, {"job_id": job_id, "status": "running", "steps": [],
                         "context_snapshot": {}}
        return 200, self._job

    def delete_app(self, bot_id, app_id, *, delete_files, commit):
        self.deletes.append((bot_id, app_id, list(delete_files), commit))
        if not commit:
            return 200, {"ok": True, "deletion_candidates": ["scripts/task.py"]}
        if self._delete_flips_phase and self.probe is not None:
            self.probe.phase = "torn_down"
        if self._delete_exec is not None:
            return 200, self._delete_exec
        return 200, {"ok": True, "actually_deleted": ["scripts/task.py"], "failed_deletes": []}


def _run(probe, api, **kw):
    fc = FakeClock()
    kw.setdefault("clock", fc.now)
    kw.setdefault("sleep", fc.sleep)
    kw.setdefault("poll_timeout_s", 60.0)
    kw.setdefault("poll_interval_s", 5.0)
    return verify_one_app(_pkg(), BOT, api=api, probe=probe, **kw)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_happy_path_pass_and_clean_teardown():
    probe = FakeProbe()
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == PASS, r.summary
    assert r.failed_step is None
    # teardown ran and pod was flipped to torn-down
    assert api.deletes, "delete_app should have been called"
    assert any(commit for *_, commit in api.deletes)
    assert probe.phase == "torn_down"
    steps = {s.step for s in r.steps}
    assert model.STEP_ASSERT in steps and model.STEP_TEARDOWN in steps


def test_no_teardown_flag_skips_delete():
    probe = FakeProbe()
    api = FakeApi(probe=probe)
    r = _run(probe, api, teardown=False)
    assert r.status == PASS
    assert api.deletes == []
    assert model.STEP_TEARDOWN not in {s.step for s in r.steps}


# ── OAuth ─────────────────────────────────────────────────────────────────────

def test_awaiting_oauth_blocks_without_teardown():
    probe = FakeProbe()
    api = FakeApi(probe=probe, install=(202, {"jobs": [{
        "bot_id": BOT, "job_id": "j-1", "status": "awaiting_oauth",
        "missing": [{"integration": "Google (GOG)"}], "next": "set up oauth",
    }]}))
    r = _run(probe, api)
    assert r.status == BLOCKED_OAUTH
    assert api.deletes == [], "nothing was built — teardown must not run"
    assert "Google (GOG)" in r.summary


# ── Install failure classes ───────────────────────────────────────────────────

def test_completed_with_errors_fails_with_action_detail():
    probe = FakeProbe()
    api = FakeApi(probe=probe, job={
        "job_id": "j-1", "status": "complete", "completed_with_errors": True,
        "steps": [], "context_snapshot": {"scheduled_actions_installed": [
            {"action_id": "task-check", "mechanism": "launchd", "status": "failed",
             "error": "bootstrap failed rc=5"},
        ]},
    })
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_INSTALL
    assert "task-check" in r.summary and "bootstrap failed" in r.summary
    # teardown still runs to clean the partial install
    assert any(commit for *_, commit in api.deletes)


def test_job_failed_status_fails_and_tears_down():
    probe = FakeProbe()
    api = FakeApi(probe=probe, job={
        "job_id": "j-1", "status": "failed", "completed_with_errors": False,
        "steps": [{"label": "BUILD", "status": "failed", "detail": "llm error"}],
        "context_snapshot": {},
    })
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_INSTALL
    assert "llm error" in r.summary
    assert any(commit for *_, commit in api.deletes)


def test_poll_timeout_marks_fail_and_tears_down():
    probe = FakeProbe()
    api = FakeApi(probe=probe, job_never_terminal=True)
    r = _run(probe, api, poll_timeout_s=30.0, poll_interval_s=5.0)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_INSTALL
    assert "did not finish" in r.summary
    assert api.job_calls >= 2, "should have polled repeatedly before timing out"
    assert any(commit for *_, commit in api.deletes), "wedged job must still be torn down"


def test_stuck_awaiting_approval_reported_specifically():
    probe = FakeProbe()
    api = FakeApi(probe=probe)
    api._job_never_terminal = True

    def _await_approval(job_id):
        api.job_calls += 1
        return 200, {"job_id": job_id, "status": "awaiting_approval",
                     "steps": [], "context_snapshot": {}}

    api.get_job = _await_approval  # type: ignore[assignment]
    r = _run(probe, api, poll_timeout_s=20.0, poll_interval_s=5.0)
    assert r.status == FAIL
    assert "awaiting_approval" in r.summary


def test_install_error_fails_without_teardown():
    probe = FakeProbe()
    api = FakeApi(probe=probe, install=(500, {"error": "boom"}))
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_INSTALL
    assert api.deletes == [], "no job dispatched → no teardown"


# ── Preflight gate ────────────────────────────────────────────────────────────

def test_preflight_hard_block_fails_without_install():
    probe = FakeProbe(preflight={
        "error": None, "ready_to_build": False, "ready_to_run": False,
        "app_dependencies": [], "requirements": [
            {"type": "system", "id": "pandoc", "severity": "build_blocker",
             "state": "missing", "message": "pandoc not installed"},
        ],
    })
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_PREFLIGHT
    assert api.installs == [], "hard blocker must short-circuit before install"
    assert api.deletes == []


def test_preflight_unparseable_requirement_fails():
    probe = FakeProbe(preflight={
        "error": None, "ready_to_build": False, "ready_to_run": False,
        "app_dependencies": [], "requirements": [
            {"type": "custom", "id": "mystery", "severity": "build_blocker",
             "state": "unknown", "message": "could not evaluate"},
        ],
    })
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_PREFLIGHT
    assert "unparseable" in r.summary
    assert api.installs == []


def test_preflight_integration_only_proceeds_to_install():
    # An integration build_blocker is NOT a hard stop — install routes it to
    # awaiting_oauth. Preflight must let it through.
    probe = FakeProbe(preflight={
        "error": None, "ready_to_build": False, "ready_to_run": False,
        "app_dependencies": [], "requirements": [
            {"type": "integration", "id": "gog", "severity": "build_blocker",
             "state": "missing", "message": "connect Google"},
        ],
    })
    api = FakeApi(probe=probe, install=(202, {"jobs": [{
        "bot_id": BOT, "job_id": "j-1", "status": "awaiting_oauth",
        "missing": [{"integration": "GOG"}], "next": "x",
    }]}))
    r = _run(probe, api)
    assert api.installs, "integration-only preflight must proceed to install"
    assert r.status == BLOCKED_OAUTH


# ── Post-install assertion failures ───────────────────────────────────────────

def test_missing_unit_fails_assert_but_still_tears_down():
    probe = FakeProbe(sched={UNIT_LABEL: {
        "installed": False, "managed": False, "status_error": None,
    }})
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_ASSERT
    assert "NOT present" in r.summary
    assert any(commit for *_, commit in api.deletes), "assert-fail must not strand residue"


def test_scheduler_probe_error_is_warn_not_fail():
    # A non-authoritative scheduler probe (sudo can't escalate) must not be
    # reported as a missing unit — tooling failure ≠ finding.
    probe = FakeProbe(sched={UNIT_LABEL: {
        "installed": False, "managed": False, "status_error": "cannot_escalate",
    }})
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == PASS, r.summary


def test_missing_installed_artifact_fails():
    m = _manifest()
    m["scheduled_actions"][0].pop("installed_artifact")
    probe = FakeProbe(manifest=m)
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_ASSERT


def test_recon_broken_bucket_fails():
    probe = FakeProbe(recon={
        "missing_marker": [{"spec_id": PKG_ID, "path": "scripts/task.py",
                            "reason": "claimed_no_marker", "bucket": "missing_marker"}],
    })
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == FAIL
    assert "not owned/healthy" in r.summary


def test_recon_absent_is_skipped_not_failed():
    probe = FakeProbe(recon={})  # app not yet reconciled
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == PASS  # skip does not fail
    assert r.checks_skipped >= 1


def test_wrong_definition_status_fails():
    m = _manifest()
    m["definition_status"] = "discovered"
    probe = FakeProbe(manifest=m)
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == FAIL
    assert "defined" in r.summary


def test_missing_from_capability_index_fails():
    probe = FakeProbe(installed_md="Some Other App")
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == FAIL
    assert "INSTALLED_APPS" in r.summary


# ── Platform profile ──────────────────────────────────────────────────────────

def test_linux_skips_coverage_check():
    probe = FakeProbe(platform="linux", covered=set())
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    # coverage is a known-dead check on Linux (S7) — must skip, not fail
    assert r.status == PASS
    cov_checks = [
        c for s in r.steps for c in s.checks if c.name == "integrity_coverage"
    ]
    assert cov_checks and cov_checks[0].skipped


def test_macos_uncovered_is_warn_not_fail():
    probe = FakeProbe(platform="macos", covered=set())
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == PASS  # coverage is eventually-consistent → WARN only


# ── Teardown residue ──────────────────────────────────────────────────────────

def test_teardown_residue_unit_still_firing_fails():
    # The precise S4 gap: the delete route archives the manifest + clears the
    # index but leaves Phase-4.5 launchd units bootstrapped and firing. Model
    # that by flipping phase for everything EXCEPT the scheduler.
    class UnitLeakProbe(FakeProbe):
        def scheduler_status(self, label):
            return {"installed": True, "managed": True, "status_error": None}

    probe = UnitLeakProbe()
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_TEARDOWN
    unit_checks = [
        c for s in r.steps if s.step == model.STEP_TEARDOWN
        for c in s.checks if c.name.startswith("unit_removed")
    ]
    assert any("STILL firing" in c.detail for c in unit_checks)


def test_teardown_orphan_heartbeat_section_fails():
    probe = FakeProbe(heartbeat=[{"file": "HEARTBEAT.md", "anchor": "Task Check",
                                  "pkg_id": PKG_ID}])
    # keep heartbeat sections after teardown by not flipping phase for heartbeat;
    # simulate S4 gap: delete flips units + manifest but leaves sections.

    class LeakyProbe(FakeProbe):
        def heartbeat_sections(self, bot_id):
            return list(self._heartbeat)  # never cleared

    lp = LeakyProbe(heartbeat=[{"file": "HEARTBEAT.md", "anchor": "Task Check",
                                "pkg_id": PKG_ID}])
    api = FakeApi(probe=lp)
    r = verify_one_app(_pkg(), BOT, api=api, probe=lp,
                       clock=FakeClock().now, sleep=lambda s: None)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_TEARDOWN
    assert "orphan managed section" in r.summary


def test_teardown_delete_exec_failure_fails():
    probe = FakeProbe()
    api = FakeApi(probe=probe, delete_exec={"ok": False, "error": "EACCES"})
    r = _run(probe, api)
    assert r.status == FAIL
    assert r.failed_step == model.STEP_TEARDOWN


def test_teardown_index_same_name_other_app_is_not_residue():
    """Live false positive (2026-07-11 sweep, Journal): the uninstalled
    install's index entry IS gone, but a different, legitimately-installed
    app with the same display name keeps rendering that name into
    INSTALLED_APPS.md forever. Name presence attributable to another visible
    manifest must not read as residue.
    """
    from evolve_admin.gallery_verify.assertions import teardown_residue_checks

    class NameCollisionProbe(FakeProbe):
        def installed_apps_md(self, bot_id):
            # Mirrors the live md shape: the OTHER app's section renders both
            # the name and lowercase hint words derived from it.
            return ("## Journal — daily log owned by the OTHER installed app\n"
                    "**Hint words:** `Journal`, `journal`\n")

    probe = NameCollisionProbe(
        visible=[{"id": "i-old", "pkg_id": "p-old", "name": "Journal",
                  "status": "active"}],
        manifest_after=None,  # this install's manifest archived
    )
    probe.phase = "torn_down"
    step = model.StepResult(step=model.STEP_TEARDOWN)
    teardown_residue_checks(
        probe, PKG_ID, BOT,
        {"id": "journal", "name": "Journal", "scheduled_actions": []},
        step,
    )
    idx = [c for c in step.checks if c.name == "index_cleared"]
    assert idx and not idx[0].failed
    assert "different installed app" in idx[0].detail


def test_teardown_index_residue_without_attribution_still_fails():
    """The conservative direction is preserved: a name still in the index
    with NO other visible manifest accounting for it is real residue — and
    attribution matches IDENTITY fields only, so an unrelated app whose
    description merely mentions the name does not exempt the residue."""
    from evolve_admin.gallery_verify.assertions import teardown_residue_checks

    class LeakyIndexProbe(FakeProbe):
        def installed_apps_md(self, bot_id):
            return "## Journal — stale entry from the failed install"

    probe = LeakyIndexProbe(visible=[
        {"id": "i-notes", "pkg_id": "p-notes", "name": "Notes",
         "status": "active", "description": "syncs with Journal nightly"},
    ])
    probe.phase = "torn_down"
    step = model.StepResult(step=model.STEP_TEARDOWN)
    teardown_residue_checks(
        probe, PKG_ID, BOT,
        {"id": "journal", "name": "Journal", "scheduled_actions": []},
        step,
    )
    idx = [c for c in step.checks if c.name == "index_cleared"]
    assert idx and idx[0].failed
    assert "still in INSTALLED_APPS.md" in idx[0].detail


# ── Dry-run ───────────────────────────────────────────────────────────────────

def test_dry_run_installed_app_reverifies():
    probe = FakeProbe()
    api = FakeApi(probe=probe)
    r = _run(probe, api, mode="dry-run")
    assert r.status == PASS
    assert api.installs == [] and api.deletes == []


def test_dry_run_not_installed_is_skipped():
    probe = FakeProbe(manifest=None)
    probe._manifest = None  # app_manifest returns None
    api = FakeApi(probe=probe)
    r = _run(probe, api, mode="dry-run")
    assert r.status == SKIPPED


# ── parse_install_response (chain vs single) ──────────────────────────────────

def test_parse_single_job_dispatched():
    out = parse_install_response(200, {"jobs": [{"job_id": "j-1", "bot_id": BOT}]}, BOT)
    assert out.kind == "dispatched" and out.job_id == "j-1"


def test_parse_chain_picks_right_bot():
    body = {"jobs": [
        {"bot_id": "other", "job_id": "j-other"},
        {"bot_id": BOT, "job_id": "j-mine"},
    ]}
    out = parse_install_response(200, body, BOT)
    assert out.kind == "dispatched" and out.job_id == "j-mine"


def test_parse_awaiting_oauth():
    out = parse_install_response(202, {"jobs": [{
        "bot_id": BOT, "job_id": "j", "status": "awaiting_oauth", "missing": [{}],
    }]}, BOT)
    assert out.kind == "awaiting_oauth"


def test_parse_blocked():
    out = parse_install_response(200, {"jobs": [{
        "bot_id": BOT, "blocked": True, "preflight": {"requirements": [
            {"severity": "build_blocker", "state": "missing", "id": "x", "message": "no x"},
        ]},
    }]}, BOT)
    assert out.kind == "blocked" and "no x" in out.detail


def test_parse_cost_gate():
    out = parse_install_response(412, {"requires_confirmation": True}, BOT)
    assert out.kind == "error" and "confirmation" in out.detail


def test_parse_top_level_error_for_bot():
    out = parse_install_response(200, {"jobs": [], "errors": [f"{BOT}: forge exploded"]}, BOT)
    assert out.kind == "error" and "forge exploded" in out.detail


def test_parse_bot_error_overrides_dispatched_entry():
    # A per-bot error alongside a job entry is authoritative — the install
    # failed for this bot; must not be reported as dispatched (→ poll timeout).
    body = {"jobs": [{"bot_id": BOT, "job_id": "j-1"}],
            "errors": [f"{BOT}: dispatch thread died"]}
    out = parse_install_response(200, body, BOT)
    assert out.kind == "error" and "dispatch thread died" in out.detail


# ── Adversarial-review regression guards ──────────────────────────────────────

def test_poll_interval_zero_does_not_hang():
    # interval=0 under an injected clock would starve the timeout check; the
    # floor keeps polling bounded.
    probe = FakeProbe()
    api = FakeApi(probe=probe, job_never_terminal=True)
    r = _run(probe, api, poll_timeout_s=1.0, poll_interval_s=0.0)
    assert r.status == FAIL
    assert "did not finish" in r.summary


def test_thin_pass_flags_coverage_floor():
    # Linux (coverage skipped) + recon absent (skipped) → both ownership checks
    # skipped → a PASS must carry a visible coverage_floor WARN.
    probe = FakeProbe(platform="linux", recon={})
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == PASS
    assert r.checks_warned >= 1
    floor = [c for s in r.steps for c in s.checks if c.name == "coverage_floor"]
    assert floor and not floor[0].ok and floor[0].severity == model.WARN
    assert "warned" in r.summary


def test_substantive_coverage_no_floor_warn():
    # macOS with recon owned_ok + covered → ownership verified → no floor warn.
    probe = FakeProbe(platform="macos")
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    assert r.status == PASS
    floor = [c for s in r.steps for c in s.checks if c.name == "coverage_floor"]
    assert floor and floor[0].ok


# ── artifact / helper units ───────────────────────────────────────────────────

def test_scheduled_unit_target_classification():
    assert scheduled_unit_target({"mechanism": "launchd_python_signal",
                                  "install": {"plist_label": "L"}}) == ("unit", "L")
    assert scheduled_unit_target({"mechanism": "launchd",
                                  "installed_artifact": "/x/y/com.foo.bar.plist"}) == (
        "unit", "com.foo.bar")
    assert scheduled_unit_target({"mechanism": "oc_heartbeat_instruction",
                                  "installed_artifact": "HEARTBEAT.md#Sec"}) == (
        "section", "HEARTBEAT.md#Sec")
    assert scheduled_unit_target({"mechanism": "crontab",
                                  "installed_artifact": "crontab:3"})[0] == "crontab"
    assert scheduled_unit_target({"mechanism": "external",
                                  "installed_artifact": "x"})[0] == "none"
    assert scheduled_unit_target({"mechanism": "systemd",
                                  "installed_artifact": "app-x.service"}) == (
        "unit", "app-x.service")


def test_summarize_scheduled_failures():
    job = {"context_snapshot": {"scheduled_actions_installed": [
        {"action_id": "a", "mechanism": "launchd", "status": "failed", "error": "e1"},
        {"action_id": "b", "mechanism": "crontab", "status": "ok"},
    ]}}
    s = summarize_scheduled_failures(job)
    assert "a" in s and "e1" in s and "b" not in s


# ── sweep + exit code ─────────────────────────────────────────────────────────

def test_sweep_exit_code_and_counts():
    good_probe = FakeProbe()
    good_api = FakeApi(probe=good_probe)
    bad_probe = FakeProbe(manifest={**_manifest(), "definition_status": "discovered"})
    bad_api = FakeApi(probe=bad_probe)

    fc = FakeClock()
    results = []
    results.append(verify_one_app(_pkg("p-good"), BOT, api=good_api, probe=good_probe,
                                  clock=fc.now, sleep=fc.sleep))
    results.append(verify_one_app(_pkg("p-bad"), BOT, api=bad_api, probe=bad_probe,
                                  clock=fc.now, sleep=fc.sleep))
    rep = model.RunReport(apps=results)
    assert rep.counts()[PASS] == 1
    assert rep.counts()[FAIL] == 1
    assert rep.exit_code() == 1


def test_run_sweep_all_pass_exit_zero():
    probe = FakeProbe()
    api = FakeApi(probe=probe)
    fc = FakeClock()
    results = run_sweep([(_pkg(), BOT)], api=api, probe=probe,
                        clock=fc.now, sleep=fc.sleep)
    rep = model.RunReport(apps=results)
    assert rep.exit_code() == 0


def test_run_sweep_survives_one_app_crash():
    # An unexpected exception past the dispatch point (here app_manifest raises)
    # propagates out of verify_one_app AFTER its finally runs teardown; the sweep
    # guard must record it as FAIL and keep going, not abort the batch.
    class BoomProbe(FakeProbe):
        def app_manifest(self, pkg_id, bot_id):
            raise RuntimeError("kaboom")

    boom_probe = BoomProbe()
    boom_api = FakeApi(probe=boom_probe)
    fc = FakeClock()
    results = run_sweep(
        [(_pkg("p-boom"), BOT)], api=boom_api, probe=boom_probe,
        clock=fc.now, sleep=fc.sleep,
    )
    assert len(results) == 1
    assert results[0].status == FAIL
    assert results[0].failed_step == "harness"
    # teardown still fired for the crashed app (finally in verify_one_app)
    assert any(commit for *_, commit in boom_api.deletes)


def test_report_render_and_artifact_roundtrip(tmp_path):
    from evolve_admin.gallery_verify import report as report_mod
    probe = FakeProbe()
    api = FakeApi(probe=probe)
    r = _run(probe, api)
    rep = model.RunReport(mode="install", pod_platform="macos", apps=[r])
    text = report_mod.render_console(rep)
    assert "PASS" in text and "exit 0" in text
    out = report_mod.write_artifact(rep, tmp_path / "out.json")
    import json
    data = json.loads(out.read_text())
    assert data["schema"] == "gallery-verify-v1"
    assert data["apps"][0]["status"] == PASS


# ── Dependency-aware sweep ────────────────────────────────────────────────────

TM_ID = "p-tm"
ES_ID = "p-es"


def _dep_pkg(pkg_id: str, name: str, deps: list | None = None) -> dict:
    p = _pkg(pkg_id, name)
    p["app_dependencies"] = deps or []
    return p


def _req(dep_id: str, name: str) -> dict:
    return {"pkg_id": dep_id, "display_name": name, "required": True}


class SweepProbe(FakeProbe):
    """Multi-package probe: tracks the installed set per (pkg_id, bot_id) and
    models the live dependent-preflight — a package with a required dep
    reports an unmet build blocker until that dep is installed on the bot.
    This is exactly the coupling that made the live sweep structurally unable
    to pass: standalone verify tears the dep down before the dependent runs.
    """

    def __init__(self, packages, **kw):
        super().__init__(packages=packages, **kw)
        self.installed: set[tuple[str, str]] = set()
        self._by_id = {p["pkg_id"]: p for p in packages}

    def gallery_package(self, pkg_id):
        return self._by_id.get(pkg_id)

    def preflight(self, pkg_id, bot_id):
        rows = []
        for dep in self._by_id.get(pkg_id, {}).get("app_dependencies") or []:
            if dep.get("required", True) and (dep["pkg_id"], bot_id) not in self.installed:
                rows.append({
                    "type": "app", "id": dep["pkg_id"],
                    "display_name": dep.get("display_name", dep["pkg_id"]),
                    "severity": "build_blocker", "state": "missing",
                    "message": "dependency not installed",
                })
        return {"pkg_id": pkg_id, "bot_id": bot_id, "error": None,
                "ready_to_build": not rows, "ready_to_run": not rows,
                "app_dependencies": rows, "requirements": []}

    def app_manifest(self, pkg_id, bot_id):
        if (pkg_id, bot_id) not in self.installed:
            return None
        name = self._by_id.get(pkg_id, {}).get("display_name", pkg_id)
        return {"id": name.lower().replace(" ", "-"), "name": name,
                "display_name": name, "pkg_id": pkg_id, "spec_id": pkg_id,
                "status": "approved", "definition_status": "defined",
                "manifest_shape": "v7-arc", "classification": {},
                "scheduled_actions": []}

    def recon_buckets(self, bot_id):
        return {"owned_ok": [
            {"spec_id": pid, "path": f"scripts/{pid}.py",
             "reason": "claimed_and_marked", "bucket": "owned_ok"}
            for pid, b in self.installed if b == bot_id]}

    def covered_ids(self, bot_id):
        return {pid for pid, b in self.installed if b == bot_id}

    def installed_apps_md(self, bot_id):
        return "\n".join(
            self._by_id.get(pid, {}).get("display_name", pid)
            for pid, b in self.installed if b == bot_id)

    def heartbeat_sections(self, bot_id):
        return []

    def delivery_state(self):
        return {"entries": {}}


class SweepApi:
    """Multi-package API twin of SweepProbe: install flips the (pkg, bot) to
    installed (job completes), a committed delete flips it back. Records call
    order so tests can assert the dependency stayed installed across its
    dependent's verify and was torn down in reverse order afterwards.
    """

    def __init__(self, probe, *, fail_installs=(), oauth_installs=()):
        self.probe = probe
        self.fail_installs = set(fail_installs)
        self.oauth_installs = set(oauth_installs)
        self.calls: list[tuple[str, str, str]] = []  # (op, pkg-or-app id, bot)
        self._job_status: dict[str, str] = {}
        self._n = 0

    def install(self, pkg_id, bot_id, *, force, confirmed):
        self.calls.append(("install", pkg_id, bot_id))
        if pkg_id in self.oauth_installs:
            return 202, {"jobs": [{
                "bot_id": bot_id, "job_id": f"j-{pkg_id}", "status": "awaiting_oauth",
                "missing": [{"integration": "GOG"}], "next": "oauth",
            }]}
        self._n += 1
        jid = f"j-{self._n}"
        if pkg_id in self.fail_installs:
            self._job_status[jid] = "failed"
        else:
            self._job_status[jid] = "complete"
            self.probe.installed.add((pkg_id, bot_id))
        return 200, {"jobs": [{"job_id": jid, "bot_id": bot_id}]}

    def get_job(self, job_id):
        st = self._job_status.get(job_id, "complete")
        steps = ([{"label": "BUILD", "status": "failed", "detail": "build error"}]
                 if st == "failed" else [])
        return 200, {"job_id": job_id, "status": st,
                     "completed_with_errors": False, "steps": steps,
                     "context_snapshot": {}}

    def delete_app(self, bot_id, app_id, *, delete_files, commit):
        if not commit:
            return 200, {"ok": True, "deletion_candidates": []}
        self.calls.append(("delete", app_id, bot_id))
        for pid, p in self.probe._by_id.items():
            if p.get("display_name", "").lower().replace(" ", "-") == app_id:
                self.probe.installed.discard((pid, bot_id))
        return 200, {"ok": True, "actually_deleted": [], "failed_deletes": []}


def _dep_env(*, fail_installs=(), oauth_installs=()):
    tm = _dep_pkg(TM_ID, "Task Mgr")
    es = _dep_pkg(ES_ID, "Evening Sweep", deps=[_req(TM_ID, "Task Mgr")])
    probe = SweepProbe([tm, es])
    api = SweepApi(probe, fail_installs=fail_installs, oauth_installs=oauth_installs)
    return tm, es, probe, api


def _dep_sweep(targets, api, probe, **kw):
    fc = FakeClock()
    kw.setdefault("clock", fc.now)
    kw.setdefault("sleep", fc.sleep)
    return run_sweep(targets, api=api, probe=probe, **kw)


def test_sweep_orders_dependency_first_and_defers_its_teardown():
    tm, es, probe, api = _dep_env()
    # Deliberately dependent-first input: the sweep must reorder.
    results = _dep_sweep([(es, BOT), (tm, BOT)], api, probe)
    by = {r.pkg_id: r for r in results}
    assert by[TM_ID].status == PASS, by[TM_ID].summary
    assert by[ES_ID].status == PASS, by[ES_ID].summary
    installs = [(p, b) for op, p, b in api.calls if op == "install"]
    assert installs == [(TM_ID, BOT), (ES_ID, BOT)]
    # Reverse-order teardown: dependent first, dependency last — and the
    # dependency was still installed while the dependent verified.
    deletes = [a for op, a, _ in api.calls if op == "delete"]
    assert deletes == ["evening-sweep", "task-mgr"]
    assert (api.calls.index(("delete", "task-mgr", BOT))
            > api.calls.index(("install", ES_ID, BOT)))
    assert probe.installed == set(), "everything must be torn down by sweep end"
    # The deferred teardown's steps landed on the dependency's result.
    assert model.STEP_TEARDOWN in {s.step for s in by[TM_ID].steps}


def test_failed_dependency_fail_fasts_dependent_with_clear_row():
    tm, es, probe, api = _dep_env(fail_installs={TM_ID})
    results = _dep_sweep([(tm, BOT), (es, BOT)], api, probe)
    by = {r.pkg_id: r for r in results}
    assert by[TM_ID].status == FAIL
    assert by[ES_ID].status == FAIL
    assert by[ES_ID].failed_step == model.STEP_DEPENDENCY
    assert "Task Mgr" in by[ES_ID].summary
    assert "not attempted" in by[ES_ID].summary
    assert ("install", ES_ID, BOT) not in api.calls, "dependent must not install"
    # The failed dependency's own (dispatched) install is still torn down.
    assert any(op == "delete" for op, *_ in api.calls)


def test_oauth_blocked_dependency_cascades_blocked_oauth():
    tm, es, probe, api = _dep_env(oauth_installs={TM_ID})
    results = _dep_sweep([(tm, BOT), (es, BOT)], api, probe)
    by = {r.pkg_id: r for r in results}
    assert by[TM_ID].status == BLOCKED_OAUTH
    assert by[ES_ID].status == BLOCKED_OAUTH, "OAuth cascade must not count as FAIL"
    assert "Task Mgr" in by[ES_ID].summary and "OAuth" in by[ES_ID].summary
    assert ("install", ES_ID, BOT) not in api.calls
    assert not any(op == "delete" for op, *_ in api.calls), "nothing was built"
    rep = model.RunReport(apps=results)
    assert rep.exit_code() == 0


def test_mid_chain_crash_still_tears_down_deferred_dependency():
    tm, es, probe, api = _dep_env()

    class BoomProbe(SweepProbe):
        def app_manifest(self, pkg_id, bot_id):
            if pkg_id == ES_ID and (pkg_id, bot_id) in self.installed:
                raise RuntimeError("kaboom")
            return super().app_manifest(pkg_id, bot_id)

    boom = BoomProbe([tm, es])
    boom_api = SweepApi(boom)
    results = _dep_sweep([(tm, BOT), (es, BOT)], boom_api, boom)
    by = {r.pkg_id: r for r in results}
    assert by[ES_ID].status == FAIL and by[ES_ID].failed_step == "harness"
    assert boom.installed == set(), "crash must not strand the deferred dependency"
    assert model.STEP_TEARDOWN in {s.step for s in by[TM_ID].steps}


def test_no_teardown_sweep_keeps_dependency_installed():
    tm, es, probe, api = _dep_env()
    results = _dep_sweep([(tm, BOT), (es, BOT)], api, probe, teardown=False)
    assert all(r.status == PASS for r in results)
    assert not any(op == "delete" for op, *_ in api.calls)
    assert probe.installed == {(TM_ID, BOT), (ES_ID, BOT)}


def test_dependency_cycle_fails_both_without_installs():
    a = _dep_pkg("p-a", "App A", deps=[_req("p-b", "App B")])
    b = _dep_pkg("p-b", "App B", deps=[_req("p-a", "App A")])
    probe = SweepProbe([a, b])
    api = SweepApi(probe)
    results = _dep_sweep([(a, BOT), (b, BOT)], api, probe)
    assert len(results) == 2
    for r in results:
        assert r.status == FAIL
        assert r.failed_step == model.STEP_DEPENDENCY
        assert "cycle" in r.summary
    assert api.calls == [], "cycle members must not install"


def test_out_of_sweep_dependency_left_to_preflight():
    tm, es, probe, api = _dep_env()
    # Dep not targeted and not installed → the old, now-accurate preflight FAIL.
    results = _dep_sweep([(es, BOT)], api, probe)
    assert results[0].status == FAIL
    assert results[0].failed_step == model.STEP_PREFLIGHT
    # Dep pre-installed on the pod (outside the sweep) → dependent passes.
    probe.installed.add((TM_ID, BOT))
    results = _dep_sweep([(es, BOT)], api, probe)
    assert results[0].status == PASS, results[0].summary


def test_multi_bot_deferral_is_per_bot():
    tm, es, probe, api = _dep_env()
    b1, b2 = "bot1", "bot2"
    results = _dep_sweep([(tm, b1), (tm, b2), (es, b1), (es, b2)], api, probe)
    assert all(r.status == PASS for r in results), [r.summary for r in results]
    assert probe.installed == set()
    # TM@b1 released right after ES@b1 (its last b1 dependent), while TM@b2
    # stayed installed until ES@b2 had run.
    assert (api.calls.index(("delete", "task-mgr", b1))
            < api.calls.index(("install", ES_ID, b2)))
    assert (api.calls.index(("delete", "task-mgr", b2))
            > api.calls.index(("install", ES_ID, b2)))


def test_on_result_fires_once_per_app_with_final_status():
    tm, es, probe, api = _dep_env()
    ticks: list[tuple[str, str, set]] = []

    def _tick(r):
        ticks.append((r.pkg_id, r.status, {s.step for s in r.steps}))

    results = _dep_sweep([(tm, BOT), (es, BOT)], api, probe, on_result=_tick)
    assert len(ticks) == len(results) == 2
    tm_tick = next(t for t in ticks if t[0] == TM_ID)
    # The dependency's tick fired only after its deferred teardown ran.
    assert tm_tick[1] == PASS and model.STEP_TEARDOWN in tm_tick[2]


def test_dry_run_sweep_ignores_dependency_ordering():
    tm, es, probe, api = _dep_env()
    probe.installed = {(TM_ID, BOT), (ES_ID, BOT)}
    results = _dep_sweep([(es, BOT), (tm, BOT)], api, probe, mode="dry-run")
    # No reordering in dry-run: input order preserved, nothing installed/deleted.
    assert [r.pkg_id for r in results] == [ES_ID, TM_ID]
    assert api.calls == []


def test_duplicate_targets_do_not_crash_the_sweep():
    # Adversarial regression: DeferredTeardown twins from duplicate (pkg, bot)
    # targets compared equal, so pending.remove() deleted the wrong one and
    # the sweep died on ValueError, losing the whole report.
    tm, es, probe, api = _dep_env()
    results = _dep_sweep([(tm, BOT), (tm, BOT), (es, BOT)], api, probe)
    assert len(results) == 3
    assert probe.installed == set()
    assert all(r.status == PASS for r in results), [r.summary for r in results]


def test_raising_on_result_never_strands_installs():
    # Adversarial regression: a raising on_result (e.g. BrokenPipeError from a
    # closed stderr pipe under `-v | head`) inside the drain stranded every
    # dependency still pending. Teardowns must all run before callbacks fire.
    d1 = _dep_pkg("p-dep1", "Dep One")
    d2 = _dep_pkg("p-dep2", "Dep Two")
    child = _dep_pkg("p-child", "Child",
                     deps=[_req("p-dep1", "Dep One"), _req("p-dep2", "Dep Two")])
    probe = SweepProbe([d1, d2, child])
    api = SweepApi(probe)

    def _boom_tick(r):
        raise BrokenPipeError("stderr gone")

    with pytest.raises(BrokenPipeError):
        _dep_sweep([(d1, BOT), (d2, BOT), (child, BOT)], api, probe,
                   on_result=_boom_tick)
    assert probe.installed == set(), "a reporting callback must never strand installs"


def test_depth2_chain_tears_down_in_full_reverse_order():
    # Adversarial regression: for A ← B ← C (C does NOT declare A), A was
    # released as soon as B ran — deleted while B, which requires it, was
    # still installed. A deferred foundation must wait for pending dependents.
    a = _dep_pkg("p-a", "App A")
    b = _dep_pkg("p-b", "App B", deps=[_req("p-a", "App A")])
    c = _dep_pkg("p-c", "App C", deps=[_req("p-b", "App B")])
    probe = SweepProbe([a, b, c])
    api = SweepApi(probe)
    results = _dep_sweep([(c, BOT), (b, BOT), (a, BOT)], api, probe)
    assert all(r.status == PASS for r in results), [r.summary for r in results]
    deletes = [x for op, x, _ in api.calls if op == "delete"]
    assert deletes == ["app-c", "app-b", "app-a"]
    assert probe.installed == set()


def test_deferred_dependency_crash_surfaces_teardown_on_visible_row():
    # Adversarial regression: when the DEPENDENCY's verify crashed after its
    # install dispatched, the deferred teardown ran against a discarded orphan
    # result — its outcome vanished from the report. The harness row must
    # adopt the orphan so the teardown checks stay visible.
    tm, es, probe, api = _dep_env()

    class BoomProbe(SweepProbe):
        def app_manifest(self, pkg_id, bot_id):
            if pkg_id == TM_ID and (pkg_id, bot_id) in self.installed:
                raise RuntimeError("kaboom-after-install")
            return super().app_manifest(pkg_id, bot_id)

    boom = BoomProbe([tm, es])
    boom_api = SweepApi(boom)
    results = _dep_sweep([(tm, BOT), (es, BOT)], boom_api, boom)
    by = {r.pkg_id: r for r in results}
    assert by[TM_ID].status == FAIL and by[TM_ID].failed_step == "harness"
    assert "kaboom-after-install" in by[TM_ID].summary
    # The deferred teardown ran AND its step landed on the visible row.
    assert model.STEP_TEARDOWN in {s.step for s in by[TM_ID].steps}
    assert boom.installed == set()
    # The dependent fail-fasted on the crashed dependency.
    assert by[ES_ID].failed_step == model.STEP_DEPENDENCY


def test_order_packages_stable_and_cycle_detection():
    from evolve_admin.gallery_verify.deps import order_packages

    tm = _dep_pkg(TM_ID, "Task Mgr")
    es = _dep_pkg(ES_ID, "Evening Sweep", deps=[_req(TM_ID, "Task Mgr")])
    j = _dep_pkg("p-j", "Journal")
    ordered, cycles = order_packages([j, es, tm])
    assert [p["pkg_id"] for p in ordered] == ["p-j", TM_ID, ES_ID]
    assert cycles == {}
    # No in-sweep deps → input order untouched (pure refactor for the rest).
    ordered, _ = order_packages([es, j])  # tm absent → edge out of sweep
    assert [p["pkg_id"] for p in ordered] == [ES_ID, "p-j"]
    # Cycle members (and their dependents) are flagged, not ordered.
    a = _dep_pkg("p-a", "A", deps=[_req("p-b", "B")])
    b = _dep_pkg("p-b", "B", deps=[_req("p-a", "A")])
    down = _dep_pkg("p-d", "D", deps=[_req("p-a", "A")])
    ordered, cycles = order_packages([a, b, down, j])
    assert [p["pkg_id"] for p in ordered] == ["p-j", "p-a", "p-b", "p-d"]
    assert set(cycles) == {"p-a", "p-b", "p-d"}
    assert "cycle" in cycles["p-a"]


def test_main_enriches_deps_from_full_package_json(monkeypatch, tmp_path):
    # Index rows carry no app_dependencies; the CLI must enrich via
    # probe.gallery_package so the sweep still orders dep-first.
    from evolve_admin.gallery_verify import __main__ as cli
    from evolve_admin.gallery_verify import probe as probe_mod
    from evolve_admin.gallery_verify import api as api_mod

    tm = _dep_pkg(TM_ID, "Task Mgr")
    es = _dep_pkg(ES_ID, "Evening Sweep", deps=[_req(TM_ID, "Task Mgr")])

    class IndexRowProbe(SweepProbe):
        def gallery_packages(self):
            # Denormalized index rows: no app_dependencies key.
            return [
                {k: v for k, v in p.items() if k != "app_dependencies"}
                for p in (es, tm)  # dependent-first, forcing a reorder
            ]

    fp = IndexRowProbe([tm, es])
    api = SweepApi(fp)
    monkeypatch.setattr(probe_mod, "LivePodProbe", lambda **kw: fp)
    monkeypatch.setattr(api_mod, "LiveAdminApi", lambda **kw: api)
    rc = cli.main(["--bot", BOT, "--yes", "--out", str(tmp_path / "r.json")])
    assert rc == 0
    installs = [(p, b) for op, p, b in api.calls if op == "install"]
    assert installs == [(TM_ID, BOT), (ES_ID, BOT)]
    assert fp.installed == set()


# ── __main__ cost gate + filters ──────────────────────────────────────────────

def test_main_helpers_meta_and_match():
    from evolve_admin.gallery_verify import __main__ as cli
    ea = _pkg("p-ea", "EA Pack", tags=["starter-pack", "archetype"])
    assert cli._is_meta(ea)
    assert not cli._is_meta(_pkg())
    assert cli._matches(_pkg(), {"task manager"}, set())
    assert cli._matches(_pkg(), set(), {"task-management"})
    assert not cli._matches(_pkg(), {"nonexistent"}, set())


def test_main_full_sweep_requires_yes(monkeypatch):
    from evolve_admin.gallery_verify import __main__ as cli
    from evolve_admin.gallery_verify import probe as probe_mod
    from evolve_admin.gallery_verify import api as api_mod

    fp = FakeProbe(packages=[_pkg(), _pkg("p-2", "Journal")])
    monkeypatch.setattr(probe_mod, "LivePodProbe", lambda **kw: fp)
    monkeypatch.setattr(api_mod, "LiveAdminApi", lambda **kw: FakeApi(probe=fp))
    rc = cli.main(["--bot", BOT])  # install mode, no --apps, no --yes
    assert rc == 2, "full-sweep install without --yes must refuse"


def test_main_dry_run_runs_without_confirmation(monkeypatch, tmp_path):
    from evolve_admin.gallery_verify import __main__ as cli
    from evolve_admin.gallery_verify import probe as probe_mod
    from evolve_admin.gallery_verify import api as api_mod

    fp = FakeProbe(packages=[_pkg()])
    api = FakeApi(probe=fp)
    monkeypatch.setattr(probe_mod, "LivePodProbe", lambda **kw: fp)
    monkeypatch.setattr(api_mod, "LiveAdminApi", lambda **kw: api)
    rc = cli.main(["--bot", BOT, "--dry-run", "--out", str(tmp_path / "r.json")])
    assert rc == 0
    assert api.installs == [] and api.deletes == []
