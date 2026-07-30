"""tests/test_install_chain.py — dependency-ordered install chains (audit S5a).

Covers the S5a contract (docs/audit-gallery-framework-2026-07-02.md §2):

  * topo order — required-dep closure resolves foundations-first, root last;
    nested bundles (a dep that is itself a meta-package) recurse; each
    package appears once
  * cycle detection — a required-dep cycle raises loudly, with the path
  * mid-chain failure — later links marked blocked (blocked_by set), chain
    failed; resume retries the failed link via a fresh clone job and runs
    the formerly-blocked links; succeeded links never re-run
  * already-installed skip — links skip via the same installed_state check
    preflight uses, at creation and again at advance/resume time
  * awaiting_oauth suspends the chain resumably; the OAuth sweeper's resume
    re-enters the chain (install_chain_id stamp) and the rest of the links run
  * concurrent-duplicate guard — a second install of the same (pkg, bot)
    resumes the incomplete chain instead of minting a competitor
  * single-app regression — installs of packages without app_dependencies
    take the exact pre-chain route path (job_id response, no chain)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


# ── Fixture packages ──────────────────────────────────────────────────────────
#
# Synthetic graph (imported gallery, so nothing depends on shipped packages):
#
#   p-cccc0001 (Leaf C)                      — no deps
#   p-aaaa0001 (App A)      → C              — plain app with a foundation
#   p-bbbb0001 (Bundle B)   → C, A           — nested meta-package
#   p-dddd0001 (Root Pack)  → A, B           — the bundle under install
#
# Required-dep closure of Root Pack, DFS post-order: C, A, B, Root.

LEAF_C = "p-cccc0001"
APP_A = "p-aaaa0001"
BUNDLE_B = "p-bbbb0001"
ROOT = "p-dddd0001"

TOPO_ORDER = [LEAF_C, APP_A, BUNDLE_B, ROOT]


def _pkg(pkg_id: str, name: str, deps: list[str] = (), *,
         requirements: dict | None = None, optional_deps: list[str] = ()) -> dict:
    return {
        "pkg_id": pkg_id,
        "name": name,
        "display_name": name.replace("-", " ").title(),
        "objective": f"test package {name}",
        "build_spec": f"Build {name}.",
        "pkg_version": "2026.07.02-1.0",
        "app_dependencies": (
            [{"pkg_id": d, "display_name": d, "required": True,
              "reason": "test dep"} for d in deps]
            + [{"pkg_id": d, "display_name": d, "required": False,
                "reason": "optional"} for d in optional_deps]
        ),
        "requirements": requirements or {},
    }


@pytest.fixture
def shared_dir(tmp_path):
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture
def gallery_env(shared_dir):
    """Import the synthetic package graph into the imported gallery."""
    from evolve_admin.applications.gallery import import_package

    for pkg in (
        _pkg(LEAF_C, "leaf-c"),
        _pkg(APP_A, "app-a", [LEAF_C]),
        _pkg(BUNDLE_B, "bundle-b", [LEAF_C, APP_A]),
        _pkg(ROOT, "root-pack", [APP_A, BUNDLE_B]),
    ):
        ok, reason = import_package(pkg, shared_dir)
        assert ok, reason
    return shared_dir


@pytest.fixture
def chain_env(gallery_env):
    """Fake-forge harness: a registry-backed manifest store + a runner seam
    that 'installs' apps by completing the job and registering a manifest.

    Yields a dict; ``fail_pkgs`` (a set) can be mutated mid-test to make
    specific packages fail their build.
    """
    from evolve_admin.applications import install_chain as ic
    from evolve_admin.applications.manifest import ApplicationManifest

    shared_dir = gallery_env
    manifests: list = []
    calls: list[str] = []
    fail_pkgs: set[str] = set()

    def _register_manifest(job) -> None:
        manifests.append(ApplicationManifest.from_dict({
            "id": job.app_id,
            "name": job.app_id,
            "bot_id": job.bot_id,
            "pkg_id": job.pkg_id,
            "status": "approved",
            "install_job": {"status": "complete"},
        }))

    def runner(sd: Path, job_id: str, bot_id: str) -> None:
        from evolve_admin.applications.forge_jobs import load_job, save_job
        job = load_job(job_id, sd)
        assert job is not None, f"runner got unknown job {job_id}"
        calls.append(job.pkg_id)
        if job.pkg_id in fail_pkgs:
            job.status = "failed"
            save_job(job, sd)
            return
        job.status = "complete"
        save_job(job, sd)
        _register_manifest(job)

    def _list_manifests(sd: Path, bot_id: str):
        return [m for m in manifests if m.bot_id == bot_id]

    ic.set_install_runner(runner)
    ic.set_force_sync(True)
    with patch(
        "evolve_admin.applications.manifest.list_manifests",
        side_effect=_list_manifests,
    ):
        try:
            yield {
                "shared_dir": shared_dir,
                "manifests": manifests,
                "calls": calls,
                "fail_pkgs": fail_pkgs,
                "register": _register_manifest,
            }
        finally:
            ic.set_install_runner(None)
            ic.set_force_sync(False)


# ── Topological closure ───────────────────────────────────────────────────────


class TestClosure:
    def test_topo_order_nested_bundle(self, gallery_env):
        """Foundations before dependents, root last, each package once —
        including through the nested meta-package (Bundle B)."""
        from evolve_admin.applications.install_chain import resolve_dependency_closure

        closure = resolve_dependency_closure(ROOT, gallery_env)
        assert [p["pkg_id"] for p in closure] == TOPO_ORDER

    def test_optional_deps_excluded(self, shared_dir):
        """Optional dependencies are runtime enhancements — never pulled
        into the chain."""
        from evolve_admin.applications.gallery import import_package
        from evolve_admin.applications.install_chain import resolve_dependency_closure

        ok, reason = import_package(_pkg(LEAF_C, "leaf-c"), shared_dir)
        assert ok, reason
        ok, reason = import_package(
            _pkg("p-eeee0001", "opt-root", optional_deps=[LEAF_C]), shared_dir,
        )
        assert ok, reason
        closure = resolve_dependency_closure("p-eeee0001", shared_dir)
        assert [p["pkg_id"] for p in closure] == ["p-eeee0001"]

    def test_cycle_detected_loudly(self, shared_dir):
        from evolve_admin.applications.gallery import import_package
        from evolve_admin.applications.install_chain import (
            DependencyCycleError, resolve_dependency_closure,
        )

        ok, _ = import_package(_pkg("p-eeee0002", "cyc-a", ["p-ffff0002"]), shared_dir)
        assert ok
        ok, _ = import_package(_pkg("p-ffff0002", "cyc-b", ["p-eeee0002"]), shared_dir)
        assert ok
        with pytest.raises(DependencyCycleError) as exc_info:
            resolve_dependency_closure("p-eeee0002", shared_dir)
        assert "p-eeee0002" in str(exc_info.value)
        assert "p-ffff0002" in str(exc_info.value)

    def test_missing_dep_raises(self, shared_dir):
        from evolve_admin.applications.gallery import import_package
        from evolve_admin.applications.install_chain import resolve_dependency_closure

        ok, _ = import_package(
            _pkg("p-eeee0003", "dangling", ["p-00000bad"]), shared_dir,
        )
        assert ok
        with pytest.raises(ValueError, match="p-00000bad"):
            resolve_dependency_closure("p-eeee0003", shared_dir)


# ── Advance: happy path / skip / failure / resume ────────────────────────────


class TestAdvance:
    def test_happy_path_runs_in_topo_order(self, chain_env):
        from evolve_admin.applications import install_chain as ic

        sd = chain_env["shared_dir"]
        chain = ic.create_chain(ROOT, "bot-a", sd)
        done = ic.advance_chain(chain.chain_id, sd)

        assert chain_env["calls"] == TOPO_ORDER
        assert done.status == "complete"
        assert [lk.state for lk in done.links] == ["succeeded"] * 4
        # Every link job is chain-stamped and complete on disk
        from evolve_admin.applications.forge_jobs import load_job
        for lk in done.links:
            job = load_job(lk.job_id, sd)
            assert job.status == "complete"
            assert job.context_snapshot["install_chain_id"] == chain.chain_id

    def test_already_installed_deps_skip(self, chain_env):
        """Links whose package is installed are born skipped and never run."""
        from evolve_admin.applications import install_chain as ic

        sd = chain_env["shared_dir"]
        # Pre-install Leaf C + App A out of band
        fake_job = type("J", (), {
            "app_id": "leaf-c", "bot_id": "bot-a", "pkg_id": LEAF_C,
        })
        chain_env["register"](fake_job)
        fake_job2 = type("J", (), {
            "app_id": "app-a", "bot_id": "bot-a", "pkg_id": APP_A,
        })
        chain_env["register"](fake_job2)

        chain = ic.create_chain(ROOT, "bot-a", sd)
        by_pkg = {lk.pkg_id: lk for lk in chain.links}
        assert by_pkg[LEAF_C].state == "skipped"
        assert by_pkg[APP_A].state == "skipped"

        done = ic.advance_chain(chain.chain_id, sd)
        assert done.status == "complete"
        assert chain_env["calls"] == [BUNDLE_B, ROOT]

    def test_mid_chain_failure_blocks_later_then_resume(self, chain_env):
        from evolve_admin.applications import install_chain as ic
        from evolve_admin.applications.forge_jobs import load_job

        sd = chain_env["shared_dir"]
        chain_env["fail_pkgs"].add(APP_A)

        chain = ic.create_chain(ROOT, "bot-a", sd)
        failed = ic.advance_chain(chain.chain_id, sd)

        assert failed.status == "failed"
        by_pkg = {lk.pkg_id: lk for lk in failed.links}
        assert by_pkg[LEAF_C].state == "succeeded"
        assert by_pkg[APP_A].state == "failed"
        assert by_pkg[BUNDLE_B].state == "blocked"
        assert by_pkg[BUNDLE_B].blocked_by == APP_A
        assert by_pkg[ROOT].state == "blocked"
        assert by_pkg[ROOT].blocked_by == APP_A
        assert chain_env["calls"] == [LEAF_C, APP_A]
        failed_job_id = by_pkg[APP_A].job_id

        # Operator fixes the cause; resume re-runs from App A
        chain_env["fail_pkgs"].clear()
        resumed = ic.resume_chain(chain.chain_id, sd)

        assert resumed.status == "complete"
        by_pkg = {lk.pkg_id: lk for lk in resumed.links}
        assert [by_pkg[p].state for p in TOPO_ORDER] == \
            ["succeeded", "succeeded", "succeeded", "succeeded"]
        # Leaf C did NOT re-run; App A retried once; blocked links ran
        assert chain_env["calls"] == [LEAF_C, APP_A, APP_A, BUNDLE_B, ROOT]
        # The retry went through a fresh clone job, not the failed job
        assert by_pkg[APP_A].job_id != failed_job_id
        prior = load_job(failed_job_id, sd)
        assert prior.superseded_by_job_id == by_pkg[APP_A].job_id
        clone = load_job(by_pkg[APP_A].job_id, sd)
        assert clone.is_retry is True
        assert clone.context_snapshot["install_chain_id"] == chain.chain_id

    def test_resume_of_complete_chain_never_reinstalls(self, chain_env):
        """Adversarial: resume after success must be a no-op (idempotent)."""
        from evolve_admin.applications import install_chain as ic

        sd = chain_env["shared_dir"]
        chain = ic.create_chain(ROOT, "bot-a", sd)
        ic.advance_chain(chain.chain_id, sd)
        assert chain_env["calls"] == TOPO_ORDER

        again = ic.resume_chain(chain.chain_id, sd)
        assert again.status == "complete"
        assert chain_env["calls"] == TOPO_ORDER  # no new runs

    def test_resume_skips_links_installed_out_of_band(self, chain_env):
        """A dep that got installed by other means between failure and
        resume SKIPs instead of re-installing (double-install guard)."""
        from evolve_admin.applications import install_chain as ic

        sd = chain_env["shared_dir"]
        chain_env["fail_pkgs"].add(APP_A)
        chain = ic.create_chain(ROOT, "bot-a", sd)
        ic.advance_chain(chain.chain_id, sd)

        # App A gets installed outside the chain (e.g. single install)
        chain_env["fail_pkgs"].clear()
        fake_job = type("J", (), {
            "app_id": "app-a", "bot_id": "bot-a", "pkg_id": APP_A,
        })
        chain_env["register"](fake_job)

        resumed = ic.resume_chain(chain.chain_id, sd)
        assert resumed.status == "complete"
        by_pkg = {lk.pkg_id: lk for lk in resumed.links}
        assert by_pkg[APP_A].state == "skipped"
        # App A never re-ran after its original failure
        assert chain_env["calls"] == [LEAF_C, APP_A, BUNDLE_B, ROOT]

    def test_foreign_in_flight_install_parks_chain(self, chain_env):
        """A dep mid-install by a job OUTSIDE the chain parks the chain
        (blocked) instead of racing a duplicate build."""
        from evolve_admin.applications import install_chain as ic
        from evolve_admin.applications.manifest import ApplicationManifest

        sd = chain_env["shared_dir"]
        chain_env["manifests"].append(ApplicationManifest.from_dict({
            "id": "leaf-c", "name": "leaf-c", "bot_id": "bot-a",
            "pkg_id": LEAF_C, "status": "updating",
            "install_job": {"status": "running"},
        }))

        chain = ic.create_chain(ROOT, "bot-a", sd)
        parked = ic.advance_chain(chain.chain_id, sd)
        assert parked.status == "blocked"
        assert chain_env["calls"] == []  # nothing ran

        # The foreign install finishes → resume skips it and completes
        chain_env["manifests"].clear()
        fake_job = type("J", (), {
            "app_id": "leaf-c", "bot_id": "bot-a", "pkg_id": LEAF_C,
        })
        chain_env["register"](fake_job)
        resumed = ic.resume_chain(chain.chain_id, sd)
        assert resumed.status == "complete"
        assert chain_env["calls"] == [APP_A, BUNDLE_B, ROOT]

    def test_legacy_pkg_id_manifest_skips_not_rebuilds(self, chain_env):
        """Adversarial (MAJOR 2a): a bot that installed a dep under its
        RETIRED pkg_id must SKIP, not silently rebuild a second copy under
        the surviving id. Uses the real Task Manager migration entry."""
        from evolve_admin.applications import install_chain as ic
        from evolve_admin.applications.gallery import (
            LEGACY_KEY_MIGRATION, import_package,
        )
        from evolve_admin.applications.manifest import ApplicationManifest

        # Pick a real retired→surviving pair and build a root that depends on
        # the surviving id.
        retired, surviving = next(iter(LEGACY_KEY_MIGRATION.items()))
        sd = chain_env["shared_dir"]
        # Ensure the surviving package resolves (it's a shipped builtin);
        # build a synthetic root depending on it.
        ok, reason = import_package(
            _pkg("p-9999a001", "legacy-root", [surviving]), sd,
        )
        assert ok, reason

        # The bot already runs it — but the manifest carries the RETIRED id.
        chain_env["manifests"].append(ApplicationManifest.from_dict({
            "id": "task-manager", "name": "task-manager", "bot_id": "bot-a",
            "pkg_id": retired, "status": "approved", "install_job": None,
        }))

        chain = ic.create_chain("p-9999a001", "bot-a", sd)
        by_pkg = {lk.pkg_id: lk for lk in chain.links}
        # The dependency link is born skipped despite the id mismatch
        assert by_pkg[surviving].state == "skipped"

        done = ic.advance_chain(chain.chain_id, sd)
        assert done.status == "complete"
        # The already-installed dep was NOT rebuilt
        assert surviving not in chain_env["calls"]

    def test_cross_chain_shared_dep_not_double_built(self, chain_env):
        """Adversarial (MAJOR 1): two chains (different roots) sharing a
        dependency on the same bot must not both forge-build it. With the
        slot lock + active-job-aware installed_state, the second chain skips
        the shared dep the first already installed."""
        from evolve_admin.applications import install_chain as ic

        sd = chain_env["shared_dir"]
        # Two roots that both depend on Leaf C (and App A → Leaf C).
        chain_a = ic.create_chain(ROOT, "bot-a", sd)      # → C, A, B, ROOT
        chain_b = ic.create_chain(BUNDLE_B, "bot-a", sd)  # → C, A, B

        ic.advance_chain(chain_a.chain_id, sd)
        # Chain A installed C, A, B, ROOT
        assert chain_env["calls"] == TOPO_ORDER

        ic.advance_chain(chain_b.chain_id, sd)
        # Chain B finds C, A, B all installed → builds nothing new
        assert chain_env["calls"] == TOPO_ORDER
        done_b = ic.load_chain(chain_b.chain_id, sd)
        assert done_b.status == "complete"
        assert all(lk.state in ("succeeded", "skipped") for lk in done_b.links)

    def test_diamond_alias_closure_dedups_on_canonical(self, shared_dir):
        """Adversarial (MAJOR 2b): a diamond where one branch names the
        retired id and another the surviving id must yield ONE link for the
        shared package, not two (which would double-count the cost)."""
        from evolve_admin.applications.gallery import (
            LEGACY_KEY_MIGRATION, import_package,
        )
        from evolve_admin.applications.install_chain import (
            resolve_dependency_closure,
        )

        retired, surviving = next(iter(LEGACY_KEY_MIGRATION.items()))
        # branch-1 depends on the retired id, branch-2 on the surviving id,
        # root depends on both branches.
        for pid, name, deps in (
            ("p-8888b001", "branch-1", [retired]),
            ("p-8888b002", "branch-2", [surviving]),
            ("p-8888b003", "diamond-root", ["p-8888b001", "p-8888b002"]),
        ):
            ok, reason = import_package(_pkg(pid, name, deps), shared_dir)
            assert ok, reason

        closure = resolve_dependency_closure("p-8888b003", shared_dir)
        ids = [p["pkg_id"] for p in closure]
        # The shared package appears exactly once, under its canonical id
        assert ids.count(surviving) == 1
        assert retired not in ids

    def test_advance_lock_excludes_concurrent_advancers(self, chain_env):
        """While one advance holds the chain lock, a second advance returns
        the on-disk state without running anything (no double work)."""
        from evolve_admin.applications import install_chain as ic

        sd = chain_env["shared_dir"]
        chain = ic.create_chain(ROOT, "bot-a", sd)

        lock = ic._lock_for(chain.chain_id)
        assert lock.acquire(blocking=False)
        try:
            result = ic.advance_chain(chain.chain_id, sd)
            assert result is not None
            assert chain_env["calls"] == []  # did not run any link
        finally:
            lock.release()


# ── OAuth suspension + sweeper re-entry ───────────────────────────────────────


class TestOAuthSuspension:
    @pytest.fixture
    def oauth_gallery(self, chain_env):
        """Add a package whose dep needs an integration, so the chain
        suspends at that link."""
        from evolve_admin.applications.gallery import import_package

        sd = chain_env["shared_dir"]
        ok, reason = import_package(_pkg(
            "p-0aa70001", "needs-oauth", [LEAF_C],
            requirements={"integrations": [{"id": "gog", "required": True}]},
        ), sd)
        assert ok, reason
        ok, reason = import_package(
            _pkg("p-0aa70002", "oauth-root", ["p-0aa70001"]), sd,
        )
        assert ok, reason
        return chain_env

    @staticmethod
    def _suspend_chain(oauth_gallery):
        """Drive the chain to its awaiting_oauth suspension point."""
        from evolve_admin.applications import install_chain as ic

        sd = oauth_gallery["shared_dir"]

        def _prereq(bot_id, requirements, **kw):
            if requirements.get("integrations"):
                return {"satisfied": False, "missing": [{
                    "integration_id": "gog",
                    "integration": "Google (GOG)",
                    "reason": "not configured",
                    "action_url": "/api/skills/install/gog",
                    "action_label": "Set up Gmail & Calendar",
                }]}
            return {"satisfied": True, "missing": []}

        with patch(
            "evolve_admin.applications.oauth_orchestrator."
            "evaluate_install_prerequisites",
            side_effect=_prereq,
        ):
            chain = ic.create_chain("p-0aa70002", "bot-a", sd)
            suspended = ic.advance_chain(chain.chain_id, sd)
        return chain, suspended

    def test_oauth_gap_suspends_chain_resumably(self, oauth_gallery):
        from evolve_admin.applications.forge_jobs import load_job

        sd = oauth_gallery["shared_dir"]
        chain, suspended = self._suspend_chain(oauth_gallery)

        assert suspended.status == "awaiting_oauth"
        by_pkg = {lk.pkg_id: lk for lk in suspended.links}
        assert by_pkg[LEAF_C].state == "succeeded"
        assert by_pkg["p-0aa70001"].state == "awaiting_oauth"
        assert by_pkg["p-0aa70002"].state == "pending"
        # Leaf C ran; the oauth link did NOT run
        assert oauth_gallery["calls"] == [LEAF_C]
        # The suspended link's job is a real awaiting_oauth job, chain-stamped
        job = load_job(by_pkg["p-0aa70001"].job_id, sd)
        assert job.status == "awaiting_oauth"
        assert job.context_snapshot["install_chain_id"] == chain.chain_id

    def test_resume_after_oauth_abandon_recheck_not_blind_build(self, oauth_gallery):
        """Adversarial: the sweeper abandons the OAuth wait (job failed),
        the operator resumes WITHOUT completing OAuth — the retry clone must
        re-run the OAuth gate and re-suspend, not build the app with its
        integration still missing."""
        from evolve_admin.applications import install_chain as ic
        from evolve_admin.applications.forge_jobs import load_job, save_job

        sd = oauth_gallery["shared_dir"]
        chain, suspended = self._suspend_chain(oauth_gallery)
        by_pkg = {lk.pkg_id: lk for lk in suspended.links}
        oauth_job_id = by_pkg["p-0aa70001"].job_id

        # Sweeper abandon: 30 minutes pass, no OAuth
        job = load_job(oauth_job_id, sd)
        job.status = "failed"
        job.context_snapshot["oauth_abandon_reason"] = "oauth_abandoned"
        save_job(job, sd)
        oauth_gallery["calls"].clear()

        def _still_missing(bot_id, requirements, **kw):
            if requirements.get("integrations"):
                return {"satisfied": False, "missing": [{
                    "integration_id": "gog", "integration": "Google (GOG)",
                    "reason": "still not configured",
                    "action_url": "/api/skills/install/gog",
                    "action_label": "Set up Gmail & Calendar",
                }]}
            return {"satisfied": True, "missing": []}

        with patch(
            "evolve_admin.applications.oauth_orchestrator."
            "evaluate_install_prerequisites",
            side_effect=_still_missing,
        ):
            resumed = ic.resume_chain(chain.chain_id, sd)

        assert resumed.status == "awaiting_oauth"
        by_pkg = {lk.pkg_id: lk for lk in resumed.links}
        assert by_pkg["p-0aa70001"].state == "awaiting_oauth"
        assert oauth_gallery["calls"] == []  # the build never ran
        # The wait restarted on a fresh clone, superseding the abandoned job
        clone_id = by_pkg["p-0aa70001"].job_id
        assert clone_id != oauth_job_id
        clone = load_job(clone_id, sd)
        assert clone.status == "awaiting_oauth"
        assert load_job(oauth_job_id, sd).superseded_by_job_id == clone_id

    def test_sweeper_resume_reenters_chain(self, oauth_gallery):
        """OAuth completes → the sweeper resumes the job AND the chain runs
        through to completion (the install_chain_id re-entry path)."""
        from evolve_admin.applications import install_chain as ic
        from evolve_admin.oauth.sweeper import check_awaiting_oauth_jobs

        sd = oauth_gallery["shared_dir"]
        chain, _ = self._suspend_chain(oauth_gallery)
        oauth_gallery["calls"].clear()

        satisfied = {"satisfied": True, "missing": []}
        with patch(
            "evolve_admin.oauth.orchestrator.evaluate_install_prerequisites",
            return_value=satisfied,
        ), patch(
            "evolve_admin.applications.oauth_orchestrator."
            "evaluate_install_prerequisites",
            return_value=satisfied,
        ):
            resumed_ids = check_awaiting_oauth_jobs(sd)

        assert len(resumed_ids) == 1
        done = ic.load_chain(chain.chain_id, sd)
        assert done.status == "complete"
        # The oauth link ran (via chain re-entry), then the root
        assert oauth_gallery["calls"] == ["p-0aa70001", "p-0aa70002"]


# ── S6 requirement types through the chain preflight ──────────────────────────
#
# S6 (#3412) added ``files`` / ``messaging_channel`` requirement types to
# preflight_check after S5a was written. The per-link hard preflight consumes
# preflight_check's flat requirements list, so the new types must flow through
# per their declared severity: a missing build_blocker file fails the link
# cleanly (detail set, later links blocked — never a crash), while the default
# runtime_warning severity lets the chain proceed and the app degrade loudly
# at runtime.


class TestChainS6RequirementTypes:

    @pytest.fixture
    def file_req_env(self, chain_env, monkeypatch, tmp_path):
        """Bot home stands in at a temp dir; sudo fallbacks always fail."""
        import subprocess as _sp
        from evolve_admin.applications import gallery

        home = tmp_path / "bot-home"
        home.mkdir()
        monkeypatch.setattr(gallery, "_home_of", lambda bot_id: home)

        real_run = _sp.run

        def _no_sudo(cmd, *a, **k):
            if cmd and cmd[0] == "sudo":
                raise FileNotFoundError("no sudo in tests")
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(_sp, "run", _no_sudo)
        chain_env["bot_home"] = home
        return chain_env

    def _import_root_with_file_req(self, sd, *, severity=None):
        from evolve_admin.applications.gallery import import_package

        req = {
            "id": "root-tokens",
            "path": ".openclaw/root-tokens.json",
            "display_name": "Root tokens file",
            "required": True,
            "reason": "test file requirement",
        }
        if severity:
            req["severity"] = severity
        pkg = _pkg("p-eeee0001", "root-with-file", [LEAF_C],
                   requirements={"files": [req]})
        ok, reason = import_package(pkg, sd)
        assert ok, reason
        return "p-eeee0001"

    def test_missing_build_blocker_file_fails_link_cleanly(self, file_req_env):
        from evolve_admin.applications import install_chain as ic

        sd = file_req_env["shared_dir"]
        root = self._import_root_with_file_req(sd, severity="build_blocker")

        chain = ic.create_chain(root, "bot-a", sd)
        done = ic.advance_chain(chain.chain_id, sd)

        assert done.status == "failed"
        by_pkg = {lk.pkg_id: lk for lk in done.links}
        assert by_pkg[LEAF_C].state == "succeeded"
        assert by_pkg[root].state == "failed"
        assert "preflight failed" in by_pkg[root].detail
        assert "root-tokens" in by_pkg[root].detail
        # The dep ran; the blocked root never reached the runner
        assert file_req_env["calls"] == [LEAF_C]

    def test_missing_runtime_warning_file_does_not_block_chain(self, file_req_env):
        from evolve_admin.applications import install_chain as ic

        sd = file_req_env["shared_dir"]
        root = self._import_root_with_file_req(sd)  # default severity

        chain = ic.create_chain(root, "bot-a", sd)
        done = ic.advance_chain(chain.chain_id, sd)

        assert done.status == "complete"
        assert file_req_env["calls"] == [LEAF_C, root]

    def test_present_file_satisfies_build_blocker(self, file_req_env):
        from evolve_admin.applications import install_chain as ic

        sd = file_req_env["shared_dir"]
        root = self._import_root_with_file_req(sd, severity="build_blocker")
        target = file_req_env["bot_home"] / ".openclaw" / "root-tokens.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}")

        chain = ic.create_chain(root, "bot-a", sd)
        done = ic.advance_chain(chain.chain_id, sd)

        assert done.status == "complete"
        assert file_req_env["calls"] == [LEAF_C, root]


# ── Route surface ─────────────────────────────────────────────────────────────


@pytest.fixture
def flask_env(gallery_env, tmp_path):
    from flask import Flask
    from evolve_admin.web.gallery_routes import register_gallery_routes

    app = Flask(__name__)
    app.config["TESTING"] = True
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"members": ["bot-a"]}))
    with patch("evolve_admin.applications.oauth_orchestrator.start_sweeper"):
        register_gallery_routes(app, network_path, gallery_env)
    return {"client": app.test_client(), "shared_dir": gallery_env}


class TestRoutes:
    def test_single_app_install_unchanged(self, flask_env):
        """Regression: a package with no app_dependencies takes the exact
        pre-chain path — plain job result, HTTP 200, no chain artifacts."""
        client = flask_env["client"]
        sd = flask_env["shared_dir"]

        with patch(
            "evolve_admin.applications.oauth_orchestrator."
            "evaluate_install_prerequisites",
            return_value={"satisfied": True, "missing": []},
        ), patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            resp = client.post(
                f"/api/gallery/{LEAF_C}/install", json={"bot_ids": ["bot-a"]},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert "errors" not in data
        assert len(data["jobs"]) == 1
        assert "job_id" in data["jobs"][0]
        assert "chain" not in data["jobs"][0]
        assert not (sd / "forge" / "chains").exists()

    def test_bundle_projection_gates_on_suite_total(self, flask_env):
        """An unconfirmed bundle install 412s with the CHAIN total, not just
        the root's own cost — confirming the root alone would under-state
        the suite by the dependency count."""
        client = flask_env["client"]

        resp = client.post(
            f"/api/gallery/{ROOT}/install", json={"bot_ids": ["bot-a"]},
        )
        assert resp.status_code == 412
        data = resp.get_json()
        assert data["requires_confirmation"] is True
        row = data["projections"][0]
        assert row["chain_link_count"] == 4
        assert row["chain_total_mid_usd"] > row["mid_usd"]

    def test_bundle_install_creates_chain(self, flask_env):
        from evolve_admin.applications import install_chain as ic

        client = flask_env["client"]
        sd = flask_env["shared_dir"]

        with patch(
            "evolve_admin.applications.install_chain.advance_chain_async",
        ) as mock_adv:
            resp = client.post(
                f"/api/gallery/{ROOT}/install",
                json={"bot_ids": ["bot-a"], "confirmed": True},
            )

        assert resp.status_code == 202
        data = resp.get_json()
        assert "errors" not in data
        entry = data["jobs"][0]
        assert entry["bot_id"] == "bot-a"
        chain_info = entry["chain"]
        assert [lk["pkg_id"] for lk in chain_info["links"]] == TOPO_ORDER
        assert chain_info["links_total"] == 4
        mock_adv.assert_called_once()

        stored = ic.load_chain(chain_info["chain_id"], sd)
        assert stored is not None
        assert stored.root_pkg_id == ROOT
        assert stored.operator_confirmed is True

    def test_duplicate_bundle_install_resumes_existing(self, flask_env):
        """Adversarial: double Install click must not mint a second chain."""
        from evolve_admin.applications import install_chain as ic

        client = flask_env["client"]
        sd = flask_env["shared_dir"]

        with patch(
            "evolve_admin.applications.install_chain.advance_chain_async",
        ) as mock_adv:
            first = client.post(
                f"/api/gallery/{ROOT}/install",
                json={"bot_ids": ["bot-a"], "confirmed": True},
            ).get_json()
            second = client.post(
                f"/api/gallery/{ROOT}/install",
                json={"bot_ids": ["bot-a"], "confirmed": True},
            ).get_json()

        c1 = first["jobs"][0]["chain"]["chain_id"]
        assert second["jobs"][0].get("already_active") is True
        assert second["jobs"][0]["chain"]["chain_id"] == c1
        assert len(ic.list_chains(sd, bot_id="bot-a")) == 1
        # Second call resumed (retry_failed) rather than created
        assert mock_adv.call_count == 2
        assert mock_adv.call_args_list[1].kwargs.get("retry_failed") is True

    def test_force_installs_root_only_no_chain(self, flask_env):
        client = flask_env["client"]
        sd = flask_env["shared_dir"]

        with patch(
            "evolve_admin.applications.oauth_orchestrator."
            "evaluate_install_prerequisites",
            return_value={"satisfied": True, "missing": []},
        ), patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            resp = client.post(
                f"/api/gallery/{ROOT}/install",
                json={"bot_ids": ["bot-a"], "force": True},
            )

        assert resp.status_code == 200
        assert "job_id" in resp.get_json()["jobs"][0]
        assert not (sd / "forge" / "chains").exists()

    def test_cycle_returns_400(self, flask_env):
        from evolve_admin.applications.gallery import import_package

        sd = flask_env["shared_dir"]
        ok, _ = import_package(_pkg("p-eeee0002", "cyc-a", ["p-ffff0002"]), sd)
        assert ok
        ok, _ = import_package(_pkg("p-ffff0002", "cyc-b", ["p-eeee0002"]), sd)
        assert ok

        resp = flask_env["client"].post(
            "/api/gallery/p-eeee0002/install", json={"bot_ids": ["bot-a"]},
        )
        assert resp.status_code == 400
        assert "cycle" in resp.get_json()["error"]

    def test_chain_status_and_resume_endpoints(self, flask_env):
        from evolve_admin.applications import install_chain as ic

        client = flask_env["client"]
        sd = flask_env["shared_dir"]
        chain = ic.create_chain(ROOT, "bot-a", sd)

        resp = client.get(f"/api/gallery/chains/{chain.chain_id}")
        assert resp.status_code == 200
        assert resp.get_json()["chain_id"] == chain.chain_id

        resp = client.get("/api/gallery/chains?bot=bot-a")
        assert [c["chain_id"] for c in resp.get_json()["chains"]] == [chain.chain_id]

        with patch(
            "evolve_admin.applications.install_chain.advance_chain_async",
        ) as mock_adv:
            resp = client.post(f"/api/gallery/chains/{chain.chain_id}/resume")
        assert resp.status_code == 202
        assert resp.get_json()["ok"] is True
        assert mock_adv.call_args.kwargs.get("retry_failed") is True

        assert client.get("/api/gallery/chains/c-00000000").status_code == 404

    def test_resume_of_complete_chain_409(self, flask_env):
        from evolve_admin.applications import install_chain as ic

        client = flask_env["client"]
        sd = flask_env["shared_dir"]
        chain = ic.create_chain(ROOT, "bot-a", sd)
        chain.status = "complete"
        ic.save_chain(chain, sd)

        resp = client.post(f"/api/gallery/chains/{chain.chain_id}/resume")
        assert resp.status_code == 409
