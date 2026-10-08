"""Per-bot user role + engagement + block overlay.

Sidecar to OpenClaw's per-bot allowlist at
``<bot_home>/.openclaw/credentials/<provider>-default-allowFrom.json``.
The allowlist remains source-of-truth for *admission* — "is this stable_id
authorized to talk to the bot?" The overlay layers four pieces of state
on top, keyed on the same ``(platform, stable_id)`` tuples:

- **Per-identity role** (``admin`` / ``primary_user`` / ``participant`` /
  ``blocked``) — overlay is the explicit-override source; ``admin`` and
  ``primary_user`` defaults derive from ``network.json`` pod-admin /
  primary-owner claims via ``resolve_role()``.
- **Per-identity engagement surfaces** (subset of ``{"group", "dm"}``)
  controlling whether the bot honors group mentions vs. DMs from this
  identity.
- **Sticky block index** — survives re-pairing. Distinct from the
  existing 1-hour rejection cool-off in ``routes_bot_users``; a blocked
  identity is silent-ignored regardless of OC allowFrom membership.
- **Per-channel newcomer mode** — ``auto_admit`` /
  ``require_approval`` (existing default) / ``closed`` — plus the
  channel's default engagement surface set for newly-admitted users.

The file lives at ``{shared_dir}/rosters/{bot_id}.json``, owned by the
``evolve`` user. ``{shared_dir}`` already carries the right ACL — no
sudo or /tmp staging needed. Writes are atomic temp-file + rename
through ``os.replace``. Every mutation appends one line to the daily
audit log at ``{shared_dir}/rosters/log/{YYYY-MM-DD}.jsonl``.

Identity keys are ``"<platform>:<stable_id>"`` strings. Channel keys
are bare provider names today (``"telegram"``, ``"slack"``, ...) —
matching the per-provider granularity of OC's existing allowlist
files. Multi-instance per platform (e.g. a single bot in multiple
Slack workspaces) is a future extension; the schema accommodates a
richer ``"slack:T0XXX"`` form when needed.

Spec: internal/spec-user-roster-and-roles-2026-06-07.md
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _now_iso

from . import external_ids as _ext_ids


log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────


ROLES = ("admin", "primary_user", "participant", "blocked")
"""Tuple form for ordered iteration. Matches spec §3."""

NEWCOMER_MODES = (
    "auto_admit", "require_approval", "closed", "admit_with_passphrase",
)
"""Per-channel handling of an unknown ``/start`` sender. Matches spec §11.

H.2 (2026-06-08) added ``admit_with_passphrase``: the newcomer is
auto-admitted iff their message contains the bot's
``primary_passphrase_override`` (currently per-bot; per-channel
passphrase storage is a future enhancement). Plugin-side enforcement
of the passphrase check is a follow-up; this PR adds the constant
so the mode persists across overlay reads/writes.
"""

VALID_SURFACES = ("group", "dm")
"""Subset of ``{"group", "dm"}``. Order in the stored list is not significant."""

CURRENT_VERSION = 1


# ── Path / load / save ───────────────────────────────────────────────────


def overlay_path(shared_dir: Path, bot_id: str) -> Path:
    """Per-bot overlay file path."""
    return Path(shared_dir) / "rosters" / f"{bot_id}.json"


def _empty_overlay(bot_id: str) -> dict[str, Any]:
    return {
        "bot_id": bot_id,
        "version": CURRENT_VERSION,
        "channels": {},
        "identities": {},
        "blocked": {},
        # Sticky-dismissed "Active · not admitted" identities — the
        # operator triaged the row away. Distinct from ``blocked``:
        # ignoring only hides the Users-page seen-recently row; it does
        # NOT touch OC admission or the allowlist. Same shape + write
        # path as ``blocked`` so it survives across page loads.
        "ignored": {},
        # Reserved for Phase B — read by Layer 2 once capabilities ship.
        # Phase A creates the keys so consumers can assume the shape.
        "role_bindings": {
            "admin": ["*"],
            "primary_user": ["*"],
            "participant": [],
            "blocked": [],
        },
    }


def load_overlay(shared_dir: Path, bot_id: str) -> dict[str, Any]:
    """Read the overlay file. Returns the empty-overlay shape when absent
    or unreadable so callers can treat "no overlay yet" as the default
    state without branching."""
    p = overlay_path(shared_dir, bot_id)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return _empty_overlay(bot_id)
    except (PermissionError, OSError) as e:
        log.warning("overlay read for %s failed: %s — using empty", bot_id, e)
        return _empty_overlay(bot_id)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("overlay parse for %s failed: %s — using empty", bot_id, e)
        return _empty_overlay(bot_id)
    # Defensive: ensure top-level keys exist. Lets later code assume the
    # shape without re-checking. Schema drift between versions handled
    # here too — add the new keys at their defaults, never strip unknown
    # keys (preserves forward-compat for a downgrade).
    base = _empty_overlay(bot_id)
    for k, v in base.items():
        data.setdefault(k, v)
    data["bot_id"] = bot_id  # don't trust a stale on-disk bot_id
    return data


def save_overlay(shared_dir: Path, bot_id: str,
                 overlay: dict[str, Any]) -> None:
    """Atomic temp-file + os.replace write. Creates parent dirs as needed.

    Mode is forced to 0o644 (world-readable) so bot-user processes can
    read the overlay for role resolution — the C.4 TS roleResolver reads
    these files directly per turn. Without the explicit chmod the file
    inherits ``tempfile.mkstemp``'s mode (0o600 in spec; umask-modified
    in practice), which would break the per-turn speaker context block
    for any pod whose umask doesn't relax to 644. There's no secret in
    the overlay; making it readable is consistent with the existing
    OC allowFrom.json files (mode 644) and with the daemon's GET
    endpoint that already exposes the same data.
    """
    p = overlay_path(shared_dir, bot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    overlay = dict(overlay)
    overlay["bot_id"] = bot_id
    overlay.setdefault("version", CURRENT_VERSION)
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{bot_id}-overlay-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(overlay, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
        # Explicit chmod — see docstring. Failing here doesn't undo the
        # mutation; warn and continue so a perm-issue doesn't break the
        # mutation pipeline.
        try:
            os.chmod(p, 0o644)
        except OSError as exc:
            log.warning("overlay chmod for %s failed: %s", bot_id, exc)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Identity key helpers ─────────────────────────────────────────────────


def identity_key(platform: str, stable_id: str) -> str:
    """Canonical overlay-map key. ``"telegram:98765432"``."""
    return f"{platform}:{stable_id}"


def get_identity(overlay: dict, platform: str,
                 stable_id: str) -> "dict | None":
    """Overlay entry for an identity, or None if no overlay state."""
    return (overlay.get("identities") or {}).get(
        identity_key(platform, stable_id))


def is_blocked(overlay: dict, platform: str, stable_id: str) -> bool:
    """Sticky block check. True iff this identity is in the block index."""
    return identity_key(platform, stable_id) in (overlay.get("blocked") or {})


def is_ignored(overlay: dict, platform: str, stable_id: str) -> bool:
    """Sticky-ignore check. True iff this identity is in the ignore index.

    Used by the Users page to drop dismissed identities from the
    "Active · not admitted" list. Unlike ``is_blocked`` this has no
    bearing on admission — it's a UI-triage state only.
    """
    return identity_key(platform, stable_id) in (overlay.get("ignored") or {})


# ── Resolution (read-side, against network.json) ─────────────────────────


def _pod_admin_ids_for(network: dict, platform: str) -> "set[str]":
    """Pod-admin external_ids[<platform>] from network.json.

    Via :func:`external_ids.ids_for` — pre-M1-B2 this iterated the raw
    value, so a bare-string entry (the shape the per-bot writers emit)
    expanded character by character into a set of digits.
    """
    pod = network.get("pod") or {}
    return set(_ext_ids.ids_for(pod.get("admins"), platform))


def _primary_owner_ids_for(network: dict, bot_id: str,
                           platform: str) -> "set[str]":
    """Every primary-owner external_id on ``platform`` for the bot.

    Plural since M1-B2 (invariant 6): one person, one row, many platform
    ids. Admission matches ANY of them.
    """
    bots = network.get("bots") or {}
    bot = bots.get(bot_id) or {}
    return set(_ext_ids.ids_for(bot.get("primary_user"), platform))


def resolve_role(overlay: dict, network: dict, bot_id: str,
                 platform: str, stable_id: str) -> str:
    """Compute the effective role for an identity at admission time.

    Priority chain (matches spec §3 "Role resolution"):

      1. Block index hit → ``blocked`` (sticky; overrides everything)
      2. Pod-admin claim → ``admin``
      3. Explicit overlay role → that role
      4. Primary-owner claim → ``primary_user``
      5. Default → ``participant``

    Used by the GET endpoint and by Layer 2 (per-role MCP allowlist) once
    that ships in Phase B.
    """
    if is_blocked(overlay, platform, stable_id):
        return "blocked"
    if stable_id in _pod_admin_ids_for(network, platform):
        return "admin"
    entry = get_identity(overlay, platform, stable_id) or {}
    explicit = entry.get("role")
    if explicit in ROLES:
        return explicit
    if stable_id in _primary_owner_ids_for(network, bot_id, platform):
        return "primary_user"
    return "participant"


def channel_block(overlay: dict, channel_key: str) -> dict[str, Any]:
    """Channel settings block, defaulted. Never returns None.

    Channel key is the bare platform name today (``"telegram"``).
    """
    ch = (overlay.get("channels") or {}).get(channel_key) or {}
    return {
        "newcomer_mode":
            ch.get("newcomer_mode") if ch.get("newcomer_mode")
            in NEWCOMER_MODES else "require_approval",
        "default_engagement_surfaces":
            _coerce_surfaces(ch.get("default_engagement_surfaces"))
            or list(_default_surfaces_for_channel(channel_key)),
    }


def _default_surfaces_for_channel(channel_key: str) -> tuple[str, ...]:
    """Fall-back default when the channel has no explicit override.

    Today the existing per-channel allowlists carry no group-vs-DM
    distinction, so we lean toward ``["group", "dm"]`` for channels
    that are commonly both (Slack, Telegram), and ``["dm"]`` for
    DM-first surfaces. Operators tighten via the newcomer_mode PUT.
    """
    if channel_key in ("telegram", "slack", "discord"):
        return ("group", "dm")
    if channel_key in ("whatsapp",):
        return ("dm",)
    return ("group", "dm")


def resolve_engagement_surfaces(overlay: dict, network: dict, bot_id: str,
                                platform: str, stable_id: str,
                                channel_key: str) -> list[str]:
    """Effective engagement surface set for this identity on this channel.

    Order:
      1. Explicit overlay entry → use it.
      2. Pod-admin or primary_owner default → ``["group", "dm"]`` (both).
      3. Channel's ``default_engagement_surfaces`` → use that.

    Always returns a list with at least one valid surface.
    """
    entry = get_identity(overlay, platform, stable_id) or {}
    explicit = _coerce_surfaces(entry.get("engagement_surfaces"))
    if explicit:
        return explicit
    if (stable_id in _pod_admin_ids_for(network, platform)
            or stable_id in _primary_owner_ids_for(network, bot_id, platform)):
        return ["group", "dm"]
    return list(channel_block(overlay, channel_key)[
        "default_engagement_surfaces"])


def _coerce_surfaces(value: Any) -> "list[str]":
    """Validate + normalize a surface list. Returns ``[]`` on bad input
    (so callers can use it as a falsy sentinel for "not specified")."""
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for v in value:
        if isinstance(v, str) and v in VALID_SURFACES and v not in seen:
            seen.append(v)
    return seen


# ── Mutations (write-side, with audit log) ───────────────────────────────


def _audit_log_append(shared_dir: Path, record: dict[str, Any]) -> None:
    """Append one JSONL line to today's roster audit log.

    Failures here log but do not raise — a mutation that wrote
    successfully should not be reported as failed because the log
    append hit a transient I/O error. The mutation itself is the
    primary durability surface; the log is observability.
    """
    try:
        # Use UTC date for the log filename so cross-timezone reads of
        # the same event find the same file.
        today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        log_dir = Path(shared_dir) / "rosters" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True) + "\n"
        with open(log_dir / f"{today}.jsonl", "a") as f:
            f.write(line)
    except (PermissionError, OSError) as e:
        log.warning("roster audit log append failed: %s", e)


def set_identity_role(overlay: dict, platform: str, stable_id: str,
                      role: str, *, by: str) -> dict[str, Any]:
    """Set or change an identity's overlay role. Returns the new entry.

    Raises ``ValueError`` on an invalid role. ``"blocked"`` is rejected
    here — use ``block_identity`` instead, which also writes the block
    index so the role survives a normal role mutation.
    """
    if role not in ROLES or role == "blocked":
        raise ValueError(
            f"set_identity_role: invalid role {role!r}; "
            f"use block_identity() for blocked"
        )
    ids = overlay.setdefault("identities", {})
    key = identity_key(platform, stable_id)
    before = dict(ids.get(key) or {})
    entry = dict(before)
    entry["role"] = role
    entry.setdefault("engagement_surfaces", [])
    entry.setdefault("notes", "")
    entry.setdefault("added_at", _now_iso())
    entry["updated_at"] = _now_iso()
    entry["updated_by"] = by
    entry.setdefault("added_by", by)
    ids[key] = entry
    return entry


def set_identity_engagement(overlay: dict, platform: str, stable_id: str,
                            surfaces: list[str], *,
                            by: str) -> dict[str, Any]:
    """Set the engagement surfaces for an identity. Returns the new entry."""
    norm = _coerce_surfaces(surfaces)
    if not norm:
        raise ValueError(
            f"set_identity_engagement: surfaces must be a non-empty subset of "
            f"{VALID_SURFACES}, got {surfaces!r}"
        )
    ids = overlay.setdefault("identities", {})
    key = identity_key(platform, stable_id)
    entry = dict(ids.get(key) or {})
    entry["engagement_surfaces"] = norm
    entry.setdefault("notes", "")
    entry.setdefault("added_at", _now_iso())
    entry["updated_at"] = _now_iso()
    entry["updated_by"] = by
    entry.setdefault("added_by", by)
    ids[key] = entry
    return entry


def block_identity(overlay: dict, platform: str, stable_id: str, *,
                   by: str, reason: str = "") -> dict[str, Any]:
    """Add an identity to the sticky block index. Returns the block record.

    Idempotent — re-blocking refreshes ``blocked_at`` but does not stack.
    The caller is responsible for also revoking the OC allowFrom entry
    (see ``routes_bot_users._revoke``); this function only updates the
    overlay's block state.
    """
    blocked = overlay.setdefault("blocked", {})
    key = identity_key(platform, stable_id)
    record = {
        "blocked_at": _now_iso(),
        "blocked_by": by,
        "reason": (reason or "").strip(),
    }
    blocked[key] = record
    return record


def unblock_identity(overlay: dict, platform: str, stable_id: str, *,
                     by: str) -> bool:
    """Remove an identity from the block index. Returns True iff present.

    Does NOT re-admit them to the OC allowlist — that's a separate
    explicit step (operator must approve a new ``/start``, or be added
    manually via the existing pairing flow).
    """
    blocked = overlay.setdefault("blocked", {})
    key = identity_key(platform, stable_id)
    if key in blocked:
        del blocked[key]
        return True
    return False


def ignore_identity(overlay: dict, platform: str, stable_id: str, *,
                    by: str, reason: str = "") -> dict[str, Any]:
    """Add an identity to the sticky ignore index. Returns the record.

    Idempotent — re-ignoring refreshes ``ignored_at`` but does not stack.
    Ignoring is a UI-triage action: it hides the identity from the Users
    page "Active · not admitted" list but does NOT revoke admission or
    touch the OC allowlist (that's ``block_identity``'s job). The caller
    does not need to revoke anything alongside this.
    """
    ignored = overlay.setdefault("ignored", {})
    key = identity_key(platform, stable_id)
    record = {
        "ignored_at": _now_iso(),
        "ignored_by": by,
        "reason": (reason or "").strip(),
    }
    ignored[key] = record
    return record


def unignore_identity(overlay: dict, platform: str, stable_id: str, *,
                      by: str) -> bool:
    """Remove an identity from the ignore index. Returns True iff present.

    The next Users-page load will surface the identity again in the
    "Active · not admitted" list if it still has recent activity.
    """
    ignored = overlay.setdefault("ignored", {})
    key = identity_key(platform, stable_id)
    if key in ignored:
        del ignored[key]
        return True
    return False


def set_channel_newcomer_mode(overlay: dict, channel_key: str,
                              mode: str, *, by: str,
                              default_engagement_surfaces:
                              "list[str] | None" = None) -> dict[str, Any]:
    """Set the per-channel newcomer mode. Returns the new channel block."""
    if mode not in NEWCOMER_MODES:
        raise ValueError(
            f"set_channel_newcomer_mode: invalid mode {mode!r}; "
            f"expected one of {NEWCOMER_MODES}"
        )
    chans = overlay.setdefault("channels", {})
    block = dict(chans.get(channel_key) or {})
    block["newcomer_mode"] = mode
    if default_engagement_surfaces is not None:
        norm = _coerce_surfaces(default_engagement_surfaces)
        if not norm:
            raise ValueError(
                f"set_channel_newcomer_mode: default_engagement_surfaces "
                f"must be a non-empty subset of {VALID_SURFACES}"
            )
        block["default_engagement_surfaces"] = norm
    block["updated_at"] = _now_iso()
    block["updated_by"] = by
    chans[channel_key] = block
    return block


# ── Mutation orchestrators (load → mutate → save → log) ──────────────────


def mutate_overlay(shared_dir: Path, bot_id: str, action: str, by: str,
                   mutate_fn) -> dict[str, Any]:
    """Load, apply ``mutate_fn(overlay)``, save, and audit.

    ``mutate_fn`` receives the overlay dict and may mutate it in place;
    its return value (if any) is recorded as the audit ``after`` payload.
    The audit ``before`` is captured before the mutation runs.
    """
    overlay = load_overlay(shared_dir, bot_id)
    before_snapshot = _audit_snapshot(overlay)
    result = mutate_fn(overlay)
    save_overlay(shared_dir, bot_id, overlay)
    after_snapshot = _audit_snapshot(overlay)
    _audit_log_append(shared_dir, {
        "ts": _now_iso(),
        "action": action,
        "target_bot": bot_id,
        "by": by,
        "before": before_snapshot,
        "after": after_snapshot,
    })
    return result if isinstance(result, dict) else overlay


def _audit_snapshot(overlay: dict) -> dict[str, Any]:
    """Compact snapshot of the overlay state used in audit log entries.

    Drops the boilerplate so the diff between ``before`` and ``after``
    is human-readable in the log file.
    """
    return {
        "identities": dict(overlay.get("identities") or {}),
        "blocked": dict(overlay.get("blocked") or {}),
        "ignored": dict(overlay.get("ignored") or {}),
        "channels": dict(overlay.get("channels") or {}),
    }
