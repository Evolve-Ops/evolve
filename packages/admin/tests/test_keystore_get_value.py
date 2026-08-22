"""tests/test_keystore_get_value.py — KeystoreManager.get_value smoke test.

Guards against silent breakage of the absolute-import fallback in
``keystore._import_keystore_cls`` / ``_now_iso_from_cost``. The original
``from .cost import …`` resolves to ``evolve_admin.cost`` which doesn't
exist; the fallback lets the real cost.py (on the analyzer path) be
located. If anyone reverts that, this test breaks loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_keystore_manager_constructs(tmp_path):
    """Constructor must import cost.Keystore without raising."""
    from evolve_admin.keystore import KeystoreManager

    mgr = KeystoreManager(tmp_path)
    assert mgr.ks is not None


def test_get_value_returns_none_for_unregistered(tmp_path):
    from evolve_admin.keystore import KeystoreManager

    mgr = KeystoreManager(tmp_path)
    assert mgr.get_value("nothing_here") is None


def test_get_value_returns_stored_value(tmp_path, monkeypatch):
    """Round-trip: register, set, read back.

    We stub out keychain detection so the test always exercises the
    file-vault path — gives deterministic behavior across machines.
    The vault key auto-lands at ``{tmp_path}/keystore/.machine-key``
    via :func:`evolve_admin.keystore._shared_vault_key_path`.
    """
    from evolve_admin import keystore as ks_mod
    from evolve_admin.keystore import KeystoreManager

    monkeypatch.setattr(ks_mod, "_has_security_cmd", lambda: False)

    mgr = KeystoreManager(tmp_path)
    mgr.register(
        "github_intake",
        provider="github",
        scope="shared",
        description="test",
        bots=None,
        value="ghp_test_value",
    )

    assert mgr.get_value("github_intake") == "ghp_test_value"
    # The shared key lives at {shared_dir}/keystore/.machine-key, not in
    # the calling user's home. Regression guard for the evo-account-
    # separation fix shipped 2026-06-04.
    assert (tmp_path / "keystore" / ".machine-key").exists()


def test_vault_decrypts_across_simulated_users(tmp_path, monkeypatch):
    """Reproduces the evo-vs-evolve decryption mismatch.

    Pre-fix: the vault key lived at ``Path.home()/.openclaw/.evolve-
    vault-key``. Daemon (uid=evolve) wrote token with key in evolve's
    home; subprocess (uid=evo, different HOME) generated a NEW random
    key in its own home, XOR-"decrypted" the file with the wrong key,
    and got garbage that failed ``.decode()``. The except caught it
    and returned None — manifested as ``no token in keystore slot
    'github_intake'`` while the UI saw the token fine.

    Post-fix: both readers go through ``{shared_dir}/keystore/
    .machine-key``, so the round-trip works regardless of HOME. This
    test simulates the cross-uid case by flipping HOME between the
    write and the read.
    """
    from evolve_admin import keystore as ks_mod
    from evolve_admin.keystore import KeystoreManager

    monkeypatch.setattr(ks_mod, "_has_security_cmd", lambda: False)

    home_a = tmp_path / "home_a"
    home_a.mkdir()
    home_b = tmp_path / "home_b"
    home_b.mkdir()
    shared = tmp_path / "shared"

    # Write phase — like the admin daemon (uid=evolve).
    monkeypatch.setenv("HOME", str(home_a))
    mgr_a = KeystoreManager(shared)
    mgr_a.register(
        "github_intake",
        provider="github",
        scope="shared",
        description="cross-uid round-trip",
        bots=None,
        value="ghp_cross_uid_value",
    )
    assert mgr_a.get_value("github_intake") == "ghp_cross_uid_value"

    # Read phase — like the evo subprocess (uid=evo, different HOME).
    # Without the fix this returns None.
    monkeypatch.setenv("HOME", str(home_b))
    mgr_b = KeystoreManager(shared)
    assert mgr_b.get_value("github_intake") == "ghp_cross_uid_value"


def test_set_value_idempotent_on_existing_vault_dir(tmp_path, monkeypatch):
    """The vault dir's mode is set on creation, not on every write.

    Pre-fix: ``_file_store_set`` called ``vault_dir.chmod(0o700)``
    unconditionally. Once the dir existed (owned by whoever first
    created it), any subsequent caller — even with the right ACL — hit
    EPERM on the chmod and the write aborted before storing the value.
    Post-fix: chmod runs only when mkdir actually creates the dir;
    re-entry on an existing dir is a no-op even if the mode were
    different.
    """
    import os as _os

    from evolve_admin import keystore as ks_mod
    from evolve_admin.keystore import KeystoreManager

    monkeypatch.setattr(ks_mod, "_has_security_cmd", lambda: False)

    mgr = KeystoreManager(tmp_path)
    mgr.register(
        "k1",
        provider="github",
        scope="shared",
        description="first",
        bots=None,
        value="v1",
    )
    vault_dir = tmp_path / "keystore" / "vault"
    assert vault_dir.exists()

    # Simulate a foreign-owned dir by switching the mode to something
    # that would normally trigger chmod. The fix's contract is "don't
    # touch existing dir modes," so this set call must succeed and
    # leave the mode alone.
    _os.chmod(vault_dir, 0o755)
    mgr.register(
        "k2",
        provider="github",
        scope="shared",
        description="second",
        bots=None,
        value="v2",
    )
    assert mgr.get_value("k2") == "v2"
    assert (vault_dir.stat().st_mode & 0o777) == 0o755


def test_migrates_legacy_per_home_key(tmp_path, monkeypatch):
    """Existing PATs encrypted with the legacy per-home key must keep
    working after the fix lands.

    Setup mirrors the field state: a token was written by the admin
    daemon pre-fix (vault key under HOME), the operator deploys the
    fix, the daemon's next read finds no shared key, sees the legacy
    one, migrates it. The encrypted blob decrypts cleanly.
    """
    import os as _os

    from evolve_admin import keystore as ks_mod
    from evolve_admin.keystore import KeystoreManager

    monkeypatch.setattr(ks_mod, "_has_security_cmd", lambda: False)

    home = tmp_path / "home_evolve"
    home.mkdir()
    shared = tmp_path / "shared"
    monkeypatch.setenv("HOME", str(home))
    # The module-level _LEGACY_VAULT_KEY_FILE was bound at import time
    # against the test runner's HOME; re-derive it against our temp
    # HOME so migration looks where we just wrote the key.
    monkeypatch.setattr(
        ks_mod, "_LEGACY_VAULT_KEY_FILE", home / ".openclaw" / ".evolve-vault-key"
    )

    # Hand-build a legacy key + a vault entry that was XOR-encrypted
    # with it. (Mirrors what the pre-fix code would have left on disk.)
    legacy_key = b"\x42" * 32
    legacy_path = ks_mod._LEGACY_VAULT_KEY_FILE
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(legacy_key)

    vault_dir = shared / "keystore" / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    plaintext = b"ghp_legacy_value"
    ciphertext = bytes(
        b ^ legacy_key[i % 32] for i, b in enumerate(plaintext)
    )
    (vault_dir / "github_intake.enc").write_bytes(ciphertext)

    mgr = KeystoreManager(shared)
    # The keystore registry needs the entry to exist for get_value to
    # return non-None — register without overwriting the file we just
    # placed (no value arg → no store call).
    mgr.register(
        "github_intake",
        provider="github",
        scope="shared",
        description="legacy migration test",
        bots=None,
    )
    assert mgr.get_value("github_intake") == "ghp_legacy_value"
    # Side effect: the legacy key got migrated to the shared location.
    assert (shared / "keystore" / ".machine-key").read_bytes() == legacy_key
