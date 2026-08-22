"""Keystore Linux fail-fast — zero `security` spawns off-darwin (8.3 L2b).

Design: docs/design-linux-port-2026-06-10.md §7. The macOS Keychain is
darwin-only; on any other platform the keystore must short-circuit to the
file vault (machine-key) BEFORE spawning `security` — discovering the
CLI's absence via subprocess failure pays a spawn per call and implies a
backend that can never exist there.

Proof style: module-level subprocess booby-trap (same as
``test_systemd_jobspec_golden.py``) — ``subprocess.run``/``Popen``/
``os.system``/``posix_spawn`` all explode, so the only way these tests
pass is if the short-circuit fires before any spawn is attempted.

The suite-wide ``EVOLVE_KEYSTORE_NO_KEYCHAIN`` escape (set by conftest)
is explicitly REMOVED in these tests: what's being proven is the
platform gate, not the env escape.

Phase 2.2 adds systemd-creds as the Linux strong backend behind the same
seam; until then the file vault is the only Linux backend — by design.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import keystore  # noqa: E402


@pytest.fixture(autouse=True)
def _no_subprocess_anywhere(monkeypatch):
    """ZERO subprocess spawns — `run`/`call`/`check_output` all construct
    ``Popen``, so trapping the constructors traps every spawn path."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            f"a REAL subprocess spawn was attempted in a keystore fail-fast "
            f"test — the non-darwin short-circuit did not fire. args={a!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(os, "posix_spawn", _boom)
    monkeypatch.setattr(os, "posix_spawnp", _boom)


@pytest.fixture(autouse=True)
def _platform_gate_only(monkeypatch, tmp_path):
    """Clear the env escapes (the platform gate must stand alone), reset
    the module-level keychain latch, and point the legacy vault-key probe
    at a nonexistent path so host state can't leak in."""
    monkeypatch.delenv("EVOLVE_KEYSTORE_NO_KEYCHAIN", raising=False)
    monkeypatch.delenv("EVOLVE_FORCE_FILE_VAULT", raising=False)
    monkeypatch.setattr(keystore, "_keychain_state", None)
    monkeypatch.setattr(
        keystore, "_LEGACY_VAULT_KEY_FILE", tmp_path / "no-legacy-key"
    )
    yield
    keystore._keychain_state = None


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


# ── the short-circuit itself ──────────────────────────────────────────────────


def test_has_security_cmd_false_on_linux_without_spawn(linux) -> None:
    assert keystore._has_security_cmd() is False


def test_keychain_available_false_on_linux(linux) -> None:
    assert keystore._keychain_available() is False


def test_keychain_set_raises_keychain_unavailable_on_linux(linux) -> None:
    with pytest.raises(keystore.KeychainUnavailable, match="file vault"):
        keystore._keychain_set(keystore.KEYCHAIN_SERVICE, "k", "v")


def test_keychain_get_raises_keychain_unavailable_on_linux(linux) -> None:
    with pytest.raises(keystore.KeychainUnavailable, match="file vault"):
        keystore._keychain_get(keystore.KEYCHAIN_SERVICE, "k")


# ── end-to-end: store/retrieve goes straight to the file vault ────────────────


def test_store_and_retrieve_use_file_vault_on_linux(linux, tmp_path) -> None:
    mgr = keystore.KeystoreManager(tmp_path)
    mgr._store_value("test_api_key", "value-123")

    vault = tmp_path / "keystore" / "vault"
    assert (vault / "test_api_key.enc").exists(), "value did not land in the file vault"
    # Encrypted at rest — never plaintext on disk.
    assert b"value-123" not in (vault / "test_api_key.enc").read_bytes()
    assert mgr._retrieve_value("test_api_key") == "value-123"


def test_rotation_prev_value_round_trips_in_vault_on_linux(linux, tmp_path) -> None:
    mgr = keystore.KeystoreManager(tmp_path)
    mgr._store_value("test_api_key", "old-value")
    mgr._store_value("test_api_key", "new-value")
    assert mgr._retrieve_value("test_api_key") == "new-value"
    assert mgr._retrieve_previous("test_api_key") == "old-value"


# ── darwin contrast: the gate is the platform, not a blanket False ────────────


def test_security_cmd_still_probed_on_darwin(monkeypatch) -> None:
    """On darwin the probe DOES consult `which security` — proves the
    short-circuit is platform-gated rather than disabling the Keychain
    everywhere. The spawn is faked (booby-trap stays armed underneath:
    only `subprocess.run` is rebound, to a recorder)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[list[str]] = []

    def _fake_run(args, **kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert keystore._has_security_cmd() is True
    assert calls == [["which", "security"]]
