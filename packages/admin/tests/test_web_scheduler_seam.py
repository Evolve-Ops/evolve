"""Phase 4.3 C Track S Step S2b — web layer on the Scheduler seam.

Covers the launchctl call sites migrated out of the admin web layer
(routes_admin, routes_maintenance, routes_oc, routes_trust,
admin_bot_routes) onto ``get_scheduler()`` / ``get_launchd_scheduler()``
/ module-level probe adapters.

Every test injects a fake runner — NO test in this file may ever spawn
a real ``launchctl``. ``kickstart -k`` and ``bootout`` are live-traffic
destructive (they restart/stop running bot gateways on a pod host).

The assertions pin two things per route:
  1. argv parity — the seam must issue the exact argv the pre-seam
     subprocess call did (including sudo-vs-not; the maintenance/trust
     probes are deliberately sudo-less).
  2. response-shape parity — the SPA parses these JSON bodies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).resolve().parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.runtime import (  # noqa: E402
    FakeScheduler,
    LaunchdScheduler,
    set_scheduler,
)
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402


class FakeRunner:
    """Records every argv; replies per-launchctl-verb (mirrors
    test_scheduler_seam.FakeRunner)."""

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    @staticmethod
    def _key(argv: list[str]) -> str:
        args = list(argv)
        if args and args[0] == "sudo":
            args = args[1:]
        if not args:
            return ""
        if args[0].endswith("launchctl"):
            return args[1] if len(args) > 1 else "launchctl"
        return args[0].rsplit("/", 1)[-1]

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self.responses.get(self._key(argv), (0, "", ""))


@pytest.fixture(autouse=True)
def _reset_scheduler_singleton():
    """Never leak an injected scheduler into other tests — and never let
    these tests fall through to the real launchd-backed default."""
    set_scheduler(None)
    yield
    set_scheduler(None)


def _write_network(tmp_path: Path, bots: dict) -> Path:
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "members": list(bots.keys()),
        "bots": bots,
        "sharedDir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir(exist_ok=True)
    return network_path


# ── routes_admin: POST /api/admin/gateway/<bot>/stop ─────────────────────────


@pytest.fixture
def gateway_stop_client(tmp_path, monkeypatch):
    import evolve_admin.web.server as server_mod
    from evolve_admin.web.routes_admin import register_admin_routes

    audited: list = []
    monkeypatch.setattr(
        server_mod, "_audit_log_entry",
        lambda action, bot, detail=None: audited.append((action, bot, detail)),
    )

    network_path = _write_network(tmp_path, {"team-bot-a": {"user": "no-such-user-xyz"}})
    app = Flask(__name__)
    register_admin_routes(app, network_path)
    return {"app": app, "audited": audited}


def test_gateway_stop_issues_sudo_bootout_without_removing_plist(
    gateway_stop_client, tmp_path,
):
    runner = FakeRunner()
    set_scheduler(LaunchdScheduler(runner=runner, plist_dir=tmp_path))

    c = gateway_stop_client["app"].test_client()
    resp = c.post("/api/admin/gateway/team-bot-a/stop", json={"confirm": True})

    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True, "bot": "team-bot-a",
        "service": "ai.openclaw.team-bot-a-gateway", "domain": "system",
    }
    # Exact pre-seam argv: sudo'd bare bootout — and ONLY bootout. No
    # rm: the plist must survive so /restart can bring the gateway back.
    assert runner.calls == [
        ["sudo", "/bin/launchctl", "bootout", "system/ai.openclaw.team-bot-a-gateway"],
    ]
    assert gateway_stop_client["audited"] == [
        ("gateway.stop", "team-bot-a", {"service": "ai.openclaw.team-bot-a-gateway", "domain": "system"}),
    ]


def test_gateway_stop_failure_collects_per_domain_errors(
    gateway_stop_client, tmp_path,
):
    runner = FakeRunner({"bootout": (3, "", "Boot-out failed: 3: No such process")})
    set_scheduler(LaunchdScheduler(runner=runner, plist_dir=tmp_path))

    c = gateway_stop_client["app"].test_client()
    resp = c.post("/api/admin/gateway/team-bot-a/stop", json={"confirm": True})

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert body["service"] == "ai.openclaw.team-bot-a-gateway"
    assert body["errors"] == ["system: Boot-out failed: 3: No such process"]


# ── admin_bot_routes: POST /api/admin/infra/<daemon_id>/restart ───────────────


@pytest.fixture
def infra_restart_client(tmp_path, monkeypatch):
    import os

    import evolve_admin.web.admin_bot_routes as abr

    # Bypass peer-auth (same pattern as test_admin_coherence_check_route).
    runner_uid = os.getuid()
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids", lambda _names: {runner_uid},
    )

    # The route stats /Library/LaunchDaemons/<label>.plist for its 404
    # guard; fake it so the test never depends on the host's real plists.
    class _AlwaysExistsPath:
        def __init__(self, s: str) -> None:
            self._s = str(s)

        def exists(self) -> bool:
            return True

        def __str__(self) -> str:
            return self._s

    monkeypatch.setattr(abr, "Path", _AlwaysExistsPath)

    network_path = _write_network(tmp_path, {})
    app = Flask(__name__)

    @app.before_request
    def _inject_peer():
        from flask import request
        request.environ["REMOTE_TRANSPORT"] = "unix-socket"
        request.environ["REMOTE_PEER_UID"] = str(runner_uid)

    abr.register_admin_bot_routes(app, network_path)
    return app.test_client()


def test_infra_restart_issues_sudo_kickstart(infra_restart_client, tmp_path):
    runner = FakeRunner()
    set_scheduler(LaunchdScheduler(runner=runner, plist_dir=tmp_path))

    resp = infra_restart_client.post(
        "/api/admin/infra/evolve.heal/restart", json={"reason": "test"},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "daemon_id": "evolve.heal", "reason": "test"}
    assert runner.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k", "system/ai.evolve.evolve.heal"],
    ]


def test_infra_restart_failure_preserves_rc_and_stderr_fields(
    infra_restart_client, tmp_path,
):
    runner = FakeRunner({"kickstart": (113, "", "Could not find service\n")})
    set_scheduler(LaunchdScheduler(runner=runner, plist_dir=tmp_path))

    resp = infra_restart_client.post("/api/admin/infra/evolve.heal/restart", json={})

    assert resp.status_code == 500
    # The pre-seam response embedded the launchctl exit code and stderr
    # as separate fields — pinned here because the SPA/evo tool surfaces
    # them verbatim.
    assert resp.get_json() == {
        "ok": False,
        "error": "launchctl kickstart returned 113",
        "stderr": "Could not find service",
    }


def test_infra_restart_rejects_unknown_daemon(infra_restart_client, tmp_path):
    runner = FakeRunner()
    set_scheduler(LaunchdScheduler(runner=runner, plist_dir=tmp_path))

    resp = infra_restart_client.post("/api/admin/infra/not.allowed/restart", json={})

    assert resp.status_code == 400
    assert runner.calls == []  # allowlist gate fires before any launchctl


# ── routes_oc: POST /api/maintenance/backup-now ──────────────────────────────


class _StubScheduler:
    """Protocol-shaped stub — backup-now only needs restart()."""

    def __init__(self, result: tuple[bool, str] = (True, "")) -> None:
        self.result = result
        self.restarted: list[str] = []

    def restart(self, label: str):
        self.restarted.append(label)
        return self.result


@pytest.fixture
def backup_now_client(tmp_path):
    import evolve_admin.web.server  # noqa: F401 — routes_oc resolves shims through it
    from evolve_admin.web.routes_oc import register_oc_routes

    network_path = _write_network(
        tmp_path, {"team-bot-a": {"backupRepoUrl": "git@example.invalid:backup.git"}},
    )
    app = Flask(__name__)
    register_oc_routes(app, network_path)
    return app.test_client()


def test_backup_now_kicks_daemon_via_restart_verb(backup_now_client):
    stub = _StubScheduler((True, ""))
    set_scheduler(stub)

    resp = backup_now_client.post("/api/maintenance/backup-now", json={})

    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True, "results": [{"bot_id": "team-bot-a", "status": "kicked"}],
    }
    # Label WITHOUT the domain prefix — restart() owns "system/".
    assert stub.restarted == ["ai.evolve.team-bot-a.backup"]


def test_backup_now_classifies_missing_daemon(backup_now_client):
    stub = _StubScheduler((False, "Could not find service “ai.evolve.team-bot-a.backup”"))
    set_scheduler(stub)

    resp = backup_now_client.post("/api/maintenance/backup-now", json={"botId": "team-bot-a"})

    body = resp.get_json()
    assert body["ok"] is True
    assert body["results"][0]["bot_id"] == "team-bot-a"
    assert body["results"][0]["status"] == "no-daemon"


def test_backup_now_other_failures_report_failed_with_error(backup_now_client):
    stub = _StubScheduler((False, "sudo: a password is required"))
    set_scheduler(stub)

    resp = backup_now_client.post("/api/maintenance/backup-now", json={"botId": "team-bot-a"})

    body = resp.get_json()
    assert body["results"][0] == {
        "bot_id": "team-bot-a", "status": "failed",
        "error": "sudo: a password is required",
    }


# ── routes_trust: GET /api/defer/health ──────────────────────────────────────


@pytest.fixture
def defer_health_client(tmp_path, monkeypatch):
    import evolve_admin.web.routes_trust as rt

    network_path = _write_network(tmp_path, {})
    app = Flask(__name__)
    rt._register_trust_routes(app, network_path)
    return {"app": app, "module": rt}


def _patch_trust_probe(monkeypatch, module, runner, tmp_path):
    monkeypatch.setattr(
        module, "_probe_scheduler",
        LaunchdScheduler(use_sudo=False, runner=runner, plist_dir=tmp_path, timeout=5.0),
    )


def test_defer_health_probe_is_sudoless_launchctl_list(
    defer_health_client, monkeypatch, tmp_path,
):
    runner = FakeRunner({"list": (0, '{\n\t"PID" = 42;\n};\n', "")})
    _patch_trust_probe(monkeypatch, defer_health_client["module"], runner, tmp_path)

    resp = defer_health_client["app"].test_client().get("/api/defer/health")

    assert resp.status_code == 200
    assert resp.get_json()["runner_loaded"] is True
    # Pre-seam argv had NO sudo — the probe must not grow one (there is
    # no sudoers grant guarantee for ad-hoc probes; see CLAUDE.md).
    assert runner.calls == [
        ["/bin/launchctl", "list", "ai.openclaw.evolve.defer-runner"],
    ]


def test_defer_health_runner_not_loaded_on_nonzero_rc(
    defer_health_client, monkeypatch, tmp_path,
):
    runner = FakeRunner({"list": (113, "", "Could not find service")})
    _patch_trust_probe(monkeypatch, defer_health_client["module"], runner, tmp_path)

    resp = defer_health_client["app"].test_client().get("/api/defer/health")

    assert resp.status_code == 200
    assert resp.get_json()["runner_loaded"] is False


def test_defer_health_macos_derives_unsudo_launchd_status_probe(
    defer_health_client, monkeypatch, tmp_path,
):
    """macOS (no test-injected probe): _get_probe_scheduler guarded-derives an
    UNSUDO'd LaunchdScheduler from the injected launchd singleton, so the
    status() probe issues the byte-identical no-sudo argv."""
    rt = defer_health_client["module"]
    monkeypatch.setattr(rt, "_probe_scheduler", None)  # force the derive path
    set_profile(MACOS)

    runner = FakeRunner({"list": (0, '{\n\t"PID" = 7;\n};\n', "")})
    set_scheduler(LaunchdScheduler(runner=runner, plist_dir=tmp_path))

    resp = defer_health_client["app"].test_client().get("/api/defer/health")

    assert resp.status_code == 200
    assert resp.get_json()["runner_loaded"] is True
    # status() on macOS → the historical no-sudo `launchctl list <label>`.
    assert runner.calls == [
        ["/bin/launchctl", "list", "ai.openclaw.evolve.defer-runner"],
    ]


def test_defer_health_linux_status_truthful_no_launchctl(
    defer_health_client, monkeypatch,
):
    """Linux (non-launchd adapter): status() routes through the injected
    portable adapter — truthful runner_loaded, NO launchctl, no 500. A
    FakeScheduler stands in for SystemdScheduler (same status() shape; raw()
    would raise, proving no launchd-verbatim path is taken)."""
    rt = defer_health_client["module"]
    monkeypatch.setattr(rt, "_probe_scheduler", None)
    set_profile(LINUX)

    sched = FakeScheduler()
    set_scheduler(sched)

    # Daemon not registered → managed=False → runner_loaded=False, truthfully.
    resp = defer_health_client["app"].test_client().get("/api/defer/health")
    assert resp.status_code == 200
    assert resp.get_json()["runner_loaded"] is False

    # Register the unit → status()["managed"] is True → runner_loaded=True.
    from runtime.scheduler import JobSpec
    monkeypatch.setattr(rt, "_probe_scheduler", None)
    sched.seed_job(
        JobSpec(
            label="ai.openclaw.evolve.defer-runner",
            program_args=["/bin/true"],
        ),
        running=True,
    )
    resp2 = defer_health_client["app"].test_client().get("/api/defer/health")
    assert resp2.status_code == 200
    assert resp2.get_json()["runner_loaded"] is True


# ── routes_maintenance: GET /api/launchd/jobs ────────────────────────────────


def test_launchd_jobs_probes_are_sudoless_and_single_shot(
    tmp_path, monkeypatch,
):
    import evolve_admin.web.server  # noqa: F401 — routes_maintenance imports from it
    import evolve_admin.web.routes_maintenance as rm

    runner = FakeRunner({
        "list": (0, "PID\tStatus\tLabel\n123\t0\tai.evolve.test-job\n", ""),
        "print-disabled": (0, '\t"ai.evolve.test-job" => disabled\n', ""),
    })
    monkeypatch.setattr(
        rm, "_probe_scheduler",
        LaunchdScheduler(use_sudo=False, runner=runner, plist_dir=tmp_path, timeout=10.0),
    )

    network_path = _write_network(tmp_path, {})
    app = Flask(__name__)
    rm._register_maintenance_routes(app, network_path)

    resp = app.test_client().get("/api/launchd/jobs")

    assert resp.status_code == 200
    assert "jobs" in resp.get_json()
    # Exactly the two historical probes, in order, both sudo-less:
    # one `launchctl list` (the full table — PID + last-exit columns),
    # one `launchctl print-disabled system`.
    assert runner.calls == [
        ["/bin/launchctl", "list"],
        ["/bin/launchctl", "print-disabled", "system"],
    ]


def test_launchd_jobs_gates_out_on_linux(tmp_path, monkeypatch):
    """Linux: /api/launchd/jobs platform-gates OUT — returns an empty job list
    (the Infra-Jobs page shows nothing-to-show) with NO launchctl probe and no
    500. The route is launchd-only (plist glob + raw() probes have no systemd
    analogue yet)."""
    import evolve_admin.web.server  # noqa: F401
    import evolve_admin.web.routes_maintenance as rm

    set_profile(LINUX)

    # A probe that would fail the test if ever reached — the Linux gate must
    # short-circuit before any launchctl call.
    def boom(argv):
        raise AssertionError(f"launchctl probe must not run on Linux; argv={argv}")

    monkeypatch.setattr(rm, "_probe_scheduler", None)
    set_scheduler(LaunchdScheduler(runner=boom, plist_dir=tmp_path))

    network_path = _write_network(tmp_path, {})
    app = Flask(__name__)
    rm._register_maintenance_routes(app, network_path)

    resp = app.test_client().get("/api/launchd/jobs")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"jobs": [], "platform": "linux"}
