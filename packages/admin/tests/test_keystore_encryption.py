"""File-vault encryption: Fernet, with legacy-XOR migration (roadmap 2.2).

The keystore's file vault is the *primary* secret-at-rest path in production —
the `evolve` service daemon has no usable login keychain, so it never reaches the
macOS Keychain. That vault used repeating-key XOR ("not cryptographically
strong"). This replaces it with Fernet (AES + HMAC), while still reading any
legacy XOR blob so existing secrets are never lost.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin import keystore  # noqa: E402


def _xor(machine_key: bytes, data: bytes) -> bytes:
    return bytes(b ^ machine_key[i % len(machine_key)] for i, b in enumerate(data))


def test_roundtrip(tmp_path):
    vault = tmp_path / "keystore" / "vault"
    keystore._file_store_set(vault, "github_pat", "ghp_secret123")
    assert keystore._file_store_get(vault, "github_pat") == "ghp_secret123"


def test_on_disk_is_real_ciphertext_not_xor(tmp_path):
    vault = tmp_path / "keystore" / "vault"
    keystore._file_store_set(vault, "k", "mysecret")
    raw = (vault / "k.enc").read_bytes()
    # Fernet tokens are url-safe base64 beginning with the version byte 0x80 → "gAAAAA"
    assert raw.startswith(b"gAAAAA")
    # …and the plaintext is NOT recoverable by the old repeating-key XOR.
    machine_key = keystore._get_vault_key(vault)
    assert b"mysecret" not in _xor(machine_key, raw)


def test_tampered_ciphertext_does_not_return_the_value(tmp_path):
    vault = tmp_path / "keystore" / "vault"
    keystore._file_store_set(vault, "k", "mysecret")
    p = vault / "k.enc"
    blob = bytearray(p.read_bytes())
    blob[25] ^= 0xFF  # flip a byte
    p.write_bytes(bytes(blob))
    # Authenticated encryption: tampering must not yield the original secret.
    assert keystore._file_store_get(vault, "k") != "mysecret"


def test_legacy_xor_value_still_readable(tmp_path):
    """A secret written by the pre-2.2 XOR scheme must still decrypt."""
    vault = tmp_path / "keystore" / "vault"
    vault.mkdir(parents=True)
    machine_key = keystore._get_vault_key(vault)  # creates the canonical key
    (vault / "leg.enc").write_bytes(_xor(machine_key, b"old_secret"))
    assert keystore._file_store_get(vault, "leg") == "old_secret"


def test_legacy_value_reencrypts_as_fernet_on_next_set(tmp_path):
    vault = tmp_path / "keystore" / "vault"
    vault.mkdir(parents=True)
    machine_key = keystore._get_vault_key(vault)
    (vault / "leg.enc").write_bytes(_xor(machine_key, b"old"))
    keystore._file_store_set(vault, "leg", "new")  # migration on write
    assert (vault / "leg.enc").read_bytes().startswith(b"gAAAAA")  # now Fernet
    assert keystore._file_store_get(vault, "leg") == "new"


def test_missing_entry_returns_none(tmp_path):
    vault = tmp_path / "keystore" / "vault"
    assert keystore._file_store_get(vault, "nope") is None


def test_same_machine_key_decrypts_across_calls(tmp_path):
    """A fresh process (new function call chain) with the same key on disk reads
    the value — i.e. the Fernet key derivation is deterministic, not per-call."""
    vault = tmp_path / "keystore" / "vault"
    keystore._file_store_set(vault, "k", "persisted")
    # second independent get (no shared in-memory state) still works
    assert keystore._file_store_get(vault, "k") == "persisted"
