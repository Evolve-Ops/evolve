"""Operator subscription preferences — Phase A3.

Sparse, file-backed storage of operator overrides on top of the
catalog defaults from Phase A1. The dispatcher (Phase A2) calls
``read_subscription`` on every catalog-event send to resolve the
effective ``(enabled, frequency)`` for that event.

Storage layout — ``{shared_dir}/alerts/subscriptions.json``:

::

    {
      "version": 1,
      "subscriptions": {
        "security.audit_finding":     {"enabled": true,  "frequency": "immediate"},
        "cost.daily_threshold":       {"enabled": true,  "frequency": "once_per_day_max"},
        "decisions.proposal_applied": {"enabled": false},
        "updates.evolve_repo":        {"enabled": true,  "frequency": "weekly_digest"}
      },
      "channel_override": null,
      "digest_hour_local": 8,
      "updated_at": "2026-05-10T14:32:00Z"
    }

Sparse semantics: only operator overrides land in the file. Events
absent from the file fall back to catalog defaults. ``read_subscription``
always returns a fully-resolved value so callers don't have to know
whether it came from the catalog or the operator.

This module is the **single point** the dispatcher consults for
operator preferences. Phase A4 adds the HTTP API that calls these
helpers; Phase B builds the UI on top of A4.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import catalog as _cat
from . import subscriptions_catalog as _subcat


# ── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Subscription:
    """Effective subscription state for one event — catalog defaults
    overlaid by operator overrides. The dispatcher and UI consume this
    type; they don't need to know which fields came from where.

    Phase 2: ``enabled`` is now resolved through the event's **operator
    Subscription** (group) rather than a per-event toggle. ``frequency``
    stays per-event (digest cadence is an event-shape property). The
    legacy per-event ``enabled`` override is still honored as a fallback
    for events without a group (``subscription=None``) and for migration —
    see ``_resolve_enabled``.
    """

    event_key: str
    enabled: bool
    frequency: _cat.Frequency
    is_overridden: bool   # True iff the operator has any entry for this event


@dataclass(frozen=True)
class SubscriptionGroup:
    """Effective operator-facing Subscription (group) state — registry
    default overlaid by the operator's group-level on/off override.

    This is the unit the Configure tab toggles and the dispatcher gates
    on. ``member_event_keys`` lists the CatalogEvent keys that belong to
    the group, in catalog order.
    """

    id: str
    label: str
    description: str
    enabled: bool
    is_safety_critical: bool
    is_overridden: bool
    default_enabled: bool
    member_event_keys: tuple[str, ...]


# ── Storage path + atomic IO ────────────────────────────────────────────────


def _state_path(shared_dir: Path) -> Path:
    return shared_dir / "alerts" / "subscriptions.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    """Returns a dict in the on-disk shape. Empty defaults on first run /
    parse error — never raises to the caller."""
    p = _state_path(shared_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "version": 1,
            "subscriptions": {},
            "channel_override": None,
            "digest_hour_local": 8,
        }


def _save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    """Atomic temp-file + rename. Never partial-writes the live file."""
    p = _state_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _utc_iso_now()
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _utc_iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ── Subscription-group (operator-facing) resolution ─────────────────────────
#
# On-disk shape (additive — sits alongside the per-event ``subscriptions``
# map in the same subscriptions.json):
#
#     "subscription_groups": {
#         "upstream_tracking": {"enabled": true},
#         "cost_enforcement":  {"enabled": false}
#     }
#
# Sparse: only operator overrides land here. Absent → registry
# ``default_enabled``. This is the authoritative on/off gate; the per-event
# ``enabled`` override is a migration-era fallback (see _resolve_enabled).


_GROUPS_KEY = "subscription_groups"


def _group_enabled_from_state(
    state: dict[str, Any], subscription_id: str
) -> tuple[bool, bool]:
    """Return ``(enabled, is_overridden)`` for a Subscription group.

    Reads the operator override from ``state`` overlaid on the registry
    default. ``is_overridden`` is True iff the operator set this group's
    enabled explicitly. Unknown id → registry-less default of True so a
    stray event can't go silent (the totality ratchet guards against
    unknown ids landing in the first place).
    """
    sub = _subcat.by_id(subscription_id)
    default = sub.default_enabled if sub is not None else True
    groups = state.get(_GROUPS_KEY)
    rec = groups.get(subscription_id) if isinstance(groups, dict) else None
    if isinstance(rec, dict) and "enabled" in rec:
        return bool(rec["enabled"]), True
    return default, False


def _resolve_enabled(
    state: dict[str, Any],
    entry: _cat.CatalogEvent,
    overrides: dict[str, Any],
) -> bool:
    """Resolve an event's effective enabled state (Phase 2 group gating).

    Precedence:
      1. If the event belongs to a Subscription group, the **group's**
         on/off is authoritative. (A per-event override is ignored for
         the on/off decision — the group is the operator's surface now.)
      2. If the event has no group (``subscription=None`` — internal/
         removed), honor a legacy per-event ``enabled`` override, else
         the event's ``default_enabled``. These events are not exposed in
         Configure and preserve their existing behavior.
    """
    if entry.subscription is not None:
        enabled, _ = _group_enabled_from_state(state, entry.subscription)
        return enabled
    return bool(overrides.get("enabled", entry.default_enabled))


# ── Read API ────────────────────────────────────────────────────────────────


def read_subscription(shared_dir: Path, event_key: str) -> Subscription | None:
    """Return the effective subscription for ``event_key``.

    Catalog defaults overlaid by operator overrides. Returns ``None``
    when ``event_key`` is unknown to the catalog (typo / future event).
    """
    # Legacy key passthrough: when a catalog entry has been retired and
    # mapped to a survivor (e.g. the 2026-05-29 warn/critical collapse),
    # transparently resolve to the survivor so stale callers / state
    # files keep working until the next write.
    event_key = _cat.LEGACY_KEY_MIGRATION.get(event_key, event_key)

    entry = _cat.by_key(event_key)
    if entry is None:
        return None

    state = _load_state(shared_dir)
    saved = state.get("subscriptions") or {}
    overrides = saved.get(event_key) or {}
    # If the operator's saved state still has an override under the
    # legacy key but no override under the survivor, treat the legacy
    # override as the effective one. Avoids silently losing a user's
    # explicit on/off preference across a key rename.
    if not overrides:
        for legacy, surviving in _cat.LEGACY_KEY_MIGRATION.items():
            if surviving == event_key and legacy in saved:
                overrides = saved[legacy] or {}
                break

    # Phase 2: enabled resolves through the event's operator Subscription
    # (group). The group's on/off is the gate; the per-event ``enabled``
    # override survives only as a migration fallback (events with no group,
    # or operators who set a per-event toggle before the group layer
    # existed). _resolve_enabled encodes that precedence.
    enabled = _resolve_enabled(state, entry, overrides)
    freq_value = overrides.get("frequency")
    if freq_value:
        try:
            frequency = _cat.Frequency(freq_value)
        except ValueError:
            # Stale override (catalog removed a frequency option). Fall
            # back to catalog default — never raise to the dispatcher.
            frequency = entry.default_frequency
        else:
            # Legacy frequencies (ONCE_PER_DAY_MAX / ONCE_PER_WEEK_MAX
            # / OFF) auto-migrate to their successor. OFF flips the
            # subscription to disabled rather than picking a frequency.
            if frequency == _cat.Frequency.OFF:
                enabled = False
                frequency = entry.default_frequency
            elif frequency in _cat.LEGACY_FREQUENCY_MIGRATION:
                frequency = _cat.LEGACY_FREQUENCY_MIGRATION[frequency]
            # If the migrated frequency isn't in the (current) allowed
            # set, fall back to catalog default.
            if frequency not in entry.allowed_frequencies:
                frequency = entry.default_frequency
    else:
        frequency = entry.default_frequency

    return Subscription(
        event_key=event_key,
        enabled=bool(enabled),
        frequency=frequency,
        is_overridden=bool(overrides),
    )


def read_all_subscriptions(shared_dir: Path) -> tuple[Subscription, ...]:
    """Return one ``Subscription`` per catalog event, in catalog order.

    Used by the (Phase A4) API route ``GET /api/alerts/subscriptions``
    so the UI can render the full Subscriptions tab from a single fetch.
    """
    out: list[Subscription] = []
    for entry in _cat.CATALOG:
        sub = read_subscription(shared_dir, entry.key)
        if sub is not None:
            out.append(sub)
    return tuple(out)


def read_subscription_group(
    shared_dir: Path, subscription_id: str
) -> SubscriptionGroup | None:
    """Return the effective state of one operator Subscription (group).

    Registry default overlaid by the operator's group-level override.
    Returns ``None`` for an unknown id.
    """
    sub = _subcat.by_id(subscription_id)
    if sub is None:
        return None
    state = _load_state(shared_dir)
    enabled, is_overridden = _group_enabled_from_state(state, subscription_id)
    members = tuple(
        e.key for e in _cat.events_for_subscription(subscription_id)
    )
    return SubscriptionGroup(
        id=sub.id,
        label=sub.label,
        description=sub.description,
        enabled=enabled,
        is_safety_critical=sub.is_safety_critical,
        is_overridden=is_overridden,
        default_enabled=sub.default_enabled,
        member_event_keys=members,
    )


def read_all_subscription_groups(
    shared_dir: Path,
) -> tuple[SubscriptionGroup, ...]:
    """Return all 13 Subscription groups in registry (operator-IA) order.

    Powers the Phase-2 Configure GET — 13 groups, not ~65 events.
    """
    state = _load_state(shared_dir)
    out: list[SubscriptionGroup] = []
    for sub in _subcat.all_subscriptions():
        enabled, is_overridden = _group_enabled_from_state(state, sub.id)
        members = tuple(
            e.key for e in _cat.events_for_subscription(sub.id)
        )
        out.append(SubscriptionGroup(
            id=sub.id,
            label=sub.label,
            description=sub.description,
            enabled=enabled,
            is_safety_critical=sub.is_safety_critical,
            is_overridden=is_overridden,
            default_enabled=sub.default_enabled,
            member_event_keys=members,
        ))
    return tuple(out)


def write_subscription_group(
    shared_dir: Path, subscription_id: str, *, enabled: bool,
) -> SubscriptionGroup:
    """Persist the operator's on/off for one Subscription group.

    The whole group is gated by this single toggle — every member event's
    effective ``enabled`` resolves through it. ``subscription_id`` must be
    a known registry id (otherwise ``KeyError``).
    """
    if _subcat.by_id(subscription_id) is None:
        raise KeyError(f"unknown subscription group: {subscription_id!r}")
    state = _load_state(shared_dir)
    groups = state.setdefault(_GROUPS_KEY, {})
    if not isinstance(groups, dict):
        groups = {}
        state[_GROUPS_KEY] = groups
    rec = groups.get(subscription_id)
    if not isinstance(rec, dict):
        rec = {}
    rec["enabled"] = bool(enabled)
    groups[subscription_id] = rec
    _save_state(shared_dir, state)
    group = read_subscription_group(shared_dir, subscription_id)
    assert group is not None  # we just wrote it
    return group


def read_digest_hour_local(shared_dir: Path) -> int:
    state = _load_state(shared_dir)
    raw = state.get("digest_hour_local", 8)
    try:
        h = int(raw)
    except (TypeError, ValueError):
        return 8
    if h < 0 or h > 23:
        return 8
    return h


def read_channel_override(shared_dir: Path) -> tuple[str, str] | None:
    """Return ``(channel, chat_id)`` if the operator has set an override
    via Subscriptions UI; else ``None`` (dispatcher falls back to its
    own recipient resolution)."""
    state = _load_state(shared_dir)
    cfg = state.get("channel_override")
    if not isinstance(cfg, dict):
        return None
    channel = cfg.get("channel")
    chat_id = cfg.get("chat_id")
    if not channel or not chat_id:
        return None
    return str(channel), str(chat_id)


# ── Multi-channel fanout selection (PWA Phase 1.2) ──────────────────────────
#
# The Subscriptions UI's top "Send to:" row lets the operator pick which
# delivery surfaces every catalog event fans out to. ``pwa_push`` is the
# only additive channel today — slack/telegram/discord all share the
# single primary-chat path (network.alerts.channel) and toggling between
# them is its own decision (channel_override). PWA push is independent:
# it goes to every registered browser regardless of the primary chat
# channel.
#
# Defaults: primary chat is always on (back-compat with every existing
# operator install); pwa_push defaults *off* so a freshly-installed pod
# doesn't start firing pushes the operator never asked for.

DEFAULT_PWA_PUSH_ENABLED = False


def read_pwa_push_enabled(shared_dir: Path) -> bool:
    """Return whether ``pwa_push`` is in the operator's active channel set.

    Sparse storage — absent means default. The dispatcher reads this on
    every catalog send to decide whether to fanout to PWA subscribers
    in addition to the primary chat channel.
    """
    state = _load_state(shared_dir)
    channels = state.get("active_channels")
    if not isinstance(channels, dict):
        return DEFAULT_PWA_PUSH_ENABLED
    raw = channels.get("pwa_push")
    if raw is None:
        return DEFAULT_PWA_PUSH_ENABLED
    return bool(raw)


def write_pwa_push_enabled(shared_dir: Path, enabled: bool) -> bool:
    state = _load_state(shared_dir)
    channels = state.setdefault("active_channels", {})
    if not isinstance(channels, dict):
        # Legacy / corrupt — reset.
        channels = {}
        state["active_channels"] = channels
    channels["pwa_push"] = bool(enabled)
    _save_state(shared_dir, state)
    return bool(enabled)


# ── Write API ───────────────────────────────────────────────────────────────


def write_subscription(
    shared_dir: Path,
    event_key: str,
    *,
    enabled: bool | None = None,
    frequency: _cat.Frequency | str | None = None,
) -> Subscription:
    """Persist an operator override for one event.

    Either or both of ``enabled`` and ``frequency`` may be set; the
    other field stays at whatever was previously stored (or, if
    nothing was, the catalog default).

    Validation:
      - ``event_key`` must exist in the catalog (otherwise ``KeyError``)
      - ``frequency`` must be in the event's ``allowed_frequencies``
        (otherwise ``ValueError``) — protects against the UI wiring
        an invalid combo
    """
    # Legacy-key passthrough mirrors read_subscription — see catalog.
    # LEGACY_KEY_MIGRATION for the active retirements.
    event_key = _cat.LEGACY_KEY_MIGRATION.get(event_key, event_key)
    entry = _cat.by_key(event_key)
    if entry is None:
        raise KeyError(f"unknown catalog event: {event_key!r}")

    if frequency is not None:
        if isinstance(frequency, str):
            try:
                frequency = _cat.Frequency(frequency)
            except ValueError as exc:
                raise ValueError(
                    f"unknown frequency value {frequency!r}"
                ) from exc
        if frequency not in entry.allowed_frequencies:
            raise ValueError(
                f"frequency {frequency.value!r} not in allowed_frequencies "
                f"for {event_key}: {[f.value for f in entry.allowed_frequencies]}"
            )

    state = _load_state(shared_dir)
    subs = state.setdefault("subscriptions", {})
    rec = subs.get(event_key, {})
    if enabled is not None:
        rec["enabled"] = bool(enabled)
    if frequency is not None:
        rec["frequency"] = frequency.value
    subs[event_key] = rec
    _save_state(shared_dir, state)

    # Return the resolved subscription so callers (e.g. API handlers)
    # can echo back exactly what landed.
    sub = read_subscription(shared_dir, event_key)
    assert sub is not None  # we just wrote it
    return sub


def write_digest_hour_local(shared_dir: Path, hour: int) -> int:
    if not (0 <= hour <= 23):
        raise ValueError(f"digest_hour_local must be 0..23, got {hour}")
    state = _load_state(shared_dir)
    state["digest_hour_local"] = int(hour)
    _save_state(shared_dir, state)
    return hour


def reset(shared_dir: Path) -> None:
    """Wipe operator overrides; everything reverts to catalog defaults.

    Equivalent of the Subscriptions-UI "Reset all to defaults" button.
    Atomic — operators see all-or-nothing reset.
    """
    p = _state_path(shared_dir)
    if not p.exists():
        return
    _save_state(
        shared_dir,
        {
            "version": 1,
            "subscriptions": {},
            "subscription_groups": {},
            "channel_override": None,
            "digest_hour_local": 8,
        },
    )


__all__ = (
    "Subscription",
    "SubscriptionGroup",
    "read_subscription_group",
    "read_all_subscription_groups",
    "write_subscription_group",
    "read_subscription",
    "read_all_subscriptions",
    "read_digest_hour_local",
    "read_channel_override",
    "read_pwa_push_enabled",
    "write_subscription",
    "write_digest_hour_local",
    "write_pwa_push_enabled",
    "DEFAULT_PWA_PUSH_ENABLED",
    "reset",
)
