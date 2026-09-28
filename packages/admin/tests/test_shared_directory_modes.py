"""tests/test_shared_directory_modes.py — directory-store contact-PII 0600 self-heal.

Phase-2 prerequisite for spec-user-directory-2026-06-22: the directory store
(``{shared_dir}/directory/{bot_id}.json`` + ``directory/log/*.jsonl``) is the
FIRST shared store to hold real contact emails (PII). Phase 1's
``user_directory.storage.save_directory`` writes it 0600 from inception — but
``deploy_shared_dir``'s ``chmod -R a+rX {shared_dir}`` pass re-widens every file
to 0644 on EVERY deploy, so without a compensating re-tighten the PII rows land
world-readable on the multi-user box.

This is the directory-store sibling of ``test_shared_secret_modes.py`` (the
``secrets/`` tree): ``secret_config_perms.tighten_shared_directory_tree`` /
``tighten_shared_protected_trees`` (the post-``a+rX`` re-tighten) +
``check_shared_directory_modes`` (the per-file 0600 self-heal), wired into
``ensure_pod_perms``'s pod-wide phase.

It mirrors ``test_shared_secret_modes.py`` deliberately so the two trees stay in
lockstep. FAKE bot ids + ``*.example`` emails only (docs/PLACEHOLDER_NAMING.md).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
sys.path.insert(0, str(_ADMIN_DIR.parent / "analyzer"))

from evolve_admin import deploy, secret_config_perms as scp  # noqa: E402
from evolve_admin.user_directory import storage as uds  # noqa: E402


BOT = "team_bot_a"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dir_store(shared: Path) -> Path:
    d = shared / "directory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, mode: int, content: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)
    return path


def _by_target(checks, path: Path):
    hits = [c for c in checks if c.target == str(path)]
    assert hits, f"no check produced for {path}; got {[c.target for c in checks]}"
    return hits[0]


# ── check_shared_directory_modes — detection + repair ─────────────────────────

class TestCheckSharedDirectoryModes:

    def test_flags_world_readable_row_and_repairs_to_600(self, tmp_path):
        """The exposure: a 0644 directory row is flagged drift, and its apply
        chmods the real file back to 0600 (owner-only)."""
        row = _write(_dir_store(tmp_path) / f"{BOT}.json", 0o644)

        check = _by_target(scp.check_shared_directory_modes(tmp_path), row)
        assert not check.ok
        assert "expected 0o600" in check.detail
        assert check.apply is not None
        assert check.apply() is True
        assert stat.S_IMODE(row.stat().st_mode) == 0o600

    def test_sweeps_audit_log_jsonl_too(self, tmp_path):
        """The ``log/*.jsonl`` audit trail records email values in its payloads,
        so it is PII at rest and must be 0600 as well."""
        logf = _write(_dir_store(tmp_path) / "log" / "2026-06-22.jsonl", 0o644,
                      content='{"after": {"emails": [{"addr": "x@y.example"}]}}\n')
        check = _by_target(scp.check_shared_directory_modes(tmp_path), logf)
        assert not check.ok
        assert check.apply() is True
        assert stat.S_IMODE(logf.stat().st_mode) == 0o600

    def test_correct_0600_row_is_clean(self, tmp_path):
        row = _write(_dir_store(tmp_path) / f"{BOT}.json", 0o600)
        check = _by_target(scp.check_shared_directory_modes(tmp_path), row)
        assert check.ok
        assert check.apply is None

    def test_noop_when_no_directory_store(self, tmp_path):
        """Pods with no directory writes yet → no checks (not a failure)."""
        assert scp.check_shared_directory_modes(tmp_path) == []

    def test_symlink_flagged_not_chmodded_through(self, tmp_path):
        """A symlink in the evolve-owned directory store is anomalous — flagged
        for review, never auto-chmod'd through to its target."""
        store = _dir_store(tmp_path)
        target = _write(tmp_path / "outside.json", 0o644)
        link = store / "evil.json"
        link.symlink_to(target)
        check = _by_target(scp.check_shared_directory_modes(tmp_path), link)
        assert not check.ok
        assert "symlink" in check.detail
        assert check.apply is None  # never auto-repaired
        # The link's target was not chmodded.
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


# ── tighten_shared_directory_tree — the deploy-time re-exposer fix ─────────────

class TestTightenSharedDirectoryTree:

    def test_reverses_a_plus_rX_widen_back_to_0600(self, tmp_path):
        """End-to-end: seed 0600 rows, run the SAME ``chmod -R a+rX``
        deploy_shared_dir runs (widening each to 0644), then tighten — every row
        is back to 0600 and the dirs back to 0700."""
        shared = tmp_path / "shared"
        store = _dir_store(shared)
        row = _write(store / f"{BOT}.json", 0o600)
        logf = _write(store / "log" / "2026-06-22.jsonl", 0o600, content="{}\n")

        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)
        assert stat.S_IMODE(row.stat().st_mode) == 0o644, "precondition: a+rX widened the row"
        assert stat.S_IMODE(logf.stat().st_mode) == 0o644

        assert scp.tighten_shared_directory_tree(shared) is True
        assert stat.S_IMODE(row.stat().st_mode) == 0o600
        assert stat.S_IMODE(logf.stat().st_mode) == 0o600
        # a+rX made dirs 0705; the re-tighten restores 0700 (owner-only).
        assert stat.S_IMODE(store.stat().st_mode) == 0o700
        assert stat.S_IMODE((store / "log").stat().st_mode) == 0o700

    def test_noop_true_when_no_directory_dir(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        assert scp.tighten_shared_directory_tree(shared) is True

    def test_owner_can_still_read_after_tighten(self, tmp_path):
        shared = tmp_path / "shared"
        row = _write(_dir_store(shared) / f"{BOT}.json", 0o600, content='{"persons": {}}')
        assert scp.tighten_shared_directory_tree(shared) is True
        assert json.loads(row.read_text()) == {"persons": {}}  # owner read survives


# ── tighten_shared_protected_trees — every protected tree in one deploy call ──

class TestTightenSharedProtectedTrees:

    def test_tightens_both_secrets_and_directory(self, tmp_path):
        """The composed call deploy_shared_dir makes: secrets/ AND directory/
        both re-tightened after the a+rX widen."""
        shared = tmp_path / "shared"
        sa = _write(shared / "secrets" / "google_service_accounts" / "k.json", 0o600)
        row = _write(shared / "directory" / f"{BOT}.json", 0o600)
        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)
        assert stat.S_IMODE(sa.stat().st_mode) == 0o644
        assert stat.S_IMODE(row.stat().st_mode) == 0o644

        assert scp.tighten_shared_protected_trees(shared) is True
        assert stat.S_IMODE(sa.stat().st_mode) == 0o600
        assert stat.S_IMODE(row.stat().st_mode) == 0o600


# ── Integration with the real Phase-1 store ───────────────────────────────────

class TestRealStoreRoundTrip:

    def test_store_writes_0600_and_survives_widen_plus_tighten(self, tmp_path):
        """Write a row through the real ``upsert_entry`` (so it carries a real
        email), confirm it lands 0600, then reproduce the deploy widen → tighten
        cycle and confirm the contact PII is back to owner-only."""
        shared = tmp_path / "shared"
        shared.mkdir()
        uds.upsert_entry(
            shared, BOT, "email", "dana@acme.example",
            by="operator@test", provenance="operator-verified",
            emails=[{"addr": "dana@example.net", "rank": "primary"}])
        row = uds.directory_path(shared, BOT)
        assert stat.S_IMODE(row.stat().st_mode) == 0o600  # 0600 from inception

        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)
        assert stat.S_IMODE(row.stat().st_mode) == 0o644  # deploy re-widened it
        assert scp.tighten_shared_directory_tree(shared) is True
        assert stat.S_IMODE(row.stat().st_mode) == 0o600  # self-heal restored it


# ── Wiring: ensure_pod_perms runs the directory check pod-wide ────────────────

class TestEnsurePodPermsWiring:

    def test_drifted_directory_row_surfaces_in_ensure_pod_perms(self, tmp_path, monkeypatch):
        """End-to-end through ensure_pod_perms (check_only): a 0644 directory row
        under the pod's sharedDir produces a ``directory-mode`` drift check —
        proving the pod-wide wiring, not just the unit function."""
        shared = tmp_path / "shared"
        shared.mkdir()
        row = _write(_dir_store(shared) / f"{BOT}.json", 0o644)

        net = {
            "networkId": "test-pod",
            "sharedDir": str(shared),
            "members": [],
            "bots": {},
        }
        np = tmp_path / "network.json"
        np.write_text(json.dumps(net))
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")

        result = deploy.ensure_pod_perms(
            bot_id=None, network_path=np, check_only=True)

        hits = [c for c in result.checks
                if c.category == "directory-mode" and c.target == str(row)]
        assert hits, (
            "ensure_pod_perms did not run check_shared_directory_modes; "
            f"categories seen: {sorted({c.category for c in result.checks})}"
        )
        assert not hits[0].ok
        assert stat.S_IMODE(row.stat().st_mode) == 0o644  # check_only → no fix ran
        assert result.applied == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
