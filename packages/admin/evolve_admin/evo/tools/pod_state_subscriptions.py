"""Pod-state read tool — operator notification subscriptions.

Exposes one tool:

* ``pod_state.subscriptions`` — read the operator's effective alert /
  notification subscription state. Returns one record per catalog
  event, fully resolved (catalog default overlaid by any
  ``{shared_dir}/alerts/subscriptions.json`` override) so evo can
  answer "am I subscribed to X?" without having to know the sparse-
  storage convention.

Filed against the 2026-06-02 hallucination diagnosis
(``docs/diagnosis-evo-subscription-awareness-2026-06-02.md``):
operator on Telegram asked evo to unsubscribe from the
``security.config_drift`` notifications. Evo, with no subscription-
aware tool, invented an OC config path
(``agents.defaults.subscriptions``), got a path-not-found error from
the OC gateway, and surfaced "you're not subscribed" to the operator
— a hallucination. The operator *is* subscribed (catalog default +
absent override).

Schema (one element of ``subscriptions[]``)::

    {
      "key": "security.config_drift",
      "label": "Bot configuration changed unexpectedly",
      "category": "security",
      "description": "...",
      "enabled": true,             # effective (override OR catalog default)
      "frequency": "immediate",    # effective
      "default_enabled": true,     # what the catalog ships
      "default_frequency": "immediate",
      "is_override": false,        # True iff subscriptions.json has an entry
      "is_safety_critical": true,
      "severity": "error",
      "producer_source": "heal",
      "source_level_enabled": true,             # effective alerts.<source>.enabled
      "source_level_key": "alerts.heal.enabled",
      "allowed_frequencies": ["immediate"],
      "channels": ["primary_chat"],             # always; +"pwa_push" if enabled
    }

The ``source_level_enabled`` field is the kill switch operators
sometimes flip in Settings → Pod Config: when off, *every* event with
that ``producer_source`` is silenced regardless of the per-event
``enabled`` flag. Evo needs this distinction to explain "you're
subscribed but the heal source is off, so nothing fires."

Soft-fail behavior:
  * Missing ``subscriptions.json`` → returns all catalog defaults
    (subscriptions module already handles this internally).
  * Malformed ``subscriptions.json`` → returns ``error`` field in the
    payload alongside whatever defaults were resolvable; never raises.
  * Missing / unreadable ``network.json`` → ``source_level_enabled``
    falls back to ``True`` (the default for every catalog source) so
    evo doesn't claim "your source is off" when we just couldn't
    read it.

Spec / context:
* ``docs/diagnosis-evo-subscription-awareness-2026-06-02.md`` —
  canonical for paths, schemas, root-cause framing.
* ``docs/spec-alert-subscriptions-2026-05-10.md`` — original
  alert-subscriptions spec; A1 catalog, A3 storage, A4 routes, B UI.
* ``project_evo_oc_native_architecture`` — direct-access tool
  surface for evo.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Catalog sources that ship in ``better-engine-config.json::pod_defaults
# .alerts.<source>.enabled``. The dispatcher's ``_read_source_enabled``
# uses ``config_sandbox.resolve`` with this same path shape (see
# ``alerts/dispatcher.py:1153``); we mirror it here so a freshly-deployed
# pod with no overrides resolves to the documented defaults rather than
# silently returning ``None`` for every source.
_DEFAULT_SOURCE_ENABLED_FALLBACK = True


def _read_source_enabled(shared_dir: Path, source: str) -> bool | None:
    """Best-effort lookup of ``alerts.<source>.enabled`` via the same
    ``config_sandbox`` path the dispatcher uses.

    Returns ``True``/``False`` if resolved; ``None`` if the config-
    sandbox module is unavailable or threw — caller renders ``None`` as
    "not resolvable" instead of asserting a (possibly wrong) default.
    """
    try:
        from .. import tools as _unused  # noqa: F401 — keep import side-effects
        from evolve_admin.alerts._config_lookup import lookup  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        value = lookup(
            shared_dir, f"alerts.{source}.enabled", _DEFAULT_SOURCE_ENABLED_FALLBACK,
        )
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, bool):
        return value
    # Tolerate stringified-bool from older schema versions.
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    return _DEFAULT_SOURCE_ENABLED_FALLBACK


def _read_raw_overrides(
    shared_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Read the raw override maps from disk so we can tell which entries
    are operator overrides vs catalog/registry defaults.

    Returns ``(per_event_overrides, group_overrides, error_or_None)``.
    Phase 2: on/off lives under ``subscription_groups`` (the operator
    surface); ``subscriptions`` carries only per-event frequency
    overrides now. On malformed JSON we return ``({}, {}, msg)`` so the
    caller can surface the error without crashing the tool.
    """
    import json

    path = shared_dir / "alerts" / "subscriptions.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, {}, None
    except OSError as exc:
        return {}, {}, f"could not read subscriptions.json: {exc}"
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, {}, f"subscriptions.json is malformed JSON: {exc}"
    subs = state.get("subscriptions") if isinstance(state, dict) else None
    groups = state.get("subscription_groups") if isinstance(state, dict) else None
    return (
        subs if isinstance(subs, dict) else {},
        groups if isinstance(groups, dict) else {},
        None,
    )


def _project_subscription(
    *,
    entry: Any,
    sub: Any,
    is_override: bool,
    source_level_enabled: bool | None,
    subscription_group: Any = None,
) -> dict[str, Any]:
    """Build the model-facing record for one catalog event.

    ``entry`` is a ``CatalogEvent``; ``sub`` is a fully-resolved
    ``Subscription`` (group + per-event overlay applied). ``is_override``
    means the event's GROUP has an operator on/off override (Phase 2 — on/
    off is a group decision). ``subscription_group`` is the
    ``OperatorSubscription`` the event belongs to (or None for an internal
    event). We do not import the types at module scope — keeps this module
    loadable where the full alerts subpackage isn't importable.
    """
    return {
        "key": entry.key,
        "label": entry.label,
        "category": entry.category.value,
        "description": entry.description,
        # The operator-facing Subscription (group) the on/off lives on.
        "subscription_id": entry.subscription,
        "subscription_label": (
            subscription_group.label if subscription_group is not None else None
        ),
        # Effective (post-override) state — what the dispatcher will
        # actually do on the next send (enabled resolves through the group).
        "enabled": bool(sub.enabled),
        "frequency": sub.frequency.value,
        # Catalog defaults, surfaced separately so evo can answer "you
        # changed the default to off" vs "you're on the default."
        "default_enabled": bool(entry.default_enabled),
        "default_frequency": entry.default_frequency.value,
        "is_override": bool(is_override),
        # Phase 2: safety-critical is a group property; fall back to the
        # per-event flag for internal events (no group).
        "is_safety_critical": bool(
            subscription_group.is_safety_critical
            if subscription_group is not None
            else entry.is_safety_critical
        ),
        "severity": entry.severity.value,
        "producer_source": entry.producer_source,
        # Source-level kill switch — alerts.<source>.enabled in
        # better-engine-config.json (often surfaced in network.json
        # bundles). When False, the per-event toggle is overridden.
        "source_level_enabled": source_level_enabled,
        "source_level_key": f"alerts.{entry.producer_source}.enabled",
        "allowed_frequencies": tuple(f.value for f in entry.allowed_frequencies),
    }


def _handler(
    shared_dir: Path,
    key: str | None = None,
) -> dict[str, Any]:
    """Read the operator's effective subscription state.

    ``key``: optional filter. When provided, returns only the matching
    catalog entry (or an error if unknown). When omitted, returns every
    catalog entry in catalog order.
    """
    # Lazy imports keep the tool module importable in a test environment
    # that doesn't load the full alerts subpackage at collection time.
    try:
        from evolve_admin.alerts import catalog as _cat
        from evolve_admin.alerts import subscriptions as _subs
        from evolve_admin.alerts import subscriptions_catalog as _subcat
    except Exception as exc:  # noqa: BLE001
        log.warning("pod_state.subscriptions: alerts subpackage unavailable: %s", exc)
        return {
            "available": False,
            "error": f"alerts subpackage unavailable: {exc}",
            "subscriptions": [],
            "catalog_count": 0,
            "override_count": 0,
        }

    overrides, group_overrides, raw_error = _read_raw_overrides(shared_dir)

    def _group_for(entry: Any) -> Any:
        return _subcat.by_id(entry.subscription) if entry.subscription else None

    def _is_override(entry: Any) -> bool:
        # Phase 2: an event is "overridden" when its GROUP has an operator
        # on/off override (the operator surface) OR it carries a per-event
        # frequency override.
        gid = entry.subscription
        group_overridden = bool(gid and gid in group_overrides)
        return group_overridden or (entry.key in overrides)
    pwa_push_enabled = False
    try:
        pwa_push_enabled = bool(_subs.read_pwa_push_enabled(shared_dir))
    except Exception:  # noqa: BLE001
        pwa_push_enabled = False

    # Cache source-enabled lookups so we don't hammer config_sandbox once
    # per catalog entry (30+ events, many sharing the same producer).
    source_enabled_cache: dict[str, bool | None] = {}

    def _source_enabled(source: str) -> bool | None:
        if source not in source_enabled_cache:
            source_enabled_cache[source] = _read_source_enabled(shared_dir, source)
        return source_enabled_cache[source]

    # Filter mode: single-key lookup.
    if key is not None:
        entry = _cat.by_key(key)
        if entry is None:
            # Provide the top-K valid keys so evo can surface a useful
            # error to the operator (mirrors action.subscriptions.set's
            # KeyError message).
            valid = ", ".join(_cat.all_keys()[:8])
            return {
                "available": True,
                "error": (
                    f"unknown subscription key {key!r}. Valid keys (first "
                    f"8): {valid}, ..."
                ),
                "subscriptions": [],
                "subscription_config_path": str(
                    shared_dir / "alerts" / "subscriptions.json"
                ),
                "catalog_count": len(_cat.CATALOG),
                "override_count": len(group_overrides) + len(overrides),
            }
        sub = _subs.read_subscription(shared_dir, entry.key)
        # read_subscription returns None only for unknown keys; we just
        # confirmed the entry exists, so this assertion is the
        # contract.
        assert sub is not None
        records = [
            _project_subscription(
                entry=entry,
                sub=sub,
                is_override=_is_override(entry),
                source_level_enabled=_source_enabled(entry.producer_source),
                subscription_group=_group_for(entry),
            ),
        ]
    else:
        records = []
        for entry in _cat.CATALOG:
            sub = _subs.read_subscription(shared_dir, entry.key)
            if sub is None:
                # Should never happen for catalog-resident keys but keep
                # the loop robust against future catalog refactors.
                continue
            records.append(
                _project_subscription(
                    entry=entry,
                    sub=sub,
                    is_override=_is_override(entry),
                    source_level_enabled=_source_enabled(entry.producer_source),
                    subscription_group=_group_for(entry),
                ),
            )

    # Channels active for delivery. Primary chat is always on (the
    # subscription module treats it as the implicit baseline — see
    # active_channels comment in subscriptions.py:220). pwa_push is
    # additive and defaults off.
    channels = ["primary_chat"]
    if pwa_push_enabled:
        channels.append("pwa_push")
    # Stamp every record with the resolved channel set — same value for
    # every event (channel selection is global, not per-event).
    for rec in records:
        rec["channels"] = list(channels)

    out: dict[str, Any] = {
        "available": True,
        "subscriptions": records,
        "subscription_config_path": str(
            shared_dir / "alerts" / "subscriptions.json"
        ),
        "catalog_count": len(_cat.CATALOG),
        "override_count": len(group_overrides) + len(overrides),
        "channels_active": list(channels),
    }
    if raw_error:
        # Surface the malformed-file warning alongside the resolved
        # defaults — evo can then explain to the operator that the file
        # is corrupted and the displayed state is the catalog default,
        # not their saved preferences.
        out["error"] = raw_error
    return out


def make_handler(shared_dir: Path):
    """Bind ``shared_dir`` into a tool handler closure. Mirrors the
    ``pod_state.signals.firing`` / ``pod_state.home_narrative`` pattern.
    """
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _handler(shared_dir=shared_dir, **kwargs)
    return handler


# ─── Tool definition ────────────────────────────────────────────────────────


TOOL = Tool(
    name="pod_state.subscriptions",
    description=(
        "Read the operator's effective notification subscriptions — "
        "which catalog events route to chat, at what frequency, and "
        "whether each is currently enabled. Returns one record per "
        "catalog event with: friendly label (e.g. 'Bot configuration "
        "changed unexpectedly'), dotted key (e.g. 'security.config_drift' "
        "— the same string that appears in every notification's "
        "'subscription:' footer), effective enabled+frequency, catalog "
        "defaults, an is_override flag (True when the operator has set "
        "an override in subscriptions.json), and the source-level kill "
        "switch (alerts.<source>.enabled). Use whenever the operator "
        "asks 'am I subscribed to X?', 'unsubscribe me from Y', 'what "
        "alerts do I get?', or 'why didn't I see a notification for Z?' "
        "Pass an optional `key` arg (e.g. 'security.config_drift') to "
        "filter to one entry. ALWAYS read the effective `enabled` "
        "field, NOT the absence of an override entry: absent = catalog "
        "default, which is usually ON. NOT to be confused with firing "
        "Signals (pod_state.signals.firing) — Signals are observed pod "
        "state; subscriptions are notification-routing preferences."
    ),
    wire_description=(
        "Read the operator's effective notification subscriptions — "
        "one record per catalog event: dotted key, effective "
        "enabled+frequency, defaults, is_override, source-level kill "
        "switch. Use for 'am I subscribed to X?' or 'why didn't I see "
        "alert Z?'. Optional `key` filters to one entry. Read the "
        "effective `enabled` field — absence of an override means "
        "catalog default (usually ON), not unsubscribed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Optional. Catalog event key to filter on (dotted "
                    "form, e.g. 'security.config_drift'). When omitted, "
                    "returns every catalog event in catalog order. "
                    "Unknown keys return an error payload listing valid "
                    "keys — do NOT invent keys."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=_handler,                # placeholder; bound at registry-init time
    risk_tier=RiskTier.READ,         # pure read; no validate
    tags=("pod_state", "subscriptions", "alerts"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)


register(TOOL)


def init_for_shared_dir(shared_dir: Path) -> None:
    """Rebind the tool's handler with a real ``shared_dir``, replacing
    the unbound placeholder in the registry. Mirrors pod_state_signals /
    pod_state_home_narrative.
    """
    bound = make_handler(shared_dir)
    register(Tool(
        name=TOOL.name,
        description=TOOL.description,
        wire_description=TOOL.wire_description,
        input_schema=TOOL.input_schema,
        handler=lambda **kwargs: bound(**kwargs),
        risk_tier=TOOL.risk_tier,
        tags=TOOL.tags,
    ))
