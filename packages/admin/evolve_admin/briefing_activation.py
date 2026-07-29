"""briefing_activation — install a recorded briefing when the channel arrives.

The add-bot wizard records the operator's morning-briefing decision on
the bot's ``network.json`` block (``bots.<id>.briefing{enabled, time,
decided_at}``, M3) — but a day-1 bot usually has no messaging channel,
so the briefing app can't ship yet (the C-A4 coherence gate correctly
refuses a messaging app on a channel-less bot). The 2026-06-11 design
sync decision is **offer-now, auto-activate-later**: when such a bot
gains its FIRST messaging channel, the recorded decision activates by
itself — the briefing app installs through the normal gallery path and
the operator gets an honest receipt. Either path, no silent failure.

The trigger is the channel-registration chokepoint
(``skills._oc_install_common.write_oc_config`` — every messaging-skill
connect flows through it); this module is the pure-Python check + queue
behind it. No LLM here: the install itself is the ordinary forge job.

Spec trail: docs/decision-add-bot-m4-u1-proof-2026-06-11.md (finding 4 /
§The 07:00 window) → this fix.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Job states that mean an install is already underway — re-queueing
# would race two forge jobs onto the same app.
_IN_FLIGHT_STATES = frozenset({
    "queued", "running", "awaiting_approval", "approved",
})

#: The app_id slug the pack driver derives for the briefing package
#: ("Morning Briefing" → "morning-briefing") — the manifest filename.
BRIEFING_APP_ID = "morning-briefing"


# ── Seams (tests substitute instead of building real bot homes) ──────────────

# loader(app_id, bot_id, shared_dir) -> manifest | None
_manifest_loader = None


def set_manifest_loader(fn) -> None:
    global _manifest_loader
    _manifest_loader = fn


def _load_manifest(app_id: str, bot_id: str, shared_dir: Path):
    if _manifest_loader is not None:
        return _manifest_loader(app_id, bot_id, shared_dir)
    from .applications.manifest import load_manifest

    return load_manifest(app_id, bot_id, shared_dir)


# reader(bot_id) -> set[str]
_channels_reader = None


def set_channels_reader(fn) -> None:
    global _channels_reader
    _channels_reader = fn


def _enabled_messaging_channels(bot_id: str) -> set:
    if _channels_reader is not None:
        return _channels_reader(bot_id)
    from .channels import enabled_messaging_channels

    return enabled_messaging_channels(bot_id)


def _briefing_time(bot_block: dict[str, Any]) -> str:
    briefing = bot_block.get("briefing") or {}
    return str(briefing.get("time") or "07:00")


def _route_note(bot_block: dict[str, Any], bot_id: str, channels: set[str]) -> str:
    """One honest sentence about where the briefing will land — empty
    when a recipient is already recorded on a connected channel, the
    remaining step otherwise. Rides the activation receipt so the
    operator never gets a promise the delivery route can't keep."""
    primary = bot_block.get("primary_user") or {}
    ext = primary.get("external_ids") or {}
    if isinstance(ext, dict) and any(
        ch in channels and str(ext.get(ch) or "").strip() for ch in ext
    ):
        return ""
    return (
        f"\nOne step left so it knows who to message: set yourself as "
        f"{bot_id}'s person on the admin page (Users → Set primary user)."
    )


def maybe_activate(
    bot_id: str,
    *,
    shared_dir: Path,
    network: Optional[dict[str, Any]] = None,
) -> str:
    """Activate a recorded-but-uninstalled briefing for ``bot_id`` if
    its moment has come. Returns a short outcome string (for logs and
    tests):

    * ``"queued"`` — the briefing install (plus its calendar
      foundation, when missing) was queued through the normal gallery
      path; the pack-driver worker pushes the activation receipt or
      the failure notification when the job finishes.
    * ``"not_decided"`` / ``"briefing_off"`` — no recorded yes.
    * ``"already_installed"`` / ``"install_in_flight"`` — nothing to do.
    * ``"no_channel"`` — called without a live messaging channel (the
      gate would refuse; the next channel connect retries).

    Pure Python; safe to call repeatedly (find-or-skip semantics).
    """
    from .applications.forge_jobs import list_jobs_for_app
    from .config import load_network
    from .evo.wizard import pack as _pack
    from .evo.wizard import pack_driver as _packdrv

    if network is None:
        network = load_network()
    bot_block = ((network.get("bots") or {}).get(bot_id)) or {}
    briefing = bot_block.get("briefing")
    if not isinstance(briefing, dict):
        return "not_decided"
    if not briefing.get("enabled"):
        return "briefing_off"

    # A failed install strands the seeded manifest at status "updating"
    # (the gate refusal happens after Step 1 wrote it — exactly Ledger's
    # state after the M4 run). That is NOT installed; the forge re-install
    # path handles an existing manifest, so activation retries it.
    manifest = _load_manifest(BRIEFING_APP_ID, bot_id, shared_dir)
    if manifest is not None and str(
        getattr(manifest, "status", "") or "",
    ) != "updating":
        return "already_installed"
    for job in list_jobs_for_app(_pack.MORNING_BRIEFING_PKG_ID, bot_id, shared_dir):
        if str(job.status) in _IN_FLIGHT_STATES:
            return "install_in_flight"

    channels = _enabled_messaging_channels(bot_id)
    if not channels:
        return "no_channel"

    # Foundation before behavior — calendar feeds the briefing (the
    # same fold the wizard finalize applies).
    apps: list[dict[str, str]] = []
    if _load_manifest("calendar-sync", bot_id, shared_dir) is None:
        apps.append({
            "pkg_id": _pack.CALENDAR_SYNC_PKG_ID,
            "name": "Calendar Sync",
        })
    apps.append({
        "pkg_id": _pack.MORNING_BRIEFING_PKG_ID,
        "name": "Morning Briefing",
    })

    purpose = bot_block.get("purpose") or {}
    time_str = _briefing_time(bot_block)
    queued = _packdrv.queue_pack_installs(
        shared_dir,
        bot_id=bot_id,
        apps=apps,
        network=network,
        config_seed={
            "mission": str(purpose.get("mission") or ""),
            "delivery_time": time_str,
        },
        briefing_announce={
            "pkg_id": _pack.MORNING_BRIEFING_PKG_ID,
            "time": time_str,
            "route_note": _route_note(bot_block, bot_id, channels),
        },
    )
    failed = [q for q in queued if not q.ok]
    if failed:
        # Queue-time failure (gallery package missing, store error) —
        # the worker never sees these jobs, so report them here through
        # the same loud path the worker uses for mid-build failures.
        for q in failed:
            _packdrv.notify_outcome(
                shared_dir, network, q, bot_id,
                status="failed", detail=q.error,
                briefing_announce=None,
            )
        log.error(
            "briefing activation for %s: %d install(s) failed to queue",
            bot_id, len(failed),
        )
    log.info("briefing activation for %s: queued %s", bot_id,
             ", ".join(q.name for q in queued if q.ok) or "nothing")
    return "queued"


def on_channels_registered(
    bot_id: str,
    *,
    before: set[str],
    after: set[str],
) -> None:
    """Hook target for the channel-registration chokepoint: fire the
    activation check when a bot goes from zero messaging channels to
    one or more. Never raises — the channel write must succeed
    regardless of what happens here."""
    if before or not after:
        return
    try:
        from .config import DEFAULT_SHARED_DIR

        outcome = maybe_activate(bot_id, shared_dir=Path(DEFAULT_SHARED_DIR))
        log.info(
            "first messaging channel registered for %s (%s) — briefing "
            "activation: %s",
            bot_id, ", ".join(sorted(after)), outcome,
        )
    except Exception:
        log.exception(
            "briefing activation hook failed for %s — the channel write "
            "itself succeeded",
            bot_id,
        )
