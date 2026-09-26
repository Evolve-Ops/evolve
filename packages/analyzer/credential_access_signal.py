"""credential_access_signal — the Signal for "Evolve still cannot read a bot's
credential store after the inline heal ran".

Issue #3477. Companion to the inline heal in :mod:`oc_auth_store`: that module
repairs the Linux ACL-mask clamp *at the credential read* and retries, so the
overwhelmingly common case (clamp → heal → key found) is silent — deliberately,
because a self-healed clamp is exactly the hourly flap the
``pod_perms_drift_monitor`` silence was introduced to end. This module carries
the OTHER half: when the canonical heal has already run and the credential
source is STILL unreadable, the pod is genuinely engine-dark and only an
operator can move it.

Why the emit lives here and not in the reader
---------------------------------------------
``oc_auth_store`` is stdlib-only at import time (both the admin and the
analyzer package import it, on hosts where the Signal store may not exist yet).
Keeping the ``signals.store.observe`` call site in its own module lets the
reader reach it through one lazy import inside the failure branch, and gives
``signals.protection_registry`` a single file to claim in ``emits_from``.

Lifecycle
---------
* **Fire** — :func:`observe_unreadable`, from the reader's post-heal branch.
  One Signal per bot (``signature = credential_access:credential_store_unreadable:<bot>``),
  so a scanner sweep that re-reads the same clamped bot dedups into one alert.
* **Clear** — :func:`sweep_resolve_healthy`, from the hourly
  ``pod_perms_drift_monitor`` (which already computes, per bot, whether the
  evolve read/traverse contract holds after its periodic reassert). A bot whose
  contract verifies drops out of ``kept_signatures`` and its Signal
  auto-archives within ≤1 cycle. Deliberately NOT resolved from the reader's
  success path: that would put a Signal-store scan on every credential read.

Delivery: producer ``credential_access`` is not deny-listed in
``signal_notifier._DIRECT_DISPATCH_PRODUCERS``, and
``_catalog_event_for_signal`` maps it to ``system.credential_store_unreadable``
— a dedicated IMMEDIATE catalog event, NOT the DAILY_DIGEST
``system.watchdog_event`` / ``system.bot_file_unreadable`` buckets that left
``repo_puller_sudoers`` undelivered for 24h+ with ``deliveries: []``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("evolve.oc_store")

__all__ = [
    "PRODUCER",
    "SIGNAL_TYPE",
    "signature_for",
    "observe_unreadable",
    "sweep_resolve_healthy",
]

PRODUCER = "credential_access"
SIGNAL_TYPE = "credential_store_unreadable"

# One-command remediation. Named in the Signal body AND on the catalog event's
# cli_action so the operator gets the same answer wherever they read it.
FIX_COMMAND = "sudo evolve-admin ensure-pod-perms"


def signature_for(bot_id: str) -> str:
    """Dedup signature for one bot's unreadable-credential-store condition."""
    from schema.signal import make_signature  # lazy: keeps import light

    return make_signature(PRODUCER, SIGNAL_TYPE, bot_id)


def _resolve_shared_dir(network: "dict[str, Any] | None") -> "Path | None":
    """The pod's ``sharedDir``, or ``None`` when it cannot be resolved.

    Platform-keyed default via ``evolve_config`` (never a ``/Users/`` literal).
    Returns ``None`` rather than guessing if ``evolve_config`` is unavailable —
    a Signal written into the wrong tree is worse than no Signal.
    """
    try:
        from evolve_config import CANONICAL_SHARED_DIR, get_shared_dir  # type: ignore
    except Exception as exc:  # noqa: BLE001 — bootstrap host without the analyzer pkg
        logger.debug("credential_access: evolve_config unavailable (%s)", exc)
        return None
    if isinstance(network, dict) and network:
        try:
            return get_shared_dir(network)
        except Exception as exc:  # noqa: BLE001 — malformed network.json
            logger.debug("credential_access: get_shared_dir failed (%s)", exc)
    return Path(CANONICAL_SHARED_DIR)


def _body(bot_id: str, clamped_paths: "list[str]", heal_outcome: str) -> str:
    shown = "\n".join(f"- `{p}`" for p in clamped_paths[:6])
    more = (
        f"\n- …and {len(clamped_paths) - 6} more"
        if len(clamped_paths) > 6
        else ""
    )
    return (
        f"Evolve could not read {bot_id}'s stored provider credentials, tried "
        f"to repair the permissions itself, and still could not read them.\n\n"
        f"Unreadable:\n{shown}{more}\n\n"
        f"Self-repair result: {heal_outcome}.\n\n"
        f"While this lasts, anything that needs a provider key for this bot "
        f"reports \"no provider credentialed\" — and if this is the pod's "
        f"primary bot, that is every background Evolve feature that calls an "
        f"LLM (proposal synthesis, summaries, classifiers, the admin help "
        f"bot). Nothing is lost; the work simply does not happen until access "
        f"is restored.\n\n"
        f"Fix: `{FIX_COMMAND}`. It is idempotent and applies only the "
        f"permission repairs it lists."
    )


def observe_unreadable(
    *,
    bot_id: str,
    clamped_paths: "Iterable[str]",
    heal_outcome: str,
    network: "dict[str, Any] | None" = None,
    shared_dir: "Path | None" = None,
) -> bool:
    """Fire (or dedup into) the per-bot unreadable-credential-store Signal.

    Best-effort by construction: every failure path returns ``False`` and logs,
    because a Signal-store problem must never turn a degraded credential read
    into a raised exception inside a scanner. Returns ``True`` iff a Signal was
    written.
    """
    paths = [str(p) for p in clamped_paths]
    root = shared_dir if shared_dir is not None else _resolve_shared_dir(network)
    if root is None:
        return False
    try:
        from signals import store as signals_store  # type: ignore
    except Exception as exc:  # noqa: BLE001 — signals pkg absent on a bootstrap host
        logger.debug("credential_access: signals.store unavailable (%s)", exc)
        return False
    try:
        signals_store.observe(
            Path(root),
            signature=signature_for(bot_id),
            producer=PRODUCER,
            type=SIGNAL_TYPE,
            scope="bot",
            bot_id=bot_id,
            title=f"Evolve cannot read {bot_id}'s provider credentials",
            body=_body(bot_id, paths, heal_outcome),
            details=dict(
                clamped_paths=paths,
                heal_outcome=heal_outcome,
                vector="operations",
                magnitude=4,
                severity_active=True,
                what_it_means=(
                    "The stored provider credentials for this bot are on disk "
                    "but Evolve is locked out of the directory that holds "
                    "them. Evolve already tried its own permission repair and "
                    "the directory is still unreadable, so this will not clear "
                    "on its own."
                ),
                fix_steps=(
                    f"1. Run `{FIX_COMMAND}` — it re-applies the read "
                    "permissions Evolve needs.\n"
                    "2. If it reports that sudo grants are missing, run "
                    "`sudo evolve-admin refresh-sudoers` first, then repeat "
                    "step 1.\n"
                    "3. This alert clears by itself within an hour of access "
                    "being restored."
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — never break a credential read
        logger.error(
            "credential_access: observe failed for bot_id=%s (%s) — the "
            "unreadable credential store on %s is NOT on the Alerts page",
            bot_id, exc, ", ".join(paths) or "(unknown path)",
        )
        return False
    return True


def sweep_resolve_healthy(
    shared_dir: Path,
    *,
    checked_bots: "Iterable[str]",
    still_unreadable_bots: "Iterable[str]",
) -> int:
    """Auto-archive this producer's Signals for every CHECKED bot that is no
    longer locked out.

    Called from the hourly ``pod_perms_drift_monitor`` after its periodic
    evolve-access reassert: the bots it could not restore stay in
    ``still_unreadable_bots``, the rest of ``checked_bots`` clears.

    ``checked_bots`` is load-bearing, not decoration — it becomes the store's
    ``bot_ids`` filter. Without it, a run that evaluated NO bots (an empty
    ``members`` list, a network.json that failed to enumerate) would sweep with
    an empty keep-set and mass-resolve every bot's still-valid alert. An empty
    ``checked_bots`` therefore resolves nothing at all.

    Returns the number of Signals resolved (0 on any failure — best-effort, like
    every other sweep in that daemon).
    """
    checked = {str(b) for b in checked_bots}
    if not checked:
        return 0
    kept = {signature_for(b) for b in still_unreadable_bots}
    try:
        from signals import store as signals_store  # type: ignore

        resolved = signals_store.sweep_resolve(
            Path(shared_dir),
            producer=PRODUCER,
            kept_signatures=kept,
            types={SIGNAL_TYPE},
            bot_ids=checked,
            reason="auto-resolve: evolve can read the credential store again",
        )
    except Exception as exc:  # noqa: BLE001 — a sweep failure must not fail the run
        logger.warning("credential_access: sweep_resolve failed (%s)", exc)
        return 0
    return len(resolved)
