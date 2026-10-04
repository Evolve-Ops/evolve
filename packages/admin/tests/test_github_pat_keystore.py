"""github_pat keystore helpers (roadmap 2.8, decision D2).

The pod-wide GitHub PAT's canonical home is the keystore (Keychain when
usable; Fernet file vault otherwise — and unconditionally the vault under
EVOLVE_KEYSTORE_NO_KEYCHAIN, which conftest sets so tests never touch a
real login keychain). network.json::github.pat is legacy: migrated on
admin-server startup, scrubbed after.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from evolve_admin.keystore import (  # noqa: E402
    GITHUB_PAT_KEY,
    load_github_pat,
    migrate_github_pat_from_network,
    store_github_pat,
)


def test_store_load_roundtrip(tmp_path):
    store_github_pat(tmp_path, "ghp_roundtrip")
    assert load_github_pat(tmp_path) == "ghp_roundtrip"


def test_load_missing_returns_none(tmp_path):
    assert load_github_pat(tmp_path) is None


def test_stored_value_is_not_plaintext_on_disk(tmp_path):
    store_github_pat(tmp_path, "ghp_ciphertext_check")
    vault_file = tmp_path / "keystore" / "vault" / f"{GITHUB_PAT_KEY}.enc"
    assert vault_file.exists()
    assert b"ghp_ciphertext_check" not in vault_file.read_bytes()


def test_registered_scope_never_syncs_to_bots(tmp_path):
    """The PAT registers with a non-shared scope so `keys sync` can never
    push it into a bot's auth-profiles.json."""
    store_github_pat(tmp_path, "ghp_scope_check")
    registry = json.loads((tmp_path / "keystore" / "keys.json").read_text())
    assert registry["keys"][GITHUB_PAT_KEY]["scope"] not in ("shared", "group")


def test_migration_moves_and_scrubs(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "github": {"pat": "ghp_migrate_me", "login": "octocat"},
    }))

    assert migrate_github_pat_from_network(net_path) is True

    net = json.loads(net_path.read_text())
    assert "ghp_migrate_me" not in net_path.read_text()
    assert net["github"].get("pat") is None
    assert net["github"]["login"] == "octocat"  # non-secret fields survive
    assert load_github_pat(shared) == "ghp_migrate_me"

    # Idempotent: second run finds nothing to do.
    assert migrate_github_pat_from_network(net_path) is False
    assert load_github_pat(shared) == "ghp_migrate_me"


def test_migration_noop_without_pat(tmp_path):
    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({"sharedDir": str(tmp_path / "shared")}))
    assert migrate_github_pat_from_network(net_path) is False


def test_migration_scrubs_empty_legacy_slot(tmp_path):
    """An empty/whitespace legacy pat key is removed (nothing to store)."""
    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "github": {"pat": "   ", "login": "octocat"},
    }))
    assert migrate_github_pat_from_network(net_path) is False
    net = json.loads(net_path.read_text())
    assert "pat" not in net["github"]
    assert net["github"]["login"] == "octocat"


def test_stored_pat_is_vault_pinned_not_keychain(tmp_path, monkeypatch):
    """The PAT must land in the shared file vault even when a Keychain
    looks usable — a per-user Keychain write would strand it where the
    headless evolve daemons can never read it (review finding, 2.8)."""
    import evolve_admin.keystore as ks

    monkeypatch.delenv("EVOLVE_KEYSTORE_NO_KEYCHAIN", raising=False)
    monkeypatch.setattr(ks, "_keychain_available", lambda: True)

    def _no_keychain_write(*a, **k):
        raise AssertionError("github_pat must never be written to the Keychain")

    monkeypatch.setattr(ks, "_keychain_set", _no_keychain_write)
    ks.store_github_pat(tmp_path, "ghp_vault_pinned")
    monkeypatch.setattr(
        ks, "_keychain_get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Keychain read")),
    )
    assert ks.load_github_pat(tmp_path) == "ghp_vault_pinned"


def test_sync_never_pushes_github_pat_to_bots(tmp_path, monkeypatch):
    """Even a (mis-)registered shared scope must not sync the admin PAT
    into bot auth-profiles.json."""
    from evolve_admin.keystore import KeystoreManager

    mgr = KeystoreManager(tmp_path)
    mgr.ks.register_key(GITHUB_PAT_KEY, "github", "shared", "misregistered")
    store_github_pat(tmp_path, "ghp_should_not_sync")

    writes: list[tuple] = []
    monkeypatch.setattr(
        KeystoreManager, "_write_to_auth_profiles",
        lambda self, **kw: writes.append(kw) or True,
    )
    result = mgr.sync(["team_bot_a"])
    assert writes == []
    assert result.get("team_bot_a", []) == []


def test_migration_noop_on_missing_file(tmp_path):
    assert migrate_github_pat_from_network(tmp_path / "absent.json") is False


def test_analyzer_load_pat_prefers_keystore(tmp_path):
    """backup_visibility.load_pat reads the keystore first, falls back to
    the legacy network.json slot only when the keystore has nothing."""
    from backup_visibility import load_pat

    shared = tmp_path / "shared"
    shared.mkdir()
    config = {
        "sharedDir": str(shared),
        "github": {"pat": "ghp_legacy_slot"},
    }
    # Keystore empty → legacy fallback serves.
    assert load_pat(config) == "ghp_legacy_slot"

    # Keystore populated → it wins over the (stale) plaintext slot.
    store_github_pat(shared, "ghp_from_keystore")
    assert load_pat(config) == "ghp_from_keystore"

    # No legacy slot at all → keystore only.
    assert load_pat({"sharedDir": str(shared)}) == "ghp_from_keystore"
