"""
oc_keys.py — Evolve-native bot API key presence reader and writer.

Ported from docs/reference/openclaw-admin.py into the Evolve codebase.
Designed to run AS the bot user (via ``sudo -u {user} python3 oc_keys.py``)
so it can read/write the bot's own auth store without extra permissions.

Credential store (when run as bot user) — OpenClaw 2026.6.x migrated each
bot's per-agent auth out of the JSON file into a SQLite store; the presence
reader walks a source ladder so it works across the migration:

    1. SQLite store  ~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite
       (post-migration; the live store — secrets are NOT file-readable, only
       the set of configured providers is)
    2. Legacy JSON   ~/.openclaw/agents/<agentId>/agent/auth-profiles.json
       (pre-migration / un-migrated pods)
    3. Transitional  ~/.openclaw/agents/<agentId>/agent/auth-profiles.json.sqlite-import.<ms>.bak
       (last resort — the snapshot OpenClaw left behind during migration)

When ALL three fail, ``json_keys`` returns a LOUD ``source="none"`` marker
(never a silent empty key set) so a future storage migration surfaces an
operator advisory instead of masquerading as "no credentialed providers".

This module runs as the OWNING bot reading its OWN store, so it does not need
the privileged sudo / symlink-redirect guards that the admin-side sibling
reader ``evolve_admin.oc_store.read_auth_store`` carries (that one runs as the
``evolve`` user reading every bot's store via an ACL). The two share the same
source ladder by design; keep them in sync if the OC schema moves again.

Usage as standalone script (for key presence — safe, no key values exposed):
    sudo -u team_bot_a python3 oc_keys.py keys team_bot_a

Usage for writing (reads new key value from stdin):
    sudo -u team_bot_a python3 oc_keys.py keys set team_bot_a anthropic api_key
    sudo -u team_bot_a python3 oc_keys.py keys set team_bot_a anthropic token

Usage for removal:
    sudo -u team_bot_a python3 oc_keys.py keys remove team_bot_a anthropic api_key

Security: key values are NEVER returned in the keys presence output.
          Writing always reads key value from stdin (never from argv).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# OpenClaw per-agent on-disk layout, relative to the bot's ~/.openclaw.
_OC_RELROOT = Path(".openclaw")
_AGENTS_RELDIR = "agents"
_AGENT_SUBDIR = "agent"
_SQLITE_NAME = "openclaw-agent.sqlite"
_LEGACY_JSON_NAME = "auth-profiles.json"
_BAK_PREFIX = "auth-profiles.json.sqlite-import."
_BAK_SUFFIX = ".bak"

# Default auth-profiles path when running as the bot user (legacy WRITE target —
# set/remove still operate on the JSON file). Presence READS resolve through the
# source ladder instead (see ``_resolve_auth_blob``).
_DEFAULT_AUTH_PROFILES = (
    Path.home() / _OC_RELROOT / _AGENTS_RELDIR / "main" / _AGENT_SUBDIR / _LEGACY_JSON_NAME
)

# SQLite store contract (verified live on the mini, OC 2026.6.x):
#   CREATE TABLE auth_profile_store (store_key TEXT PRIMARY KEY,
#                                    store_json TEXT NOT NULL,
#                                    updated_at INTEGER NOT NULL);
# store_key is always "primary"; store_json is the migrated profiles blob
# ``{"version": ..., "profiles": {"<profileId>": {"provider": ...}, ...}}``.
_STORE_SELECT = "SELECT store_json FROM auth_profile_store WHERE store_key='primary'"

# Providers that have both an api_key and a token mode (e.g. Anthropic).
# Used only by the legacy/value-based reader; the sqlite reader emits both
# fields from the profile's own mode (no provider literal needed).
_HAS_TOKEN = {"anthropic"}


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _loads_lenient(text: str) -> dict:
    """Parse JSON tolerating a trailing comma before a closing brace/bracket."""
    return json.loads(re.sub(r',(\s*[}\]])', r'\1', text))


def _load_json(path: Path) -> dict:
    return _loads_lenient(path.read_text())


def _preserve_write(data: dict, path: Path) -> None:
    """Write JSON preserving the file's existing ownership and permissions."""
    try:
        st = os.stat(path)
        existing_uid: int | None = st.st_uid
        existing_gid: int | None = st.st_gid
        existing_mode: int | None = st.st_mode
    except OSError:
        existing_uid = existing_gid = existing_mode = None

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    if existing_uid is not None:
        try:
            os.chown(path, existing_uid, existing_gid)
            os.chmod(path, existing_mode)
        except OSError as e:
            print(f"[oc_keys] warning: could not restore permissions: {e}", file=sys.stderr)


# ── Profile helpers (ported from openclaw-admin.py) ───────────────────────────

def find_profile(
    profiles: dict,
    provider: str,
    mode: str,
) -> "tuple[str | None, dict | None, str | None]":
    """Return (profile_name, profile_dict, field_name) for provider+mode, or (None, None, None)."""
    for name, p in profiles.items():
        if p.get("provider") == provider:
            pmode = p.get("type", p.get("mode", ""))
            if pmode == mode:
                field = "token" if mode == "token" else "key"
                if field in p:
                    return name, p, field
    return None, None, None


# ── Credential-store source ladder (sqlite → legacy json → bak) ───────────────

def _discover_agent_dirs(agents_root: Path) -> "list[Path]":
    """Return ``<agents>/<agentId>/agent`` dirs, ``main`` first.

    Honors the historical main-agent default without HARDCODING it: a pod whose
    only agent dir is non-main (e.g. an ``email-reader`` agent) is still found.
    """
    try:
        subdirs = [p for p in agents_root.iterdir() if p.is_dir()]
    except OSError:
        return []
    subdirs.sort(key=lambda p: (p.name != "main", p.name))  # 'main' sorts first
    return [p / _AGENT_SUBDIR for p in subdirs]


def _read_sqlite_store(db: Path) -> "str | None":
    """Return ``store_json`` for ``store_key='primary'``, or ``None``.

    Opened strictly read-only and WAL-safe (``mode=ro`` + ``PRAGMA query_only``;
    never ``immutable=1``, which would ignore the ``-wal`` sidecar and miss the
    latest write). Reads as the owning bot user — no privileged fallback needed.
    """
    uri = f"file:{db}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute(_STORE_SELECT).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"[oc_keys] sqlite read failed at {db}: {e}", file=sys.stderr)
        return None
    if row and isinstance(row[0], str) and row[0].strip():
        return row[0]
    return None


def _read_newest_bak(agent_dir: Path) -> "str | None":
    """Read the newest ``auth-profiles.json.sqlite-import.<ms>.bak``, if any."""
    try:
        candidates = [
            p
            for p in agent_dir.iterdir()
            if p.name.startswith(_BAK_PREFIX) and p.name.endswith(_BAK_SUFFIX)
        ]
    except OSError:
        return None
    if not candidates:
        return None

    def _bak_epoch(p: Path) -> int:
        mid = p.name[len(_BAK_PREFIX):-len(_BAK_SUFFIX)]
        try:
            return int(mid)
        except ValueError:
            return -1

    for p in sorted(candidates, key=_bak_epoch, reverse=True):
        try:
            text = p.read_text()
        except OSError:
            continue
        if text.strip():
            return text
    return None


def _derive_oc_root(auth_path: "Path | None") -> "Path | None":
    """Derive the ``~/.openclaw`` root from an explicit legacy ``auth_path``.

    ``auth_path`` is ``<oc_root>/agents/<agentId>/agent/auth-profiles.json`` →
    ``parents[3]`` is the ``.openclaw`` root. Scoping discovery to the supplied
    path's own tree keeps an explicit (test/override) ``auth_path`` from leaking
    into the real ``~/.openclaw``. Returns ``None`` for an off-shape path, which
    disables sqlite/bak discovery and uses the explicit ``auth_path`` only.
    """
    if auth_path is None:
        return None
    try:
        return Path(auth_path).parents[3]
    except IndexError:
        return None


def _resolve_auth_blob(
    oc_root: "Path | None", auth_path: "Path | None",
) -> "tuple[str, str | None]":
    """Walk the source ladder. Returns ``(source, raw_json_text_or_None)``.

    ``source`` ∈ {``sqlite``, ``legacy_json``, ``bak``, ``none``}.
    """
    agent_dirs = (
        _discover_agent_dirs(oc_root / _AGENTS_RELDIR) if oc_root is not None else []
    )

    # 1) SQLite store (primary — the live post-migration store), main-first.
    for adir in agent_dirs:
        db = adir / _SQLITE_NAME
        try:
            present = db.exists()
        except OSError:
            present = False
        if present:
            blob = _read_sqlite_store(db)
            if blob:
                return "sqlite", blob

    # 2) Legacy auth-profiles.json (pre-migration pods). An explicit auth_path
    #    overrides discovery; otherwise read each agent dir's JSON.
    legacy_paths: list[Path] = []
    if auth_path is not None:
        legacy_paths.append(Path(auth_path))
    else:
        legacy_paths.extend(adir / _LEGACY_JSON_NAME for adir in agent_dirs)
    for p in legacy_paths:
        try:
            text = p.read_text()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if text.strip():
            return "legacy_json", text

    # 3) Transitional .sqlite-import.<ms>.bak (last resort, newest wins).
    for adir in agent_dirs:
        blob = _read_newest_bak(adir)
        if blob:
            return "bak", blob

    return "none", None


# ── Presence builders ─────────────────────────────────────────────────────────

def _keys_from_sqlite_profiles(profiles: dict) -> "dict[str, dict]":
    """Presence per provider from a migrated sqlite profiles blob.

    Secrets are NOT stored in the sqlite blob (OpenClaw moved them to the
    Keychain / encrypted store), so a CONFIGURED profile entry is the presence
    signal — not a non-empty key value. The provider comes from each profile's
    ``provider`` field (never parsed from the profileId), lowercased. The
    profile's mode sets api_key vs token; a present profile always counts for at
    least one of the two so its provider is included downstream.
    """
    keys: dict[str, dict] = {}
    for pid, p in (profiles or {}).items():
        if not isinstance(p, dict):
            continue
        provider = str(p.get("provider") or "").strip().lower()
        if not provider:
            continue
        mode = str(p.get("type", p.get("mode", ""))).strip().lower()
        pid_l = str(pid).lower()
        is_token = mode == "token" or pid_l.endswith(":token")
        # Anything that isn't explicitly a token profile counts as an api_key —
        # a present profile must mark at least one field True.
        is_api = not is_token
        entry = keys.setdefault(provider, {"api_key": False, "token": False})
        entry["api_key"] = entry["api_key"] or is_api
        entry["token"] = entry["token"] or is_token
    return keys


def _keys_from_value_profiles(profiles: dict) -> "dict[str, dict]":
    """Presence per provider from a legacy/bak JSON blob that still holds the
    raw key VALUES — presence is a non-empty value (the pre-migration semantics).
    """
    keys: dict[str, dict] = {}

    known_providers: set[str] = set()
    for p in (profiles or {}).values():
        prov = p.get("provider", "") if isinstance(p, dict) else ""
        if prov:
            known_providers.add(prov)

    for provider in sorted(known_providers):
        entry: dict = {}
        _, profile, field = find_profile(profiles, provider, "api_key")
        if profile is not None:
            val = profile.get(field, "")
            entry["api_key"] = bool(val and str(val).strip())
        else:
            entry["api_key"] = False
        if provider in _HAS_TOKEN:
            _, profile_t, field_t = find_profile(profiles, provider, "token")
            if profile_t is not None:
                val_t = profile_t.get(field_t, "")
                entry["token"] = bool(val_t and str(val_t).strip())
            else:
                entry["token"] = False
        keys[provider] = entry

    return keys


# ── Key operations ────────────────────────────────────────────────────────────

def json_keys(
    bot: str,
    auth_path: "Path | None" = None,
    *,
    oc_root: "Path | None" = None,
) -> dict:
    """Return key presence (True/False) per provider — never actual key values.

    Resolves credentials through the source ladder (sqlite → legacy json → bak).
    When run as the bot user, the store is discovered under the bot's own
    ``~/.openclaw``. ``auth_path`` pins the legacy JSON (and scopes discovery to
    its tree); ``oc_root`` pins the ``.openclaw`` root directly (test seam for a
    temp sqlite fixture).

    The result always carries a ``source`` field. ``source == "none"`` is the
    LOUD-fail marker: no readable credential source existed at all (sqlite AND
    legacy json AND bak all absent) — distinct from a readable store that
    legitimately holds zero providers (``source != "none"``, empty ``keys``).
    Callers MUST NOT treat ``source == "none"`` as "this bot has no providers".
    """
    if oc_root is None:
        oc_root = _derive_oc_root(auth_path) if auth_path is not None \
            else (Path.home() / _OC_RELROOT)

    source, blob = _resolve_auth_blob(oc_root, auth_path)
    if source == "none" or blob is None:
        return {
            "bot": bot,
            "keys": {},
            "source": "none",
            "error": "no readable OpenClaw credential store (schema may have changed)",
        }

    try:
        auth = _loads_lenient(blob)
    except Exception as e:
        return {"bot": bot, "keys": {}, "source": source,
                "error": f"could not parse auth store ({source}): {e}"}

    profiles = auth.get("profiles", {}) or {}
    if source == "sqlite":
        keys = _keys_from_sqlite_profiles(profiles)
    else:
        keys = _keys_from_value_profiles(profiles)

    return {"bot": bot, "keys": keys, "source": source}


def json_keys_set(
    bot: str,
    provider: str,
    mode: str,
    auth_path: Path | None = None,
) -> dict:
    """Read a key value from stdin and write it to auth-profiles.json.

    mode: "api_key" or "token"
    Key value is ALWAYS read from stdin — never passed as an argument.
    """
    key_value = sys.stdin.readline().strip()
    if not key_value:
        return {"error": "no key value provided on stdin", "ok": False}

    path = auth_path or _DEFAULT_AUTH_PROFILES
    try:
        auth = _load_json(path)
    except Exception as e:
        return {"error": f"could not read auth-profiles: {e}", "ok": False}

    profiles = auth.get("profiles", {})
    name, profile, field = find_profile(profiles, provider, mode)

    if profile is None:
        # Create a new profile entry. Shape depends on provider class:
        #   - LLM providers (anthropic, openai, google, xai): use
        #     ``<provider>:<mode>`` colon shape so OC's runtime model
        #     layer can find them via its ``<provider>:`` prefix
        #     lookup. Atlas hit this 2026-05-28 — its Anthropic key
        #     was filed under ``anthropic_api_key`` and forge step 2
        #     died with the misleading "no api key for openai" after
        #     OC fell through to its bundled OpenAI default.
        #   - Non-LLM providers (brave, runway, etc.): keep the
        #     underscore shape; they're consumed by skill code that
        #     reads the key by name directly.
        # Corrected in PR #1752 alongside the equivalent server.py path.
        _llm_providers = {"anthropic", "openai", "google", "xai"}
        field = "token" if mode == "token" else "key"
        sep = ":" if provider.lower() in _llm_providers else "_"
        profile_name = f"{provider}{sep}{mode}"
        profiles[profile_name] = {
            "provider": provider,
            "type": mode,
            field: key_value,
        }
    else:
        profile[field] = key_value

    auth["profiles"] = profiles
    try:
        _preserve_write(auth, path)
    except Exception as e:
        return {"error": f"could not write auth-profiles: {e}", "ok": False}

    return {"ok": True, "bot": bot, "provider": provider, "mode": mode}


def json_keys_remove(
    bot: str,
    provider: str,
    mode: str = "api_key",
    auth_path: Path | None = None,
) -> dict:
    """Remove a key profile entry from auth-profiles.json."""
    path = auth_path or _DEFAULT_AUTH_PROFILES
    try:
        auth = _load_json(path)
    except Exception as e:
        return {"error": f"could not read auth-profiles: {e}", "ok": False}

    profiles = auth.get("profiles", {})
    name, profile, field = find_profile(profiles, provider, mode)

    if name is None:
        return {"error": f"no profile found for provider={provider} mode={mode}", "ok": False}

    del profiles[name]
    auth["profiles"] = profiles
    try:
        _preserve_write(auth, path)
    except Exception as e:
        return {"error": f"could not write auth-profiles: {e}", "ok": False}

    return {"ok": True, "bot": bot, "provider": provider, "mode": mode}


# ── Standalone CLI entrypoint ─────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Usage:
        sudo -u team_bot_a python3 oc_keys.py keys team_bot_a
        sudo -u team_bot_a python3 oc_keys.py keys set team_bot_a anthropic api_key
        sudo -u team_bot_a python3 oc_keys.py keys set team_bot_a anthropic token
        sudo -u team_bot_a python3 oc_keys.py keys remove team_bot_a anthropic api_key
    """
    _args = sys.argv[1:]
    if len(_args) < 2 or _args[0] != "keys":
        print(json.dumps({"error": "usage: oc_keys.py keys {bot} | keys set {bot} {provider} {mode} | keys remove {bot} {provider} [{mode}]"}))
        sys.exit(1)

    _subcmd = _args[1]

    try:
        if _subcmd == "set":
            if len(_args) < 5:
                print(json.dumps({"error": "usage: oc_keys.py keys set {bot} {provider} {mode}"}))
                sys.exit(1)
            _result = json_keys_set(_args[2], _args[3], _args[4])
        elif _subcmd == "remove":
            if len(_args) < 4:
                print(json.dumps({"error": "usage: oc_keys.py keys remove {bot} {provider} [{mode}]"}))
                sys.exit(1)
            _mode = _args[4] if len(_args) > 4 else "api_key"
            _result = json_keys_remove(_args[2], _args[3], _mode)
        else:
            _bot_id = _args[1]
            _result = json_keys(_bot_id)
    except Exception as _exc:
        print(json.dumps({"error": str(_exc), "ok": False}))
        sys.exit(1)

    print(json.dumps(_result))
