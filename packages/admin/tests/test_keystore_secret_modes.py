"""tests/test_keystore_secret_modes.py — keystore key-material mode self-heal.

Regression test for the 2026-08-18 live finding on the reference pod:
``{shared_dir}/keystore/.machine-key`` — the Fernet key for the file vault —
was mode **0644**, ``evolve:wheel``. ``sudo -H -u <bot> /bin/cat`` on it
SUCCEEDED from an ordinary bot account, so any compromised or prompt-injected
bot could decrypt ``vault/github_pat.enc`` and recover the pod-wide GitHub PAT.
``keystore/admin-auth.key`` (the HMAC key behind admin device tokens) was 0644
on the same box.

The writers are correct and always were: ``keystore._write_shared_vault_key``
chmods 0640, ``web.admin_auth.ensure_key`` chmods 0600, ``_file_store_set``
chmods each ``vault/*.enc`` 0640. What widened them is
``deploy_shared_dir``'s ``chmod -R a+rX {shared_dir}`` — the SAME re-exposer
that produced the 2026-06-20 Google-SA-key finding (``secrets/``) and the
contact-PII exposure (``directory/``). Both of those got a post-widen
re-tighten plus a per-file ``ensure_pod_perms`` self-heal; ``keystore/`` — the
tree holding the pod's own key material — was never added. This suite pins
that third instance shut, and pins the two shapes that make the keystore fix
DIFFERENT from its two siblings:

  * it is a NAMED per-file table, not a ``chmod -R go-rwx`` subtree sweep,
    because the alert tokens in the same directory are read by other accounts
    by design (``security-cve-scan``'s finalizer runs in a bot app context);
  * the directory contract is separate from the file contract, and GUARDED.
    ``keystore/`` and ``keystore/vault/`` are 0750 — but only once the
    ``user:evo`` dir ACE carries ``search`` (``execute`` in
    ``deploy.EVO_WRITE_ACL_PERMS``). The file-mode PR shipped them at 0755
    because that ACE had no ``search`` and 0750 would have cost evo vault
    traversal; the follow-up added the ACE bit and the 0750 tighten together.
    The guard (``_evo_can_traverse``) is what keeps the two from ever landing
    out of order, since the wrong order is silent: ``_file_store_get``
    swallows the PermissionError and returns None, surfacing only as
    "no token in keystore slot".

Mirrors the style of ``test_shared_secret_modes.py`` /
``test_shared_directory_modes.py``. Placeholder refs per
docs/PLACEHOLDER_NAMING.md; no real keys.
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

from evolve_admin import deploy, secret_config_perms as scp  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _keystore(shared: Path) -> Path:
    d = shared / "keystore"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, mode: int, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)
    return path


def _machine_key(shared: Path, mode: int = 0o640) -> Path:
    return _write(_keystore(shared) / ".machine-key", mode)


def _admin_auth_key(shared: Path, mode: int = 0o600) -> Path:
    return _write(_keystore(shared) / "admin-auth.key", mode)


def _vault_entry(shared: Path, name: str = "github_pat", mode: int = 0o640) -> Path:
    return _write(_keystore(shared) / "vault" / f"{name}.enc", mode)


def _by_target(checks, path: Path):
    return [c for c in checks if c.target == str(path)]


def _modes(shared: Path) -> dict[str, int]:
    return {p.name: stat.S_IMODE(p.stat().st_mode)
            for p, _m, _w in scp.keystore_protected_targets(shared)}


# ── keystore_protected_targets — the contract table ───────────────────────────

class TestProtectedTargets:

    def test_lists_machine_key_admin_auth_key_and_vault_entries(self, tmp_path):
        shared = tmp_path / "shared"
        mk = _machine_key(shared)
        ak = _admin_auth_key(shared)
        pat = _vault_entry(shared, "github_pat")
        prev = _vault_entry(shared, "github_intake__prev")

        targets = scp.keystore_protected_targets(shared)
        by_path = {p: (m, w) for p, m, w in targets}

        assert by_path[mk][0] == 0o640
        assert by_path[ak][0] == 0o600
        assert by_path[pat][0] == 0o640
        assert by_path[prev][0] == 0o640, "__prev blobs are ciphertext too"

    def test_absent_files_are_omitted_not_reported(self, tmp_path):
        """A pod that never paired has no admin-auth.key and one that never
        wrote a vault entry has no .machine-key — both legitimate, neither is
        drift."""
        shared = tmp_path / "shared"
        _keystore(shared)
        assert scp.keystore_protected_targets(shared) == []
        assert scp.check_keystore_secret_modes(shared) == []

    def test_no_keystore_dir_at_all_is_a_noop(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        assert scp.keystore_protected_targets(shared) == []
        assert scp.check_keystore_secret_modes(shared) == []

    def test_bot_readable_keystore_files_are_not_in_the_table(self, tmp_path):
        """THE load-bearing exclusion. The alert tokens (and their chat-ids) are
        read by ``security-cve-scan``'s finalizer in a BOT app context and by
        the external liveness probe, so a 0600 clamp would take the alert
        channel offline. They must never appear in the protected table.

        ``evolve-signing.key`` used to be listed here for a reason that expired
        on 2026-08-18 — see
        ``test_retired_signing_key_is_clamped_now_that_nothing_reads_it``."""
        shared = tmp_path / "shared"
        ks = _keystore(shared)
        _machine_key(shared)
        excluded = [
            _write(ks / "security-alert-token", 0o644),
            _write(ks / "security-alert-chat-id", 0o644),
            _write(ks / "watchdog-alert-token", 0o644),
            _write(ks / "watchdog-alert-chat-id", 0o644),
            _write(ks / "keys.json", 0o644, "{}"),
        ]
        targeted = {p for p, _m, _w in scp.keystore_protected_targets(shared)}
        for path in excluded:
            assert path not in targeted, f"{path.name} must stay bot-readable"

    def test_retired_signing_key_is_clamped_now_that_nothing_reads_it(
        self, tmp_path
    ):
        """The INVERSION of the old exclusion, and the reason for it.

        ``evolve-signing.key`` was excluded because the symmetric HMAC secret
        had to be readable by ``analyzer/apply.py``, which ran as the BOT user.
        That daemon, the sign/verify surface and the key-generation step were
        all removed on 2026-08-18
        (internal/design-proposal-signing-key-2026-08-18.md), so no reader survives
        and the exception's whole premise is gone. Pre-2026-08-18 pods still
        carry the file at 0755, so it now belongs IN the table: every deploy and
        every hourly drift pass self-heals it to 0600, instead of the fix
        depending on an operator remembering a manual ``rm``."""
        shared = tmp_path / "shared"
        ks = _keystore(shared)
        signing = _write(ks / "evolve-signing.key", 0o755)

        by_path = {p: m for p, m, _w in scp.keystore_protected_targets(shared)}
        assert by_path[signing] == 0o600

        checks = scp.check_keystore_secret_modes(shared)
        drift = [c for c in checks if c.target == str(signing)]
        assert len(drift) == 1
        assert drift[0].ok is False
        assert "600" in drift[0].fix_description

        assert scp.tighten_keystore_secrets(shared) is True
        assert stat.S_IMODE(signing.stat().st_mode) == 0o600

    def test_a_pod_without_the_retired_key_sees_no_entry(self, tmp_path):
        """Fresh pods never generate the key, so the entry must be inert there
        rather than reporting a missing-file drift."""
        shared = tmp_path / "shared"
        _keystore(shared)
        _machine_key(shared)
        targeted = {p.name for p, _m, _w in scp.keystore_protected_targets(shared)}
        assert "evolve-signing.key" not in targeted

    def test_tighten_leaves_the_bot_readable_files_alone(self, tmp_path):
        """Not just absent from the table — actually untouched by the sweep.
        A ``chmod -R go-rwx keystore/`` would pass the table test above and
        still break the alert channel; this catches that."""
        shared = tmp_path / "shared"
        ks = _keystore(shared)
        _machine_key(shared, 0o644)
        token = _write(ks / "security-alert-token", 0o644)

        assert scp.tighten_keystore_secrets(shared) is True

        assert stat.S_IMODE(token.stat().st_mode) == 0o644


# ── check_keystore_secret_modes — detection ───────────────────────────────────

class TestCheckKeystoreSecretModes:

    def test_world_readable_machine_key_is_drift(self, tmp_path):
        """THE live finding: 0644 .machine-key → a failing check with a chmod
        640 repair."""
        shared = tmp_path / "shared"
        mk = _machine_key(shared, 0o644)

        (check,) = _by_target(scp.check_keystore_secret_modes(shared), mk)

        assert not check.ok
        assert check.category == "keystore-mode"
        assert "0o644" in check.detail
        assert "GitHub PAT" in check.detail
        assert check.fix_description == f"chmod 640 {mk}"

    def test_world_readable_admin_auth_key_is_drift(self, tmp_path):
        shared = tmp_path / "shared"
        ak = _admin_auth_key(shared, 0o644)

        (check,) = _by_target(scp.check_keystore_secret_modes(shared), ak)

        assert not check.ok
        assert check.fix_description == f"chmod 600 {ak}"

    def test_world_readable_vault_entry_is_drift(self, tmp_path):
        shared = tmp_path / "shared"
        pat = _vault_entry(shared, "github_pat", 0o644)

        (check,) = _by_target(scp.check_keystore_secret_modes(shared), pat)

        assert not check.ok
        assert check.fix_description == f"chmod 640 {pat}"

    def test_contract_modes_pass_clean(self, tmp_path):
        shared = tmp_path / "shared"
        _machine_key(shared, 0o640)
        _admin_auth_key(shared, 0o600)
        _vault_entry(shared, "github_pat", 0o640)

        checks = scp.check_keystore_secret_modes(shared)

        assert len(checks) == 3
        assert all(c.ok for c in checks)
        assert all(c.apply is None for c in checks)

    def test_0600_machine_key_is_drift_not_a_pass(self, tmp_path):
        """Tighter is NOT automatically fine here. On Linux the group triad IS
        the POSIX-ACL mask, so 0600 zeroes it and strands the ``user:evo`` read
        ACE — the ``no token in keystore slot`` bug the shared-key location
        exists to prevent. The contract is EXACTLY 0640."""
        shared = tmp_path / "shared"
        mk = _machine_key(shared, 0o600)

        (check,) = _by_target(scp.check_keystore_secret_modes(shared), mk)

        assert not check.ok
        assert check.fix_description == f"chmod 640 {mk}"

    def test_symlink_is_flagged_but_never_repaired(self, tmp_path):
        shared = tmp_path / "shared"
        outside = tmp_path / "elsewhere"
        _write(outside, 0o644)
        ks = _keystore(shared)
        link = ks / ".machine-key"
        link.symlink_to(outside)

        (check,) = _by_target(scp.check_keystore_secret_modes(shared), link)

        assert not check.ok
        assert "symlink" in check.detail
        assert check.apply is None
        assert stat.S_IMODE(outside.stat().st_mode) == 0o644  # untouched

    def test_foreign_owner_is_flagged_but_never_repaired(self, tmp_path, monkeypatch):
        """A 0640 file owned by someone other than evolve is not contained by a
        chmod, so the check reports it for operator review rather than pretending
        a mode fix helped."""
        shared = tmp_path / "shared"
        mk = _machine_key(shared, 0o640)
        # Pretend evolve is some other uid than the one that owns the tmp file.
        monkeypatch.setattr(scp, "_evolve_uid", lambda: mk.stat().st_uid + 1)

        (check,) = _by_target(scp.check_keystore_secret_modes(shared), mk)

        assert not check.ok
        assert "owner uid=" in check.detail
        assert check.apply is None

    def test_unresolvable_evolve_user_skips_the_owner_assertion(self, tmp_path, monkeypatch):
        """On a dev box / pre-``setup evolve-user`` pod there is no evolve
        account; the owner check must skip, not false-fire on every file."""
        shared = tmp_path / "shared"
        _machine_key(shared, 0o640)
        monkeypatch.setattr(scp, "_evolve_uid", lambda: scp._EVOLVE_UID_UNRESOLVED)

        checks = scp.check_keystore_secret_modes(shared)

        assert len(checks) == 1 and checks[0].ok


# ── tighten_keystore_secrets — the repair ─────────────────────────────────────

class TestTightenKeystoreSecrets:

    def test_repairs_every_target_from_the_a_plus_rx_state(self, tmp_path):
        shared = tmp_path / "shared"
        _machine_key(shared, 0o644)
        _admin_auth_key(shared, 0o644)
        _vault_entry(shared, "github_pat", 0o644)

        assert scp.tighten_keystore_secrets(shared) is True

        assert _modes(shared) == {
            ".machine-key": 0o640,
            "admin-auth.key": 0o600,
            "github_pat.enc": 0o640,
        }

    def test_idempotent(self, tmp_path):
        shared = tmp_path / "shared"
        _machine_key(shared, 0o644)
        assert scp.tighten_keystore_secrets(shared) is True
        assert scp.tighten_keystore_secrets(shared) is True
        assert _modes(shared) == {".machine-key": 0o640}

    def test_check_apply_callable_repairs_to_the_contract_mode(self, tmp_path):
        """The ensure_pod_perms apply path, not just the bulk sweep — each
        check must carry ITS OWN mode, not a shared default."""
        shared = tmp_path / "shared"
        mk = _machine_key(shared, 0o644)
        ak = _admin_auth_key(shared, 0o644)

        for check in scp.check_keystore_secret_modes(shared):
            assert check.apply() is True

        assert stat.S_IMODE(mk.stat().st_mode) == 0o640
        assert stat.S_IMODE(ak.stat().st_mode) == 0o600

    def test_symlinked_target_is_not_chmodded_through(self, tmp_path):
        shared = tmp_path / "shared"
        outside = _write(tmp_path / "elsewhere", 0o644)
        (_keystore(shared) / ".machine-key").symlink_to(outside)

        assert scp.tighten_keystore_secrets(shared) is False  # ELOOP, loudly
        assert stat.S_IMODE(outside.stat().st_mode) == 0o644


# ── The re-exposer → re-tighten cycle (the actual deploy sequence) ────────────

class TestDeployWidenCycle:

    def test_a_plus_rx_widens_the_machine_key_and_the_tighten_restores_it(self, tmp_path):
        """Reproduce the real cause end to end: a correctly-written 0640 key,
        the deploy's ``chmod -R a+rX {shared_dir}``, and the compensating
        re-tighten. Without the tighten the key sits at 0644 — exactly what was
        measured on the reference pod."""
        shared = tmp_path / "shared"
        mk = _machine_key(shared, 0o640)          # as _write_shared_vault_key leaves it
        pat = _vault_entry(shared, "github_pat", 0o640)

        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)
        assert stat.S_IMODE(mk.stat().st_mode) == 0o644, "the live finding"
        assert stat.S_IMODE(pat.stat().st_mode) == 0o644

        assert scp.tighten_shared_protected_trees(shared) is True
        assert stat.S_IMODE(mk.stat().st_mode) == 0o640
        assert stat.S_IMODE(pat.stat().st_mode) == 0o640

    def test_installer_style_r755_widen_is_also_repaired(self, tmp_path):
        """The setup-time ``chmod -R 755`` pass is the second re-exposer, and a
        worse one — it adds o+x on top of o+r."""
        shared = tmp_path / "shared"
        mk = _machine_key(shared, 0o640)

        subprocess.run(["/bin/chmod", "-R", "755", str(shared)], capture_output=True)
        assert stat.S_IMODE(mk.stat().st_mode) == 0o755

        assert scp.tighten_shared_protected_trees(shared) is True
        assert stat.S_IMODE(mk.stat().st_mode) == 0o640

    def test_composed_tighten_still_covers_secrets_and_directory(self, tmp_path):
        """The rename from ``tighten_shared_pii_trees`` must not drop a tree."""
        shared = tmp_path / "shared"
        sa = _write(shared / "secrets" / "google_service_accounts" / "k.json", 0o600, "{}")
        row = _write(shared / "directory" / "team-bot-a.json", 0o600, "{}")
        mk = _machine_key(shared, 0o640)

        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)
        assert scp.tighten_shared_protected_trees(shared) is True

        assert stat.S_IMODE(sa.stat().st_mode) == 0o600
        assert stat.S_IMODE(row.stat().st_mode) == 0o600
        assert stat.S_IMODE(mk.stat().st_mode) == 0o640


# ── Round-trip against the REAL keystore writer ───────────────────────────────

class TestRealKeystoreRoundTrip:

    def test_real_vault_write_survives_widen_plus_tighten(self, tmp_path, monkeypatch):
        """Write through the real ``keystore._file_store_set`` (so the key and
        the ciphertext are produced by production code, at production modes),
        run the deploy widen, re-tighten, and confirm the value still decrypts.
        Guards the fix against the failure mode that would matter most: a
        tighten that protects the key but breaks the vault."""
        pytest.importorskip("cryptography")
        from evolve_admin import keystore as ks_mod

        shared = tmp_path / "shared"
        vault = shared / "keystore" / "vault"
        # Keep the legacy per-home migration path out of this (it would copy a
        # real developer key on a laptop run).
        monkeypatch.setattr(ks_mod, "_LEGACY_VAULT_KEY_FILE", tmp_path / "absent")

        ks_mod._file_store_set(vault, "github_pat", "ghp_placeholder_value")
        mk = shared / "keystore" / ".machine-key"
        assert stat.S_IMODE(mk.stat().st_mode) == 0o640  # writer is correct

        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)
        assert stat.S_IMODE(mk.stat().st_mode) == 0o644  # deploy re-widened it

        assert scp.tighten_keystore_secrets(shared) is True
        assert stat.S_IMODE(mk.stat().st_mode) == 0o640
        assert ks_mod._file_store_get(vault, "github_pat") == "ghp_placeholder_value"


# ── Wiring: ensure_pod_perms runs the keystore check pod-wide ─────────────────

class TestEnsurePodPermsWiring:

    def test_drifted_machine_key_surfaces_in_ensure_pod_perms(self, tmp_path, monkeypatch):
        """End-to-end through ensure_pod_perms (check_only): a 0644 machine key
        under the pod's sharedDir produces a ``keystore-mode`` drift check.
        This is the assertion the original gap failed — the code was right, the
        self-heal was never wired."""
        shared = tmp_path / "shared"
        mk = _machine_key(shared, 0o644)

        np = tmp_path / "network.json"
        np.write_text(json.dumps({
            "networkId": "test-pod",
            "sharedDir": str(shared),
            "members": [],
            "bots": {},
        }))
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")

        result = deploy.ensure_pod_perms(
            bot_id=None, network_path=np, check_only=True)

        hits = [c for c in result.checks
                if c.category == "keystore-mode" and c.target == str(mk)]
        assert hits, (
            "ensure_pod_perms did not run check_keystore_secret_modes; "
            f"categories seen: {sorted({c.category for c in result.checks})}"
        )
        assert not hits[0].ok
        assert stat.S_IMODE(mk.stat().st_mode) == 0o644  # check_only → no fix ran
        assert result.applied == []

    def test_apply_mode_repairs_the_machine_key(self, tmp_path, monkeypatch):
        """The self-heal actually fires under ``check_only=False`` — the
        property that makes every future deploy converge the mode back."""
        shared = tmp_path / "shared"
        mk = _machine_key(shared, 0o644)

        np = tmp_path / "network.json"
        np.write_text(json.dumps({
            "networkId": "test-pod",
            "sharedDir": str(shared),
            "members": [],
            "bots": {},
        }))
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")

        result = deploy.ensure_pod_perms(
            bot_id=None, network_path=np, check_only=False)

        assert stat.S_IMODE(mk.stat().st_mode) == 0o640
        assert any("keystore-mode" in a and ".machine-key" in a
                   for a in result.applied), result.applied


# ── Directory contract: the 0750 half + the evo-traverse guard ────────────────
#
# Every test here pins ``_evo_can_traverse`` (or the two seams under it)
# explicitly. The real guard consults ``pwd.getpwnam("evo")`` and the on-disk
# ACL, both of which differ between the reference pod (evo exists) and CI / a
# dev laptop (it does not) — an unpinned test would pass on one and fail on the
# other.

def _dirs(shared: Path) -> dict[str, int]:
    return {p.name: stat.S_IMODE(p.stat().st_mode)
            for p, _m, _w in scp.keystore_protected_dirs(shared)}


class TestKeystoreDirTable:

    def test_lists_the_vault_only(self, tmp_path):
        shared = tmp_path / "shared"
        _vault_entry(shared)  # creates keystore/ and keystore/vault/

        by_path = {p: (m, w) for p, m, w in scp.keystore_protected_dirs(shared)}

        assert by_path[shared / "keystore" / "vault"][0] == 0o750
        assert len(by_path) == 1

    def test_keystore_root_is_never_in_the_table(self, tmp_path):
        """THE #3700 REGRESSION. ``keystore/`` is a SHARED directory — see
        KEYSTORE_BOT_READABLE_FILES. A 0750 on the root strands every one of
        those bot reads no matter what the file modes say, because traversal is
        checked before the file mode is consulted — taking the CVE-scan
        finalizer's alert channel and the external liveness probe offline."""
        shared = tmp_path / "shared"
        _vault_entry(shared)

        listed = [p for p, _m, _w in scp.keystore_protected_dirs(shared)]

        assert shared / "keystore" not in listed
        assert not any(r == scp.KEYSTORE_SUBDIR for r, _m, _w in scp.KEYSTORE_DIR_MODES)

    def test_bot_readable_files_stay_reachable_after_a_full_tighten(self, tmp_path, monkeypatch):
        """The property the table exists to preserve, asserted end-to-end: run
        every keystore tighten, then confirm each bot-readable file is still
        reachable through its parent directories.

        Reachability, not mode: the bug was never in a file's own bits. Checked
        as a non-owner would experience it — every directory on the path must
        keep its world-execute bit."""
        shared = tmp_path / "shared"
        _machine_key(shared)
        _vault_entry(shared)
        for name in scp.KEYSTORE_BOT_READABLE_FILES:
            _write(_keystore(shared) / name, 0o644)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        assert scp.tighten_shared_protected_trees(shared) is True

        ks = shared / "keystore"
        assert stat.S_IMODE(ks.stat().st_mode) & 0o001, (
            "keystore/ lost world-execute — every bot read below it is stranded"
        )
        for name in scp.KEYSTORE_BOT_READABLE_FILES:
            f = ks / name
            assert stat.S_IMODE(f.stat().st_mode) & 0o004, f"{name} lost world-read"
        # ...while the vault DID tighten.
        assert stat.S_IMODE((ks / "vault").stat().st_mode) == 0o750

    def test_absent_vault_yields_nothing(self, tmp_path):
        """A pod that never wrote an entry has no vault/ — legitimate, not a
        finding. Same contract as the file table."""
        shared = tmp_path / "shared"
        _machine_key(shared)

        assert scp.keystore_protected_dirs(shared) == []

    def test_absent_keystore_yields_nothing(self, tmp_path):
        assert scp.keystore_protected_dirs(tmp_path / "shared") == []

    def test_a_file_where_the_dir_belongs_is_not_listed(self, tmp_path):
        """``is_dir`` not ``exists``: a regular file at keystore/vault would
        otherwise be chmod'd 0750 by the tighten."""
        shared = tmp_path / "shared"
        _write(_keystore(shared) / "vault", 0o644)

        assert scp.keystore_protected_dirs(shared) == []


class TestEvoTraverseGuard:

    def test_true_when_no_evo_account(self, tmp_path, monkeypatch):
        """Pre-separation pods have nothing to strand — tighten freely."""
        monkeypatch.setattr(scp, "_evo_user_exists", lambda: False)

        assert scp._evo_can_traverse(tmp_path) is True

    def test_true_when_ace_covers_execute(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scp, "_evo_user_exists", lambda: True)
        monkeypatch.setattr(scp, "_get_perms", lambda: _FakePerms(answer=True))

        assert scp._evo_can_traverse(tmp_path) is True

    def test_false_when_ace_lacks_execute(self, tmp_path, monkeypatch):
        """The exact shape measured on the reference pod 2026-08-18:
        ``user:evo allow list,add_file,delete,add_subdirectory,…`` — no
        ``search``. Tightening here is what would strand the gateway."""
        monkeypatch.setattr(scp, "_evo_user_exists", lambda: True)
        monkeypatch.setattr(scp, "_get_perms", lambda: _FakePerms(answer=False))

        assert scp._evo_can_traverse(tmp_path) is False

    def test_asks_for_execute_on_the_evo_user(self, tmp_path, monkeypatch):
        """Pins the query itself: ``execute`` is the verb the seam maps to
        macOS ``search`` / POSIX ``x``. Asking for anything else would make the
        guard answer a different question than the one that matters."""
        fake = _FakePerms(answer=True)
        monkeypatch.setattr(scp, "_evo_user_exists", lambda: True)
        monkeypatch.setattr(scp, "_get_perms", lambda: fake)

        scp._evo_can_traverse(tmp_path)

        assert fake.calls == [(tmp_path, "evo", "execute")]

    def test_false_when_the_acl_read_raises(self, tmp_path, monkeypatch):
        """Fail SAFE: unprovable means don't tighten. A dir left 0755 leaks
        file names; a dir wrongly 0750 breaks ``evo intake promote``."""
        monkeypatch.setattr(scp, "_evo_user_exists", lambda: True)
        monkeypatch.setattr(scp, "_get_perms", lambda: _FakePerms(raises=True))

        assert scp._evo_can_traverse(tmp_path) is False


class _FakePerms:
    """Minimal stand-in for the Perms seam — records the traverse query."""

    def __init__(self, answer: bool = True, raises: bool = False):
        self._answer = answer
        self._raises = raises
        self.calls: list[tuple] = []

    def acl_user_effective(self, path, user, required):
        self.calls.append((path, user, required))
        if self._raises:
            raise OSError("getfacl unavailable")
        return self._answer


class TestTightenKeystoreDirs:

    def test_tightens_the_vault_to_0750(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        _vault_entry(shared)
        os.chmod(shared / "keystore" / "vault", 0o755)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        assert scp.tighten_keystore_dirs(shared) is True
        assert _dirs(shared) == {"vault": 0o750}

    def test_leaves_the_keystore_root_alone(self, tmp_path, monkeypatch):
        """#3700 regression: the tighten must not touch the shared root, whose
        world-execute bit carries every bot read below it."""
        shared = tmp_path / "shared"
        _vault_entry(shared)
        os.chmod(shared / "keystore", 0o755)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        scp.tighten_keystore_dirs(shared)

        assert stat.S_IMODE((shared / "keystore").stat().st_mode) == 0o755

    def test_idempotent(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        _vault_entry(shared)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        scp.tighten_keystore_dirs(shared)
        assert scp.tighten_keystore_dirs(shared) is True
        assert _dirs(shared) == {"vault": 0o750}

    def test_guard_denial_leaves_the_mode_alone(self, tmp_path, monkeypatch):
        """THE guard regression. A denied tighten must be a no-op on disk."""
        shared = tmp_path / "shared"
        _vault_entry(shared)
        os.chmod(shared / "keystore" / "vault", 0o755)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: False)

        assert scp.tighten_keystore_dirs(shared) is False
        assert _dirs(shared) == {"vault": 0o755}

    def test_missing_tree_is_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        assert scp.tighten_keystore_dirs(tmp_path / "shared") is True

    def test_does_not_chmod_through_a_symlink(self, tmp_path, monkeypatch):
        """The privileged-path property ``chmod_shared_secret`` exists for:
        ``O_NOFOLLOW`` means a link planted where the vault belongs fails the
        open (ELOOP) instead of redirecting a 0750 onto its target."""
        shared = tmp_path / "shared"
        _keystore(shared)
        victim = tmp_path / "victim"
        victim.mkdir()
        os.chmod(victim, 0o755)
        (shared / "keystore" / "vault").symlink_to(victim)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        assert scp.tighten_keystore_dirs(shared) is False
        assert stat.S_IMODE(victim.stat().st_mode) == 0o755


class TestCheckKeystoreDirModes:

    def test_ok_at_0750(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        _vault_entry(shared)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)
        scp.tighten_keystore_dirs(shared)

        checks = scp.check_keystore_dir_modes(shared)

        assert checks and all(c.ok for c in checks)
        assert all(c.category == "keystore-dir-mode" for c in checks)

    def test_never_emits_a_check_for_the_keystore_root(self, tmp_path, monkeypatch):
        """#3700 regression at the check layer: no check means no drift report
        and no repair offer for the shared root."""
        shared = tmp_path / "shared"
        _vault_entry(shared)
        os.chmod(shared / "keystore", 0o755)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        checks = scp.check_keystore_dir_modes(shared)

        assert _by_target(checks, shared / "keystore") == []

    def test_0755_is_drift_with_a_repair(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        _vault_entry(shared)
        vault = shared / "keystore" / "vault"
        os.chmod(vault, 0o755)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        hit = _by_target(scp.check_keystore_dir_modes(shared), vault)[0]

        assert not hit.ok
        assert "0o755" in hit.detail and "0o750" in hit.detail
        assert hit.apply is not None
        assert hit.apply() is True
        assert stat.S_IMODE(vault.stat().st_mode) == 0o750

    def test_guard_denial_reports_drift_but_offers_no_apply(self, tmp_path, monkeypatch):
        """Reported LOUDLY, never auto-chmod'd, and the detail names the actual
        blocker so an operator reads "fix the ACE", not "chmod harder"."""
        shared = tmp_path / "shared"
        _vault_entry(shared)
        vault = shared / "keystore" / "vault"
        os.chmod(vault, 0o755)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: False)

        hit = _by_target(scp.check_keystore_dir_modes(shared), vault)[0]

        assert not hit.ok
        assert hit.apply is None
        assert "execute/search" in hit.detail
        assert "no token in keystore slot" in hit.detail
        assert stat.S_IMODE(vault.stat().st_mode) == 0o755

    def test_apply_re_checks_the_guard_at_fix_time(self, tmp_path, monkeypatch):
        """The apply closure is built while the guard says yes but RUNS later
        (ensure_pod_perms defers every fix). If the ACE went away in between,
        the fix must decline rather than strand evo."""
        shared = tmp_path / "shared"
        _vault_entry(shared)
        vault = shared / "keystore" / "vault"
        os.chmod(vault, 0o755)
        allowed = {"v": True}
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: allowed["v"])

        hit = _by_target(scp.check_keystore_dir_modes(shared), vault)[0]
        allowed["v"] = False  # ACE lost between detection and repair

        assert hit.apply() is False
        assert stat.S_IMODE(vault.stat().st_mode) == 0o755

    def test_symlinked_vault_is_flagged_not_chmodded(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        _keystore(shared)
        real = tmp_path / "elsewhere"
        real.mkdir()
        os.chmod(real, 0o755)
        (shared / "keystore" / "vault").symlink_to(real)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        hit = _by_target(scp.check_keystore_dir_modes(shared),
                         shared / "keystore" / "vault")[0]

        assert not hit.ok
        assert "symlink" in hit.detail
        assert hit.apply is None
        assert stat.S_IMODE(real.stat().st_mode) == 0o755

    def test_owner_drift_is_reported_never_auto_repaired(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        _vault_entry(shared)
        vault = shared / "keystore" / "vault"
        monkeypatch.setattr(scp, "_evolve_uid", lambda: vault.stat().st_uid + 1)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        hit = _by_target(scp.check_keystore_dir_modes(shared), vault)[0]

        assert not hit.ok
        assert "owner uid=" in hit.detail
        assert hit.apply is None

    def test_missing_tree_yields_no_checks(self, tmp_path):
        assert scp.check_keystore_dir_modes(tmp_path / "shared") == []


class TestDirWidenCycle:

    def test_a_plus_rX_widens_the_vault_and_the_retighten_restores_0750(
            self, tmp_path, monkeypatch):
        """The full deploy sequence for the directory half: ``chmod -R a+rX``
        re-adds o+x to the vault, ``tighten_shared_protected_trees`` takes it
        back off — while the shared root keeps its world-execute throughout."""
        shared = tmp_path / "shared"
        _machine_key(shared)
        _vault_entry(shared)
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)
        scp.tighten_keystore_dirs(shared)
        assert _dirs(shared) == {"vault": 0o750}

        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)
        assert _dirs(shared) == {"vault": 0o755}

        assert scp.tighten_shared_protected_trees(shared) is True
        assert _dirs(shared) == {"vault": 0o750}
        assert _modes(shared) == {".machine-key": 0o640, "github_pat.enc": 0o640}
        assert stat.S_IMODE((shared / "keystore").stat().st_mode) & 0o001

    def test_real_vault_still_decrypts_behind_a_0750_vault_dir(self, tmp_path, monkeypatch):
        """The property that actually matters: after the directory tighten the
        production reader still round-trips. As the process's OWN user, which is
        the best a unit test can do; the cross-user half (evo through a 0750 dir
        on a real ACL) was probed live — see the section docstring."""
        pytest.importorskip("cryptography")
        from evolve_admin import keystore as ks_mod

        shared = tmp_path / "shared"
        vault = shared / "keystore" / "vault"
        monkeypatch.setattr(ks_mod, "_LEGACY_VAULT_KEY_FILE", tmp_path / "absent")
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        ks_mod._file_store_set(vault, "github_pat", "ghp_placeholder_value")
        subprocess.run(["/bin/chmod", "-R", "a+rX", str(shared)], capture_output=True)

        assert scp.tighten_shared_protected_trees(shared) is True

        assert _dirs(shared) == {"vault": 0o750}
        assert ks_mod._file_store_get(vault, "github_pat") == "ghp_placeholder_value"


class TestEvoAclContract:
    """The ACE constant itself — the prerequisite the tighten depends on."""

    def test_evo_write_acl_grants_execute(self):
        """Without ``execute`` the whole directory contract is a lockout. Pinned
        as a constant assertion because the failure is silent on a live pod."""
        assert "execute" in deploy.EVO_WRITE_ACL_PERMS.split(",")

    def test_audit_tool_mirror_stays_in_sync(self):
        """``tools/audit_pod_acls`` keeps its own copy of the tuple; a drifted
        mirror means the auditor green-lights an ACE the deploy would repair."""
        from evolve_admin.tools.audit_pod_acls import (
            EVO_WRITE_ACL_PERMS as audit_perms,
        )

        assert set(audit_perms) == set(deploy.EVO_WRITE_ACL_PERMS.split(","))

    def test_keystore_is_in_the_evo_write_contract(self):
        """The traverse ACE reaches keystore/ only because it is in this list."""
        assert "keystore" in deploy.EVO_WRITE_SHARED_SUBDIRS


class TestEnsurePodPermsDirWiring:

    def test_dir_drift_surfaces_in_ensure_pod_perms(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        _vault_entry(shared)
        os.chmod(shared / "keystore" / "vault", 0o755)
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        np = tmp_path / "network.json"
        np.write_text(json.dumps({
            "networkId": "test-pod", "sharedDir": str(shared),
            "members": [], "bots": {},
        }))

        result = deploy.ensure_pod_perms(
            bot_id=None, network_path=np, check_only=True)

        hits = [c for c in result.checks
                if c.category == "keystore-dir-mode"
                and c.target == str(shared / "keystore" / "vault")]
        assert hits, (
            "ensure_pod_perms did not run check_keystore_dir_modes; "
            f"categories seen: {sorted({c.category for c in result.checks})}"
        )
        assert not hits[0].ok
        assert stat.S_IMODE((shared / "keystore" / "vault").stat().st_mode) == 0o755

    def test_apply_mode_repairs_the_dir_mode(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        _vault_entry(shared)
        os.chmod(shared / "keystore" / "vault", 0o755)
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: True)

        np = tmp_path / "network.json"
        np.write_text(json.dumps({
            "networkId": "test-pod", "sharedDir": str(shared),
            "members": [], "bots": {},
        }))

        result = deploy.ensure_pod_perms(
            bot_id=None, network_path=np, check_only=False)

        assert _dirs(shared) == {"vault": 0o750}
        assert any("keystore-dir-mode" in a for a in result.applied), result.applied

    def test_guarded_dir_is_not_repaired_even_in_apply_mode(self, tmp_path, monkeypatch):
        """End-to-end proof of the ordering property: a pod whose evo ACE has
        not yet been repaired keeps its 0755 dirs through a full apply pass,
        rather than converging to a state that strands the gateway."""
        shared = tmp_path / "shared"
        _vault_entry(shared)
        os.chmod(shared / "keystore" / "vault", 0o755)
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        monkeypatch.setattr(scp, "_evo_can_traverse", lambda p: False)

        np = tmp_path / "network.json"
        np.write_text(json.dumps({
            "networkId": "test-pod", "sharedDir": str(shared),
            "members": [], "bots": {},
        }))

        deploy.ensure_pod_perms(bot_id=None, network_path=np, check_only=False)

        assert stat.S_IMODE((shared / "keystore" / "vault").stat().st_mode) == 0o755


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
