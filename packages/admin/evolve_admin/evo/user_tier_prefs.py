"""Per-user-per-bot tier preferences (audit #69 Phase C).

Stores each user's ``defaultRole`` choice in a per-bot JSON file under
the shared directory, keyed by the ``derive_user_key`` value. The
plugin's ModelRouter reads it at routing time and applies the per-user
choice ABOVE the operator's bot-wide ``userTierOverride.defaultRole``
(Phase A) in the precedence ladder.

Why a separate file (not a section of evolve-tiers.json):
  • evolve-tiers.json is owned by the bot user and writes go through
    ``oc_full_config_set``'s sudo-as-bot-user subprocess dance. This
    file lives in the shared directory, owned by the evolve service
    user — direct ``Path.write_text`` works, no sudo or /tmp staging
    needed.
  • The shape is fundamentally different: bot-wide singletons vs.
    per-user dict. Splitting keeps both files readable + verifiable.
  • Heal.py credits writers per-file; this file's writer is the
    admin-side handler chain (evo + future per-user UI), distinct
    from evolve-tiers.json's writers (AI Optimization page, evo
    ``tier-default`` keyword on single-user bots — until Phase C
    flips that to per-user too).

Path layout::

    {sharedDir}/{botId}/user-tier-prefs.json
    {
      "users": {
        "ext:telegram:12345":  {"defaultRole": "fast",     "updated_at": "..."},
        "ext:slack:U07ABCDEF": {"defaultRole": "power",    "updated_at": "..."}
      }
    }

The role choice is stored under ``defaultRole`` (canonical, since the
model-role migration). Files written before the migration use the
legacy ``defaultTier`` key; readers here fall back to it, mirroring the
plugin's ``defaultRole ?? defaultTier`` reader.

The user keys come from :func:`evo.identity.derive_user_key` — the
same key used for wizard state files and user profiles, so a single
``user_key`` ties together "who you are" across multiple per-bot
artefacts.

Choice semantics (mirrors the user default in ModelRouter):

  • "fast" / "standard" / "power" / "max"  — apply this role
  • "auto" / missing / unknown             — fall through to the
                                             operator default, then
                                             bot default

``max`` (Fable-class, ~2× Power cost) is a valid per-USER default.
The operator BOT-WIDE default cannot be max (ModelRouter rejects it
for classifier/maintenance routing); only explicit user requests or
this per-user file can activate Fable for a given session.

Setting a user's pref to "auto" via the evo keyword DELETES the
user's entry from the file rather than persisting a no-op record —
keeps the file from accumulating tombstones forever.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from evolve_util import atomic_write_json as _atomic_write

from .identity import derive_user_key


Choice = Literal["auto", "fast", "standard", "power", "max"]
_VALID_CHOICES: frozenset[str] = frozenset({"auto", "fast", "standard", "power", "max"})


def _prefs_path(shared_dir: Path | str, bot_id: str) -> Path:
    """Return the canonical user-tier-prefs.json path for a bot."""
    return Path(shared_dir) / bot_id / "user-tier-prefs.json"


def _load(path: Path) -> dict[str, Any]:
    """Read the file, returning the canonical empty shape on any error.

    The plugin and the operator's audit eyeballing both want
    ``{"users": {...}}`` even for an empty file — uniform shape keeps
    JS / Python downstream code simple.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"users": {}}
    if not isinstance(data, dict):
        return {"users": {}}
    users = data.get("users")
    if not isinstance(users, dict):
        data["users"] = {}
    return data


def _entry_choice(entry: dict[str, Any]) -> Any:
    """Read a user entry's role choice, ``defaultRole`` then ``defaultTier``.

    The model-role migration (``evolve-admin migrate-model-roles``)
    renames ``defaultTier`` → ``defaultRole`` in every entry. Read the
    new field first and fall back to the legacy one, mirroring the
    plugin's TS reader (``userPref.defaultRole ?? userPref.defaultTier``
    in ``ModelRouter.ts``) so admin-side Python and routing-side JS stay
    in lockstep across the migration boundary.
    """
    role = entry.get("defaultRole")
    return role if role is not None else entry.get("defaultTier")


def get_user_pref(
    shared_dir: Path | str,
    bot_id: str,
    user_key: str,
) -> str | None:
    """Return the role choice for one user on one bot, or ``None`` when
    the user has no entry.

    Reads ``defaultRole`` (canonical, post model-role migration), falling
    back to the legacy ``defaultTier`` field. Read-only; cheap; safe to
    call on every routing decision if needed. The plugin's TS counterpart
    matches this shape.
    """
    if not user_key:
        return None
    data = _load(_prefs_path(shared_dir, bot_id))
    entry = (data.get("users") or {}).get(user_key)
    if not isinstance(entry, dict):
        return None
    role = _entry_choice(entry)
    if not isinstance(role, str) or role.lower() not in _VALID_CHOICES:
        return None
    return role.lower()


def set_user_pref(
    shared_dir: Path | str,
    bot_id: str,
    user_key: str,
    choice: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Set or update one user's pref. Returns the resulting user entry.

    Behaviour by choice:
      • ``"auto"``                         — DELETE the user's entry
        (cleanup; no override means "fall through to operator
        default"). Returns ``{}``.
      • ``"fast" | "standard" | "power" | "max"``  — write/replace the
        entry with the canonical lowercased choice + ISO-8601 UTC
        ``updated_at`` timestamp.
      • Anything else                       — raises ``ValueError`` so
        the handler can surface a clear error to the user.

    ``now`` lets tests inject a deterministic timestamp; production
    callers omit it.

    Atomic write (temp-file + rename) — readers can never see a half-
    written file. Empty ``users`` map is preserved when the user being
    deleted was the last entry, so the file shape stays consistent.
    """
    if not user_key:
        raise ValueError("user_key is required")
    choice_n = (choice or "").strip().lower()
    if choice_n not in _VALID_CHOICES:
        raise ValueError(
            f"unknown choice {choice!r}; expected one of "
            f"{sorted(_VALID_CHOICES)}"
        )

    path = _prefs_path(shared_dir, bot_id)
    # The file lives in the shared directory which the evolve user owns
    # directly, so no sudo dance needed. sort_keys keeps the on-disk
    # shape stable for the operator's audit eyeballing.
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load(path)
    users = data.setdefault("users", {})

    if choice_n == "auto":
        # auto = "clear my override" — delete the entry so the operator
        # default + bot default cascade applies normally. Silent no-op
        # when the user had no entry to begin with.
        users.pop(user_key, None)
        _atomic_write(path, data, sort_keys=True)
        return {}

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    # Drop microseconds for a tidier audit trail — second-precision is
    # enough for "when did this happen".
    if "." in stamp:
        stamp = stamp.split(".", 1)[0] + "+00:00"
    # Write the canonical post-migration field. ``get_user_pref`` and the
    # plugin's TS reader both fall back to the legacy ``defaultTier``, so
    # entries written before the migration are still honoured on read.
    entry = {"defaultRole": choice_n, "updated_at": stamp}
    users[user_key] = entry
    _atomic_write(path, data, sort_keys=True)
    return entry


def list_user_prefs(
    shared_dir: Path | str, bot_id: str,
) -> dict[str, dict[str, Any]]:
    """Return the full ``{user_key: {defaultRole | defaultTier, updated_at}}`` map.

    For diagnostic + audit surfaces (e.g. an admin-UI panel that wants
    to show "5 users have set personal defaults on this bot"). Returns
    ``{}`` when the file doesn't exist or is malformed — never raises.
    """
    data = _load(_prefs_path(shared_dir, bot_id))
    users = data.get("users") or {}
    if not isinstance(users, dict):
        return {}
    # Filter to entries with a recognisable role choice — ``defaultRole``
    # (canonical) or the legacy ``defaultTier``. Defensive copy so callers
    # can mutate freely.
    out: dict[str, dict[str, Any]] = {}
    for key, entry in users.items():
        if isinstance(entry, dict) and isinstance(_entry_choice(entry), str):
            out[key] = dict(entry)
    return out


def derive_user_key_for_caller(
    network: dict[str, Any],
    *,
    bot_id: str,
    channel: str | None,
    sender_external_id: str | None,
) -> str:
    """Thin convenience wrapper over :func:`evo.identity.derive_user_key`.

    Re-exported here so handlers that only care about the prefs file
    don't have to reach across modules. Identical behaviour — same
    ``pod:`` / ``ext:`` / ``anon:`` precedence ladder.
    """
    return derive_user_key(
        network,
        bot_id=bot_id,
        channel=channel,
        sender_external_id=sender_external_id,
    )
