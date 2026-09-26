"""oc_auth_store — THE reader for OpenClaw's per-bot on-disk auth store.

Single, audited home for reading OpenClaw's auth store, regardless of which
storage backend a given OpenClaw version uses. It lives analyzer-side (not
admin-side) because BOTH packages need it and the dependency arrow only points
one way: admin depends on analyzer, never the reverse.

  * ``evolve_admin.oc_store`` is now a thin delegating shim over this module
    (its public API is unchanged — the target-bot / scanner read path).
  * ``primary_bot``'s engine-credential readers walk this same ladder (the
    primary-bot / infra_llm read path).

Before #3475 those were two independent readers with different source ladders —
"two readers, one blind" — and the primary-bot one silently missed a store that
the admin one found. One reader means a storage move can only ever break (or
fix) both at once.

Why this exists
---------------
OpenClaw 2026.6.x migrated each bot's
``~/.openclaw/agents/main/agent/auth-profiles.json`` into a per-agent SQLite
store ``~/.openclaw/agents/main/agent/openclaw-agent.sqlite``, leaving the JSON
renamed to ``auth-profiles.json.sqlite-import.<epoch_ms>.bak``. Evolve's
key resolvers still read the now-absent JSON → returned ``""`` → the app
scanner aborted LLM discovery with ``error_kind="missing_api_key"`` pod-wide.
This module reads the new store (and the two transitional layouts) so the
resolvers see the key again. OpenClaw's auth doctor performs that migration
one-way per pod: when every profile verifies into SQLite it BACKS UP AND
REMOVES the JSON, so "read the JSON" is not a survivable strategy.

Source ladder (``iter_auth_store_payloads`` / ``read_auth_store``)
-----------------------------------------------------------------
  (a) SQLite ``auth_profile_store.store_json`` (the live post-migration store),
      opened **strictly read-only and WAL-safe** via the sqlite3 library.
  (b) legacy ``auth-profiles.json`` (pre-migration pods still have it).
  (c) newest ``auth-profiles.json.sqlite-import.*.bak`` (transitional).
Each rung is walked across every discovered agent id (``main`` first).
``read_auth_store`` returns the first source that yields content and returns
``None`` only when ALL of them fail — logging that LOUDLY (a silent empty
string is exactly the failure mode that caused the incident).
``iter_auth_store_payloads`` yields EVERY source in the same order, for callers
that want to keep looking when an earlier source parsed but carried nothing
they needed (``primary_bot``'s per-provider key readers do this).

Inline ACL-clamp heal (#3477)
-----------------------------
On Linux the OC gateway re-hardens ``agents/main/agent`` to 0700 on an auth
write, and that chmod recalculates the POSIX-ACL mask to ``---`` — capping
evolve's inherited ``user:evolve:r-x`` ACE to ``#effective:---``. Every rung of
the ladder above then hits EACCES and, before this, reported the store as
*absent*: ``resolve_infra_llm()`` returned ``None`` and every engine LLM feature
said "no provider credentialed". Detection and repair both already existed
(``secret_config_perms`` verify facet 0b / ``reassert_evolve_access``), but only
on the HOURLY ``pod_perms_drift_monitor`` cadence — so a clamp landing at
21:47 stayed dark until 22:39 (observed live, evolve-vps 2026-07-29: a
~52-minute silent engine-dark window with no Signal and no trace).

So the ladder heals INLINE, mirroring ``bot_forge._outbox_ready``: an EACCES on
any source is recorded (never mistaken for "absent"), and when NO source was
readable the canonical ``secret_config_perms.heal_evolve_access`` is invoked
once — throttled per bot account, never looped — and the ladder is retried. The
admin import is lazy, inside the failure branch only: the dependency arrow runs
admin → analyzer, never the reverse, at module scope.

Three outcomes, deliberately distinguished:
  * no EACCES at all → byte-identical to the pre-#3477 behavior (no heal, no
    Signal). An unclamped pod pays nothing.
  * clamped → healed → payload found → SUCCESS. A ``WARNING`` log names the
    clamped paths and the heal outcome (the durable trace a post-hoc "why was
    the engine quiet last night?" needs) but NO Signal — a self-healed clamp
    staying silent is the anti-flap design, not an oversight.
  * clamped → heal ran → STILL unreadable → genuinely engine-dark. That fires
    the ``credential_access`` Signal (see :mod:`credential_access_signal`).

SQLite is deliberately ahead of the legacy JSON: post-migration the JSON is
either absent or a partial write-back holding the profiles OpenClaw could NOT
verify into SQLite, so preferring it would prefer the unverified credential.

Privileged-read posture (auditor-grade)
---------------------------------------
The SQLite DB is bot-owned and secret-bearing (mode 0600). The admin server
runs as the ``evolve`` user, which holds a macOS ACL **read** on each bot's
``.openclaw/`` (``deploy.set_evolve_read_acl``), so the unprivileged read-only
open is the normal path. Defenses:
  * Open strictly read-only (``mode=ro`` + ``PRAGMA query_only``); never
    writable. ``immutable=1`` is a LAST-ditch rung only (it ignores the WAL and
    can return a stale value) — never the first read.
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

Stdlib-only at import time; safe to import from both the admin and analyzer
packages.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import sqlite3
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

# Log channel is deliberately still ``evolve.oc_store``: operators (and the
# pinned admin tests) grep that name, and the channel identity is part of this
# reader's observable contract — the code moved, the channel did not.
logger = logging.getLogger("evolve.oc_store")

__all__ = [
    "read_auth_store",
    "iter_auth_store_payloads",
    "auth_store_present",
    "read_anthropic_key",
    "read_sqlite_store_json",
    # Inline-heal test seams (#3477).
    "set_heal_fn",
    "reset_heal_throttle",
]

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


def _resolve_account(
    bot_id: str | None,
    *,
    user: str | None = None,
    network: "dict | None" = None,
) -> "str | None":
    """Resolve the OS ACCOUNT name for a bot, or ``None``.

    ``user`` (already an account name) wins; otherwise the logical ``bot_id``
    is mapped through ``evolve_config.get_bot_user`` (bot_id ≠ account name in
    general), falling back to ``bot_id`` itself — the common case.

    Split out of :func:`_resolve_home` because the inline ACL heal needs the
    account even when the caller passed an already-resolved ``home`` (the
    ``heal_evolve_access(bot_id, bot_user)`` contract takes both).

    Cost: pure name math — and therefore free on the hot path — *when the caller
    supplies* ``user`` or ``network``, which every hot-path caller does. With
    neither, ``get_bot_user`` loads network.json itself (uncached), so this
    becomes one small read. Correctness wins there: substituting an empty
    network to keep it I/O-free is exactly the bug this comment block records.
    """
    if user:
        return user
    if not bot_id:
        return None
    account = bot_id
    try:
        from evolve_config import get_bot_user as _get_bot_user  # type: ignore

        # Hand ``network`` straight through — INCLUDING None. get_bot_user's own
        # ``if config is None: config = load_config()`` is the blessed loader;
        # anything substituted here bypasses it.
        #
        # This used to read ``_get_bot_user(bot_id, network or {})`` behind a
        # ``from evolve_config import load_network`` that has never existed —
        # evolve_config exports ``load_config``; ``load_network`` lives in the
        # admin package. So the import always raised, the handler set
        # ``network = {}``, and ``{}`` (not being None) then defeated
        # get_bot_user's self-load: every lookup missed and fell back to the
        # bot_id. Bots whose account name IS their bot_id were unaffected, which
        # is why it went unnoticed; aliased bots silently resolved to a home that
        # is either absent (no home exists at the bot_id name at all: a loud
        # read failure) or STALE — the worse half: `evolve` resolved to
        # /Users/evolve, the abandoned pre-account-separation profile, and read
        # the WRONG credentials while reporting success. Measured on the
        # reference pod 2026-08-18: 2 of 9 bots aliased, both broken.
        #
        # Passing None makes get_bot_user read network.json. That is a real read
        # (load_config is uncached), so callers on a hot path should keep passing
        # ``network``/``user``/``home`` as they already do — see the docstring.
        resolved = _get_bot_user(bot_id, network)
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
    return account


def _resolve_home(
    bot_id: str | None,
    *,
    user: str | None = None,
    home: "Path | None" = None,
    network: "dict | None" = None,
) -> "Path | None":
    """Resolve a bot's home directory.

    Precedence (first usable wins):
      1. ``home`` — caller already resolved it (e.g. ``bot_home(bot_id, net)``).
      2. ``user`` — an OS ACCOUNT name; resolved by the blessed
         ``evolve_config.user_home`` (pwd, with a platform-profile fallback —
         never a hardcoded ``/Users/`` literal here).
      3. ``bot_id`` — logical name; resolve the account via
         :func:`_resolve_account` (bot_id ≠ account name in general), then hand
         the account to ``user_home``.

    Returns ``None`` only when nothing resolvable was supplied, or the blessed
    account→home resolver is unavailable.
    """
    if home is not None:
        return Path(home)
    # Blessed account→home resolver. Lazy import keeps this module stdlib-only
    # at import time; account-name home math (pwd + platform-profile fallback)
    # lives in evolve_config, never duplicated here.
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
    account = _resolve_account(bot_id, user=user, network=network)
    if not account:
        return None
    return user_home(account)


# ── EACCES observation (the clamp side-channel) ────────────────────────────────
#
# Every rung of the ladder below deliberately swallows OSError so a missing or
# malformed source degrades to "skip this source". That swallow is also what
# made the ACL clamp indistinguishable from an absent store (#3477). Each rung
# now reports an EACCES into a caller-supplied ``clamp`` dict — a pure
# side-channel: ``None`` (the default) restores the exact prior behavior, so
# every existing caller and every non-clamped read is untouched.


def _note_eacces(clamp: "dict | None", path: Path, exc: BaseException) -> None:
    """Record ``path`` as permission-denied, if that is what ``exc`` says.

    Only EACCES/EPERM count. ENOENT, ELOOP, ENOTDIR and friends are genuine
    "this source isn't here / isn't usable" answers and must NOT trigger a
    privileged heal — the whole point is to separate "locked out" from "absent".
    """
    if clamp is None:
        return
    if getattr(exc, "errno", None) not in (errno.EACCES, errno.EPERM):
        return
    paths = clamp.setdefault("paths", [])
    text = str(path)
    if text not in paths:
        paths.append(text)


def _clamped_paths(clamp: "dict | None") -> "list[str]":
    """The EACCES paths recorded in ``clamp`` (empty when there were none)."""
    if not clamp:
        return []
    got = clamp.get("paths")
    return list(got) if isinstance(got, list) else []


# ── Agent-dir discovery (main-first, never hardcoded) ─────────────────────────


def _agent_rel(agent_id: str) -> "tuple[str, ...]":
    """Relative components of an agent's dir under the bot home."""
    return (_OPENCLAW_RELDIR, _AGENTS_RELDIR, agent_id, _AGENT_SUBDIR)


def _discover_agent_ids(home: Path, clamp: "dict | None" = None) -> "list[str]":
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
    except OSError as exc:
        # A clamped ``.openclaw`` (or ``agents``) hides every agent id at once —
        # record it so the caller can tell "locked out" from "no agents dir".
        _note_eacces(clamp, agents_root, exc)
        ids = []
    if _DEFAULT_AGENT_ID not in ids:
        ids.append(_DEFAULT_AGENT_ID)
    ids.sort(key=lambda n: (n != _DEFAULT_AGENT_ID, n))  # 'main' sorts first
    return ids


# ── Path validation (privileged-read guard) ───────────────────────────────────


def _validated_sqlite_path(
    home: Path,
    agent_id: str = _DEFAULT_AGENT_ID,
    clamp: "dict | None" = None,
) -> "Path | None":
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
    except OSError as exc:
        _note_eacces(clamp, home, exc)
        return None
    cur = home
    for part in _agent_rel(agent_id):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError as exc:
            _note_eacces(clamp, cur, exc)
            return None
        if stat.S_ISLNK(st.st_mode):
            logger.warning(
                "oc_store: refusing symlinked component in auth-store path: %s", cur
            )
            return None
    db = cur / _SQLITE_NAME
    try:
        st = os.lstat(db)
    except OSError as exc:
        # THE 2026-07-29 shape: ``agents/main/agent`` itself lstats fine (its
        # parent is traversable) but the clamped mask denies the x bit needed to
        # stat anything INSIDE it, so the db "vanishes" with EACCES.
        _note_eacces(clamp, db, exc)
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

    Three rungs, most-current first:

      1. An unprivileged read-only library open (``mode=ro`` + ``PRAGMA
         query_only``) — the common path, since the evolve user holds an ACL
         read on the bot's ``.openclaw`` tree. WAL-safe: it consults the ``-wal``
         sidecar, so a row the running gateway has committed but not yet
         checkpointed is visible.
      2. The root ``sudo /usr/bin/sqlite3 -readonly`` read — for a pre-ACL bot,
         or when a read-only open cannot initialise the ``-shm`` sidecar.
      3. ``immutable=1`` — a last-ditch read of the MAIN db file only. It
         ignores the WAL (so it can return a stale value, hence last) but it
         needs neither write access to the dir nor the sudo grant, which is the
         one case rung 2 cannot cover: a pod whose sudoers has not been
         refreshed with the sqlite3 grant (``refresh-sudoers`` is manual by
         design). A clean last-connection close checkpoints the WAL into the
         main file, so with the gateway down this read IS current.

    Any ``OperationalError`` — permission-denied, a read-only-WAL ``-shm`` init
    failure, OR a "no such table" on an unexpected schema — moves to the next
    rung; exhausting them returns ``None`` and the caller falls through to the
    legacy/bak sources. Any other sqlite error (e.g. corruption /
    ``DatabaseError``) returns ``None`` directly.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
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
        # which can initialise the WAL shared-memory, then to immutable=1.
        logger.debug("oc_store: read-only sqlite open/query failed (%s); trying sudo", e)
        via_sudo = _read_sqlite_store_via_sudo(db)
        return via_sudo if via_sudo else _read_sqlite_store_immutable(db)
    except sqlite3.Error:
        return None
    if row and isinstance(row[0], str) and row[0].strip():
        return row[0]
    return None


def _read_sqlite_store_immutable(db: Path) -> "str | None":
    """Last-ditch ``immutable=1`` read of the main db file (rung 3 above).

    Unprivileged and side-effect-free — it never creates the ``-shm``/``-wal``
    sidecars, which is why it survives a read-only ACL — but it also never
    consults the WAL, so a value the live gateway has not checkpointed is
    invisible here. Only ever reached after both current-value rungs failed.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=2.0)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute(_SELECT_SQL).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.debug("oc_store: immutable sqlite read failed (%s)", e)
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


def read_sqlite_store_json(db: Path) -> "dict | None":
    """Parsed ``store_json`` from an EXPLICIT sqlite store path, or ``None``.

    The by-path entry point (``primary_bot.read_agent_auth_store_json`` is its
    caller): the ladder entry points below take a bot identity and walk every
    source, this one reads exactly the DB it is handed. Tolerant of OpenClaw
    schema drift — a missing file/table/column, a malformed payload, or a
    non-dict blob all yield ``None``, never an exception.

    NB the caller-supplied path skips the symlink / owner guards
    (:func:`_validated_sqlite_path`), which only the identity-driven ladder can
    apply; pass a path you resolved yourself.
    """
    try:
        if not Path(db).exists():
            return None
    except OSError:
        return None
    raw = _read_sqlite_store(Path(db))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ── Text-file sources (legacy json + transitional bak) ────────────────────────


def _read_text_file(
    path: Path, *, allow_sudo_cat: bool, clamp: "dict | None" = None
) -> "str | None":
    """Read a JSON file as text; ``None`` if absent/unreadable.

    ``allow_sudo_cat`` permits the ``sudo /bin/cat`` root fallback on
    PermissionError (granted only for ``auth-profiles.json`` — the ``.bak``
    transitional file has no cat grant and relies on the evolve read ACL).
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (PermissionError, OSError) as exc:
        # Recorded BEFORE the sudo-cat attempt: if that root read succeeds we
        # return content and the caller never looks at ``clamp`` — but if it
        # fails (or is not granted for this file) the clamp must be visible.
        _note_eacces(clamp, path, exc)
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


def _read_newest_bak(agent_dir: Path, clamp: "dict | None" = None) -> "str | None":
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
    except OSError as exc:
        _note_eacces(clamp, agent_dir, exc)
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
        text = _read_text_file(p, allow_sudo_cat=False, clamp=clamp)
        if text:
            return text
    return None


# ── The source ladder ─────────────────────────────────────────────────────────


def _iter_payloads_for_home(
    home_path: Path, clamp: "dict | None" = None
) -> "Iterator[str]":
    """Yield every readable auth-store payload under ``home_path``, in ladder
    order: sqlite (each agent, main-first) → legacy json → newest bak.

    Mirrors the bot-context sibling ``oc_keys._resolve_auth_blob``. For the
    common single-``main`` bot this is the historical single-dir ladder.

    ``clamp`` is the optional EACCES side-channel (see :func:`_note_eacces`);
    the default ``None`` makes this byte-for-byte the pre-#3477 walk.
    """
    agent_ids = _discover_agent_ids(home_path, clamp)
    agent_dirs = [home_path.joinpath(*_agent_rel(aid)) for aid in agent_ids]

    # (a) Per-agent sqlite store (post-2026.6 OpenClaw).
    for aid in agent_ids:
        db = _validated_sqlite_path(home_path, aid, clamp)
        if db is not None:
            js = _read_sqlite_store(db)
            if js:
                yield js

    # (b) Legacy auth-profiles.json (pre-migration pods).
    for agent_dir in agent_dirs:
        js = _read_text_file(
            agent_dir / _LEGACY_JSON_NAME, allow_sudo_cat=True, clamp=clamp
        )
        if js:
            yield js

    # (c) Transitional .sqlite-import.<ms>.bak (newest wins).
    for agent_dir in agent_dirs:
        js = _read_newest_bak(agent_dir, clamp)
        if js:
            yield js


# ── Inline ACL-clamp heal + retry (#3477) ─────────────────────────────────────

# Minimum seconds between heal attempts for the SAME bot account within one
# process. Mirrors ``bot_forge._HEAL_THROTTLE_SEC`` (10s there, for a 2s poll
# loop); the credential ladder is walked far less often, so a wider window is
# still responsive while making a setfacl storm impossible. Keyed PER ACCOUNT so
# a pod-wide scanner sweep can heal bot 2 even though it just healed bot 1.
_HEAL_THROTTLE_SEC = 30.0

# account -> time.monotonic() of the last heal attempt. Process-local; a
# short-lived daemon run gets exactly one attempt per bot, which is the point.
_HEAL_STATE: "dict[str, float]" = {}

# Test seam: ``callable(bot_id, bot_user) -> bool``. ``None`` (production) means
# the canonical ``evolve_admin.secret_config_perms.heal_evolve_access``, imported
# LAZILY inside the failure branch only — analyzer must never import admin at
# module scope (the dependency arrow is admin → analyzer).
_HEAL_FN = None


def set_heal_fn(fn) -> None:
    """Install (or clear, with ``None``) the ACL-heal seam. Tests only."""
    global _HEAL_FN
    _HEAL_FN = fn


def reset_heal_throttle() -> None:
    """Clear the per-account heal throttle. Tests only."""
    _HEAL_STATE.clear()


def _attempt_clamp_heal(
    bot_id: "str | None", account: str, clamped: "list[str]"
) -> "str | None":
    """One throttled canonical-heal attempt for a clamped credential dir.

    Returns a short human-readable outcome string when a heal was ATTEMPTED
    (the caller then retries the ladder), or ``None`` when no attempt was made
    (throttled in this process, or the heal is unreachable) so the caller does
    not pointlessly re-walk the same denied paths.

    The return value deliberately reports "did we try", not "did it work": the
    authoritative answer is whether the RETRY finds a payload, never a repair
    function's own claim of success (the "false-green: passed then re-hardened"
    lesson from ``secret_config_perms``).
    """
    now = time.monotonic()
    last = _HEAL_STATE.get(account, 0.0)
    if last and now - last < _HEAL_THROTTLE_SEC:
        logger.warning(
            "oc_store: credential source for %s still unreadable (EACCES on %s) "
            "but a heal ran <%.0fs ago — not retrying (throttled, never looped)",
            account, ", ".join(clamped), _HEAL_THROTTLE_SEC,
        )
        return None
    _HEAL_STATE[account] = now

    heal_fn = _HEAL_FN
    if heal_fn is None:
        try:
            from evolve_admin.secret_config_perms import (  # type: ignore
                heal_evolve_access as heal_fn,
            )
        except Exception as exc:  # noqa: BLE001 — analyzer-only host / bootstrap
            logger.error(
                "oc_store: credential source for %s unreadable (EACCES on %s) and "
                "the canonical heal is unavailable here (%s) — the clamp will "
                "persist until the hourly pod_perms_drift_monitor tick",
                account, ", ".join(clamped), exc,
            )
            return None
    try:
        ok = bool(heal_fn(bot_id or account, account))
    except Exception as exc:  # noqa: BLE001 — a heal raise must not break the read
        logger.error(
            "oc_store: heal_evolve_access(%s, %s) raised %s after EACCES on %s",
            bot_id or account, account, exc, ", ".join(clamped),
        )
        return f"heal_evolve_access raised {type(exc).__name__}"
    return "contract restored" if ok else "contract NOT fully restored"


def _iter_payloads_healed(
    home_path: Path,
    bot_id: "str | None",
    *,
    user: "str | None" = None,
    network: "dict | None" = None,
) -> "Iterator[str]":
    """:func:`_iter_payloads_for_home` that survives the Linux ACL-mask clamp.

    Walks the ladder; if NOTHING was readable **and** at least one rung was
    denied with EACCES, heals once (throttled) and re-walks. See the module
    docstring for the three outcomes and why only the third one alerts.

    "Nothing readable AND an EACCES" is deliberately conservative: a bot with no
    auth store at all records no EACCES and never heals, and a partially-clamped
    bot that still yielded a usable payload is not worth a privileged repair
    mid-read (the hourly reassert owns that). It is also exactly the observed
    incident shape — the clamp is on the dir, so every rung beneath it fails at
    once.
    """
    clamp: dict = {}
    found = False
    for raw in _iter_payloads_for_home(home_path, clamp):
        found = True
        yield raw
    if found:
        return
    clamped = _clamped_paths(clamp)
    if not clamped:
        # No permission denial — a genuinely absent store. Return without so
        # much as resolving an account name, so an unclamped pod makes exactly
        # the calls it made before #3477.
        return
    account = _resolve_account(bot_id, user=user, network=network)
    if not account:
        # Nothing to hand ``heal_evolve_access(bot_id, bot_user)`` — degrade as
        # before rather than guess an account.
        return

    outcome = _attempt_clamp_heal(bot_id, account, clamped)
    if outcome is None:
        return

    retry_clamp: dict = {}
    healed_any = False
    for raw in _iter_payloads_for_home(home_path, retry_clamp):
        if not healed_any:
            healed_any = True
            # SUCCESS — deliberately Signal-free, but the durable trace lives
            # here: a post-hoc "was the engine locked out last night?" greps
            # this line. Emitted BEFORE the yield on purpose — ``read_auth_store``
            # abandons this generator after the first payload, so anything after
            # the loop would never run.
            logger.warning(
                "oc_store: credential source for %s was ACL-clamped (EACCES on "
                "%s); heal_evolve_access → %s; re-read SUCCEEDED — no operator "
                "action needed (self-healed inline, not waiting for the hourly "
                "tick)",
                account, ", ".join(clamped), outcome,
            )
        yield raw
    if healed_any:
        return

    still = _clamped_paths(retry_clamp) or clamped
    logger.error(
        "oc_store: credential source for %s is STILL unreadable after "
        "heal_evolve_access (%s) — EACCES on %s. Every provider key for this "
        "bot reads as absent until an operator runs "
        "`sudo evolve-admin ensure-pod-perms`.",
        account, outcome, ", ".join(still),
    )
    try:
        from credential_access_signal import observe_unreadable  # type: ignore

        observe_unreadable(
            bot_id=bot_id or account,
            clamped_paths=still,
            heal_outcome=outcome,
            network=network,
        )
    except Exception as exc:  # noqa: BLE001 — alerting must not break the read
        logger.error(
            "oc_store: could not emit the credential_access Signal for %s (%s)",
            account, exc,
        )


def iter_auth_store_payloads(
    bot_id: str | None,
    *,
    user: str | None = None,
    home: "Path | None" = None,
    network: "dict | None" = None,
) -> "Iterator[str]":
    """Yield EVERY readable raw auth-profiles JSON string for a bot, in ladder
    order (sqlite → legacy json → newest bak).

    For callers that must keep looking past the first readable source because
    an earlier one parsed fine but carried nothing they needed — e.g. a sqlite
    store holding only an OAuth profile while the leftover JSON still carries
    an api_key for another provider. Silent by design (a generator that yields
    nothing is a legitimate keyless bot); the LOUD "no source at all" log lives
    in :func:`read_auth_store`.

    Heals an ACL-mask clamp inline (#3477) — see the module docstring. A caller
    that abandons the generator after the first payload never reaches that code
    (it only runs when the ladder was exhausted with nothing found).
    """
    home_path = _resolve_home(bot_id, user=user, home=home, network=network)
    if home_path is None:
        return
    yield from _iter_payloads_healed(
        home_path, bot_id, user=user, network=network
    )


# ── Public API ────────────────────────────────────────────────────────────────


def read_auth_store(
    bot_id: str | None,
    *,
    user: str | None = None,
    home: "Path | None" = None,
    network: "dict | None" = None,
) -> "str | None":
    """Return the raw auth-profiles JSON string for a bot, or ``None``.

    Walks the source ladder (sqlite store → legacy json → newest bak) and
    returns the FIRST source with content. The caller's preferred way to
    identify the bot is whichever of ``home`` / ``user`` / ``bot_id`` it
    already holds (see :func:`_resolve_home`).

    Returns ``None`` only when EVERY source fails — and logs that loudly. A
    bot that authenticates to Anthropic via the gateway token (no raw key)
    still returns its JSON here (the parser simply finds no anthropic api_key),
    so ``None`` means a genuine read failure, never "this bot has no key".

    An ACL-mask clamp is healed inline before that verdict is reached (#3477),
    so ``None`` here also means "not merely locked out" — the heal already ran.
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

    for raw in _iter_payloads_healed(
        home_path, bot_id, user=user, network=network
    ):
        return raw

    logger.error(
        "oc_store: NO auth source resolved for bot_id=%r (home=%s, agents=%s) — "
        "checked the sqlite store, legacy auth-profiles.json, and .sqlite-import "
        "bak for each agent. Anthropic-key resolution will degrade for this bot.",
        bot_id,
        home_path,
        ", ".join(_discover_agent_ids(home_path)),
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
