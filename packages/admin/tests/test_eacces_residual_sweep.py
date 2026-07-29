"""test_eacces_residual_sweep.py — the residual EACCES-safe sweep (Py3.12).

Follow-up to the durable .openclaw ACL-mask PR (which routed the deploy-critical
``Path.exists()``/``.stat()`` sites through
``secret_config_perms.exists_or_unreachable`` and guarded the scanner
``_get_workspace`` chokepoint). On Python 3.12 (Ubuntu/VPS pods) a 0700 ACL-mask
clamp on a bot's ``.openclaw`` makes ``.exists()``/``.is_file()``/``.is_dir()``/
``.glob()``/``.iterdir()``/``.rglob()`` RAISE ``PermissionError`` instead of
returning ``False`` — code that assumed they return ``False`` then crashes.

These tests pin the residual sites so a clamp can NEVER crash a scan, a deploy,
or a reconciliation pass, and so the disposition is correct:

  * scanner walk/probe sites → skip/empty under EACCES, but LOGGED (the directory
    walk itself is the EACCES point — never silently confabulate "absent").
  * present-or-proceed gates (``exists_or_unreachable``) → unreachable == present,
    so reconciliation never falsely flags a still-existing file as missing.
  * the app-permissions reconciler → SKIP (skipped/skip_reason), NOT an empty
    preview that would clobber the bot's real permission state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import MACOS, set_profile  # noqa: E402

from evolve_admin import deploy, secret_config_perms  # noqa: E402
from evolve_admin.applications import scanner as _scanner  # noqa: E402
from evolve_admin.applications import reconciliation as _recon  # noqa: E402
from evolve_admin.app_permissions import reconciler as _rc  # noqa: E402
from evolve_admin.app_permissions import reconcile_bot_permissions, PREVIEW_FILENAME  # noqa: E402
from evolve_admin.runtime import FakePerms, set_perms, set_scheduler  # noqa: E402


_EACCES = PermissionError(13, "Permission denied")


def _patch_raises(monkeypatch, method: str, marker: str) -> None:
    """Make ``Path.<method>`` RAISE EACCES for any path containing ``marker``,
    while leaving every other path (e.g. pytest's tmp setup) untouched.

    Simulates the 0700-clamp behaviour of Py3.12: the probe raises rather than
    returning False. The raise timing (call-time here vs iteration-time on a real
    clamp) is irrelevant — every guarded call site wraps the call itself."""
    orig = getattr(Path, method)

    def _patched(self, *a, **k):
        if marker in str(self):
            raise PermissionError(13, "Permission denied")
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, method, _patched)


@pytest.fixture(autouse=True)
def _restore_seams():
    yield
    set_profile(None)
    set_perms(None)
    set_scheduler(None)


# ── scanner: present-or-proceed gates (.exists() RAISES → no crash) ───────────


def test_collect_memory_files_survives_exists_eacces(tmp_path, monkeypatch):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    _patch_raises(monkeypatch, "exists", "clamped_ws")
    # Old code: search_dir.exists() RAISES → crashes collect_inventory. New code
    # routes through exists_or_unreachable and the glob is guarded → [] not crash.
    assert _scanner._collect_memory_files(ws, "bot") == []


def test_extract_scheduled_action_candidates_survives_exists_eacces(tmp_path, monkeypatch):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    _patch_raises(monkeypatch, "exists", "clamped_ws")
    assert _scanner._extract_scheduled_action_candidates(ws, "bot") == []


def test_enumerate_launch_agents_survives_exists_eacces(monkeypatch):
    monkeypatch.setattr(_scanner, "get_bot_user", lambda *a, **k: "clampbot")
    monkeypatch.setattr(_scanner, "load_network", lambda *a, **k: {})
    _patch_raises(monkeypatch, "exists", "/clampbot/")
    assert _scanner._enumerate_launch_agents("clampbot") == []


def test_read_heartbeat_md_sections_survives_exists_eacces(monkeypatch):
    monkeypatch.setattr(_scanner, "get_bot_user", lambda *a, **k: "clampbot")
    monkeypatch.setattr(_scanner, "load_network", lambda *a, **k: {})
    _patch_raises(monkeypatch, "exists", "/clampbot/")
    assert _scanner._read_heartbeat_md_sections("clampbot") == []


# ── scanner: walk-loop guards (rglob/iterdir RAISES → no crash + LOG) ─────────


def test_collect_inventory_survives_walk_eacces(tmp_path, monkeypatch, caplog):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    # The bot-user helpers degrade to [] on their own try/except; neutralise them
    # so the test isolates the workspace-walk guard.
    monkeypatch.setattr(_scanner, "get_bot_user", lambda *a, **k: "clampbot")
    monkeypatch.setattr(_scanner, "load_network", lambda *a, **k: {})
    for meth in ("rglob", "glob", "iterdir", "exists"):
        _patch_raises(monkeypatch, meth, "clamped_ws")

    with caplog.at_level("WARNING"):
        inv = _scanner.collect_inventory(ws, "clampbot")

    # No crash, returns a real inventory object with empty walked-content.
    assert isinstance(inv, _scanner.WorkspaceInventory)
    assert inv.directories == []
    assert inv.python_scripts == []
    # The clamp was LOGGED, not silently swallowed as "empty workspace".
    assert any("unreachable" in r.message for r in caplog.records)


def test_collect_named_dirs_survives_iterdir_eacces(tmp_path, monkeypatch, caplog):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    _patch_raises(monkeypatch, "iterdir", "clamped_ws")
    with caplog.at_level("WARNING"):
        assert _scanner._collect_named_dirs(ws, "bot") == []
    assert any("unreachable" in r.message for r in caplog.records)


def test_scan_compliance_survives_workspace_walk_eacces(tmp_path, monkeypatch):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    # Isolate the workspace-walk guard: no manifests, no signal emission.
    monkeypatch.setattr(_scanner, "_get_workspace", lambda b: ws)
    monkeypatch.setattr(_scanner, "_emit_compliance_signals", lambda *a, **k: set())
    import evolve_admin.applications.manifest as _manifest
    monkeypatch.setattr(_manifest, "list_manifests", lambda *a, **k: [])
    # Both the unregistered-script walk AND the secrets walk hit rglob.
    _patch_raises(monkeypatch, "rglob", "clamped_ws")
    _patch_raises(monkeypatch, "exists", "clamped_ws")

    result = _scanner.scan_compliance("clampbot", shared)
    # No crash; a structured result is still returned.
    assert result["bot_id"] == "clampbot"
    assert "issues" in result


def test_generate_manifest_survives_evidence_probe_eacces(tmp_path, monkeypatch):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    inv = _scanner.WorkspaceInventory(workspace=ws, bot_id="clampbot")
    app = _scanner.DetectedApplication(
        id="appx", name="App X", confidence=0.9,
        evidence_files=["scripts/x.py"], evidence_summary="x",
        suggested_goals=[], suggested_tests=[], suggested_privacy=[],
    )
    # is_file()/is_dir() on the evidence path RAISE under the clamp.
    _patch_raises(monkeypatch, "is_file", "clamped_ws")
    _patch_raises(monkeypatch, "is_dir", "clamped_ws")
    monkeypatch.setattr(_scanner, "_call_anthropic", lambda *a, **k: "")
    monkeypatch.setattr(_scanner, "_read_api_key", lambda *a, **k: "")

    manifest = _scanner.generate_manifest_for_app(app, inv, "tier3")
    # No crash; a manifest dict is still produced (LLM stubbed empty → stub path).
    assert isinstance(manifest, dict)


def test_stamp_discovered_files_survives_candidate_probe_eacces(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    manifest = {
        "id": "appx", "name": "App X", "bot_id": "clampbot",
        "evidence_files": ["scripts/x.py"], "files": [],
    }
    manifest_path = tmp_path / "appx.json"
    manifest_path.write_text(json.dumps(manifest))
    logs: list[str] = []
    # p.is_file()/is_dir() on the evidence candidate RAISE under the clamp, and
    # the debug "exists=" line's workspace.exists() RAISES too.
    _patch_raises(monkeypatch, "is_file", "clamped_ws")
    _patch_raises(monkeypatch, "is_dir", "clamped_ws")
    _patch_raises(monkeypatch, "exists", "clamped_ws")

    # Must not raise.
    _scanner._stamp_discovered_files(manifest, ws, manifest_path, log_collector=logs)
    assert any("unreachable" in line for line in logs)


# ── reconciliation: present-or-proceed (unreachable == present) ───────────────


def test_file_exists_treats_eacces_as_present(tmp_path, monkeypatch):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    _patch_raises(monkeypatch, "exists", "clamped_ws")
    # Unreachable must read as PRESENT — never let a clamp reconcile away a file.
    assert _recon._file_exists(ws, "scripts/x.py") is True


def test_check_missing_actions_eacces_not_flagged_missing(tmp_path, monkeypatch):
    ws = tmp_path / "clamped_ws"
    ws.mkdir()
    (ws / "HEARTBEAT.md").write_text("# Heartbeat\n\n## Protein\n\nbody")
    manifest = {
        "id": "test",
        "provenance": {"field_origins": {
            "scheduled_actions": {"source": "forge_built"},  # authored → would stage
        }},
        "scheduled_actions": [{
            "id": "p", "state": "active", "quality": "extracted",
            "trigger": {
                "kind": "heartbeat",
                "evidence_path": "HEARTBEAT.md",
                "evidence_locator": "Protein",
            },
        }],
    }
    # Both the existence probe AND the verifying read raise under the clamp.
    _patch_raises(monkeypatch, "exists", "clamped_ws")
    _patch_raises(monkeypatch, "read_text", "clamped_ws")

    silent_drops, staged = _recon.check_missing_actions(manifest, ws)
    # Conservative skip: NOT a false "evidence_file_missing".
    assert silent_drops == []
    assert staged == []


# ── app_permissions reconciler: SKIP (not empty-preview) under EACCES ─────────


def _make_bot_home(tmp_path: Path, bot_id: str) -> Path:
    home = tmp_path / bot_id
    md = home / ".openclaw" / "workspace" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    (md / "i-app.json").write_text(json.dumps({
        "id": "i-app", "name": "App", "status": "active",
        "files": [{"path": "tools/x.py", "layer": "script"}],
    }))
    (home / ".openclaw" / "openclaw.json").write_text(
        json.dumps({"tools": {"exec": {"security": "full"}}}))
    (home / ".openclaw" / "exec-approvals.json").write_text("{}")
    return home


def _network(bot_id: str) -> dict:
    return {"networkId": "t", "bots": {bot_id: {"role": "member", "user": bot_id}}}


def test_reconcile_skips_on_manifests_dir_eacces(tmp_path, monkeypatch):
    home = _make_bot_home(tmp_path, "clampbot")
    # The manifests-dir walk RAISES under the clamp (iterdir + exists).
    _patch_raises(monkeypatch, "iterdir", "/manifests")
    _patch_raises(monkeypatch, "exists", "/manifests")

    result = reconcile_bot_permissions(
        "clampbot", network=_network("clampbot"), home_override=home,
    )
    # Disposition: SKIP with a clear reason — NOT an empty preview write that
    # would clobber the bot's real permission state.
    assert result.skipped is True
    assert "unreachable" in result.skip_reason.lower()
    assert not (home / ".openclaw" / PREVIEW_FILENAME).exists()


def test_iter_manifest_files_propagates_eacces(tmp_path, monkeypatch):
    md = tmp_path / "manifests"
    md.mkdir()
    _patch_raises(monkeypatch, "exists", "/manifests")
    # Deliberately propagates so the caller can distinguish unreachable from absent.
    with pytest.raises(PermissionError):
        list(_rc._iter_manifest_files(md))


# ── deploy: set_evolve_read_acl carve-out can't crash the deploy ──────────────


def test_set_evolve_read_acl_survives_carveout_eacces(tmp_path, monkeypatch):
    """If grant_read_recursive ever fails to heal the mask, the credentials +
    profiles carve-out's .glob()/.exists() would EACCES-crash the whole ACL grant
    (and thus the deploy). The best-effort try/except must absorb it."""
    set_profile(MACOS)          # verify_evolve_access no-ops on macOS
    set_perms(FakePerms())

    bot_home = tmp_path / "home" / "clampbot"
    oc = bot_home / ".openclaw"
    (oc / "workspace").mkdir(parents=True)
    (oc / "credentials").mkdir()
    (oc / "profiles").mkdir()

    monkeypatch.setattr(deploy, "_user_home", lambda u: bot_home)
    monkeypatch.setattr(deploy, "_bot_user_for", lambda b: "clampbot")
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: _R())
    # profiles_dir.glob("*.md") RAISES — the carve-out site the chip flags.
    _patch_raises(monkeypatch, "glob", "/profiles")

    # Must not raise: the whole carve-out block is wrapped best-effort.
    deploy.set_evolve_read_acl("clampbot")


def test_set_evolve_read_acl_survives_full_openclaw_clamp(tmp_path, monkeypatch):
    """The full-clamp shape: the ACL heal (grant_read_recursive) does NOT recover
    the mask (FakePerms no-ops it), so EVERY probe under .openclaw raises — the
    carve-out AND the trailing _ensure_evolve_write_dir / evolve-backup /
    claude-projects / zshrc / workspace-root probes. The whole function must
    still complete without raising (the deploy must not die '0 of N bots')."""
    set_profile(MACOS)
    set_perms(FakePerms())

    bot_home = tmp_path / "home" / "clampbot2"
    (bot_home / ".openclaw" / "workspace").mkdir(parents=True)

    monkeypatch.setattr(deploy, "_user_home", lambda u: bot_home)
    monkeypatch.setattr(deploy, "_bot_user_for", lambda b: "clampbot2")
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: _R())
    # Every filesystem probe under the bot's tree raises — the real 0700-clamp shape.
    for meth in ("exists", "glob", "iterdir", "is_file", "stat"):
        _patch_raises(monkeypatch, meth, "clampbot2")

    # Must not raise — every probe site is guarded (exists_or_unreachable or wrap).
    deploy.set_evolve_read_acl("clampbot2")


class _R:
    returncode = 0
    stdout = ""
    stderr = ""
