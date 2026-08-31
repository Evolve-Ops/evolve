"""tests/test_shared_secret_modes.py — pod-wide shared-secret 0600 self-heal.

Regression test for the 2026-06-20 live finding: a Google Path-C
service-account JSON key with domain-wide delegation
(``{shared_dir}/secrets/google_service_accounts/<ref>.json``) was found at
mode **0644 (world-readable)** on the multi-user mini — installed by the OLD
``sudo-cp-then-chmod`` flow whose ``check=False`` chmod silently swallowed
failures. A DwD SA key is password-equivalent for every Workspace user it can
impersonate, so a world-readable key exposes the whole domain to every local
and bot user on the box.

The current installers (``web/wizard_google_routes._install_sa_file``,
``google_workspace_setup.install_sa_key``, ``google_auth`` token writer) are
all correct — ``O_NOFOLLOW`` + 0600 from inception. What was MISSING is the
deploy-time self-heal that re-asserts 0600 on a PRE-fix key or any later
drift, the way per-bot secrets get one via ``check_bot_secret_modes``. This is
that sibling: ``secret_config_perms.check_shared_secret_modes`` +
``chmod_shared_secret``, wired into ``ensure_pod_perms``'s pod-wide phase.

Mirrors the style of ``test_oc_config_secret_mode.py`` /
``test_w10g_daemon_layer.py``. Placeholder refs per docs/PLACEHOLDER_NAMING.md;
no real keys.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy, secret_config_perms as scp  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sa_dir(shared: Path) -> Path:
    d = shared / "secrets" / "google_service_accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _oauth_dir(shared: Path) -> Path:
    d = shared / "secrets" / "google_oauth_tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, mode: int, content: str = "{}") -> Path:
    path.write_text(content)
    os.chmod(path, mode)
    return path


def _by_target(checks, path: Path):
    hits = [c for c in checks if c.target == str(path)]
    assert hits, f"no check produced for {path}; got {[c.target for c in checks]}"
    return hits[0]


# ── check_shared_secret_modes — detection ─────────────────────────────────────

class TestCheckSharedSecretModes:

    def test_flags_world_readable_sa_key_and_repairs_to_600(self, tmp_path):
        """The actual incident: a 0644 SA key is flagged drift, and its apply
        chmods the real file back to 0600."""
        key = _write(_sa_dir(tmp_path) / "sa-prime-placeholder.json", 0o644)

        check = _by_target(scp.check_shared_secret_modes(tmp_path), key)
        assert not check.ok
        assert "0o644" in check.detail
        assert "DwD-capable" in check.detail  # the operator-facing why
        assert check.apply is not None

        assert check.apply() is True
        assert stat.S_IMODE(key.stat().st_mode) == 0o600

    def test_correctly_600_key_is_ok_with_no_apply(self, tmp_path):
        """A correctly-0600 key passes and offers NO apply — the self-heal does
        not run a needless chmod on a compliant file (idempotency)."""
        key = _write(_sa_dir(tmp_path) / "sa-good-placeholder.json", 0o600)

        check = _by_target(scp.check_shared_secret_modes(tmp_path), key)
        assert check.ok, check.detail
        assert check.apply is None
        assert check.fix_description == ""

    def test_meta_sidecar_is_enforced(self, tmp_path):
        """The ``*.meta.json`` sidecar (carries the SA client_id) is caught by
        the ``*.json`` match and held to 0600 too."""
        meta = _write(_sa_dir(tmp_path) / "sa-prime-placeholder.meta.json", 0o644)

        check = _by_target(scp.check_shared_secret_modes(tmp_path), meta)
        assert not check.ok
        assert check.apply() is True
        assert stat.S_IMODE(meta.stat().st_mode) == 0o600

    def test_oauth_token_record_is_enforced(self, tmp_path):
        """Path-A per-bot OAuth token records under google_oauth_tokens/ share
        the same 0600 contract."""
        tok = _write(_oauth_dir(tmp_path) / "placeholder-bot.json", 0o640)

        check = _by_target(scp.check_shared_secret_modes(tmp_path), tok)
        assert not check.ok
        assert "0o640" in check.detail
        assert check.apply() is True
        assert stat.S_IMODE(tok.stat().st_mode) == 0o600

    def test_mixed_dir_only_drifted_files_carry_an_apply(self, tmp_path):
        """A dir with one good + one bad key: the good one is ok/no-apply, the
        bad one is drift/has-apply. Proves per-file granularity."""
        good = _write(_sa_dir(tmp_path) / "good.json", 0o600)
        bad = _write(_sa_dir(tmp_path) / "bad.json", 0o644)

        checks = scp.check_shared_secret_modes(tmp_path)
        assert _by_target(checks, good).ok
        assert _by_target(checks, good).apply is None
        assert not _by_target(checks, bad).ok
        assert _by_target(checks, bad).apply is not None

    def test_noop_when_secrets_dirs_absent(self, tmp_path):
        """Pods that never configured Google integration have no secrets/ dirs
        — the check produces nothing (no noise, no crash)."""
        assert scp.check_shared_secret_modes(tmp_path) == []

    def test_empty_secrets_dir_produces_no_checks(self, tmp_path):
        """An existing-but-empty secrets dir is silent too."""
        _sa_dir(tmp_path)
        _oauth_dir(tmp_path)
        assert scp.check_shared_secret_modes(tmp_path) == []

    def test_tmp_staging_file_is_ignored(self, tmp_path):
        """The installers' same-dir ``.json.tmp`` staging file is not a secret
        payload and must not be flagged (it never ends in ``.json``)."""
        _write(_sa_dir(tmp_path) / "sa-prime.json.tmp", 0o644)
        assert scp.check_shared_secret_modes(tmp_path) == []

    def test_subdir_is_ignored(self, tmp_path):
        """A non-regular entry (a directory ending in .json, however unlikely)
        is skipped — only regular files are secret key files."""
        weird = _sa_dir(tmp_path) / "notakey.json"
        weird.mkdir()
        assert scp.check_shared_secret_modes(tmp_path) == []

    def test_symlink_is_flagged_not_auto_repaired(self, tmp_path):
        """A symlink in the evolve-owned 0700 secrets dir is anomalous: flagged
        as drift for operator review, but NOT auto-chmod'd (we never chmod
        through a symlink to an attacker-chosen target)."""
        outside = tmp_path / "outside-target.json"
        _write(outside, 0o644)
        link = _sa_dir(tmp_path) / "evil-link.json"
        link.symlink_to(outside)

        check = _by_target(scp.check_shared_secret_modes(tmp_path), link)
        assert not check.ok
        assert "symlink" in check.detail
        assert check.apply is None
        # The link target's mode must be untouched (no chmod-through).
        assert stat.S_IMODE(outside.stat().st_mode) == 0o644


# ── chmod_shared_secret — the privileged repair primitive ─────────────────────

class TestChmodSharedSecret:

    def test_chmods_regular_file_to_600(self, tmp_path):
        f = _write(tmp_path / "k.json", 0o644)
        assert scp.chmod_shared_secret(f) is True
        assert stat.S_IMODE(f.stat().st_mode) == 0o600

    def test_refuses_to_follow_symlink(self, tmp_path):
        """O_NOFOLLOW: a symlink at the path fails the open (ELOOP) → returns
        False and the link TARGET's mode is untouched. This is the TOCTOU /
        symlink-swap defense."""
        target = _write(tmp_path / "target.json", 0o644)
        link = tmp_path / "link.json"
        link.symlink_to(target)

        assert scp.chmod_shared_secret(link) is False
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_missing_file_returns_false_not_raise(self, tmp_path):
        """A file removed between detection and repair (ENOENT race) returns
        False — surfaced as a loud fix-failure, never an unhandled raise."""
        assert scp.chmod_shared_secret(tmp_path / "gone.json") is False


# ── tighten_shared_secret_tree — the deploy-time re-exposer fix ───────────────
#
# THE root-cause path: deploy_shared_dir runs `chmod -R a+rX {shared_dir}` so
# bots can read each other's output, which ALSO world-reads the 0600 secret keys
# under secrets/ — re-widening a correctly-installed key to 0644 on EVERY deploy.
# tighten_shared_secret_tree re-protects the subtree immediately after that pass.
# These tests reproduce the exact a+rX → re-tighten cycle on a real tmp tree.

class TestTightenSharedSecretTree:

    def _seed(self, shared: Path) -> tuple[Path, Path, Path]:
        sa = _write(_sa_dir(shared) / "sa-prime-placeholder.json", 0o600)
        meta = _write(_sa_dir(shared) / "sa-prime-placeholder.meta.json", 0o600)
        tok = _write(_oauth_dir(shared) / "placeholder-bot.json", 0o600)
        os.chmod(shared / "secrets" / "google_service_accounts", 0o700)
        os.chmod(shared / "secrets" / "google_oauth_tokens", 0o700)
        os.chmod(shared / "secrets", 0o700)
        return sa, meta, tok

    def test_reverses_a_plus_rX_widen_back_to_0600(self, tmp_path):
        """The live failure mode end-to-end: seed 0600 keys, run the SAME
        `chmod -R a+rX` deploy_shared_dir runs (widening every key to 0644),
        then tighten — every key is back to 0600 and the dirs back to 0700."""
        import subprocess

        shared = tmp_path / "shared"
        shared.mkdir()
        sa, meta, tok = self._seed(shared)

        # Reproduce deploy_shared_dir's recursive world-read pass.
        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)
        assert stat.S_IMODE(sa.stat().st_mode) == 0o644, "precondition: a+rX widened the key"
        assert stat.S_IMODE(tok.stat().st_mode) == 0o644

        assert scp.tighten_shared_secret_tree(shared) is True
        assert stat.S_IMODE(sa.stat().st_mode) == 0o600
        assert stat.S_IMODE(meta.stat().st_mode) == 0o600
        assert stat.S_IMODE(tok.stat().st_mode) == 0o600
        # Dirs: a+rX made them 0705; the re-tighten restores 0700 (owner-only).
        assert stat.S_IMODE((shared / "secrets" / "google_service_accounts").stat().st_mode) == 0o700
        assert stat.S_IMODE((shared / "secrets").stat().st_mode) == 0o700

    def test_noop_true_when_no_secrets_dir(self, tmp_path):
        """Non-Google pod (no secrets/): no-op, returns True (not a failure)."""
        shared = tmp_path / "shared"
        shared.mkdir()
        assert scp.tighten_shared_secret_tree(shared) is True

    def test_owner_can_still_read_after_tighten(self, tmp_path):
        """0600 keeps owner read — the evolve MCP bridge (owner) must still be
        able to load the key after the re-tighten."""
        shared = tmp_path / "shared"
        shared.mkdir()
        sa, _, _ = self._seed(shared)
        assert scp.tighten_shared_secret_tree(shared) is True
        assert sa.read_text() == "{}"  # owner read survives 0600


# ── Wiring: ensure_pod_perms runs the shared-secret check pod-wide ────────────

class TestEnsurePodPermsWiring:

    def test_drifted_shared_secret_surfaces_in_ensure_pod_perms(self, tmp_path, monkeypatch):
        """End-to-end through ensure_pod_perms (check_only): a 0644 SA key under
        the pod's sharedDir produces a ``shared-secret-mode`` drift check. This
        proves the pod-wide wiring, not just the unit function."""
        import json

        shared = tmp_path / "shared"
        shared.mkdir()
        key = _write(_sa_dir(shared) / "sa-prime-placeholder.json", 0o644)

        # No bots → the per-bot loop (which would reach real /Users/<bot>
        # paths) doesn't run. Point sharedDir at our tmp tree.
        net = {
            "networkId": "test-pod",
            "sharedDir": str(shared),
            "members": [],
            "bots": {},
        }
        np = tmp_path / "network.json"
        np.write_text(json.dumps(net))
        # Keep the cellar check off the real Homebrew tree.
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")

        result = deploy.ensure_pod_perms(
            bot_id=None, network_path=np, check_only=True,
        )

        sec = [c for c in result.checks
               if c.category == "shared-secret-mode" and c.target == str(key)]
        assert sec, (
            "ensure_pod_perms did not run check_shared_secret_modes; "
            f"categories seen: {sorted({c.category for c in result.checks})}"
        )
        assert not sec[0].ok
        # check_only=True → no fix ran, so the file is still 0644 on disk.
        assert stat.S_IMODE(key.stat().st_mode) == 0o644
        assert result.applied == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
