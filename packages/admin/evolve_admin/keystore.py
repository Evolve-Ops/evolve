"""
keystore.py — Evolve shared API key management.

Tracks which API keys exist across the bot network, which are shared
vs per-bot, and orchestrates sync so shared keys (Brave API, MAX token,
etc.) are pushed to all bots at once.

Key design decisions:
  1. The keystore does NOT store plaintext key values in the shared dir.
     The shared dir might be accessible to multiple users/processes.
     On macOS, encrypted storage uses the system Keychain when the calling
     user has a usable one; everywhere else — headless macOS daemons (no
     Keychain session) and all non-darwin platforms — the storage backend
     is the file vault (machine-key). That file vault is the COMMON path
     in production today, not an edge case (the evolve LaunchDaemon has no
     Keychain session). On Linux there is no Keychain at all: the keystore
     fails fast to the file vault without spawning `security`.
     # Phase 2.2 (strong-store-mandatory) adds systemd-creds as the Linux
     # strong backend behind this same seam — design-linux-port §7.

  2. Key values live in each bot's auth-profiles.json (where OpenClaw
     reads them). The keystore is a coordination layer, not a vault.
     "sync" means: read value from keychain → write to each bot's
     auth-profiles.json.

  3. Per-bot keys are never synced — they're registered for tracking only.

  4. Rotation flow:
       evolve-admin keys rotate brave_api
       → prompts for new value
       → stores in keychain
       → syncs to all authorized bots
       → logs rotation event with timestamp

Key scope model:
  shared   — all bots get the same value (Brave API, weather API, etc.)
  group    — a named subset of bots gets the same value (e.g., "MAX-bots")
  per-bot  — tracked but never synced (each bot manages its own)

Auth-profiles.json format (OpenClaw's format):
  {
    "profiles": {
      "anthropic": { "apiKey": "sk-ant-...", "provider": "anthropic" },
      "brave": { "apiKey": "BSA...", "provider": "brave" },
      ...
    }
  }
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from evolve_util import now_iso as _now_iso

from .config import bot_home as _bot_home
from .config import bot_user_for as _bot_user_for

logger = logging.getLogger("evolve.keystore")


# The ``Keystore`` class and ``now_iso`` helper live in the analyzer
# package's ``cost.py`` (top-level module on the analyzer path). The
# original ``from .cost import …`` written here resolves to
# ``evolve_admin.cost`` which doesn't exist — those imports only worked
# because no test exercised them. Resolve via absolute import with a
# clear error if the analyzer package isn't importable.

def _import_keystore_cls():
    try:
        from cost import Keystore as _Keystore  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "Cannot locate cost.Keystore — ensure the evolve-analyzer package "
            "is installed (Phase 6.1 ships it as an editable install alongside "
            "evolve-admin)"
        ) from e
    return _Keystore


def _now_iso_from_cost() -> str:
    try:
        from cost import now_iso as _now_iso  # type: ignore[import-not-found]
    except ImportError:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    return _now_iso()


# Keychain service name for storing key values
KEYCHAIN_SERVICE = "ai.openclaw.evolve.keystore"

AUTH_PROFILES_PATH = "agents/main/agent/auth-profiles.json"


class KeystoreManager:
    """High-level keystore operations for the evolve-admin CLI."""

    def __init__(self, shared_dir: Path) -> None:
        Keystore = _import_keystore_cls()
        self.ks = Keystore(shared_dir)
        self.shared_dir = shared_dir

    # ── Key registration ──────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        provider: str,
        scope: str,
        description: str = "",
        bots: list[str] | None = None,
        value: str | None = None,
    ) -> None:
        """Register a key and optionally store its value."""
        self.ks.register_key(name, provider, scope, description, bots)

        if value:
            self._store_value(name, value)
        elif scope in ("shared", "group"):
            print(f"  Registered '{name}' ({scope}). To store value: evolve-admin keys set {name}")

    def set_value(self, name: str, value: str | None = None) -> None:
        """Store or update the value for a key."""
        entry = self.ks.get_key_entry(name)
        if not entry:
            print(f"  Key '{name}' not registered. Use: evolve-admin keys add {name}")
            return

        if value is None:
            value = getpass.getpass(f"  Value for '{name}': ")

        self._store_value(name, value)
        print(f"  Value stored for '{name}'.")

    # ── Sync ──────────────────────────────────────────────────────────────────

    def sync(
        self,
        bots: list[str],
        key_names: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, list[str]]:
        """
        Push shared key values to each bot's auth-profiles.json.
        Returns {bot_id: [keys_synced]}.
        """
        all_keys = self.ks.list_keys()
        to_sync = [
            k for k in all_keys
            if k["scope"] in ("shared", "group")
            and (key_names is None or k["name"] in key_names)
            # The pod-admin GitHub PAT must never land in a bot's
            # auth-profiles.json, even if its registry entry is ever
            # re-registered with a sync-eligible scope (roadmap 2.8).
            and k["name"] != GITHUB_PAT_KEY
        ]

        results: dict[str, list[str]] = {}

        for bot_id in bots:
            synced = []
            for key in to_sync:
                # Check if this key applies to this bot
                authorized_bots = key.get("bots")  # None = all bots
                if authorized_bots is not None and bot_id not in authorized_bots:
                    continue

                value = self._retrieve_value(key["name"])
                if not value:
                    print(f"  [{bot_id}] No value stored for '{key['name']}' — skipping")
                    continue

                if dry_run:
                    print(f"  [dry-run] Would sync '{key['name']}' → {bot_id}")
                    synced.append(key["name"])
                    continue

                ok = self._write_to_auth_profiles(
                    bot_id=bot_id,
                    provider=key["provider"],
                    key_name=key["name"],
                    value=value,
                )
                if ok:
                    synced.append(key["name"])
                    print(f"  ✓ Synced '{key['name']}' → {bot_id}")
                else:
                    print(f"  ✗ Failed to sync '{key['name']}' → {bot_id}")

            if synced:
                self.ks.record_sync(bot_id)
            results[bot_id] = synced

        return results

    def rotate(
        self,
        key_name: str,
        bots: list[str],
        new_value: str | None = None,
        dry_run: bool = False,
    ) -> None:
        """Update key value and sync to all authorized bots."""
        entry = self.ks.get_key_entry(key_name)
        if not entry:
            print(f"  Key '{key_name}' not registered.")
            return

        if new_value is None:
            new_value = getpass.getpass(f"  New value for '{key_name}': ")

        if not dry_run:
            self._store_value(key_name, new_value)
            # Record rotation timestamp
            registry = self.ks.load_registry()
            if key_name in registry.get("keys", {}):
                registry["keys"][key_name]["last_rotated"] = _now_iso_from_cost()
                self.ks.save_registry(registry)

        # Sync to bots
        self.sync(bots, key_names=[key_name], dry_run=dry_run)
        if not dry_run:
            print(f"  ✓ '{key_name}' rotated and synced to {len(bots)} bot(s).")

    def status(self, bots: list[str]) -> None:
        """Print key status table."""
        keys = self.ks.list_keys()
        sync_log = self.ks.load_sync_log()

        if not keys:
            print("  No keys registered. Use: evolve-admin keys add <name>")
            return

        print(f"\n  {'Key':<25} {'Scope':<10} {'Provider':<15} {'Last Rotated':<20}")
        print(f"  {'-'*25} {'-'*10} {'-'*15} {'-'*20}")
        for k in keys:
            last_rotated = k.get("last_rotated") or "never"
            if last_rotated and last_rotated != "never":
                last_rotated = last_rotated[:10]  # date only
            has_value = "✓" if self._has_value(k["name"]) else "✗"
            rollback_flag = " ↩" if self._has_previous(k["name"]) else ""
            print(f"  {k['name']:<24} {k['scope']:<10} {k['provider']:<15} {last_rotated:<20} [{has_value}]{rollback_flag}")

        # Honest backend label: the file vault (machine-key) is the real
        # storage on non-darwin platforms and for headless macOS daemons —
        # don't imply Keychain where it can't apply.
        backend = (
            "macOS Keychain (file vault fallback)"
            if _keychain_available()
            else "file vault (machine-key)"
        )
        print(f"\n  Value storage: {backend}")

        print(f"\n  Bot sync status:")
        for bot_id in bots:
            last = sync_log.get(bot_id, "never")
            if last != "never":
                last = last[:16].replace("T", " ")
            print(f"    {bot_id:<20} last synced: {last}")

    # ── Auth-profiles / rollback ──────────────────────────────────────────────

    def _write_to_auth_profiles(
        self, bot_id: str, provider: str, key_name: str, value: str
    ) -> bool:
        """Write a key value to a bot's auth-profiles.json."""
        oc_base = _bot_home(bot_id) / ".openclaw"
        profiles_path = oc_base / AUTH_PROFILES_PATH

        if not profiles_path.parent.exists():
            print(f"  Cannot find auth-profiles dir for {bot_id}")
            return False

        try:
            if profiles_path.exists():
                profiles = json.loads(profiles_path.read_text())
            else:
                profiles = {"profiles": {}}

            profiles.setdefault("profiles", {})[provider] = {
                "apiKey": value,
                "provider": provider,
                "_evolve_key_name": key_name,
                "_evolve_synced_at": _now_iso(),
            }

            # Preserve file permissions
            mode = profiles_path.stat().st_mode if profiles_path.exists() else 0o600
            tmp = profiles_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(profiles, indent=2))
            tmp.chmod(mode)
            tmp.rename(profiles_path)

            # OC 2026.6+ imports auth-profiles.json into the per-agent SQLite
            # store on agent-CLI init, NOT on gateway start. An operator who
            # runs `keys sync/rotate/rollback` from the CLI and then restarts
            # the gateway via a NON-admin-endpoint path (deploy.restart_gateway /
            # `launchctl kickstart`) never triggers that import, so the running
            # agent can stay on the stale/empty key we just superseded. Prime the
            # durable store now (reusing the #3136 write-side helper) so the next
            # restart loads the value we just wrote. The key is NEVER passed to
            # that subprocess — it lives only in the 0600 JSON OpenClaw reads
            # during the import. Best-effort + idempotent (OpenClaw upserts the
            # single primary row), so a failure must not fail the write that
            # already landed (oc_store reads the JSON regardless) and repeated
            # syncs don't churn. Resolve the macOS account via the seam — bot_id
            # is the logical name and may differ from the OS user — and hand the
            # helper the same home we wrote into so the import targets that
            # bot's agent dir.
            try:
                from .oc_auth_provision import ensure_agent_auth_store_imported
                ensure_agent_auth_store_imported(
                    bot_id, _bot_user_for(bot_id), bot_home=oc_base.parent
                )
            except Exception as exc:
                # The helper itself never raises; this guards only the lazy
                # import. Priming the sqlite store must never undo a write that
                # already succeeded. Log the type only (no traceback / value).
                logger.debug(
                    "auth-store import priming skipped for %s: %s (non-fatal)",
                    bot_id, type(exc).__name__,
                )
            return True

        except PermissionError:
            print(f"  Permission denied writing to {profiles_path}")
            print(f"  (Run evolve-admin as sudo, or the bot user needs to sync their own keys)")
            return False
        except Exception as e:
            print(f"  Error writing auth-profiles for {bot_id}: {e}")
            return False

    # ── Keychain storage ──────────────────────────────────────────────────────

    def rollback(
        self,
        key_name: str,
        bots: list[str],
        dry_run: bool = False,
    ) -> None:
        """Roll back a key to its previous value (swaps current ↔ prev)."""
        entry = self.ks.get_key_entry(key_name)
        if not entry:
            print(f"  Key '{key_name}' not registered.")
            return

        prev_value = self._retrieve_previous(key_name)
        if prev_value is None:
            print(f"  No previous value stored for '{key_name}' — nothing to roll back.")
            return

        if dry_run:
            print(f"  [dry-run] Would roll back '{key_name}' to previous value")
            self.sync(bots, key_names=[key_name], dry_run=True)
            return

        # _store_value saves current as __prev automatically — the swap is implicit
        self._store_value(key_name, prev_value)

        registry = self.ks.load_registry()
        if key_name in registry.get("keys", {}):
            registry["keys"][key_name]["last_rotated"] = _now_iso_from_cost()
            self.ks.save_registry(registry)

        self.sync(bots, key_names=[key_name])
        print(f"  ✓ '{key_name}' rolled back and synced to {len(bots)} bot(s).")

    def get_value(self, name: str) -> str | None:
        """Read the stored value for a registered key, if any.

        Public read used by callers that need the value in admin-server
        context (e.g. the intake promoter posting to GitHub). Returns
        ``None`` if the key isn't registered or has no stored value.
        """
        if not self.ks.get_key_entry(name):
            return None
        return self._retrieve_value(name)

    def _store_value(self, key_name: str, value: str) -> None:
        """Store key value in the macOS Keychain (darwin, when usable) or
        the file vault (machine-key) — the backend everywhere else.
        Always saves the current value as __prev before overwriting.

        When the calling user has the ``security`` CLI on PATH but no
        usable login keychain (the common case for the ``evolve`` service
        daemon — no `loginwindow` session, no Keychain unlock), the first
        keychain write fails and we permanently fall through to the file
        vault for the rest of the process lifetime. ``_keychain_set``
        raises a :class:`KeychainUnavailable` instead of leaking the
        token-bearing argv via ``CalledProcessError``. On non-darwin
        platforms the Keychain attempt is skipped entirely — no
        ``security`` subprocess is ever spawned.
        """
        if _keychain_available():
            try:
                _keychain_set(KEYCHAIN_SERVICE, key_name, value)
                return
            except KeychainUnavailable as e:
                _mark_keychain_unavailable(str(e))
        _file_store_set(self.shared_dir / "keystore" / "vault", key_name, value)

    def _retrieve_value(self, key_name: str) -> str | None:
        """Retrieve key value from storage.

        Tries the keychain first when it's known-usable; otherwise (or on
        miss) falls back to the file vault. The fall-through on miss
        matters during the keychain → file-vault transition: a value
        written via the fallback path is invisible to a keychain-only
        read.
        """
        if _keychain_available():
            val = _keychain_get(KEYCHAIN_SERVICE, key_name)
            if val is not None:
                return val
        return _file_store_get(self.shared_dir / "keystore" / "vault", key_name)

    def _retrieve_previous(self, key_name: str) -> str | None:
        """Retrieve the previous (pre-rotation) value for a key."""
        if _keychain_available():
            val = _keychain_get(KEYCHAIN_SERVICE, f"{key_name}__prev")
            if val is not None:
                return val
        return _file_store_get(self.shared_dir / "keystore" / "vault", f"{key_name}__prev")

    def _has_value(self, key_name: str) -> bool:
        return self._retrieve_value(key_name) is not None

    def _has_previous(self, key_name: str) -> bool:
        return self._retrieve_previous(key_name) is not None


# ── macOS Keychain helpers ────────────────────────────────────────────────────

class KeychainUnavailable(RuntimeError):
    """Raised when the macOS Keychain CLI is on PATH but unusable by the
    calling user (no login keychain, locked, no `loginwindow` session, etc.).

    Carries the sanitized stderr from ``security`` so the caller can
    surface it for diagnosis. The triggering command and any secret
    argument are NEVER included — that's the whole point of using this
    instead of letting :class:`subprocess.CalledProcessError` propagate."""


# Module-level cache: tri-state.
#   None  — never attempted, default to trying keychain first
#   True  — a keychain write succeeded at least once this process
#   False — a keychain write failed; fall through to file vault for the
#           remainder of this process to avoid re-paying the failure cost
#           on every call.
_keychain_state: bool | None = None


def _keychain_available() -> bool:
    """Whether we should attempt a keychain write/read for this call.

    Returns False once :func:`_mark_keychain_unavailable` has been called,
    regardless of whether ``security`` is on PATH. Returns False when
    ``security`` isn't on PATH at all. The ``EVOLVE_KEYSTORE_NO_KEYCHAIN``
    env var forces the file vault unconditionally — used by the test
    suites (so tests never touch a developer's real login keychain) and
    available to operators who want vault-only storage."""
    if os.environ.get("EVOLVE_KEYSTORE_NO_KEYCHAIN"):
        return False
    if _keychain_state is False:
        return False
    return _has_security_cmd()


def _mark_keychain_unavailable(reason: str) -> None:
    """Latch keychain-unusable for the remainder of this process.

    Called on the first failed keychain write. Subsequent calls become
    no-ops. Emits a one-shot stderr note so operators tailing the daemon
    log see why we fell back to the file vault."""
    global _keychain_state
    if _keychain_state is False:
        return
    _keychain_state = False
    try:
        print(
            f"[keystore] macOS Keychain unavailable for this user — using "
            f"the file vault (machine-key). cause: {reason}",
            file=sys.stderr,
            flush=True,
        )
    except Exception:  # noqa: BLE001
        pass


def _has_security_cmd() -> bool:
    # The `security` CLI is darwin-only. Short-circuit BEFORE any
    # subprocess so non-darwin hosts go straight to the file vault
    # (machine-key) — discovering the CLI's absence via spawn failure
    # would pay a subprocess per call and imply a backend that can never
    # exist here (design-linux-port §7).
    if sys.platform != "darwin":
        return False
    # Test/CI escape hatch — when set, force the file-vault path even on
    # macOS hosts that have `security`. Used by test_cli_keystore_get.py
    # so subprocess-based tests of `evolve-admin keystore get` work
    # deterministically without writing to the real macOS Keychain.
    if os.environ.get("EVOLVE_FORCE_FILE_VAULT"):
        return False
    return bool(subprocess.run(["which", "security"], capture_output=True).returncode == 0)


def _run_security_or_raise(args: list[str]) -> None:
    """Run a ``security`` subcommand; raise :class:`KeychainUnavailable`
    with sanitized stderr on non-zero exit.

    CRITICAL: do NOT propagate :class:`subprocess.CalledProcessError` from
    callers that pass a secret in ``argv`` (notably ``-w <password>``).
    ``CalledProcessError.__str__`` embeds ``self.cmd`` verbatim, which
    means the secret ends up in every traceback, log line, and HTTP
    error response that stringifies the exception. The current symptom
    that motivated this guard: the UI showed
    ``failed to save token: Command '['security', 'add-generic-password',
    '-s', '...', '-a', 'github_intake', '-w', 'ghp_...']' returned …``
    with the operator's PAT in plaintext."""
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode == 0:
        return
    stderr = (r.stderr or "").strip()
    # security's stderr is messages like
    #   "security: SecKeychainItemCreateFromContent ... -25244"
    # which don't contain the password value, but scrub for token-shaped
    # substrings anyway as defense in depth.
    stderr = _scrub_token_like(stderr)
    raise KeychainUnavailable(
        f"security exited {r.returncode}: {stderr or 'no stderr'}"
    )


def _scrub_token_like(text: str) -> str:
    """Best-effort redaction of GitHub-PAT-shaped substrings.

    Defense in depth — the calls in this module no longer place secrets
    on argv that reach exception strings, but if anything new gets added
    or stderr ever contains echoed-back input, this cuts the blast radius.
    Matches: ``ghp_*``, ``ghs_*``, ``gho_*``, ``ghr_*``, ``ghu_*``,
    ``github_pat_*``."""
    import re
    return re.sub(
        r"\b(ghp|ghs|gho|ghr|ghu)_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        "<redacted-token>",
        text,
    )


def _keychain_set(service: str, account: str, value: str) -> None:
    # Booby-trap for direct callers: the gated paths never reach here on
    # non-darwin (_keychain_available() is False), and a raw `security`
    # spawn on Linux would only fail slower and less clearly.
    if sys.platform != "darwin":
        raise KeychainUnavailable(
            "macOS Keychain is darwin-only — the file vault (machine-key) "
            "is the storage backend on this platform"
        )
    # Save current value as __prev before overwriting
    current = _keychain_get(service, account)
    if current is not None:
        prev_account = f"{account}__prev"
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", prev_account],
            capture_output=True,
        )
        _run_security_or_raise(
            ["security", "add-generic-password", "-s", service, "-a", prev_account, "-w", current],
        )
    # Delete existing entry first (ignore error if doesn't exist)
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True,
    )
    _run_security_or_raise(
        ["security", "add-generic-password", "-s", service, "-a", account, "-w", value],
    )
    # Mark keychain-usable on the first success so we don't keep paying
    # the probe cost on every read. (Once True, stays True for the
    # process — keychain access doesn't typically revoke mid-process.)
    global _keychain_state
    if _keychain_state is not True:
        _keychain_state = True


def _keychain_get(service: str, account: str) -> str | None:
    # Same booby-trap as _keychain_set — see there.
    if sys.platform != "darwin":
        raise KeychainUnavailable(
            "macOS Keychain is darwin-only — the file vault (machine-key) "
            "is the storage backend on this platform"
        )
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return r.stdout.strip()
    return None


# ── File vault (machine-key) — the backend on non-darwin and headless macOS ──
#
# Phase 2.2 (strong-store-mandatory) adds systemd-creds as the Linux strong
# backend behind the same _store_value/_retrieve_value seam (design-linux-port §7).
#
# Key location: ``{shared_dir}/keystore/.machine-key``, NOT
# ``Path.home()/.openclaw/.evolve-vault-key``. Pre-2026-06-04 the key
# lived in the calling process's home, which broke the moment
# ``evo`` was split off as a separate macOS account (see
# spec-evo-account-separation-2026-05-25.md): the admin daemon writes a
# token as ``evolve``, XOR-encrypting with a key in ``/Users/evolve/…``;
# the evo MCP subprocess later reads the same byte stream but generates
# a *different* per-home key under ``/Users/evo/…`` and decrypts to
# garbage, which gets swallowed by ``except Exception: return None`` in
# :func:`_file_store_get` and surfaces as ``no token in keystore slot``
# even though the UI sees the token just fine. Anchoring the key in
# ``{shared_dir}`` (which already grants read+write ACL to both users via
# ``ensure_pod_perms``) makes the cross-user round-trip work.
#
# Migration: on first read after this fix lands, if the shared key is
# absent and the calling user has a legacy per-home key file, copy it
# in. The migration only succeeds when run as the user whose home holds
# the original key (i.e. ``evolve``, the admin daemon) — evo can't read
# evolve's home. That's fine: the admin daemon hits the keystore on
# every UI render, so the first /api/inbox/repos call after deploy
# performs the migration and the existing PAT keeps working.

_LEGACY_VAULT_KEY_FILE = Path.home() / ".openclaw" / ".evolve-vault-key"


def _shared_vault_key_path(vault_dir: Path) -> Path:
    """``{shared_dir}/keystore/.machine-key`` — the canonical location.

    ``vault_dir`` is ``{shared_dir}/keystore/vault``; the key file sits
    one level up next to it so a sibling ``vault/`` listing doesn't show
    the encryption key alongside the encrypted blobs.
    """
    return vault_dir.parent / ".machine-key"


def _get_vault_key(vault_dir: Path) -> bytes:
    """Return the shared machine key, migrating or creating as needed.

    Resolution order:
      1. ``{shared_dir}/keystore/.machine-key`` (the canonical location)
      2. Legacy per-home key at ``Path.home()/.openclaw/.evolve-vault-key``
         — migrated into the canonical location on read
      3. Fresh ``os.urandom(32)`` written to the canonical location

    Raises :class:`PermissionError` propagated from the filesystem if
    neither read nor write to the canonical location is possible — the
    expected mode for ``evo`` callers when the admin daemon hasn't yet
    populated the file. Callers (``_file_store_set`` /
    ``_file_store_get``) handle that gracefully: the set path lets it
    bubble so the UI sees a real error; the get path catches and returns
    None so a missing/unreadable key looks the same as a missing entry.
    """
    canonical = _shared_vault_key_path(vault_dir)
    if canonical.exists():
        return canonical.read_bytes()

    legacy = _LEGACY_VAULT_KEY_FILE
    if legacy.exists() and legacy.is_file():
        try:
            key = legacy.read_bytes()
        except OSError:
            key = None
        if key:
            try:
                _write_shared_vault_key(canonical, key)
            except OSError:
                # Migration failed (e.g. evo reaching evolve's home).
                # Don't generate a fresh key — that would silently
                # invalidate every previously-stored entry. Surface the
                # underlying permission error to the caller.
                raise
            return key

    key = os.urandom(32)
    _write_shared_vault_key(canonical, key)
    return key


def _write_shared_vault_key(path: Path, key: bytes) -> None:
    """Atomically write the shared machine key. Mode 0640.

    The parent dir is created if missing. Mode 0640 plus the inherited
    evo-read ACL on ``{shared_dir}/keystore/`` (granted by
    ``ensure_pod_perms``) lets evo read the file without needing world-
    readable POSIX bits. Atomic write via tempfile + replace so a
    concurrent reader never observes a partially-written key.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".machine-key.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ensure_dir_mode(path: Path, mode: int) -> None:
    """``mkdir -p`` then chmod, but tolerate non-owner re-invocation.

    Original bug: this code called ``vault_dir.chmod(0o700)``
    unconditionally on every write, which raised EPERM the moment a
    second user (e.g. ``evo`` or anyone running the CLI ad-hoc) tried to
    set a key on a dir that ``evolve`` had already created. Fix: only
    chmod when we just created the dir, and swallow EPERM otherwise — at
    that point the dir already exists and the mode is whatever the
    first-creator set it to. (We trust ``ensure_pod_perms`` to keep it
    sane long-term; this function isn't the right place to fight an
    out-of-band chmod.)
    """
    if path.exists():
        return
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except PermissionError:
        pass


def _fernet(machine_key: bytes):
    """Build a Fernet (AES-128-CBC + HMAC) from the machine key.

    The Fernet key is a stable SHA-256 of the machine key (→ a valid 32-byte
    url-safe-base64 key regardless of the machine key's own length), so the same
    machine key always yields the same cipher key.
    """
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(machine_key).digest()))


def _file_store_set(vault_dir: Path, key_name: str, value: str) -> None:
    """Authenticated-encrypt ``value`` with the machine key and store in a file.

    Uses Fernet (AES + HMAC), replacing the old repeating-key XOR, which was
    obfuscation, not encryption. The machine key lives on the same disk, so this
    protects against casual disk/backup inspection — NOT against a local process
    that can read both files; that residual is the single-tenant trust boundary
    (docs/threat-model.md).
    """
    _ensure_dir_mode(vault_dir, 0o750)
    machine_key = _get_vault_key(vault_dir)
    # Save current encrypted file as __prev before overwriting (byte copy —
    # format-agnostic, so legacy-XOR and Fernet blobs both round-trip via prev).
    current_path = vault_dir / f"{key_name}.enc"
    if current_path.exists():
        prev_path = vault_dir / f"{key_name}__prev.enc"
        prev_path.write_bytes(current_path.read_bytes())
        try:
            prev_path.chmod(0o640)
        except PermissionError:
            pass
    encrypted = _fernet(machine_key).encrypt(value.encode())
    out = vault_dir / f"{key_name}.enc"
    out.write_bytes(encrypted)
    try:
        out.chmod(0o640)
    except PermissionError:
        pass


def _file_store_get(vault_dir: Path, key_name: str) -> str | None:
    path = vault_dir / f"{key_name}.enc"
    if not path.exists():
        return None
    try:
        machine_key = _get_vault_key(vault_dir)
        raw = path.read_bytes()
    except Exception:
        return None
    from cryptography.fernet import InvalidToken

    try:
        return _fernet(machine_key).decrypt(raw).decode()
    except InvalidToken:
        # Legacy XOR ciphertext written before the Fernet migration — decrypt
        # with the old scheme so existing secrets keep working. The next set()
        # re-encrypts the value as Fernet, so this path is self-retiring.
        try:
            return bytes(
                b ^ machine_key[i % len(machine_key)] for i, b in enumerate(raw)
            ).decode()
        except Exception:
            return None
    except Exception:
        return None


# ── Pod-wide GitHub PAT (roadmap 2.8) ────────────────────────────────────────
#
# Decision D2 (internal/decision-security-defaults-2026-06-10.md): the PAT no
# longer persists in plaintext network.json::github.pat. It lives in the
# keystore's Fernet file vault.
#
# Deliberately VAULT-PINNED, never the macOS Keychain: every reader is a
# headless cross-process daemon (backup, backup_signal, admin routes), and
# the Keychain is per-user — a successful Keychain write from whatever user
# happened to run the wizard/migration (`evolve-admin serve` is documented
# as safe to run as any user) would strand the PAT where the evolve
# LaunchDaemons can never read it, recreating the exact
# wizard-says-persisted / monitor-says-missing gap 2.8 closes.

GITHUB_PAT_KEY = "github_pat"


def _github_pat_vault_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "keystore" / "vault"


def store_github_pat(shared_dir: Path, pat: str) -> None:
    """Register + store the pod-wide GitHub PAT (file vault, never Keychain).

    Scope ``per-bot`` is deliberate: ``sync()`` pushes only shared/group
    keys into bots' auth-profiles.json, and the admin PAT must never land
    in a bot's credential file (sync also hard-excludes this key).
    Registration keeps the key visible to ``evolve-admin keys status``.
    """
    mgr = KeystoreManager(Path(shared_dir))
    if not mgr.ks.get_key_entry(GITHUB_PAT_KEY):
        mgr.ks.register_key(
            GITHUB_PAT_KEY,
            provider="github",
            scope="per-bot",
            description="Pod-wide GitHub PAT (backup visibility + key registration)",
        )
    _file_store_set(_github_pat_vault_dir(shared_dir), GITHUB_PAT_KEY, pat)


def load_github_pat(shared_dir: Path) -> "str | None":
    """The pod-wide GitHub PAT from the file vault, or None.

    Vault-pinned to mirror :func:`store_github_pat` — no Keychain probe, so
    reads behave identically for every uid and keychain-lock state.
    """
    try:
        value = _file_store_get(_github_pat_vault_dir(shared_dir), GITHUB_PAT_KEY)
    except Exception:
        return None
    return (value or "").strip() or None


def migrate_github_pat_from_network(network_path: "Path | None" = None) -> bool:
    """One-time migration: move plaintext ``network.json::github.pat``
    into the keystore and scrub it from network.json.

    Idempotent and best-effort — returns True only when a plaintext PAT
    was found, stored, and scrubbed. Called from the admin server's
    startup path so existing pods migrate on the deploy that ships 2.8.
    The keystore write happens BEFORE the network.json scrub, so a crash
    between the two leaves the PAT readable in both places, never in
    neither (the next startup finishes the scrub).
    """
    from .config import DEFAULT_NETWORK_CONFIG, load_network, save_network

    path = network_path or DEFAULT_NETWORK_CONFIG
    try:
        net = load_network(path)
    except Exception:
        return False
    github = net.get("github")
    if not isinstance(github, dict):
        return False
    pat = (github.get("pat") or "").strip()
    if not pat:
        # Scrub an empty/whitespace legacy slot too (no secret to store,
        # but the dead key shouldn't linger and re-trip greps forever).
        if "pat" in github:
            del github["pat"]
            try:
                save_network(net, path)
            except Exception as _exc:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "github.pat empty-slot scrub failed (will retry next start): %s",
                    _exc,
                )
        return False
    store_github_pat(Path(net.get("sharedDir") or "/Users/Shared/evolve"), pat)
    del github["pat"]
    save_network(net, path)
    return True
