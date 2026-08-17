"""
Per-bot Setup checklist — tracks the operator's progress through the
recommended next steps after a bot is provisioned: GitHub backup, search
plugin, secondary LLM credentials, cost profile, a gallery app, and so on.

Two surfaces (locked design 2026-06-01):

  1. A counter chip on the Overview's bot tile (``Setup X/N``) that opens
     a modal mirroring the Settings-tab page. Suppressible per-bot via
     "Stop showing on tile".
  2. A permanent page under per-bot Settings — same content, always
     reachable even after the chip is suppressed.

Storage on disk lives in ``network.json::bots.<bot>.setup_checklist``::

    {
      "tile_chip_suppressed_at": "<iso8601>" | null,
      "items": {
        "<item_id>": {
          "state": "pending" | "done" | "dismissed",
          "last_changed_at": "<iso8601>"
        },
        ...
      }
    }

The per-item ``state`` field is authoritative. ``done`` is set by the
auto-detector; ``dismissed`` is an explicit operator "no thanks". Both
count toward the counter; only ``pending`` keeps the chip visible.

New-item resurface: when a new entry is added to ``CHECKLIST_ITEMS``,
``evaluate()`` seeds it with the current timestamp on first run. If the
operator previously suppressed the chip, that timestamp is later than
``tile_chip_suppressed_at`` and ``is_chip_visible()`` returns True
again. The chip's job is "have you considered this?", and a new
consideration deserves a fresh prompt.

Step 1 scaffolded the data layer with stub detectors; step 2 wired the
real detectors (see DETECTORS below — one isolated function per item).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from evolve_util import now_iso_micro as _now_iso

from . import channel_registry as _channel_registry


STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_DISMISSED = "dismissed"

_VALID_STATES = frozenset({STATE_PENDING, STATE_DONE, STATE_DISMISSED})


# Detector signature: (bot_id, network, openclaw_cfg) -> done?
# ``openclaw_cfg`` is the bot's parsed openclaw.json (or None when the
# caller hasn't loaded it). Detectors that don't need it ignore the arg.
Detector = Callable[[str, dict[str, Any], "dict[str, Any] | None"], bool]


# ``applies_to`` predicate signature: (bot_id, network) -> does this item apply?
# Lets an item opt out of bot types it makes no sense for (e.g. the built-in
# system assistants), so an N/A item never renders, seeds state, or counts.
AppliesTo = Callable[[str, dict[str, Any]], bool]


def _applies_always(bot_id: str, network: dict[str, Any]) -> bool:
    """Default: the item applies to every bot."""
    return True


def _not_reserved(bot_id: str, network: dict[str, Any]) -> bool:
    """The item applies only to member bots, not the reserved/built-in
    assistants (``evolve`` / ``evo``).

    Used for setup items that are meaningless for a system assistant — it has
    no gallery apps, no GitHub backup, and no secondary-LLM credential story of
    its own. Lazy-imports ``is_reserved_account`` to avoid an import cycle,
    matching the other lazy ``..config`` imports in this module.
    """
    try:
        from .config import is_reserved_account
    except Exception:
        return True
    return not is_reserved_account(bot_id, network)


@dataclass(frozen=True)
class ChecklistItem:
    """One row of the Setup checklist.

    ``deep_link`` is the admin-UI route the row's "go" button opens — the
    same link is used by the chip's modal and the Settings page.

    ``applies_to`` decides whether the item is relevant to a given bot;
    ``evaluate()`` filters non-applicable items out before running detectors,
    so they never render, seed stored state, or count toward the chip. Defaults
    to "applies to all" (``_applies_always``).
    """

    id: str
    label: str
    detect: Detector
    deep_link: str | None = None
    applies_to: AppliesTo = _applies_always


# ── Detectors ─────────────────────────────────────────────────────────────
#
# Each detector returns True iff its setup item is "done" for the given bot.
# All detectors must be tolerant of missing files / malformed config — return
# False on uncertainty rather than raising. The caller (evaluate()) treats
# False as ``pending``, which is the safe default for a setup checklist
# (nag the operator rather than silently mark complete).
#
# Detectors that read external files use ``_read_json_or_none`` for resilient
# reads (direct read → sudo /bin/cat fallback → None). The pattern mirrors
# web.routes_bot_users._read_json_or_none which handles the same ACL edge
# case (evolve has read on .openclaw/ but not always on subdirectories).


# Pairing channels. Projection of the channel registry's ``pairing`` column —
# the providers we surface in the Users UI are also the providers we count for
# the Setup checklist, and now both derive from the same place rather than
# from a "keep in sync" comment (invariant 7).
_PAIRING_CHANNELS: tuple[str, ...] = _channel_registry.pairing_channel_ids()

# Plugins that satisfy the "search plugin installed" item. Brave is the
# pod-invariant default (network.json::podInvariantIntegrations); tavily is
# the documented alternative an operator can swap in. Add new search
# providers here when they ship.
_SEARCH_PLUGINS: tuple[str, ...] = ("brave", "tavily")

# Known LLM providers that store credentials in auth-profiles.json.
# Mirrors web.server._KNOWN_PROVIDERS — kept here so the detector module
# doesn't import a Flask-attached file just for this constant.
_LLM_PROVIDERS: frozenset[str] = frozenset({
    "anthropic", "openai", "google", "xai", "mistral", "cohere",
    "groq", "perplexity", "together", "deepseek", "moonshot",
    "runway", "suno",
})


def _read_json_or_none(path: Path) -> "dict | None":
    """Read JSON with a sudo /bin/cat fallback. Returns the parsed dict on
    success, ``{}`` on parse error, or ``None`` when the file is genuinely
    absent / unreadable even via the sudo grant.

    Ported from web.routes_bot_users to avoid importing that Flask-attached
    module just for a 20-line helper. The shape rationale (why not
    ``Path.exists()``) is documented there.
    """
    text: "str | None" = None
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        text = None
    if text is None:
        try:
            r = subprocess.run(
                ["sudo", "-n", "/bin/cat", str(path)],
                check=True, capture_output=True, text=True, timeout=5,
            )
            text = r.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}


def _bot_credentials_dir(bot_id: str, network: dict[str, Any]) -> Path:
    """``/Users/<bot>/.openclaw/credentials/`` for the bot."""
    # Lazy import — keeps the detector module importable without a real
    # ``evolve_admin.config`` (handy when this module is exercised in
    # isolated unit tests that monkeypatch detectors).
    from .config import bot_home
    return bot_home(bot_id, network) / ".openclaw" / "credentials"


def _detect_purpose(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """The bot has a declared purpose (archetype) in network.json.

    Purpose is the Effectiveness-Layer anchor — what the bot is FOR — that
    the Fit Reviewer reads to decide what "more effective" means for it.
    It already lives at ``network.json::bots[<id>].purpose`` (the same
    ``network`` dict this detector receives), so there's no file read here.
    Delegates to ``bot_purpose.has_purpose`` so the "is it set?" predicate
    has a single home shared with the write API and the reviewer.
    """
    try:
        from bot_purpose import has_purpose
    except Exception:
        return False
    return has_purpose(network, bot_id)


def _detect_pairing(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """At least one messaging channel has an approved user.

    "Paired" means the bot can actually talk to someone — a non-empty
    ``allowFrom`` list for at least one channel. A pairing.json that
    exists with no approved users doesn't count: the channel is
    configured but the bot is still mute.
    """
    creds_dir = _bot_credentials_dir(bot_id, network)
    for channel in _PAIRING_CHANNELS:
        allow = _read_json_or_none(creds_dir / f"{channel}-default-allowFrom.json")
        if isinstance(allow, dict) and allow.get("allowFrom"):
            return True
    return False


def _detect_github(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """Backup is wired AND the daemon has actually pushed recently.

    Requires both:
      1. ``network.json::bots[bot].backupRepoUrl`` is set (the wiring),
         AND
      2. ``{workspace}/evolve-backup/state.json::last_success_at`` is
         within the last 24h (the wiring actually works).

    Distinguishes "configured but the daemon never ran / push failed"
    from "configured and pushing nightly." Without #2 the operator
    would see the checklist say done while the backup is silently
    broken — exactly the kind of false-positive the spec warns against
    (see project_safety_nets_shipped_2026_05_23 for the same pattern).
    """
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    url = bot_cfg.get("backupRepoUrl")
    if not (isinstance(url, str) and url.strip()):
        return False
    try:
        from .config import get_bot_workspace
    except Exception:
        return False
    ws = get_bot_workspace(bot_id)
    if ws is None:
        return False
    state_path = ws / "evolve-backup" / "state.json"
    state = _read_json_or_none(state_path)
    if not isinstance(state, dict):
        return False
    last_success = state.get("last_success_at")
    if not isinstance(last_success, str) or not last_success.strip():
        return False
    # Parse ISO 8601 (the writer in backup.py uses datetime.utcnow().isoformat()
    # which lacks a tz suffix; tolerate both bare-isoformat and with-tz).
    try:
        dt = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() <= 24 * 3600


def _detect_search(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """A search plugin is enabled in openclaw.json (``plugins.entries``)."""
    if not openclaw_cfg:
        return False
    entries = (openclaw_cfg.get("plugins") or {}).get("entries") or {}
    if not isinstance(entries, dict):
        return False
    for plugin_id in _SEARCH_PLUGINS:
        entry = entries.get(plugin_id)
        if isinstance(entry, dict) and entry.get("enabled") is not False:
            return True
    return False


def _detect_ai_optim_tiers(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """The workhorse tier (tier2) has at least one model assigned.

    Tier config lives in ``~/.openclaw/evolve-tiers.json`` — a separate
    file owned by the bot user, NOT in openclaw.json. OC's schema
    validator actively rejects ``tiers`` written under
    ``agents.defaults.model``, so the cross-file split is intentional.
    See packages/analyzer/oc_model.py for the storage rationale.

    Only tier2 (workhorse) is required — it runs human-facing turns,
    so without it the bot can't take a turn at all. The judge tier
    (tier0) defaults to the workhorse model when unset; picking a
    distinct cross-provider judge is a recommendation surfaced on the
    AI Optimization page but not a gate. Other tiers (tier1 power,
    tier3 grunt) have sensible defaults and aren't required here.

    ``openclaw_cfg`` is ignored — the data we need isn't in that file.
    """
    try:
        from .config import bot_home
    except Exception:
        return False
    tiers_path = bot_home(bot_id, network) / ".openclaw" / "evolve-tiers.json"
    tiers_doc = _read_json_or_none(tiers_path)
    if not isinstance(tiers_doc, dict):
        return False
    # Resolve tier2 across both config shapes: a migrated rungs/roles file has
    # no top-level ``tiers`` key, so the bare ``.get("tiers")`` would report
    # "tier2 unconfigured" on every migrated pod (a false checklist-incomplete).
    try:
        from primary_bot import (  # type: ignore
            resolve_tier_chain,
            merge_model_catalog,
            tier_override_is_broken,
        )
    except Exception:
        return False
    # Explicitly-broken beats defaulted-OK. An operator-written tier2 whose
    # models are empty/whitespace is BROKEN config, not absent config — the
    # keyed merge now no-ops such an empty override so the bot still resolves
    # via defaults (it won't brick), but the checklist must still nag the
    # operator to fix what they typed (spec §Addendum 2 / #2561 onboarding
    # semantics). Absent (omitted) tier2 stays configured-via-defaults.
    if tier_override_is_broken(tiers_doc, "tier2"):
        return False
    # Keyed-merge the pod-base catalog (network.json::models) so a bot that
    # relies on a pod-wide rung for `standard` reads as configured, matching
    # the gateway's keyed-merge resolution (spec §Addendum A.4). Without this
    # the checklist would false-flag incomplete on a pod-base-only standard.
    # With defaults folded in, an ABSENT tier2 now resolves (configured); only
    # an explicit-but-empty tier2 (caught above) flags.
    pod_models = (network or {}).get("models") if isinstance(network, dict) else None
    merged = merge_model_catalog(pod_models, tiers_doc)
    models = resolve_tier_chain(merged, "tier2")
    return any(isinstance(m, str) and m.strip() for m in models)


def _detect_secondary_llm(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """At least 2 distinct LLM providers have credentials in auth-profiles.json.

    Reads ``/Users/<bot>/.openclaw/agents/main/agent/auth-profiles.json``
    via the sudo /bin/cat fallback (evolve has a sudoers grant for this
    file — see wizard_verify._read_auth_profiles).
    """
    try:
        from .config import bot_home
    except Exception:
        return False
    auth_path = (
        bot_home(bot_id, network)
        / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
    )
    auth = _read_json_or_none(auth_path)
    if not isinstance(auth, dict):
        return False
    profiles = auth.get("profiles")
    if not isinstance(profiles, dict):
        return False
    providers: set[str] = set()
    for prof in profiles.values():
        if not isinstance(prof, dict):
            continue
        provider = prof.get("provider")
        if isinstance(provider, str) and provider in _LLM_PROVIDERS:
            providers.add(provider)
            if len(providers) >= 2:
                return True
    return False


def _detect_cost_profile(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """The operator has set at least one cost-knob field in openclaw.json.

    Mirrors ``analyzer.cost_profiles.COST_FIELDS`` — the first four live
    under ``agents.defaults`` and ``session`` lives at root (OC rejects
    ``agents.defaults.session``). Any one of them present + non-None means
    a profile has been applied.
    """
    if not openclaw_cfg:
        return False
    defaults = (openclaw_cfg.get("agents") or {}).get("defaults") or {}
    if isinstance(defaults, dict):
        for field in ("heartbeat", "contextPruning", "compaction",
                      "bootstrapTotalMaxChars"):
            if defaults.get(field) is not None:
                return True
    if openclaw_cfg.get("session") is not None:
        return True
    return False


def _detect_gallery_app(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """At least one app installed from the gallery (manifest with ``pkg_id``)."""
    try:
        from .applications.manifest import list_manifests
    except Exception:
        return False
    shared_dir = Path(network.get("sharedDir") or "/Users/Shared/evolve")
    try:
        manifests = list_manifests(shared_dir, bot_id)
    except Exception:
        return False
    return any(m.is_gallery_app() for m in manifests)


def _detect_spend_cap(
    bot_id: str,
    network: dict[str, Any],
    openclaw_cfg: dict[str, Any] | None,
) -> bool:
    """``network.json::bots[bot_id].daily_cap_usd`` is set to a positive number.

    action_cost rejects ≤ 0 (the supported "no cap" state is a missing
    field, not a zero), so we mirror that and treat 0 / negatives /
    non-numeric values as "not set."
    """
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    cap = bot_cfg.get("daily_cap_usd")
    if cap is None:
        return False
    try:
        return float(cap) > 0
    except (TypeError, ValueError):
        return False


# Registry — order is display order. Adding a new entry here is what
# drives the "new item resurface" path in is_chip_visible().
#
# ``deep_link`` is the admin SPA's ``data-page`` value (no leading slash —
# the frontend's nav-click pattern uses ``[data-page="X"]`` selectors).
# A future enhancement can layer per-bot pre-selection on top, but for
# step 3 this is "take the operator to the right page; they're already
# on the bot they want to configure."
CHECKLIST_ITEMS: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        # Purpose is foundational — it's the anchor the Fit Reviewer needs
        # before it can suggest anything, so it leads the list. The
        # deep_link opens the Settings page, which hosts the Purpose card
        # (botcfg-purpose) under its Bots subtab. "settings" is the real
        # nav-item data-page; there is no "bot-detail" nav target (the
        # per-bot config moved under Settings → Bots), so linking there
        # would land in _setupChecklistGo's "no nav target" branch.
        "purpose",
        "Bot goals / purpose defined",
        _detect_purpose,
        deep_link="settings",
    ),
    ChecklistItem(
        "pairing",
        "Messaging channel paired",
        _detect_pairing,
        deep_link="users",
    ),
    ChecklistItem(
        "github",
        "GitHub backup wired",
        _detect_github,
        deep_link="integrations-keys",
        # N/A for the built-in assistants — their state lives on the pod, not in
        # a per-bot GitHub backup repo.
        applies_to=_not_reserved,
    ),
    ChecklistItem(
        "search",
        "Search plugin installed",
        _detect_search,
        deep_link="apps",
    ),
    ChecklistItem(
        "ai_optim_tiers",
        "AI optimization tiers configured",
        _detect_ai_optim_tiers,
        deep_link="ai-optimization",
    ),
    ChecklistItem(
        "secondary_llm",
        "Secondary LLM provider configured",
        _detect_secondary_llm,
        deep_link="integrations-keys",
        # N/A for the built-in assistants — they run on the pod's primary model
        # config, not a per-bot secondary-provider credential set.
        applies_to=_not_reserved,
    ),
    ChecklistItem(
        "cost_profile",
        "Cost-optimization profile picked",
        _detect_cost_profile,
        deep_link="cost-measures",
    ),
    ChecklistItem(
        "gallery_app",
        "First gallery app installed",
        _detect_gallery_app,
        deep_link="apps",
        # N/A for the built-in assistants — gallery apps are a member-bot
        # capability, not something a system assistant installs for itself.
        applies_to=_not_reserved,
    ),
    ChecklistItem(
        "spend_cap",
        # Wording match for the Cost Optimization page's "Edit cost levers"
        # daily-cap picker. The cap is the L1 circuit-breaker threshold —
        # when crossed, heartbeat / background sessions auto-pause via
        # spend_alert.py. Surfacing the breaker vocabulary here makes the
        # link between the checklist row and the picker on the page
        # operator-obvious instead of relying on shared mental model.
        "Daily cap (circuit breaker) set",
        _detect_spend_cap,
        deep_link="cost-measures",
    ),
)

ITEMS_BY_ID: dict[str, ChecklistItem] = {item.id: item for item in CHECKLIST_ITEMS}


def _bot_checklist(network: dict[str, Any], bot_id: str) -> dict[str, Any]:
    """Return the bot's setup_checklist sub-dict, scaffolding it if absent."""
    bots = network.setdefault("bots", {})
    bot = bots.setdefault(bot_id, {})
    cl = bot.get("setup_checklist")
    if not isinstance(cl, dict):
        cl = {"tile_chip_suppressed_at": None, "items": {}}
        bot["setup_checklist"] = cl
    cl.setdefault("tile_chip_suppressed_at", None)
    cl.setdefault("items", {})
    return cl


def get_state(network: dict[str, Any], bot_id: str) -> dict[str, Any]:
    """Return the raw ``setup_checklist`` dict (creating it if absent)."""
    return _bot_checklist(network, bot_id)


def set_item_state(
    network: dict[str, Any],
    bot_id: str,
    item_id: str,
    state: str,
) -> None:
    if state not in _VALID_STATES:
        raise ValueError(f"invalid checklist state: {state!r}")
    if item_id not in ITEMS_BY_ID:
        raise KeyError(f"unknown checklist item: {item_id!r}")
    cl = _bot_checklist(network, bot_id)
    existing = cl["items"].get(item_id)
    if existing and existing.get("state") == state:
        # Preserve last_changed_at when nothing actually changed — the
        # resurface logic relies on these timestamps being stable.
        return
    cl["items"][item_id] = {"state": state, "last_changed_at": _now_iso()}


def dismiss_item(network: dict[str, Any], bot_id: str, item_id: str) -> None:
    """Mark an item ``dismissed`` — counts as done, sticks across evaluate()."""
    set_item_state(network, bot_id, item_id, STATE_DISMISSED)


def suppress_chip(network: dict[str, Any], bot_id: str) -> None:
    """Hide the tile chip until a new item lands (or reset_chip is called)."""
    _bot_checklist(network, bot_id)["tile_chip_suppressed_at"] = _now_iso()


def reset_chip(network: dict[str, Any], bot_id: str) -> None:
    """Bring the tile chip back. Per-item dismissals stay — toggle them
    individually in the Settings page if you want them re-evaluated."""
    _bot_checklist(network, bot_id)["tile_chip_suppressed_at"] = None


def evaluate(
    network: dict[str, Any],
    bot_id: str,
    openclaw_cfg: dict[str, Any] | None = None,
    *,
    items: tuple[ChecklistItem, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run each detector, persist pending↔done transitions, and return the
    merged render payload.

    The result is a dict keyed by item id with: ``state``, ``last_changed_at``,
    ``label``, ``deep_link``, and ``auto_detected`` (False for dismissed
    items so the UI can label the row "you set this manually").

    Mutates ``network`` in place; callers serving a checklist endpoint
    should follow up with ``save_network(network)`` so new-item seeds and
    auto-detected transitions are persisted.

    ``items`` is for tests — production callers should rely on the
    module-level ``CHECKLIST_ITEMS``.

    Items whose ``applies_to(bot_id, network)`` is False are dropped before any
    detector runs — so for the built-in assistants (``evolve`` / ``evo``) the
    N/A items (gallery_app / github / secondary_llm) never render, never seed
    stored state, and never count. ``counter()`` / ``is_chip_visible()`` /
    ``should_show_in_actions_menu()`` derive from the returned merged dict, so
    they adjust automatically.
    """
    registry = items if items is not None else CHECKLIST_ITEMS
    registry = tuple(
        item for item in registry if item.applies_to(bot_id, network)
    )
    cl = _bot_checklist(network, bot_id)
    stored = cl["items"]
    out: dict[str, dict[str, Any]] = {}
    for item in registry:
        prev = stored.get(item.id)
        if prev and prev.get("state") == STATE_DISMISSED:
            out[item.id] = {
                "state": STATE_DISMISSED,
                "last_changed_at": prev["last_changed_at"],
                "label": item.label,
                "deep_link": item.deep_link,
                "auto_detected": False,
            }
            continue
        detected_done = bool(item.detect(bot_id, network, openclaw_cfg))
        new_state = STATE_DONE if detected_done else STATE_PENDING
        if not prev or prev.get("state") != new_state:
            stored[item.id] = {"state": new_state, "last_changed_at": _now_iso()}
        out[item.id] = {
            "state": stored[item.id]["state"],
            "last_changed_at": stored[item.id]["last_changed_at"],
            "label": item.label,
            "deep_link": item.deep_link,
            "auto_detected": True,
        }
    return out


def evaluate_dirty(
    network: dict[str, Any],
    bot_id: str,
    openclaw_cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Like ``evaluate()``, but also returns a ``dirty`` flag.

    True iff the persisted ``setup_checklist`` sub-dict for this bot was
    mutated by the evaluate call (first-time seed, or detector flipped a
    state). Callers can gate ``save_network`` on this flag to avoid
    writing on every tile refresh when nothing changed — important
    because ``save_network`` invokes ``sudo /usr/sbin/chown`` on every
    write to assert ownership.
    """
    pre = json.dumps(get_state(network, bot_id), sort_keys=True)
    merged = evaluate(network, bot_id, openclaw_cfg)
    post = json.dumps(get_state(network, bot_id), sort_keys=True)
    return merged, pre != post


def counter(merged_state: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """``(done, total)`` for the chip label. Done counts both ``done`` and ``dismissed``."""
    done = sum(
        1
        for entry in merged_state.values()
        if entry["state"] in (STATE_DONE, STATE_DISMISSED)
    )
    return done, len(merged_state)


def is_chip_visible(
    network: dict[str, Any],
    bot_id: str,
    merged_state: dict[str, dict[str, Any]],
) -> bool:
    """Whether to render the Setup chip on the bot tile.

    Visible when at least one item is ``pending`` AND either the chip is
    not suppressed, or a pending item has ``last_changed_at`` later than
    ``tile_chip_suppressed_at`` (the new-item / pending-revert resurface).
    """
    pending = [
        entry for entry in merged_state.values() if entry["state"] == STATE_PENDING
    ]
    if not pending:
        return False
    cl = _bot_checklist(network, bot_id)
    suppressed_at = cl.get("tile_chip_suppressed_at")
    if not suppressed_at:
        return True
    return any(entry["last_changed_at"] > suppressed_at for entry in pending)


def should_show_in_actions_menu(
    network: dict[str, Any],
    bot_id: str,
    merged_state: dict[str, dict[str, Any]],
) -> bool:
    """Whether to surface a "Setup checklist" entry in the bot tile's
    Actions ⋯ menu.

    True when the operator suppressed the chip but still has at least one
    pending item — the menu is the escape hatch back to the checklist
    when the visible chip has been turned off. False if the chip was
    never suppressed (the chip itself is the entry point) or if every
    item is done/dismissed (nothing to nag about).
    """
    cl = _bot_checklist(network, bot_id)
    if not cl.get("tile_chip_suppressed_at"):
        return False
    return any(entry["state"] == STATE_PENDING for entry in merged_state.values())
