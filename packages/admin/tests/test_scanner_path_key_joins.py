"""F-C1 regression — scanner.py path-key joins canonicalize BOTH sides.

The bug (audit ``internal/apps-shared-contract-audit-2026-06-29.md``, follow-on
F-C1): three ``scanner.py`` joins compared a workspace-relative disk-walk path
against raw manifest ``path`` strings. Those strings are workspace-relative from
migrate_v7/v13 but **absolute** from ``extend_application`` — the same "two
sides, different canonical form → join miss" shape as #3303. Symptoms:

  * ``7048`` — a false ``unregistered_script`` finding (the live bug).
  * ``6499`` — a duplicate stamp entry (fresh file_id on a path that already
    had an entry).
  * ``7155`` — dropped owner attribution on a misplaced-secret finding.

The fix routes every join key through the shared
``path_keys.ws_rel_key`` canonicalizer so both sides meet on the canonical
workspace-relative form.

Each behavioural test below is written to **FAIL on origin/main** (pre-fix) and
**PASS on the F-C1 branch**, and also pins the over/under-canonicalization
guard: a genuinely-unregistered script must STILL fire, and two distinct paths
must NOT merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from evolve_admin.applications import manifest as manifest_mod
from evolve_admin.applications import scanner
from evolve_admin.applications.path_keys import ws_rel_key


# ── ws_rel_key: the canonicalizer's over/under-canonicalization contract ──────


def test_ws_rel_key_both_forms_meet_on_one_key():
    ws = Path("/ws")
    # absolute-under-workspace reduces to the workspace-relative form …
    assert ws_rel_key("/ws/a/foo.py", ws) == "a/foo.py"
    # … and a workspace-relative input normalizes to the SAME key.
    assert ws_rel_key("a/foo.py", ws) == "a/foo.py"
    assert ws_rel_key("/ws/a/foo.py", ws) == ws_rel_key("a/foo.py", ws)


def test_ws_rel_key_distinct_paths_stay_distinct():
    # Over-canonicalization guard: two genuinely-different paths must NOT merge.
    ws = Path("/ws")
    assert ws_rel_key("a/foo.py", ws) != ws_rel_key("b/foo.py", ws)
    assert ws_rel_key("/ws/a/foo.py", ws) != ws_rel_key("/ws/b/foo.py", ws)


def test_ws_rel_key_foreign_absolute_kept_absolute():
    # A path outside the workspace stays absolute → matches no in-workspace file
    # (the row honestly falls to missing/unregistered, never a false match).
    ws = Path("/ws")
    assert ws_rel_key("/etc/passwd", ws) == "/etc/passwd"


def test_ws_rel_key_never_resolves_relative_against_cwd(monkeypatch, tmp_path):
    # The #3303 keystone: a relative input must be CWD-INDEPENDENT.
    ws = Path("/ws")
    before = ws_rel_key("scripts/x.py", ws)
    monkeypatch.chdir(tmp_path)
    after = ws_rel_key("scripts/x.py", ws)
    assert before == after == "scripts/x.py"


# ── Test harness for scan_compliance ─────────────────────────────────────────


class _FakeManifest:
    """Minimal stand-in exposing only the attributes scan_compliance reads."""

    def __init__(
        self,
        *,
        id: str,
        bot_id: str = "atlas",
        files=None,
        crons=None,
        compliance_suppressed: bool = False,
        raw=None,
    ):
        self.id = id
        self.name = id
        self.bot_id = bot_id
        self.description = "desc"
        self.status = "active"
        self.last_reviewed = None
        self.last_test_exit_code = None
        self.last_test_run = None
        self.compliance_suppressed = compliance_suppressed
        self.raw = raw or {}
        self._files = list(files or [])
        self._crons = list(crons or [])

    def file_paths(self):
        return list(self._files)

    def cron_lines(self):
        return list(self._crons)


def _no_crontab(*args, **kwargs):
    # crontab -l is unavailable in the test env — return non-zero so the cron
    # section is skipped deterministically (no sudo, no hang).
    return subprocess.CompletedProcess(
        args=args[0] if args else [], returncode=1, stdout="", stderr=""
    )


def _run_scan(tmp_path, monkeypatch, manifests, workspace):
    # list_manifests is imported INSIDE scan_compliance (`from .manifest import
    # list_manifests`), so patch it on the source module.
    monkeypatch.setattr(
        manifest_mod, "list_manifests", lambda shared_dir, bot_id: manifests
    )
    monkeypatch.setattr(scanner, "_get_workspace", lambda bot_id: workspace)
    monkeypatch.setattr(scanner.subprocess, "run", _no_crontab)
    return scanner.scan_compliance("atlas", tmp_path)


def _unregistered(result):
    return {
        i["path"]
        for i in result["issues"]
        if i["issue_type"] == "unregistered_script"
    }


# ── 7048 — false unregistered_script (the firing bug) ─────────────────────────


def test_absolute_claim_does_not_misfire_unregistered(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "foo.py").write_text("print('hi')\n")   # claimed (as ABSOLUTE)
    (ws / "scripts" / "bar.py").write_text("print('bye')\n")  # genuinely unregistered
    (ws / "b").mkdir()
    (ws / "b" / "foo.py").write_text("print('b')\n")          # distinct from a/foo.py

    abs_foo = str(ws / "scripts" / "foo.py")
    # foo.py claimed via an ABSOLUTE path; a/foo.py claimed via a relative path
    # that does not exist on disk (only b/foo.py does).
    m = _FakeManifest(id="app1", files=[abs_foo, "a/foo.py"])

    result = _run_scan(tmp_path, monkeypatch, [m], ws)
    unreg = _unregistered(result)

    # FIRING BUG: the absolute-claimed script must NOT mis-fire.
    assert "scripts/foo.py" not in unreg
    # UNDER-canonicalization guard: a genuinely-unregistered script STILL fires.
    assert "scripts/bar.py" in unreg
    # OVER-canonicalization guard: claiming a/foo.py must NOT suppress b/foo.py.
    assert "b/foo.py" in unreg


def test_relative_claim_still_suppresses_and_unclaimed_still_fires(tmp_path, monkeypatch):
    # Positive control on the common (already-working) relative-claim path: the
    # fix must not regress the ordinary case.
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "ok.py").write_text("x\n")
    (ws / "scripts" / "stray.py").write_text("y\n")

    m = _FakeManifest(id="app1", files=["scripts/ok.py"])
    result = _run_scan(tmp_path, monkeypatch, [m], ws)
    unreg = _unregistered(result)

    assert "scripts/ok.py" not in unreg
    assert "scripts/stray.py" in unreg


# ── 7155 — dropped owner attribution on misplaced_secret ──────────────────────


def test_absolute_api_key_source_attributes_owner(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "cfg").mkdir(parents=True)
    secret_file = ws / "cfg" / "creds.env"
    secret_file.write_text("TOKEN=ghp_" + "a" * 36 + "\n")  # matches GitHub-PAT pattern

    abs_secret = str(secret_file)
    m = _FakeManifest(
        id="appX",
        raw={"recursive_llm": {"api_key_source": abs_secret}},
    )

    result = _run_scan(tmp_path, monkeypatch, [m], ws)
    secrets = [
        i
        for i in result["issues"]
        if i["issue_type"] == "misplaced_secret" and i["path"] == "cfg/creds.env"
    ]
    assert secrets, "the planted secret should be detected"
    # Pre-fix: index keyed by the ABSOLUTE source, looked up by the ws-relative
    # disk path → miss → app_id None / no principle overlay.
    assert secrets[0]["app_id"] == "appX"
    assert secrets[0].get("principle_violation") == "apps_inherit_bot_llm"


# ── 6499 — duplicate stamp entry on an absolute-stored path ───────────────────


def test_stamp_absolute_existing_path_not_reduplicated(tmp_path):
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    f = ws / "scripts" / "tool.py"
    f.write_text("print(1)\n")

    abs_path = str(f)
    manifest_path = tmp_path / "m.json"
    mdict = {
        "id": "app1",
        "name": "App One",
        "bot_id": "atlas",
        "evidence_files": ["scripts/tool.py"],
        # Existing registered entry stored as an ABSOLUTE path (extend_application).
        "files": [
            {
                "file_id": "f-existing",
                "path": abs_path,
                "layer": "script",
                "owned_by": "pkg-1",
            }
        ],
        "pkg_id": "pkg-1",
    }

    scanner._stamp_discovered_files(mdict, ws, manifest_path)

    tool_entries = [e for e in mdict["files"] if e["path"].endswith("tool.py")]
    # Pre-fix: rel "scripts/tool.py" != abs_path → a 2nd entry with a fresh
    # file_id is minted. Post-fix: the absolute entry matches → no duplicate.
    assert len(tool_entries) == 1, f"expected no duplicate, got {mdict['files']}"
    assert tool_entries[0]["file_id"] == "f-existing"


# ── Secondary (non-CWD) — reconciliation.check_missing_files ───────────────────


def test_reconciliation_absolute_path_not_false_missing(tmp_path):
    from evolve_admin.applications.reconciliation import check_missing_files

    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    f = ws / "scripts" / "keep.py"
    f.write_text("x\n")

    # An existing file claimed via an ABSOLUTE path. Pre-fix: a bare
    # `.lstrip("/")` mangles it into a non-existent nested path → false missing
    # (silent-drop or staged). Post-fix: ws_rel_key reduces it → exists → kept.
    manifest = {"files": [{"path": str(f), "layer": "script"}]}
    drops, staged = check_missing_files(manifest, ws)

    dropped_paths = [d.get("path") for d in drops if isinstance(d, dict)]
    staged_paths = [s["path"] for s in staged]
    assert not any(p and p.endswith("keep.py") for p in dropped_paths)
    assert not any(p.endswith("keep.py") for p in staged_paths)
