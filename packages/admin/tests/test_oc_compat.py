"""oc_compat + the compat-contract preflight gate + the Update card's veto.

Design: internal/design-oc-upgrade-safety-2026-09-08.md §2 row 3. The
contract itself lives in packages/plugin/src/contract/ and is exercised by
the node:test suites there; this module tests the half that DECIDES with the
contract's verdict — which versions Evolve offers, which it refuses, and what
an operator has to type to go anyway.

The invariant every test here circles: **absence is never validation.** A
missing manifest, an unreadable one, a version with no recorded run, a run
with a skip — all of them are ``untested``, and ``untested`` gets no button.
The 2026-09-07 upgrade happened because "npm has something newer" and "Evolve
works on it" were the same question to this card.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import oc_compat as cc  # noqa: E402
from evolve_admin import safe_upgrade as su  # noqa: E402
from evolve_admin import upstream_version as uv  # noqa: E402
from evolve_admin.web import routes_maintenance as rm  # noqa: E402

_MAINTENANCE_JS = (
    _ADMIN_DIR / "evolve_admin" / "web" / "static" / "js" / "pages" / "maintenance.js"
)
_INDEX_HTML = _ADMIN_DIR / "evolve_admin" / "web" / "index.html"

TESTED = "2026.9.2"
UNTESTED = "2026.9.3"


def _repo(tmp_path: Path, tested: list[str] | None) -> Path:
    """A fake repo root carrying packages/plugin/compat.json."""
    root = tmp_path / "repo"
    (root / "packages" / "plugin").mkdir(parents=True, exist_ok=True)
    if tested is not None:
        (root / "packages" / "plugin" / "compat.json").write_text(
            json.dumps({"evolveVersion": "0.1.0", "openclaw": {"tested": tested}})
        )
    return root


def _run(version: str, ok: bool, statuses: dict[str, str] | None = None) -> dict:
    statuses = statuses or {"subagent-reentry-guarded": "pass"}
    return {
        "ocVersion": version,
        "evolveVersion": "0.1.0",
        "startedAt": "2026-09-09T04:05:06.000Z",
        "durationMs": 1234,
        "ok": ok,
        "results": [
            {"id": cid, "title": cid, "incident": "internal/x.md", "status": st,
             "detail": "", "evidence": {}}
            for cid, st in statuses.items()
        ],
    }


# ── The release's claim ──────────────────────────────────────────────────────

def test_manifest_reads_the_tested_list(tmp_path):
    m = cc.load_manifest(_repo(tmp_path, [TESTED]))
    assert m.tested == [TESTED]
    assert m.read_error is None


def test_a_missing_manifest_is_an_error_not_an_empty_pass(tmp_path):
    m = cc.load_manifest(_repo(tmp_path, None))
    assert m.tested == []
    assert m.read_error == "manifest not found"


def test_a_malformed_manifest_never_reads_as_validated(tmp_path):
    root = _repo(tmp_path, [])
    (root / "packages" / "plugin" / "compat.json").write_text("{not json")
    m = cc.load_manifest(root)
    assert m.tested == []
    assert "unreadable" in (m.read_error or "")
    # And the state it produces is untested, not tested.
    st = cc.validation_state(TESTED, shared_dir=tmp_path / "shared", repo_root=root)
    assert st.state == cc.STATE_UNTESTED


def test_the_shipped_manifest_parses_and_declares_a_tested_list():
    """The real packages/plugin/compat.json, as committed."""
    m = cc.load_manifest()
    assert m.read_error is None, m.read_error
    assert isinstance(m.tested, list)


# ── Recorded runs ────────────────────────────────────────────────────────────

def test_record_run_round_trips(tmp_path):
    shared = tmp_path / "shared"
    run = _run(UNTESTED, False, {"a": "fail", "b": "pass"})
    dest = cc.record_run(run, shared_dir=shared)
    assert dest is not None
    assert cc.load_run(UNTESTED, shared_dir=shared) == run


def test_an_unattributable_run_is_not_recorded(tmp_path):
    run = _run(UNTESTED, True)
    run["ocVersion"] = None
    assert cc.record_run(run, shared_dir=tmp_path / "shared") is None


def test_a_version_with_a_path_separator_cannot_escape_the_runs_dir(tmp_path):
    shared = tmp_path / "shared"
    cc.record_run(_run("../../etc/evil", True), shared_dir=shared)
    written = list(cc.runs_dir(shared).glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == cc.runs_dir(shared)


def test_failing_checks_lists_skips_as_well_as_failures():
    run = _run(UNTESTED, False, {"a": "pass", "b": "fail", "c": "skip"})
    assert cc.failing_checks(run) == ["b", "c"]
    assert cc.failing_checks(None) == []


# ── The verdict ──────────────────────────────────────────────────────────────

def test_a_manifest_listed_version_is_tested(tmp_path):
    st = cc.validation_state(TESTED, shared_dir=tmp_path / "s", repo_root=_repo(tmp_path, [TESTED]))
    assert st.state == cc.STATE_TESTED
    assert st.failing == []


def test_a_version_with_no_evidence_is_untested(tmp_path):
    st = cc.validation_state(UNTESTED, shared_dir=tmp_path / "s", repo_root=_repo(tmp_path, [TESTED]))
    assert st.state == cc.STATE_UNTESTED
    assert st.failing == []
    assert st.tested == [TESTED]


def test_a_passing_recorded_run_validates_a_version_the_manifest_has_not_caught_up_to(tmp_path):
    shared = tmp_path / "s"
    cc.record_run(_run(UNTESTED, True, {"a": "pass"}), shared_dir=shared)
    st = cc.validation_state(UNTESTED, shared_dir=shared, repo_root=_repo(tmp_path, []))
    assert st.state == cc.STATE_TESTED


def test_a_failing_run_outranks_the_manifest_claim(tmp_path):
    """A recorded failure is newer evidence than the release's claim."""
    shared = tmp_path / "s"
    cc.record_run(_run(TESTED, False, {"subagent-reentry-guarded": "fail"}), shared_dir=shared)
    st = cc.validation_state(TESTED, shared_dir=shared, repo_root=_repo(tmp_path, [TESTED]))
    assert st.state == cc.STATE_FAILED
    assert st.failing == ["subagent-reentry-guarded"]
    assert st.checked_at == "2026-09-09T04:05:06.000Z"


def test_no_version_at_all_is_untested(tmp_path):
    st = cc.validation_state(None, shared_dir=tmp_path / "s", repo_root=_repo(tmp_path, [TESTED]))
    assert st.state == cc.STATE_UNTESTED
    assert st.overridden is False


# ── The operator override ────────────────────────────────────────────────────

def test_the_confirmation_phrase_names_the_failing_checks():
    phrase = cc.override_confirmation_phrase(UNTESTED, ["a-check", "b-check"])
    assert UNTESTED in phrase
    assert "a-check" in phrase and "b-check" in phrase


def test_the_confirmation_phrase_for_an_unrun_version_says_so():
    assert cc.override_confirmation_phrase(UNTESTED, []) == f"upgrade {UNTESTED} without validation"


def test_a_wrong_confirmation_writes_nothing(tmp_path):
    shared = tmp_path / "s"
    repo = _repo(tmp_path, [])
    ok, err = cc.record_override(UNTESTED, confirmation="yes", shared_dir=shared, repo_root=repo)
    assert ok is False
    assert "must read exactly" in (err or "")
    assert cc.overrides(shared) == []
    assert cc.is_overridden(UNTESTED, shared_dir=shared) is False


def test_a_correct_confirmation_records_the_named_checks(tmp_path):
    shared = tmp_path / "s"
    repo = _repo(tmp_path, [])
    cc.record_run(_run(UNTESTED, False, {"a-check": "fail", "b-check": "skip"}), shared_dir=shared)
    phrase = cc.override_confirmation_phrase(UNTESTED, ["a-check", "b-check"])
    ok, err = cc.record_override(UNTESTED, confirmation=phrase, shared_dir=shared, repo_root=repo)
    assert (ok, err) == (True, None)
    entries = cc.overrides(shared)
    assert len(entries) == 1
    assert entries[0]["version"] == UNTESTED
    assert entries[0]["failing"] == ["a-check", "b-check"]
    assert entries[0]["state_at_override"] == cc.STATE_FAILED
    assert entries[0]["at"].endswith("Z")
    assert cc.is_overridden(UNTESTED, shared_dir=shared) is True


def test_an_override_goes_stale_when_a_fresh_run_changes_the_failing_set(tmp_path):
    """The phrase is re-derived from the CURRENT failing set, so yesterday's
    sentence no longer authorizes today's different breakage."""
    shared = tmp_path / "s"
    repo = _repo(tmp_path, [])
    cc.record_run(_run(UNTESTED, False, {"a-check": "fail"}), shared_dir=shared)
    old_phrase = cc.override_confirmation_phrase(UNTESTED, ["a-check"])
    cc.record_run(_run(UNTESTED, False, {"c-check": "fail"}), shared_dir=shared)
    ok, err = cc.record_override(UNTESTED, confirmation=old_phrase, shared_dir=shared, repo_root=repo)
    assert ok is False
    assert "c-check" in (err or "")


def test_overriding_a_validated_version_is_refused_as_unnecessary(tmp_path):
    ok, err = cc.record_override(
        TESTED, confirmation="anything", shared_dir=tmp_path / "s",
        repo_root=_repo(tmp_path, [TESTED]),
    )
    assert ok is False
    assert "already validated" in (err or "")


def test_a_corrupt_override_line_does_not_hide_a_good_one(tmp_path):
    shared = tmp_path / "s"
    cc.compat_dir(shared).mkdir(parents=True)
    cc.overrides_path(shared).write_text('{not json\n{"version": "2026.9.3"}\n')
    assert cc.is_overridden(UNTESTED, shared_dir=shared) is True


# ── The preflight gate ───────────────────────────────────────────────────────

def test_gate_passes_for_a_tested_version(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_repo_root", lambda: _repo(tmp_path, [TESTED]))
    result, reqs = su.gate_compat_contract(TESTED, shared_dir=tmp_path / "s")
    assert result.ok is True
    assert reqs == []
    assert result.details["state"] == cc.STATE_TESTED


def test_gate_emits_one_blocker_per_failing_contract_check(tmp_path, monkeypatch):
    """'3 blockers' has to read as WHICH assumptions break — that is the
    whole point of feeding the contract into the preflight table."""
    shared = tmp_path / "s"
    monkeypatch.setattr(cc, "_repo_root", lambda: _repo(tmp_path, []))
    cc.record_run(
        _run(UNTESTED, False, {
            "subagent-reentry-guarded": "fail",
            "plugin-install-flags-accepted": "fail",
            "hook-ctx-fields-present": "pass",
        }),
        shared_dir=shared,
    )
    result, reqs = su.gate_compat_contract(UNTESTED, shared_dir=shared)
    assert result.ok is False
    ids = [r.id for r in reqs]
    assert ids == [
        "compat-contract-subagent-reentry-guarded",
        "compat-contract-plugin-install-flags-accepted",
    ]
    assert all(r.blocking for r in reqs)
    assert all(r.source_gate == "compat_contract" for r in reqs)
    assert "subagent-reentry-guarded" in reqs[0].summary


def test_gate_emits_one_blocker_when_nothing_has_been_run(tmp_path, monkeypatch):
    """Nine unknowns would read as nine failures; 'it has not been run' is one
    fact."""
    monkeypatch.setattr(cc, "_repo_root", lambda: _repo(tmp_path, []))
    result, reqs = su.gate_compat_contract(UNTESTED, shared_dir=tmp_path / "s")
    assert result.ok is False
    assert [r.id for r in reqs] == ["compat-contract-unvalidated"]
    assert "Absence of a run is not a pass" in reqs[0].remediation


def test_gate_no_ops_without_a_resolved_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_repo_root", lambda: _repo(tmp_path, []))
    result, reqs = su.gate_compat_contract(None, shared_dir=tmp_path / "s")
    assert result.ok is True
    assert reqs == []


def test_an_override_makes_the_rows_advisory_not_absent(tmp_path, monkeypatch):
    """The operator said go. The report still has to say what is broken."""
    shared = tmp_path / "s"
    repo = _repo(tmp_path, [])
    monkeypatch.setattr(cc, "_repo_root", lambda: repo)
    cc.record_run(_run(UNTESTED, False, {"a-check": "fail"}), shared_dir=shared)
    cc.record_override(
        UNTESTED, confirmation=cc.override_confirmation_phrase(UNTESTED, ["a-check"]),
        shared_dir=shared, repo_root=repo,
    )
    result, reqs = su.gate_compat_contract(UNTESTED, shared_dir=shared)
    assert result.ok is True
    assert [r.id for r in reqs] == ["compat-contract-a-check"]
    assert reqs[0].blocking is False
    assert "override recorded" in reqs[0].summary


def test_the_gate_is_in_the_canonical_gate_order():
    assert "compat_contract" in su.GATE_ORDER


# ── The HTTP surface ─────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    (shared / su.REPORTS_SUBDIR).mkdir(parents=True)
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared), "bots": {"team-bot-a": {}}, "members": ["team-bot-a"],
    }))
    monkeypatch.setattr(uv, "installed_package_version", lambda *a, **k: TESTED)
    monkeypatch.setattr(uv, "per_bot_versions", lambda *a, **k: {})
    monkeypatch.setattr(cc, "_repo_root", lambda: _repo(tmp_path, [TESTED]))

    app = Flask(__name__)
    rm._register_maintenance_routes(app, network_path)
    return {"client": app.test_client(), "shared": shared}


def _oc_version(client, monkeypatch, latest: str) -> dict:
    import urllib.request as ureq

    class _Resp:
        def read(self): return json.dumps({"version": latest}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(ureq, "urlopen", lambda *a, **k: _Resp())
    r = client["client"].get("/api/oc/version")
    assert r.status_code == 200
    return r.get_json()


def test_oc_version_marks_an_untested_candidate(client, monkeypatch):
    body = _oc_version(client, monkeypatch, UNTESTED)
    assert body["validation"]["state"] == cc.STATE_UNTESTED
    assert body["validation"]["version"] == UNTESTED
    assert body["validation"]["tested"] == [TESTED]
    assert body["validation"]["confirmation_phrase"] == f"upgrade {UNTESTED} without validation"


def test_oc_version_marks_a_tested_candidate(client, monkeypatch):
    body = _oc_version(client, monkeypatch, TESTED)
    assert body["validation"]["state"] == cc.STATE_TESTED


def test_override_endpoint_refuses_a_wrong_phrase(client):
    r = client["client"].post("/api/oc/compat/override",
                              json={"version": UNTESTED, "confirm": "ok"})
    assert r.status_code == 400
    assert "must read exactly" in r.get_json()["error"]
    assert cc.overrides(client["shared"]) == []


def test_override_endpoint_requires_a_version(client):
    r = client["client"].post("/api/oc/compat/override", json={"confirm": "x"})
    assert r.status_code == 400


def test_override_endpoint_records_the_named_checks(client):
    shared = client["shared"]
    cc.record_run(_run(UNTESTED, False, {"a-check": "fail"}), shared_dir=shared)
    phrase = cc.override_confirmation_phrase(UNTESTED, ["a-check"])
    r = client["client"].post("/api/oc/compat/override",
                              json={"version": UNTESTED, "confirm": phrase})
    assert r.status_code == 200
    assert r.get_json()["validation"]["overridden"] is True
    assert cc.overrides(shared)[0]["failing"] == ["a-check"]


# ── The Update card ──────────────────────────────────────────────────────────

def _js() -> str:
    return _MAINTENANCE_JS.read_text()


def test_the_apply_button_requires_a_validated_openclaw():
    m = re.search(r"const canApply = ([^;]+);", _js())
    assert m, "canApply gate not found"
    assert "validated" in m.group(1), "canApply must require a validated OpenClaw"


def test_the_card_says_not_yet_validated_and_names_the_failing_checks():
    src = _js()
    assert "not yet validated by Evolve" in src
    assert "failed on:" in src
    assert "contract run pending" in src


def test_the_withheld_branch_offers_the_override_and_nothing_else():
    src = _js()
    m = re.search(r"\} else if \(!validated\) \{(.+?)\n  \} else if", src, re.S)
    assert m, "no dedicated branch for an unvalidated OpenClaw"
    branch = m.group(1)
    assert "openOcOverride()" in branch
    assert "Run upgrade now" not in branch


def test_the_override_modal_is_not_a_native_dialog():
    """Native dialogs are suppressed in the desktop shell, so a confirm()
    here would silently do nothing (style-guide §9.6 rule 5). Comments are
    stripped first: the apply modal's docstring quotes the CLI's
    ``click.confirm("Proceed?")``, which is prose, not a dialog."""
    src = re.sub(r"^\s*//.*$", "", _js(), flags=re.M)
    src = src.replace("confirmOcOverride(", "").replace("confirmOcApply(", "")
    for native in ("window.confirm(", "alert(", " confirm("):
        assert native not in src, f"native dialog {native!r} in the maintenance page"
    assert "oc-override-modal" in _js()


def test_the_override_handlers_are_window_exported():
    src = _js()
    for name in ("openOcOverride", "confirmOcOverride", "closeOcOverride"):
        assert f"window.{name} = {name};" in src


def test_the_override_modal_markup_exists_and_has_a_width_class():
    html = _INDEX_HTML.read_text()
    assert 'id="oc-override-modal"' in html
    m = re.search(r'<input id="oc-override-input"[^>]*>', html)
    assert m, "override input not found"
    assert "input-w-" in m.group(0), "style-guide §9.2: every input carries a width class"


# ── End-to-end through run_preflight ─────────────────────────────────────────

def test_run_preflight_blocks_on_an_unvalidated_candidate(tmp_path, monkeypatch):
    """The wiring, not just the gate: a preflight against a version Evolve has
    not validated comes back red with a named compat blocker, which is what
    keeps the Update card's apply endpoint from running."""
    from unittest.mock import patch

    monkeypatch.setattr(cc, "_repo_root", lambda: _repo(tmp_path, []))
    metadata = {
        "version": UNTESTED,
        "bin": {"openclaw": "dist/cli.js"},
        "dist": {"unpackedSize": 5_000_000, "tarball": "https://registry.example/oc.tgz"},
        "engines": {"node": ">=18"},
    }
    with patch.object(su, "_fetch_registry_metadata", return_value=metadata), \
         patch.object(su, "_node_version", return_value="20.11.1"), \
         patch.object(su, "_installed_version", return_value=TESTED), \
         patch.object(su, "_fetch_candidate_plugin_ids", return_value=(set(), None)), \
         patch.object(su, "gate_send_surface",
                      return_value=(su.GateResult(ok=True, details={}), [])):
        report = su.run_preflight(
            target_spec=UNTESTED, network={"members": [], "bots": {}},
            shared_dir=tmp_path, persist=False,
        )

    assert report.ok is False
    assert report.gates["compat_contract"].ok is False
    blockers = [r.id for r in report.requirements if r.blocking]
    assert "compat-contract-unvalidated" in blockers


def test_run_preflight_passes_the_gate_for_a_tested_candidate(tmp_path, monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(cc, "_repo_root", lambda: _repo(tmp_path, [UNTESTED]))
    metadata = {
        "version": UNTESTED,
        "bin": {"openclaw": "dist/cli.js"},
        "dist": {"unpackedSize": 5_000_000, "tarball": "https://registry.example/oc.tgz"},
        "engines": {"node": ">=18"},
    }
    with patch.object(su, "_fetch_registry_metadata", return_value=metadata), \
         patch.object(su, "_node_version", return_value="20.11.1"), \
         patch.object(su, "_installed_version", return_value=TESTED), \
         patch.object(su, "_fetch_candidate_plugin_ids", return_value=(set(), None)), \
         patch.object(su, "gate_send_surface",
                      return_value=(su.GateResult(ok=True, details={}), [])):
        report = su.run_preflight(
            target_spec=UNTESTED, network={"members": [], "bots": {}},
            shared_dir=tmp_path, persist=False,
        )

    assert report.gates["compat_contract"].ok is True
    assert not [r for r in report.requirements if r.source_gate == "compat_contract"]


# ── The apply endpoint's independent veto ────────────────────────────────────

def test_apply_refuses_an_unvalidated_target_even_behind_a_green_report(client, monkeypatch):
    """A report written before the compat gate existed is `ok: true` and NOT
    stale (staleness compares versions, not gates), so the endpoint has to
    re-derive validation rather than trust the report's age."""
    from evolve_admin import deploy_resilience as dres
    from evolve_admin import oc_upgrade_apply as apply_mod

    shared = client["shared"]
    report_id = "20260909T040506Z-abcd1234"
    (shared / su.REPORTS_SUBDIR / f"{report_id}.json").write_text(json.dumps({
        "report_id": report_id, "ok": True, "summary": "All gates passed",
        "current": {"installed_version": TESTED},
        "candidate": {"target_spec": "latest", "resolved_version": UNTESTED},
        "gates": {}, "requirements": [],
    }))
    monkeypatch.setattr(rm, "_report_stale_reason", lambda _r: None)
    monkeypatch.setattr(su, "inflight_report_id", lambda: None)
    called: list = []
    monkeypatch.setattr(dres, "try_acquire_deploy_lock",
                        lambda *a, **k: called.append("lock") or object())
    monkeypatch.setattr(apply_mod, "stream_privileged_upgrade",
                        lambda *a, **k: called.append("privileged") or (True, ""))

    r = client["client"].post("/api/oc/upgrade/apply",
                              json={"reportId": report_id, "confirm": True})
    assert r.status_code == 409
    assert "has not been validated" in r.get_json()["error"]
    # Nothing privileged ran, and the deploy lock was never taken.
    assert called == []


def test_apply_proceeds_past_the_veto_once_an_override_is_recorded(client, monkeypatch):
    from evolve_admin import deploy_resilience as dres

    shared = client["shared"]
    cc.record_run(_run(UNTESTED, False, {"a-check": "fail"}), shared_dir=shared)
    cc.record_override(
        UNTESTED, confirmation=cc.override_confirmation_phrase(UNTESTED, ["a-check"]),
        shared_dir=shared, repo_root=cc._repo_root(),
    )
    report_id = "20260909T040507Z-abcd1234"
    (shared / su.REPORTS_SUBDIR / f"{report_id}.json").write_text(json.dumps({
        "report_id": report_id, "ok": True, "summary": "All gates passed",
        "current": {"installed_version": TESTED},
        "candidate": {"target_spec": "latest", "resolved_version": UNTESTED},
        "gates": {}, "requirements": [],
    }))
    monkeypatch.setattr(rm, "_report_stale_reason", lambda _r: None)
    monkeypatch.setattr(su, "inflight_report_id", lambda: None)
    # Refuse at the NEXT gate (the deploy lock) so the test proves the compat
    # veto was passed without starting a real upgrade.
    monkeypatch.setattr(dres, "try_acquire_deploy_lock", lambda *a, **k: None)

    r = client["client"].post("/api/oc/upgrade/apply",
                              json={"reportId": report_id, "confirm": True})
    assert r.status_code == 409
    assert "deploy is already in progress" in r.get_json()["error"]


def test_the_safety_report_renders_the_compat_gate_row():
    """The requirements block already names each failing check; the gate row
    is the headline above it. A gate the report omits reads as a gate that did
    not run."""
    src = _js()
    m = re.search(r"const gateOrder = \[([^\]]+)\]", src)
    assert m and "compat_contract" in m.group(1)
    assert "compat_contract: 'compat-contract'" in src
    assert "contract fails on:" in src
