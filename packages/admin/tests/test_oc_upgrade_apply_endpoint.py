"""POST /api/oc/upgrade/apply — the admin UI's "Run upgrade now" endpoint.

Spec: docs/spec-oc-upgrade-from-ui-2026-07-28.md §6 + verification items
**A1–A6** and **U1**.

The endpoint is the trigger the safe-upgrade preflight never had. Its whole
job is to refuse loudly in every state that isn't "a green, current report on
a pod that isn't mid-deploy" — and, when it does run, to hand the privileged
half nothing but a report id.

What each test pins:

* **A1** no report id → 400.
* **A2** a red report → 409, no privileged invocation.
* **A3** a stale report → 409, no privileged invocation.
* **A4** canary release mode → 409 carrying the pinned "release promote"
  message (a direct upgrade races the gated pipeline).
* **A5** the C1 deploy lock held → 409.
* **A6** happy path → 202 + jobId, and the job log carries the phase sequence
  in CLI order.
* **U1** the apply button renders only when the report is ``ok && !stale``.

Plus the two invariants that make the design safe rather than merely correct:
the endpoint passes ONLY the report id across the privilege boundary, and it
never hands the SPA a `sudo …` hint (the in-app Terminal runs as the
passwordless `evolve` user and could not answer the prompt).
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import evolve_admin.web.server as web_server  # noqa: E402
from evolve_admin import deploy_resilience as dres  # noqa: E402
from evolve_admin import oc_upgrade_apply as apply_mod  # noqa: E402
from evolve_admin import safe_upgrade as su  # noqa: E402
from evolve_admin import upstream_version as uv  # noqa: E402
from evolve_admin.web import routes_maintenance as rm  # noqa: E402

_MAINTENANCE_JS = (
    _ADMIN_DIR / "evolve_admin" / "web" / "static" / "js" / "pages" / "maintenance.js"
)

INSTALLED = "2026.7.0"
TARGET = "2026.7.1"
REPORT_ID = "20260728T120000Z-abcd1234"


# A `sudo <command>` the operator is being told to run. Naming the sudoers
# FILE ("/etc/sudoers.d/evolve") or saying an action needs "no sudo" is fine —
# what must never reach the SPA is an instruction to type `sudo …`, because the
# in-app Terminal runs as the passwordless `evolve` service user and can never
# answer the prompt.
_SUDO_HINT_RE = re.compile(
    r"sudo\s+(?:-\w+\s+)*"
    r"(?:/\S+|evolve-admin|openclaw|npm|launchctl|systemctl|rm|cp|mv|chmod|chown)\b"
)

# `//` line comments — stripped before asserting on what the SPA *renders*, so
# a comment explaining the removed hint doesn't read as the hint itself.
_JS_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.M)


def _report(ok: bool = True, installed: str = INSTALLED, target: str = TARGET) -> dict:
    return {
        "report_id": REPORT_ID,
        "checked_at": "2026-07-28T12:00:00Z",
        "ok": ok,
        "summary": "All gates passed — safe to upgrade." if ok else "1 blocker",
        "current": {"installed_version": installed},
        "candidate": {"target_spec": "latest", "resolved_version": target},
        "gates": {"config_references": {"ok": ok, "details": {"bots": []}}},
        "requirements": [],
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A test app with the maintenance routes registered and every privileged
    or network-touching leaf stubbed. ``calls`` records what crossed the
    privilege boundary."""
    shared = tmp_path / "shared"
    (shared / su.REPORTS_SUBDIR).mkdir(parents=True)
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "members": ["team_bot_a"],
    }))

    calls: dict = {"privileged": [], "locks": 0, "released": 0}

    monkeypatch.setattr(uv, "installed_package_version", lambda *a, **k: INSTALLED)
    monkeypatch.setattr(rm, "_npm_latest_version", lambda *a, **k: (TARGET, None))
    monkeypatch.setattr(su, "inflight_report_id", lambda: None)

    class _Lock:
        pass

    def _acquire(_shared_dir=None):
        calls["locks"] += 1
        return _Lock()

    monkeypatch.setattr(dres, "try_acquire_deploy_lock", _acquire)
    monkeypatch.setattr(dres, "release_deploy_lock",
                        lambda h: calls.__setitem__("released", calls["released"] + 1))

    def _fake_stream(report_id, on_event, **kw):
        calls["privileged"].append(report_id)
        for i, (phase, label) in enumerate(apply_mod.PHASE_ORDER, 1):
            on_event({
                "phase": phase, "label": label, "step": i,
                "total": apply_mod.TOTAL_PHASES, "level": "info",
                "message": f"[{phase}] ok",
            })
        return True, ""

    monkeypatch.setattr(apply_mod, "stream_privileged_upgrade", _fake_stream)

    # Each test gets a clean job registry — the single-active-job guard is
    # process-global state.
    web_server._jobs.clear()
    web_server._active_job_id.clear()

    app = Flask(__name__)
    rm._register_maintenance_routes(app, network_path)
    return {"client": app.test_client(), "shared": shared, "calls": calls,
            "network_path": network_path}


def _write_report(shared: Path, report: dict) -> None:
    (shared / su.REPORTS_SUBDIR / f"{report['report_id']}.json").write_text(
        json.dumps(report))


def _wait_for_job(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = web_server._jobs.get(job_id, {})
        if job.get("status") in ("complete", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished: {web_server._jobs.get(job_id)}")


# ── A1: no report id ─────────────────────────────────────────────────────────


def test_a1_missing_report_id_is_400(client):
    resp = client["client"].post("/api/oc/upgrade/apply", json={"confirm": True})
    assert resp.status_code == 400
    assert "reportId" in resp.get_json()["error"]
    assert client["calls"]["privileged"] == []


def test_a1_missing_confirm_is_400(client):
    """The endpoint is the second half of a two-step flow; an unconfirmed POST
    is a client bug, not an upgrade."""
    resp = client["client"].post("/api/oc/upgrade/apply", json={"reportId": REPORT_ID})
    assert resp.status_code == 400
    assert client["calls"]["privileged"] == []


# ── A2: red report ───────────────────────────────────────────────────────────


def test_a2_red_report_is_409_and_never_invokes_npm(client):
    _write_report(client["shared"], _report(ok=False))
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 409
    assert "did not pass its gates" in resp.get_json()["error"]
    assert client["calls"]["privileged"] == []


def test_a2_unknown_report_is_409(client):
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 409
    assert "No safety report" in resp.get_json()["error"]
    assert client["calls"]["privileged"] == []


def test_no_force_escape_hatch(client):
    """Forcing past a red preflight stays CLI-only (spec §9) — a `force` in the
    body must not change the refusal."""
    _write_report(client["shared"], _report(ok=False))
    resp = client["client"].post(
        "/api/oc/upgrade/apply",
        json={"reportId": REPORT_ID, "confirm": True, "force": True},
    )
    assert resp.status_code == 409
    assert client["calls"]["privileged"] == []


# ── A3: stale report ─────────────────────────────────────────────────────────


def test_a3_stale_installed_version_is_409(client, monkeypatch):
    """Someone upgraded out of band since the check — the gates no longer
    describe this pod."""
    _write_report(client["shared"], _report())
    monkeypatch.setattr(uv, "installed_package_version", lambda *a, **k: "2026.7.0.5")
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 409
    assert "out of date" in resp.get_json()["error"]
    assert client["calls"]["privileged"] == []


def test_a3_stale_npm_latest_is_409(client, monkeypatch):
    """npm published a newer version than the one the gates ran against."""
    _write_report(client["shared"], _report())
    monkeypatch.setattr(rm, "_npm_latest_version", lambda *a, **k: ("2026.7.2", None))
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 409
    assert "2026.7.2" in resp.get_json()["error"]
    assert client["calls"]["privileged"] == []


def test_a3_unreachable_registry_fails_closed(client, monkeypatch):
    """We cannot PROVE the report is current, so it is treated as stale."""
    _write_report(client["shared"], _report())
    monkeypatch.setattr(rm, "_npm_latest_version",
                        lambda *a, **k: (None, "getaddrinfo ENOTFOUND"))
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 409
    assert client["calls"]["privileged"] == []


def test_a3_inflight_check_is_409(client, monkeypatch):
    _write_report(client["shared"], _report())
    monkeypatch.setattr(su, "inflight_report_id", lambda: "20260728T130000Z-deadbeef")
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 409
    assert client["calls"]["privileged"] == []


# ── A4: canary ───────────────────────────────────────────────────────────────


def test_a4_canary_mode_is_409_with_the_pinned_message(client, tmp_path):
    """Inherited verbatim from /api/upgrade: under canary the fleet is held on
    the gated stable pointer while a candidate soaks, and a direct upgrade
    races that pipeline. The message is pinned by existing unit + route
    tests — this endpoint must not reword it."""
    net = json.loads(client["network_path"].read_text())
    net["pod"] = {"release": {"mode": "canary", "canary_bot": "team_bot_a"}}
    client["network_path"].write_text(json.dumps(net))
    _write_report(client["shared"], _report())

    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 409
    payload = resp.get_json()
    assert payload["release_mode"] == "canary"
    assert "release promote" in payload["error"]
    assert client["calls"]["privileged"] == []


def test_a4_canary_message_carries_no_sudo_hint(client):
    """The canary refusal renders verbatim in the SPA, where a `sudo …` hint is
    unusable — the in-app Terminal runs as the passwordless evolve user."""
    from evolve_admin.release_manager import canary_upgrade_block

    blk = canary_upgrade_block({"pod": {"release": {"mode": "canary"}}})
    assert blk is not None
    assert not _SUDO_HINT_RE.search(blk["error"])


# ── A5: deploy lock ──────────────────────────────────────────────────────────


def test_a5_deploy_lock_held_is_409(client, monkeypatch):
    _write_report(client["shared"], _report())
    monkeypatch.setattr(dres, "try_acquire_deploy_lock", lambda *a, **k: None)
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 409
    assert "deploy is already in progress" in resp.get_json()["error"]
    assert client["calls"]["privileged"] == []


def test_a5_another_job_running_is_409(client):
    _write_report(client["shared"], _report())
    web_server._active_job_id.append("upgrade-1")
    try:
        resp = client["client"].post("/api/oc/upgrade/apply",
                                     json={"reportId": REPORT_ID, "confirm": True})
        assert resp.status_code == 409
        assert resp.get_json()["jobId"] == "upgrade-1"
        assert client["calls"]["privileged"] == []
    finally:
        web_server._active_job_id.clear()


# ── A6: happy path ───────────────────────────────────────────────────────────


def test_a6_happy_path_starts_a_job_and_logs_the_cli_phase_sequence(client):
    _write_report(client["shared"], _report())
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    assert resp.status_code == 202
    payload = resp.get_json()
    assert payload["status"] == "started"
    assert payload["from"] == INSTALLED and payload["to"] == TARGET

    job = _wait_for_job(payload["jobId"])
    assert job["status"] == "complete", job.get("error")
    logged = " ".join(entry["msg"] for entry in job["log"])
    seen = [p for p, _ in apply_mod.PHASE_ORDER if f"[{p}] ok" in logged]
    assert seen == [p for p, _ in apply_mod.PHASE_ORDER], (
        "the job log must carry the phases in CLI order")
    assert job["progress"]["total"] == apply_mod.TOTAL_PHASES


def test_a6_only_the_report_id_crosses_the_privilege_boundary(client):
    """The injection-proofing that makes the §4.1 npm grant unnecessary: the
    web layer never names a package spec."""
    _write_report(client["shared"], _report())
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True,
                                       "target": "openclaw@npm:evil-package"})
    _wait_for_job(resp.get_json()["jobId"])
    assert client["calls"]["privileged"] == [REPORT_ID]


def test_deploy_lock_is_released_when_the_job_ends(client):
    _write_report(client["shared"], _report())
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    _wait_for_job(resp.get_json()["jobId"])
    assert client["calls"]["locks"] == 1
    assert client["calls"]["released"] == 1


def test_privileged_failure_fails_the_job_with_the_reason(client, monkeypatch):
    _write_report(client["shared"], _report())

    def _fail(report_id, on_event, **kw):
        on_event({"level": "error", "message": "npm install failed",
                  "phase": "npm_install", "step": 8, "total": 15})
        return False, apply_mod.NO_GRANT_DETAIL

    monkeypatch.setattr(apply_mod, "stream_privileged_upgrade", _fail)
    resp = client["client"].post("/api/oc/upgrade/apply",
                                 json={"reportId": REPORT_ID, "confirm": True})
    job = _wait_for_job(resp.get_json()["jobId"])
    assert job["status"] == "failed"
    assert "not authorized on this pod yet" in job["error"]
    # Spec §10 — say the grant is missing rather than failing opaquely, and
    # never with a `sudo …` hint the in-app Terminal can't satisfy.
    assert not _SUDO_HINT_RE.search(job["error"])
    assert client["calls"]["released"] == 1


# ── U1: the apply affordance is state-gated ──────────────────────────────────


def _js() -> str:
    return _MAINTENANCE_JS.read_text(encoding="utf-8")


def test_u1_apply_button_requires_ok_and_not_stale():
    src = _js()
    m = re.search(r"const canApply = ([^;]+);", src)
    assert m, "canApply gate not found in renderOcUpgradeBanner"
    gate = m.group(1)
    for token in ("sc.ok", "!sc.stale", "sc.report_id"):
        assert token in gate, f"canApply must require {token}"
    # The button only renders behind that gate.
    assert "canApply\n    ? `<div" in src or "const applyRow = canApply" in src
    assert "Run upgrade now" in src


def test_u1_recheck_demotes_to_secondary_while_apply_is_present():
    """One primary per surface (style-guide §10.4)."""
    src = _js()
    m = re.search(r"const checkBtnClass = canApply \? '([^']+)' : '([^']+)';", src)
    assert m, "check-button class is not canApply-keyed"
    assert "btn-ghost" in m.group(1)
    assert "btn-primary" in m.group(2)


def test_u1_no_sudo_cli_hint_survives_in_the_banner():
    """The whole point of the change: the copy-to-clipboard
    `sudo evolve-admin oc upgrade` dead end is gone."""
    rendered = _JS_LINE_COMMENT_RE.sub("", _js())
    assert not _SUDO_HINT_RE.search(rendered)
    assert "upgradeCmd" not in rendered
    assert "clipboard.writeText" not in rendered


def test_apply_modal_never_uses_a_native_dialog():
    """Native dialogs are suppressed in the desktop shell — the button would
    silently do nothing (style-guide §9.6 rule 5)."""
    src = _js()
    apply_section = src[src.index("async function openOcApply("):]
    assert not re.search(r"(?<![.\w])confirm\s*\(", apply_section.replace(
        "confirmOcApply(", "X("))
    assert not re.search(r"(?<![.\w])alert\s*\(", apply_section)


def test_confirm_step_shows_the_plan_before_committing():
    """Spec §7.1 — the operator learns which bots briefly lose plugin-backed
    features BEFORE the POST, mirroring where the CLI's confirm sits."""
    src = _js()
    plan = src[src.index("function _renderOcApplyPlan("):src.index("async function confirmOcApply(")]
    assert "missing_by_bot" in plan          # neutralize plan
    assert "phantom_installs" in plan        # phantom cleanup plan
    assert "Restart" in plan                 # restart list
    assert "restarts" in plan                # the "briefly offline" warning
    # And the POST lives in the SECOND step, not in the opener.
    opener = src[src.index("async function openOcApply("):src.index("function _renderOcApplyPlan(")]
    assert "/api/oc/upgrade/apply" not in opener
    assert "/api/oc/upgrade/apply" in src[src.index("async function confirmOcApply("):]


def test_new_onclick_handlers_are_window_exported():
    """Inline handlers are invisible to ESLint's no-unused-vars; the
    suppressions baseline only shrinks."""
    src = _js()
    for name in ("openOcApply", "confirmOcApply", "closeOcApply"):
        assert f"window.{name} = {name};" in src
