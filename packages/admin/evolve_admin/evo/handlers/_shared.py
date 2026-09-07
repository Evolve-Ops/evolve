"""Shared helpers for the Wave-1 read-only ``evo`` subcommand handlers.

The Wave-1 handlers (``cost``, ``usage``, ``alerts``, ``health``,
``integrations``, ``apps``, ``gallery``, ``skills``) all share the same
shape:

  1. Decide pod-wide vs. per-bot scope from the invoking bot id.
  2. Read pre-aggregated data from the shared directory.
  3. Return a Team_bot_a-style summary (short header + facts) via DispatchResult.

The shape-1 helpers live here so the handlers themselves stay small and
focused on data shaping.

Scope convention: only the primary bot reports pod-wide. Every other bot
is a silo and only sees its own data — primary users on team or personal
bots aren't supposed to learn what other bots in the pod are doing. The
primary bot is resolved via ``primary_bot.primary_bot_id`` which honors
the ``network.primary`` field, the ``role: "primary"`` flag, or the
legacy ``"evolve"`` fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..dispatch import DispatchResult
from ..identity import Role


def load_firing_signal_dicts(shared_dir: Path) -> list[dict[str, Any]]:
    """Return firing Signals as plain dicts, sorted by id.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): goes through ``signals.store.iter_active`` rather than
    globbing ``{shared_dir}/signals/firing/`` directly. Falls back to a
    raw directory read only when the analyzer ``signals`` package isn't
    importable (partial deploy / test context) — the handlers must stay
    robust there, which is why this lives behind a try/except instead of
    a hard import.
    """
    def _raw_read() -> list[dict[str, Any]]:
        # Fallback: the signals package is unavailable / unreadable. Read
        # the firing/ subdir directly (the store-access-lint annotation
        # marks this deliberate fallback).
        firing_dir = shared_dir / "signals" / "firing"  # store-access-lint: analyzer-unavailable fallback
        if not firing_dir.exists():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(firing_dir.glob("*.json")):
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    try:
        from signals import store as _signals_store  # type: ignore[import-not-found]
    except Exception:
        return _raw_read()

    try:
        return [
            sig.to_dict()
            for sig in _signals_store.iter_active(shared_dir, state="firing")
        ]
    except Exception:
        return _raw_read()


def is_pod_wide_caller(bot_id: str | None, network: dict[str, Any] | None = None) -> bool:
    """True when this bot is the pod's sysadmin partner.

    Pod-wide reports only make sense when the caller is the primary bot —
    the operator who installed Evolve. Every other bot stays in its own
    silo. Resolution goes through ``primary_bot.primary_bot_id`` so that
    pods that have renamed the primary bot (e.g. ``evo``) still match.

    Falls back to a case-insensitive ``"evolve"`` match when ``network``
    isn't supplied (covers older callsites that don't thread the dict).
    Also tolerant of ``None`` / non-string ``bot_id`` so a misconfigured
    caller doesn't crash the handler before it can produce an error
    message.
    """
    if not isinstance(bot_id, str):
        return False
    bid = bot_id.strip()
    if not bid:
        return False
    if isinstance(network, dict):
        from primary_bot import primary_bot_id  # type: ignore
        pid = primary_bot_id(network)
        if isinstance(pid, str) and pid:
            return bid.lower() == pid.lower()
    return bid.lower() == "evolve"


def pod_primary_id(network: dict[str, Any] | None) -> str | None:
    """Resolve the pod's primary bot id, or ``None`` when none resolves.

    Thin wrapper over ``primary_bot.primary_bot_id`` that tolerates a missing
    network dict (returns None) and an unimportable analyzer package (partial
    deploy / test context). ``primary_bot_id`` itself honors ``network.primary``,
    the ``role: "primary"`` flag, and the legacy ``"evolve"`` fallback — so on a
    legacy pod this returns ``"evolve"`` and on a renamed pod ``"evo"``.
    """
    if not isinstance(network, dict):
        return None
    try:
        from primary_bot import primary_bot_id  # type: ignore
    except Exception:
        return None
    pid = primary_bot_id(network)
    return pid if isinstance(pid, str) and pid else None


def pod_member_bots(network: dict[str, Any]) -> list[str]:
    """Member bot ids for a pod-wide report, EXCLUDING the primary.

    The primary bot reports pod-wide (it is the operator's sysadmin partner);
    every other bot is a silo listed individually. The primary must therefore be
    dropped from the per-member breakdown, and it is excluded by its RESOLVED id
    — not the hardcoded literal ``"evolve"``. On a legacy pod the resolved
    primary is ``"evolve"`` (byte-identical to the old filter); on a pod whose
    primary was renamed to ``"evo"`` the old ``m != "evolve"`` filter excluded
    nothing, leaking the primary into the member list — this resolves correctly.

    When no primary RESOLVES (a legacy pod that lists ``evolve`` in ``members``
    but carries no ``primary`` field / ``role: "primary"`` / ``bots.evolve``
    entry), we fall back to excluding the literal ``"evolve"`` — byte-identical
    to the old hardcoded filter on those pods, and harmless on an evo pod where
    ``evolve`` is not a member. This mirrors :func:`is_pod_wide_caller`'s own
    no-resolve fallback; the resolved id wins whenever it is available.
    """
    pid = pod_primary_id(network) or "evolve"
    return [
        m
        for m in (network.get("members") or [])
        if isinstance(m, str) and m and m != pid
    ]


def speak(subcommand: str, body: str, role: Role) -> DispatchResult:
    """Wrap a Team_bot_a-style body in the standard DispatchResult envelope.

    The plugin direct-sends ``direct_send_message`` to Telegram and
    injects the stay-silent instruction; the ``system_append`` is the
    legacy LLM-echo path used when direct-send isn't available.
    """
    return DispatchResult(
        subcommand=subcommand,
        role=role,
        mode="speak",
        system_append=(
            f"IMPORTANT: The user has typed `evo {subcommand}`. "
            "Respond ONLY with the following message, verbatim. "
            "Do not add commentary, framing, or any additional text:\n\n"
            + body
        ),
        direct_send_message=body,
    )


def fmt_usd(amount: float) -> str:
    """Format a USD amount with two decimals; preserves sign."""
    return f"${amount:,.2f}"
