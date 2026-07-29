"""oc_store — read-side adapter for OpenClaw's per-bot on-disk auth store.

This is the FIRST module of the planned OC-access adapter boundary: the single,
audited home for reading OpenClaw's auth store, regardless of which storage
backend a given OpenClaw version uses. Every Anthropic-key reader in Evolve
routes its SOURCE read through here; the PARSER stays
``primary_bot.extract_anthropic_key`` (shape-tolerant across profile-key
variants like ``anthropic:api`` vs ``anthropic:api_key``).

Why this exists
---------------
OpenClaw 2026.6.x migrated each bot's
``~/.openclaw/agents/main/agent/auth-profiles.json`` into a per-agent SQLite
store ``~/.openclaw/agents/main/agent/openclaw-agent.sqlite``, leaving the JSON
renamed to ``auth-profiles.json.sqlite-import.<epoch_ms>.bak``. Evolve's
key resolvers still read the now-absent JSON → returned ``""`` → the app
scanner aborted LLM discovery with ``error_kind="missing_api_key"`` pod-wide.
This module reads the new store (and the two transitional layouts) so the
resolvers see the key again.

Source ladder (``read_auth_store``)
-----------------------------------
  (a) SQLite ``auth_profile_store.store_json`` (the live post-migration store),
      opened **strictly read-only and WAL-safe** via the sqlite3 library.
  (b) legacy ``auth-profiles.json`` (pre-migration pods still have it).
  (c) newest ``auth-profiles.json.sqlite-import.*.bak`` (transitional).
Returns ``None`` only when ALL of those fail — and logs that LOUDLY (a silent
empty string is exactly the failure mode that caused the incident).

Privileged-read posture (auditor-grade)
---------------------------------------
The SQLite DB is bot-owned and secret-bearing (mode 0600). The admin server
runs as the ``evolve`` user, which holds a macOS ACL **read** on each bot's
``.openclaw/`` (``deploy.set_evolve_read_acl``), so the unprivileged read-only
open is the normal path. Defenses:
  * Open strictly read-only (``mode=ro`` + ``PRAGMA query_only``); never
    writable, never ``immutable=1`` (that would ignore the WAL and miss the
    latest write).
  * Never read the ``.sqlite`` bytes raw — the sqlite3 library resolves the
    WAL/-shm sidecars by name, so a raw byte read would miss WAL-pending rows.
  * Reject any symlinked component in the auth-store path before opening
    (``_validated_sqlite_path``): a compromised bot that swaps a path component
    for a symlink (to redirect the read at another file) is refused. There is
    an irreducible TOCTOU window between that check and the open/exec; the
    impact is bounded — the read is read-only and ``store_json`` is never
    logged. A redirect at a NON-database file leaks nothing (sqlite ``-readonly``
    returns "file is not a database"); the one residual is a redirect at
    *another bot's* valid store, which yields that bot's key — but the resolved
    key is used only in-process for the originating bot's own scan, and the
    ``evolve`` reader already holds an ACL read on every bot's store, so this is
    a cost-attribution confusion under a tight race, not a disclosure escalation.
  * The root ``sudo /usr/bin/sqlite3 -readonly`` fallback (used only when the
    unprivileged open hits a permission error on a pre-ACL bot) passes the
    ``-readonly`` flag, so the DB is opened read-only by the binary itself —
    no write SQL can take effect regardless of the statement.

Stdlib-only; safe to import from both the admin and analyzer packages.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

logger = logging.getLogger("evolve.oc_store")

# Per-agent layout, relative to the bot's home. Stable across the OC versions
# this module targets. The agent id is DISCOVERED (``_discover_agent_ids``),
# main-first — not hardcoded: some bots run their primary under a non-``main``
# agent id (e.g. ``email-reader``), and the credential store lives under that
# id's dir. ``_AGENT_RELDIR`` remains the conventional ``main`` path used as the
# discovery fallback so a bot with the historical layout behaves byte-for-byte
# as before.
_OPENCLAW_RELDIR = ".openclaw"
_AGENTS_RELDIR = "agents"
_AGENT_SUBDIR = "agent"
_DEFAULT_AGENT_ID = "main"
_AGENT_RELDIR: tuple[str, ...] = (_OPENCLAW_RELDIR, _AGENTS_RELDIR, _DEFAULT_AGENT_ID, _AGENT_SUBDIR)
_SQLITE_NAME = "openclaw-agent.sqlite"
_LEGACY_JSON_NAME = "auth-profiles.json"
_BAK_PREFIX = "auth-profiles.json.sqlite-import."
_BAK_SUFFIX = ".bak"

# SQLite store contract (verified live on the mini, OC 2026.6.x):
#   CREATE TABLE auth_profile_store (store_key TEXT PRIMARY KEY,
#                                    store_json TEXT NOT NULL,
#                                    updated_at INTEGER NOT NULL);
# store_key is always "primary"; store_json is the EXACT old
# auth-profiles.json bytes.
_STORE_KEY = "primary"
_SELECT_SQL = "SELECT store_json FROM auth_profile_store WHERE store_key='primary'"

# Full path; must match the _render_evolve_sudoers grant and the
# platform_profile command table (basename ``sqlite3``).
_SQLITE3_BIN = "/usr/bin/sqlite3"


# ── Home resolution ───────────────────────────────────────────────────────────


def _resolve_home(
    bot_id: str | None,
    *,
    user: str | None = None,
    home: "Path | None" = None,
    network: "dict | None" = None,
) -> "Path | None":
    """Resolve a bot's macOS home directory.

    Precedence (first usable wins):
      1. ``home`` — caller already resolved it (e.g. ``bot_home(bot_id, net)``).
      2. ``user`` — an OS ACCOUNT name; resolved by the blessed
         ``evolve_config.user_home`` (pwd, with a platform-profile fallback —
         never a hardcoded ``/Users/`` literal here).
      3. ``bot_id`` — logical name; resolve the account via the canonical
         ``evolve_config`` helpers (bot_id ≠ account name in general), then
         hand the account to ``user_home``.

    Returns ``None`` only when nothing resolvable was supplied, or the blessed
    account→home resolver is unavailable.
    """
    if home is not None:
        return Path(home)
    # Blessed account→home resolver. Lazy import keeps this module stdlib-only
    # at import time and avoids an admin/analyzer import cycle; account-name
    # home math (pwd + platform-profile fallback) lives in evolve_config, never
    # duplicated here.
    try:
        from evolve_config import user_home  # type: ignore
    except Exception as exc:
        logger.error(
            "oc_store: evolve_config.user_home unavailable (%s) — cannot resolve "
            "a home dir for bot_id=%r user=%r",
            exc,
            bot_id,
            user,
        )
        return None
    if user:
        return user_home(user)
    if not bot_id:
        return None
    # Resolve the account name from the logical bot_id (bot_id ≠ account name).
    account = bot_id
    try:
        from evolve_config import get_bot_user as _get_bot_user  # type: ignore

        if network is None:
            try:
                from evolve_config import load_network as _load_network  # type: ignore

                network = _load_network()
            except Exception as exc:
                logger.debug(
                    "oc_store: load_network failed (%s); resolving with empty network",
                    exc,
                )
                network = {}
        resolved = _get_bot_user(bot_id, network or {})
        if isinstance(resolved, str) and resolved:
            account = resolved
    except Exception as exc:
        # Common-case fall-through: the bot_id IS the account name. Logged (not
        # silently swallowed) so a real resolver failure is still visible.
        logger.debug(
            "oc_store: get_bot_user failed for bot_id=%r (%s); assuming "
            "account==bot_id",
            bot_id,
            exc,
        )
    return user_home(account)


# ── Agent-dir discovery (main-first, never hardcoded) ─────────────────────────


def _agent_rel(agent_id: str) -> "tuple[str, ...]":
    """Relative components of an agent's dir under the bot home."""
    return (_OPENCLAW_RELDIR, _AGENTS_RELDIR, agent_id, _AGENT_SUBDIR)


def _discover_agent_ids(home: Path) -> "list[str]":
    """Return the bot's OpenClaw agent ids, ``main`` first.

    Globs ``<home>/.openclaw/agents/<id>/`` and sorts ``main`` to the front so a
    bot with the historical single-``main`` layout resolves to exactly
    ``["main"]`` (byte-for-byte the old behavior), while a bot whose primary
    agent is non-``main`` (e.g. ``email-reader``) is still found. The
    conventional ``main`` id is always included as the discovery fallback — if
    the agents dir can't be listed (missing / unreadable parent), we still try
    ``main`` so this is never *worse* than the prior hardcode.
    """
    agents_root = home / _OPENCLAW_RELDIR / _AGENTS_RELDIR
    try:
        ids = [p.name for p in agents_root.iterdir() if p.is_dir()]
    except OSError:
        ids = []
    if _DEFAULT_AGENT_ID not in ids:
        ids.append(_DEFAULT_AGENT_ID)
    ids.sort(key=lambda n: (n != _DEFAULT_AGENT_ID, n))  # 'main' sorts first
    return ids


# ── Path validation (privileged-read guard) ───────────────────────────────────


def _validated_sqlite_path(home: Path, agent_id: str = _DEFAULT_AGENT_ID) -> "Path | None":
    """Resolve the per-agent sqlite path, rejecting redirected reads.

    Two privileged-read guards before any open:
      * **No symlinked component.** Walks each OpenClaw-owned component under
        ``home`` and refuses if any is a symlink — a compromised bot that swaps
        a component for a symlink (to redirect the read at another file) is
        stopped here.
      * **Owner consistency.** The db file must be owned by the same uid as the
        bot's home dir. A symlink check alone doesn't catch a *hardlink* (a
        regular file by every stat); requiring the db's owner to match the
        home owner refuses a hardlink to a file the bot doesn't own (another
        bot's store, a root-owned secret), since the bot can't reparent
        ownership.

    Returns the path only when it exists as a regular, non-symlink, owner-
    consistent file; ``None`` otherwise (missing store, redirect attempt, or
    unreadable parent).
    """
    try:
        home_uid = os.stat(home).st_uid
    except OSError:
        return None
    cur = home
    for part in _agent_rel(agent_id):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            logger.warning(
                "oc_store: refusing symlinked component in auth-store path: %s", cur
            )
            return None
    db = cur / _SQLITE_NAME
    try:
        st = os.lstat(db)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        logger.warning("oc_store: refusing symlinked auth-store db: %s", db)
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_uid != home_uid:
        logger.warning(
            "oc_store: refusing auth-store db owned by uid %s (home owner is uid %s) "
            "— possible hardlink redirect: %s",
            st.st_uid,
            home_uid,
            db,
        )
        return None
    return db


# ── SQLite read (read-only, WAL-safe) ─────────────────────────────────────────


def _read_sqlite_store(db: Path) -> "str | None":
    """Return ``store_json`` for the primary profile, or ``None``.

    Tries an unprivileged read-only library open first (the common path — the
    evolve user holds an ACL read on the bot's ``.openclaw`` tree). Any
    ``OperationalError`` — permission-denied, a read-only-WAL ``-shm`` init
    failure, OR a "no such table" on an unexpected schema — routes to the root
    ``sudo /usr/bin/sqlite3 -readonly`` fallback (harmless if it also fails;
    it just returns ``None`` and the caller falls through to the legacy/bak
    sources). Any other sqlite error (e.g. corruption / ``DatabaseError``)
    returns ``None`` directly.
    """
    uri = f"file:{db}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute(_SELECT_SQL).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        # The permission/-shm-init shapes ("unable to open database file",
        # "attempt to write a readonly database" when a read-only WAL open
        # can't initialise -shm) both surface as OperationalError, at connect
        # OR at first execute. Fall back to the root sqlite3 -readonly read,
        # which can initialise the WAL shared-memory.
        logger.debug("oc_store: read-only sqlite open/query failed (%s); trying sudo", e)
        return _read_sqlite_store_via_sudo(db)
    except sqlite3.Error:
        return None
    if row and isinstance(row[0], str) and row[0].strip():
        return row[0]
    return None


def _read_sqlite_store_via_sudo(db: Path) -> "str | None":
    """Root read-only SELECT fallback for pre-ACL bots.

    Granted in ``setup_wizard._render_evolve_sudoers`` (§2a). The ``-readonly``
    flag makes the binary open the DB read-only, so no statement can mutate it.
    """
    try:
        # sudo-grant: granted §2a — sqlite3 -readonly SELECT on the per-agent
        # auth store; the -readonly flag enforces a read-only open regardless
        # of the SQL argument.
        r = subprocess.run(
            ["sudo", _SQLITE3_BIN, "-readonly", str(db), _SELECT_SQL],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


# ── Text-file sources (legacy json + transitional bak) ────────────────────────


def _read_text_file(path: Path, *, allow_sudo_cat: bool) -> "str | None":
    """Read a JSON file as text; ``None`` if absent/unreadable.

    ``allow_sudo_cat`` permits the ``sudo /bin/cat`` root fallback on
    PermissionError (granted only for ``auth-profiles.json`` — the ``.bak``
    transitional file has no cat grant and relies on the evolve read ACL).
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        if not allow_sudo_cat:
            return None
        try:
            # sudo-grant: granted §2 — /bin/cat on the legacy auth-profiles.json.
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return None
        if r.returncode != 0 or not r.stdout:
            return None
        text = r.stdout
    return text if text.strip() else None


def _read_newest_bak(agent_dir: Path) -> "str | None":
    """Read the newest ``auth-profiles.json.sqlite-import.<ms>.bak``, if any.

    Sorted by the embedded epoch-ms suffix (lexically equivalent for fixed-
    width ints, and a numeric sort otherwise) so the most recent migration
    snapshot wins. Direct read only — the evolve read ACL covers it.
    """
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
        mid = p.name[len(_BAK_PREFIX) : -len(_BAK_SUFFIX)]
        try:
            return int(mid)
        except ValueError:
            return -1

    for p in sorted(candidates, key=_bak_epoch, reverse=True):
        text = _read_text_file(p, allow_sudo_cat=False)
        if text:
            return text
    return None


# ── Public API ────────────────────────────────────────────────────────────────


def read_auth_store(
    bot_id: str | None,
    *,
    user: str | None = None,
    home: "Path | None" = None,
    network: "dict | None" = None,
) -> "str | None":
    """Return the raw auth-profiles JSON string for a bot, or ``None``.

    Walks the source ladder (sqlite store → legacy json → newest bak). The
    caller's preferred way to identify the bot is whichever of ``home`` /
    ``user`` / ``bot_id`` it already holds (see :func:`_resolve_home`).

    Returns ``None`` only when EVERY source fails — and logs that loudly. A
    bot that authenticates to Anthropic via the gateway token (no raw key)
    still returns its JSON here (the parser simply finds no anthropic api_key),
    so ``None`` means a genuine read failure, never "this bot has no key".
    """
    home_path = _resolve_home(bot_id, user=user, home=home, network=network)
    if home_path is None:
        logger.error(
            "oc_store: cannot resolve a home dir for bot_id=%r user=%r — "
            "no auth store to read",
            bot_id,
            user,
        )
        return None

    agent_ids = _discover_agent_ids(home_path)
    agent_dirs = [home_path.joinpath(*_agent_rel(aid)) for aid in agent_ids]

    # Walk the source ladder ACROSS all discovered agent dirs (main-first):
    # sqlite first for every agent, then legacy json, then bak — mirroring the
    # bot-context sibling ``analyzer.oc_keys._resolve_auth_blob``. For the common
    # single-``main`` bot this is identical to the prior single-dir ladder.

    # (a) Per-agent sqlite store (post-2026.6 OpenClaw).
    for aid in agent_ids:
        db = _validated_sqlite_path(home_path, aid)
        if db is not None:
            js = _read_sqlite_store(db)
            if js:
                return js

    # (b) Legacy auth-profiles.json (pre-migration pods).
    for agent_dir in agent_dirs:
        js = _read_text_file(agent_dir / _LEGACY_JSON_NAME, allow_sudo_cat=True)
        if js:
            return js

    # (c) Transitional .sqlite-import.<ms>.bak (newest wins).
    for agent_dir in agent_dirs:
        js = _read_newest_bak(agent_dir)
        if js:
            return js

    logger.error(
        "oc_store: NO auth source resolved for bot_id=%r (home=%s, agents=%s) — "
        "checked the sqlite store, legacy auth-profiles.json, and .sqlite-import "
        "bak for each agent. Anthropic-key resolution will degrade for this bot.",
        bot_id,
        home_path,
        ", ".join(agent_ids),
    )
    return None


def auth_store_present(
    bot_id: str | None,
    *,
    user: str | None = None,
    home: "Path | None" = None,
    network: "dict | None" = None,
) -> "bool | None":
    """Report whether ANY auth-store artifact exists on disk for a bot.

    Returns ``True`` if the sqlite store, the legacy ``auth-profiles.json``, or
    a ``.sqlite-import.*.bak`` snapshot exists; ``False`` if none do; ``None``
    if the bot's home dir can't even be resolved.

    This distinguishes the two ``read_auth_store() is None`` cases the safe-
    upgrade dependency probe must tell apart:
      * artifact PRESENT but the reader returned ``None`` → reader/format drift
        (the 2026-06-22 incident shape — a load-bearing failure), versus
      * NO artifact at all → a gateway-auth bot that never had a raw key
        (a valid, keyless state — not a failure).

    Stat errors under a restrictive parent fail SAFE to ``True`` (present):
    "couldn't tell" must not masquerade as "definitely absent" and silence the
    drift case (cf. the ``exists()`` lies under 0700 parents gotcha). NB this
    uses ``os.lstat`` (which RAISES ``EACCES``), not ``Path.exists()`` — on
    Python 3.13+ ``Path.exists()`` swallows ``EACCES`` and returns ``False``,
    which would defeat the fail-safe on the pod's 3.14 interpreter.
    """
    home_path = _resolve_home(bot_id, user=user, home=home, network=network)
    if home_path is None:
        return None
    # Check every discovered agent dir (main-first) — a bot whose only store
    # lives under a non-``main`` agent must still read as present.
    for agent_dir in (
        home_path.joinpath(*_agent_rel(aid)) for aid in _discover_agent_ids(home_path)
    ):
        for name in (_SQLITE_NAME, _LEGACY_JSON_NAME):
            try:
                os.lstat(agent_dir / name)
                return True  # a dir entry is present (regular file or symlink)
            except FileNotFoundError:
                continue
            except OSError:
                # EACCES/ELOOP/etc — can't determine. Fail SAFE to present so a
                # present-but-unreadable store is never misread as "no artifact".
                return True
        try:
            entries = list(agent_dir.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            # Parent unreadable — can't tell; fail safe to present.
            logger.debug("oc_store: could not list %s for bak snapshots (%s)", agent_dir, exc)
            return True
        for p in entries:
            if p.name.startswith(_BAK_PREFIX) and p.name.endswith(_BAK_SUFFIX):
                return True
    return False


def read_anthropic_key(
    bot_id: str | None,
    *,
    user: str | None = None,
    home: "Path | None" = None,
    network: "dict | None" = None,
) -> str:
    """Resolve a bot's Anthropic api_key from its auth store, or ``""``.

    SOURCE is :func:`read_auth_store`; PARSER is
    ``primary_bot.extract_anthropic_key`` (kept as the single shape-tolerant
    extractor). Returns ``""`` when no key is present — which is a *valid*
    state for a gateway-auth bot that never had a raw key; callers must degrade
    (e.g. a structural scan), not hard-fail.
    """
    raw = read_auth_store(bot_id, user=user, home=home, network=network)
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.error(
            "oc_store: auth store for bot_id=%r is not valid JSON — cannot extract key",
            bot_id,
        )
        return ""
    try:
        from primary_bot import extract_anthropic_key  # type: ignore
    except Exception:
        logger.error("oc_store: primary_bot.extract_anthropic_key unavailable")
        return ""
    return extract_anthropic_key(parsed) or ""
