"""Canonical per-bot roster resolver — the single allowlist+overlay+name+activity join.

Spec: docs/spec-identity-meta-2026-06-15.md §6 piece 4 + §7 ("Canonical roster
resolver"). Invariant #1: *one source of truth, projected* — the admin Users page
GET and (in F1b) the bot's roster projection must read the **exact same join**, so
they cannot diverge. This module is that join.

A "roster record" is one admitted identity with everything the page and the bot both
need: who they are (`display_name`/`handle`), the stable admission key
(`platform`/`stable_id`), what they're allowed to do (`role` + `rights_summary` +
`engagement_surfaces`), and when they were last seen (`last_seen`/`turn_count`). The
record also carries a few admin-page-only extras (`labels`, `name_source`, `email`)
so the existing Users-page JSON stays byte-identical after it is refactored onto this
resolver; the bot projection ignores those.

Two entry points, one join:

  * ``build_record(...)`` — the per-identity join. Given an already-known
    ``(platform, stable_id)`` plus a pre-loaded overlay + activity map, produce one
    ``RosterRecord``. This is the SOLE place a record is assembled. The Users-page GET
    calls this per admitted id (it already reads the allowFrom files for its
    ``supported`` flag and pending list, so it does not need the full read below).
  * ``resolve_roster(...)`` — the canonical full-read. Loads the overlay + activity
    once, reads each channel's allowFrom list, and returns ``[RosterRecord]`` for every
    admitted identity. This is what a non-GET consumer (a future roster endpoint, the
    F1b read tool's backing call, tests) uses to get the whole roster in one call.

The name-resolution chain (``resolve_display_name`` / ``resolve_username`` /
``resolve_email`` / ``meta_to_display_name`` + identity-cache reads) lives here, not in
the web routes module, so the join has no dependency on Flask. ``routes_bot_users``
imports the pieces it still needs (e.g. ``meta_to_display_name`` for its pairing-time
identity-cache *write*).

Reads only. Role/engagement resolution is delegated to ``roster_overlay`` and activity
to ``user_activity`` — this module composes them, it does not re-implement them. All
inputs are owned by the ``evolve`` user (overlay under ``{shared_dir}/rosters/``,
allowFrom under the bot home via the routes reader's sudo-cat fallback), so there is no
sudo or /tmp staging here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from evolve_config import CANONICAL_SHARED_DIR

from . import capabilities
from . import channel_registry as _channel_registry
from . import roster_overlay as ro
from . import user_activity as ua


log = logging.getLogger(__name__)


# Channels the roster spans. Same registry projection as
# ``routes_bot_users.KNOWN_PROVIDERS`` — deriving both from the registry's
# ``pairing`` column keeps them equal without importing routes here (which
# would pull Flask into the join's import graph). The guard test
# ``test_roster_resolver.test_channels_match_known_providers`` still holds.
ROSTER_CHANNELS: tuple[str, ...] = _channel_registry.pairing_channel_ids()


@dataclass
class RosterRecord:
    """One admitted identity, fully resolved.

    The first block is the canonical projection — the fields the admin page and the
    bot both consume. The second block is admin-page-only and exists so the existing
    Users-page JSON stays byte-identical when it is refactored onto this resolver;
    the bot projection (F1b) ignores them.
    """

    # ── Canonical projection (page + bot) ────────────────────────────────
    platform: str
    stable_id: str
    display_name: "str | None"
    handle: "str | None"            # platform @username, when distinct/known
    role: str                       # admin | primary_user | participant | blocked
    engagement_surfaces: list[str]  # subset of {"group", "dm"}
    rights_summary: str             # short human phrase derived from capabilities
    last_seen: "str | None"         # ISO ts of most recent turn, or None
    turn_count: "int | None"        # turns in the 7-day window; None ⇒ no activity record

    # ── Admin-page-only extras (byte-identical GET) ──────────────────────
    labels: list[str] = field(default_factory=list)  # ["pod_admin", "owner"]
    name_source: str = "allowlist_only"              # which resolution step matched
    email: "str | None" = None                        # Slack-only today

    def to_dict(self) -> dict[str, Any]:
        """Canonical projection as a plain dict (for a future roster endpoint /
        the F1b read tool). Excludes the admin-only extras by design — the bot sees
        the projection, not the page's presentation fields."""
        return {
            "platform": self.platform,
            "stable_id": self.stable_id,
            "display_name": self.display_name,
            "handle": self.handle,
            "role": self.role,
            "engagement_surfaces": list(self.engagement_surfaces),
            "rights_summary": self.rights_summary,
            "last_seen": self.last_seen,
            "turn_count": self.turn_count,
        }


# ── The join: one identity → one record ──────────────────────────────────


def build_record(
    network: dict,
    bot_id: str,
    platform: str,
    stable_id: str,
    *,
    overlay: dict,
    activity: "dict[str, dict[str, Any]] | None",
    shared_dir: Path,
) -> RosterRecord:
    """Resolve a single admitted identity into a ``RosterRecord`` — the SOLE join.

    ``overlay`` and ``activity`` are passed in pre-loaded so a caller resolving many
    identities loads each once (the Users-page GET and ``resolve_roster`` both do).
    ``activity`` may be ``None`` (no turn rollups) — then ``last_seen``/``turn_count``
    are ``None``. Delegates role to ``roster_overlay.resolve_role``, engagement to
    ``roster_overlay.resolve_engagement_surfaces``, and the name chain to this module's
    ``resolve_display_name`` — it composes, it does not re-derive.
    """
    display_name, source = resolve_display_name(
        network, bot_id, platform, stable_id, shared_dir)
    role = ro.resolve_role(overlay, network, bot_id, platform, stable_id)
    engagement = ro.resolve_engagement_surfaces(
        overlay, network, bot_id, platform, stable_id, platform)
    handle = resolve_username(network, platform, stable_id, shared_dir)
    email = resolve_email(network, platform, stable_id)
    act = ua.lookup(activity or {}, platform, stable_id)
    # ``turn_count`` distinguishes "no activity record" (None) from "seen but no
    # turns in the 7-day window" (0). The GET keys ``last_seen``/``turns_7d`` are
    # emitted iff there *is* an activity record, i.e. iff ``turn_count is not None``.
    if act:
        last_seen = act.get("last_ts")
        turn_count = int(act.get("turns_7d") or 0)
    else:
        last_seen = None
        turn_count = None
    return RosterRecord(
        platform=platform,
        stable_id=stable_id,
        display_name=display_name,
        handle=handle,
        role=role,
        engagement_surfaces=engagement,
        rights_summary=rights_summary(role, overlay),
        last_seen=last_seen,
        turn_count=turn_count,
        labels=resolve_labels(network, bot_id, platform, stable_id),
        name_source=source,
        email=email,
    )


# ── Full-read entry point (F1b read tool / future endpoint / tests) ───────


def _default_allowfrom_reader(
    network: dict, bot_id: str,
) -> "Callable[[str], list[str] | None]":
    """Build the production allowFrom reader — reads
    ``<bot_home>/.openclaw/credentials/<channel>-default-allowFrom.json``.

    Local-imports the routes module so the allowFrom file location + the
    direct-read-then-``sudo /bin/cat`` fallback semantics stay owned in one place
    (``routes_bot_users``); re-homing that file I/O is out of scope here. The local
    import keeps Flask out of this module's load-time graph — it resolves only when a
    caller actually reads files (the GET supplies its own ids and never hits this; the
    resolver unit tests inject a reader and never hit this either).
    """
    from .web import routes_bot_users as rbu

    def _read(channel: str) -> "list[str] | None":
        allow = rbu._read_json_or_none(rbu._allowfrom_path(network, bot_id, channel))
        if allow is None:
            return None
        return [x for x in (allow.get("allowFrom") or []) if x]

    return _read


# ── Group/channel allowlist (config-level — distinct from DM pairing) ──────
#
# The DM allowlist above (``<channel>-default-allowFrom.json`` in the bot's
# credentials dir) gates *DM* admission under ``dmPolicy: pairing``. Group/channel
# admission is a SEPARATE OpenClaw gate keyed on ``channels.<channel>`` in the bot's
# ``openclaw.json`` under ``groupPolicy: "allowlist"`` — a different file, a different
# per-sender gate (``group-access`` module, fail-closed). The two routinely diverge
# (R1a diagnosis 2026-06-17): a user can be group-authorized but not DM-paired, and
# vice versa. Per the R1a decision the two lists are kept *strictly separate* and
# surfaced distinctly — DM approval never auto-grants group access. These readers
# give callers the group side so an admitted-in-a-channel identity stops reading as
# "not admitted." Management of the group list lands in the R1a follow-up; this is the
# read side only.


def effective_group_allowlist(channel_block: "dict | None") -> "list[str] | None":
    """Effective group/channel allowlist for one ``channels.<ch>`` block, or ``None``.

    Returns ``None`` when group access is **not** allowlist-gated for this channel —
    ``groupPolicy`` absent or anything other than ``"allowlist"`` (``open`` admits
    every group member, ``disabled`` admits none; neither is a *managed list* to
    surface). NB: ``open`` is deliberately not surfaced here — those members read as
    "not admitted" until a future pass models open-policy membership. That is a false
    *negative* (under-surfacing), never a false positive: this function never reports
    a sender as group-authorized whom OpenClaw would not admit.

    When ``groupPolicy == "allowlist"`` the effective list is, in order:

      1. ``groupAllowFrom`` when present (the channel's own group list), else
      2. ``allowFrom`` when ``groupAllowFromFallbackToAllowFrom`` is not ``false``
         (OpenClaw's default for that flag is ``true`` — so on a bot with no separate
         ``groupAllowFrom`` the group gate *is* ``allowFrom``; this is the
         R1a-diagnosed shape on the live pod), else
      3. ``[]`` when that fall-through is explicitly disabled.

    Provenance: the ``groupAllowFrom`` / ``groupAllowFromFallbackToAllowFrom`` keys and
    the per-sender fail-closed ``allowlist`` semantics are the OpenClaw 2026.6.1
    ``group-access`` model documented in the R1a diagnosis
    (``docs/spec-users-meta-2026-06-15.md`` § "R1a diagnosis (2026-06-17)", findings
    4-5, citing ``openclaw/dist/channel-capabilities-*.js`` + the ``group-access-*.js``
    module). The diagnosis ships in its own PR (#2997); when that merges this file's
    branch will share the repo. Keep this fall-through ORDER intact: dropping the
    ``groupAllowFrom`` branch and reading ``allowFrom`` only would mis-surface any bot
    that *does* set a separate ``groupAllowFrom``.

    Bare ``"*"`` (the ``dmPolicy: open`` wildcard sentinel) and non-string/blank
    entries are dropped — they are not real per-sender ids.
    """
    if not isinstance(channel_block, dict):
        return None
    if channel_block.get("groupPolicy") != "allowlist":
        return None
    group_allow = channel_block.get("groupAllowFrom")
    if isinstance(group_allow, list):
        ids = group_allow
    elif channel_block.get("groupAllowFromFallbackToAllowFrom", True):
        ids = channel_block.get("allowFrom") or []
    else:
        ids = []
    if not isinstance(ids, list):
        return []
    return [
        str(x) for x in ids
        if isinstance(x, str) and x.strip() and x != "*"
    ]


def _default_openclaw_reader(
    network: dict, bot_id: str,
) -> "Callable[[], dict | None]":
    """Build the production ``openclaw.json`` reader for a bot.

    Like ``_default_allowfrom_reader`` this local-imports the routes module so the
    bot-home file location + the direct-read-then-``sudo /bin/cat`` fallback stay
    owned in one place (``routes_bot_users``). Returns a zero-arg callable so callers
    read the (single) config file once. The local import keeps Flask out of this
    module's load-time graph.
    """
    from .web import routes_bot_users as rbu

    def _read() -> "dict | None":
        return rbu._read_json_or_none(rbu._openclaw_json_path(network, bot_id))

    return _read


def read_group_allowlists(
    network: dict,
    bot_id: str,
    *,
    channels: "tuple[str, ...] | None" = None,
    oc_reader: "Callable[[], dict | None] | None" = None,
) -> dict[str, list[str]]:
    """Per-channel effective group allowlists for ``bot_id`` from its ``openclaw.json``.

    Returns ``{channel: [stable_id, ...]}`` **only** for channels whose group access
    is allowlist-gated and non-empty (see ``effective_group_allowlist``). Channels
    absent from the config, not group-gated, or with an empty effective list are
    omitted. ``oc_reader() -> dict | None`` is injectable (``None`` ⇒ the production
    bot-home reader); tests pass a fixture reader so the join is exercised without
    touching the filesystem or sudo.
    """
    chans = channels if channels is not None else ROSTER_CHANNELS
    reader = oc_reader or _default_openclaw_reader(network, bot_id)
    oc = reader() or {}
    channels_block = oc.get("channels")
    if not isinstance(channels_block, dict):
        return {}
    out: dict[str, list[str]] = {}
    for ch in chans:
        eff = effective_group_allowlist(channels_block.get(ch))
        if eff:
            out[ch] = eff
    return out


def group_allowlist_gated_channels(
    network: dict,
    bot_id: str,
    *,
    channels: "tuple[str, ...] | None" = None,
    oc_reader: "Callable[[], dict | None] | None" = None,
) -> set[str]:
    """Channels whose group access IS allowlist-gated (``groupPolicy ==
    "allowlist"``) — **independent of whether the effective list is empty**.

    ``read_group_allowlists`` above returns only channels with a *non-empty*
    effective list, so a channel that is genuinely allowlist-gated but currently
    has zero members is omitted. The Users-page management UI needs to know the
    difference: a gated-but-empty channel must still show the add-by-id control
    (you have to be able to authorize the *first* group member), whereas a
    non-gated channel (``open``/``disabled``/absent) has no managed list and
    must show nothing. A channel is "gated" here iff
    ``effective_group_allowlist`` returns a list (``[]`` included) rather than
    ``None`` — i.e. exactly when ``groupPolicy == "allowlist"``.

    ``oc_reader`` is injectable so the caller can read ``openclaw.json`` once and
    share it with ``read_group_allowlists`` (the Users-page GET does this).
    """
    chans = channels if channels is not None else ROSTER_CHANNELS
    reader = oc_reader or _default_openclaw_reader(network, bot_id)
    oc = reader() or {}
    channels_block = oc.get("channels")
    if not isinstance(channels_block, dict):
        return set()
    return {
        ch for ch in chans
        if effective_group_allowlist(channels_block.get(ch)) is not None
    }


def resolve_roster(
    network: dict,
    bot_id: str,
    *,
    shared_dir: "Path | None" = None,
    channels: "tuple[str, ...] | None" = None,
    overlay: "dict | None" = None,
    activity: "dict[str, dict[str, Any]] | None" = None,
    allowfrom_reader: "Callable[[str], list[str] | None] | None" = None,
) -> list[RosterRecord]:
    """Resolve the full admitted roster for ``bot_id`` — one record per identity.

    Loads the overlay and per-user activity once (unless supplied), then for each
    channel reads the allowFrom membership and resolves every id through
    ``build_record``. ``allowfrom_reader(channel) -> list[str] | None`` is injectable
    (``None`` ⇒ the production bot-home file reader); tests pass a fixture reader so the
    join is exercised without touching the filesystem or sudo.

    Records are returned in ``(channel-order, allowFrom-order)`` — deterministic and
    matching the order the Users page renders them.
    """
    shared_dir = Path(shared_dir) if shared_dir is not None else _shared_dir(network)
    chans = channels if channels is not None else ROSTER_CHANNELS
    if overlay is None:
        overlay = ro.load_overlay(shared_dir, bot_id)
    if activity is None:
        activity = _load_activity(network, bot_id, shared_dir)
    reader = allowfrom_reader or _default_allowfrom_reader(network, bot_id)

    records: list[RosterRecord] = []
    for channel in chans:
        allow = reader(channel)
        if not allow:
            continue
        for stable_id in allow:
            if not stable_id:
                continue
            records.append(build_record(
                network, bot_id, channel, stable_id,
                overlay=overlay, activity=activity, shared_dir=shared_dir))
    return records


def _load_activity(
    network: dict, bot_id: str, shared_dir: Path,
) -> "dict[str, dict[str, Any]]":
    """Aggregate per-user activity exactly as the Users-page GET does — aggregate then
    rewrite Slack DM keys to user keys. Soft-fails to an empty map so a missing/locked
    turns dir degrades to "no last-seen", never an error."""
    try:
        activity = ua.aggregate(shared_dir, bot_id)
        return ua.rewrite_slack_dm_keys(activity, network, bot_id=bot_id)
    except Exception as exc:  # noqa: BLE001 — activity is best-effort enrichment
        log.warning("roster_resolver activity for %s failed: %s", bot_id, exc)
        return {}


# ── rights summary (role → short human phrase) ───────────────────────────


def rights_summary(role: str, overlay: dict) -> str:
    """A terse, human-legible summary of what a role can do on this bot.

    Computed from the *effective* capability set (honoring the per-bot
    ``role_bindings`` overlay), not hardcoded per role, so a retuned binding shows up
    here. Intended for the per-turn roster digest the bot reads ("P3 — participant,
    chat only") and any operator-facing roster view.
    """
    if role == "blocked":
        return "blocked"
    caps = capabilities.resolve_role_capabilities(
        role, overlay_role_bindings=(overlay.get("role_bindings") or {}))
    if caps >= set(capabilities.BUILTIN_CAPABILITIES):
        return "full control"
    # Order matters for readability — broadest reach first.
    labelled = [
        ("bot.roster.mutate", "roster"),
        ("bot.channel.config", "channels"),
        ("bot.config.modify", "config"),
        ("bot.send_external", "external reach"),
        ("bot.app.install", "apps"),
        ("bot.code.modify", "code"),
        ("bot.roles.bind", "role bindings"),
    ]
    parts = [phrase for cap, phrase in labelled if cap in caps]
    return ", ".join(parts) if parts else "chat only"


# ── Labels (descriptive chips, distinct from role) ───────────────────────


def resolve_labels(
    network: dict, bot_id: str, platform: str, stable_id: str,
) -> list[str]:
    """Descriptive role chips for an identity — ``pod_admin`` and/or ``owner``.

    Distinct from ``role``: ``role`` is the operative permission tier (a pod admin
    resolves to ``role="admin"``); ``labels`` are the descriptive badges the page has
    always shown. Derived from the same ``network.json`` claims ``roster_overlay`` uses
    for role resolution, reusing its lookups so there is one definition of "is pod
    admin / is primary owner".
    """
    labels: list[str] = []
    if stable_id in ro._pod_admin_ids_for(network, platform):
        labels.append("pod_admin")
    if stable_id in ro._primary_owner_ids_for(network, bot_id, platform):
        # "owner" for both single- and multi-user bots; multi-user-ness shows on
        # the bot tile, not the chip.
        labels.append("owner")
    return labels


# ── Name resolution chain (moved from routes_bot_users) ──────────────────
#
# First-hit-wins priority chain. Behavior is preserved verbatim from the original
# ``routes_bot_users`` helpers so the Users-page JSON is byte-identical after the
# refactor; the source-tag strings ("resolved_names", "pod_admin_names",
# "bot_primary", "identity_cache", "allowlist_only") are part of the contract the UI
# and the existing tests assert on.


def resolve_display_name(
    network: dict, bot_id: str, channel: str, ext_id: str, shared_dir: Path,
) -> "tuple[str | None, str]":
    """Multi-source display-name lookup. Returns ``(name, source-tag)``.

    Priority: ``pod.admins.resolved_names[<ch>:<id>]`` → ``pod.admins.names[<id>]`` →
    the bot's ``primary_user.name`` (when this id is the primary) → the pairing-time
    ``identity_cache`` → the background user-name cache ``pod.users.resolved[<ch>:<id>]``
    (warmed by the Usage-page sweep; covers discovered non-admin users the
    admin-keyed sources above don't carry) → ``(None, "allowlist_only")``.

    The ``pod.users.resolved`` step is intentionally LAST: every step above it is
    an admin/roster source whose output is part of the byte-identical Users-page
    contract, so the new step can only *add* a name for an id that currently
    resolves to None — never change an existing resolution.
    """
    pod = network.get("pod") or {}
    admins = pod.get("admins") or {}
    # 1. resolved_names cache (channel-keyed; populated by name_resolver).
    cache_key = f"{channel}:{ext_id}"
    resolved = (admins.get("resolved_names") or {}).get(cache_key) or {}
    name = resolved.get("name") or resolved.get("username")
    if name:
        return name, "resolved_names"
    # 2. pod.admins.names (manual map, keyed by ext_id).
    name = (admins.get("names") or {}).get(ext_id)
    if name:
        return name, "pod_admin_names"
    # 3. bot primary's recorded name.
    # Membership: a person may hold several ids on one channel (M1-B2 /
    # invariant 6), and the recorded name belongs to all of them.
    if ext_id in ro._primary_owner_ids_for(network, bot_id, channel):
        primary_block = (
            (network.get("bots") or {}).get(bot_id) or {}
        ).get("primary_user") or {}
        if primary_block.get("name"):
            return primary_block["name"], "bot_primary"
    # 4. identity_cache (pairing-time meta).
    cached = read_identity_cache(shared_dir, channel, ext_id)
    if cached:
        name = cached.get("display_name")
        if not name:
            name = meta_to_display_name(channel, cached.get("meta") or {})
        if name:
            return name, "identity_cache"
    # 5. pod.users.resolved (background user-name cache; warmed by the
    #    Usage-page sweep + the user_resolver CLI). Cache-only — never a live
    #    API call here. Lazy import keeps the evo package out of this module's
    #    load-time graph (matches _default_allowfrom_reader's discipline).
    #    Positive entries only; a negative sentinel keeps falling through to
    #    allowlist_only, so an unresolvable id renders the same as today.
    from .evo import user_resolver as _ur
    user_entry = _ur.cached_user(network, channel, ext_id, max_age_days=0)
    if user_entry and user_entry.get("resolved"):
        name = user_entry.get("name") or user_entry.get("username")
        if name:
            return name, "users_resolved"
    return None, "allowlist_only"


def resolve_username(
    network: dict, channel: str, ext_id: str, shared_dir: Path,
) -> "str | None":
    """Platform @handle for a user, or None. Same sources as the name chain but
    returns the username rather than the display name."""
    pod = network.get("pod") or {}
    admins = pod.get("admins") or {}
    cache_key = f"{channel}:{ext_id}"
    resolved = (admins.get("resolved_names") or {}).get(cache_key) or {}
    username = resolved.get("username")
    if username:
        return username
    cached = read_identity_cache(shared_dir, channel, ext_id)
    if cached:
        meta = cached.get("meta") or {}
        username = meta.get("username")
        if username:
            return username
    return None


def resolve_email(network: dict, channel: str, ext_id: str) -> "str | None":
    """Email lookup, separate from the name chain. Today only Slack carries email
    (via ``pod.admins.resolved_names`` when the bot app has ``users:read.email``)."""
    pod = network.get("pod") or {}
    admins = pod.get("admins") or {}
    cache_key = f"{channel}:{ext_id}"
    resolved = (admins.get("resolved_names") or {}).get(cache_key) or {}
    email = resolved.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def identity_cache_path(shared_dir: Path, channel: str, ext_id: str) -> Path:
    return Path(shared_dir) / "identity_cache" / channel / f"{ext_id}.json"


def read_identity_cache(
    shared_dir: Path, channel: str, ext_id: str,
) -> "dict | None":
    path = identity_cache_path(shared_dir, channel, ext_id)
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, PermissionError,
            json.JSONDecodeError, ValueError, OSError):
        return None


def meta_to_display_name(channel: str, meta: dict) -> "str | None":
    """Channel-specific extraction of a primary display name from pairing meta.

    OpenClaw's pairing JSON carries provider-specific meta — Telegram's
    ``firstName/lastName/username``, Slack's ``name``, Discord's
    ``global_name/username``. Pull the most-human-recognizable form, falling back to
    whatever's present. Also imported by ``routes_bot_users`` for its pairing-time
    identity-cache write, so the read and write sides agree on the extraction.
    """
    if not isinstance(meta, dict):
        return None
    if channel == "telegram":
        first = (meta.get("firstName") or "").strip()
        last = (meta.get("lastName") or "").strip()
        full = (first + " " + last).strip()
        return full or meta.get("username") or meta.get("name")
    if channel == "slack":
        return (meta.get("real_name") or meta.get("display_name")
                or meta.get("name"))
    if channel == "discord":
        return (meta.get("global_name") or meta.get("username")
                or meta.get("name"))
    if channel == "whatsapp":
        return meta.get("pushname") or meta.get("name")
    return meta.get("name") or meta.get("username")


def _shared_dir(network: dict) -> Path:
    # Default via the platform-keyed canonical path (evolve_config), not a hardcoded
    # macOS literal — the 8.3 Linux port depends on no bare ``/Users/`` paths.
    return Path(network.get("sharedDir") or CANONICAL_SHARED_DIR)
