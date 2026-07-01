"""investigation.peer_baseline — Same-role-class distribution lookup.

Spec: docs/spec-smarter-generators-2026-05-28.md §"Gap E — no cross-bot
peer comparison".

A single-bot threshold can fire on a bot that's *normal for its role*
(an auditor bot's per-call cost legitimately differs from a
conversational bot's). Comparing against same-role peers is a stronger
signal than any single-bot baseline.

Role assignment:
  * Preferred: explicit ``bots.<id>.role`` in network.json.
  * Fallback: inferred from primary model + cadence shape — Haiku
    primary + frequent heartbeats → "auditor"; Sonnet primary → "primary".
  * Last resort: "unknown" (every bot lumped together).

The MVP returns the distribution over peers + the bot's own value. The
caller decides what "outlier" means for its specific metric.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class PeerBaselineResult:
    """Peer distribution for a metric.

    ``role`` is what the bot was classified as. ``peer_values`` is the
    metric value for each peer (excluding the bot itself). ``bot_value``
    is the bot's own value when supplied — None when the caller only
    wants the distribution.

    Statistics are computed in helper methods so generators can ask
    for whichever cut is meaningful (median, p90, ratio to median).
    """

    bot_id: str
    metric_name: str
    role: str
    peer_values: list[float] = field(default_factory=list)
    bot_value: float | None = None

    @property
    def peer_count(self) -> int:
        return len(self.peer_values)

    @property
    def peer_median(self) -> float | None:
        if not self.peer_values:
            return None
        sorted_vals = sorted(self.peer_values)
        n = len(sorted_vals)
        mid = n // 2
        if n % 2 == 1:
            return sorted_vals[mid]
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2

    @property
    def peer_p90(self) -> float | None:
        if not self.peer_values:
            return None
        sorted_vals = sorted(self.peer_values)
        idx = max(0, int(0.9 * len(sorted_vals)) - 1)
        return sorted_vals[idx]

    @property
    def ratio_to_median(self) -> float | None:
        if self.bot_value is None or not self.peer_median:
            return None
        if self.peer_median <= 0:
            return None
        return self.bot_value / self.peer_median


# ─────────────────────────────────────────────────────────────────────────────
# Role inference
# ─────────────────────────────────────────────────────────────────────────────


def _role_from_config(bot_id: str, config: dict) -> str | None:
    """Read explicit ``bots.<id>.role`` from network.json. None when unset."""
    bots = config.get("bots") or {}
    spec = bots.get(bot_id) or {}
    role = spec.get("role")
    if isinstance(role, str) and role:
        return role
    return None


def _role_from_inference(bot_id: str, oc_json_reader: Callable[[str], dict | None] | None) -> str:
    """Heuristic role inference from primary model + cadence.

    Tight, conservative rules — when in doubt, return "unknown" so the
    caller's distribution lumps everyone together rather than getting
    mis-classified peers.

    * primary model token contains "haiku" → "auditor"
    * primary model token contains "sonnet" or "opus" → "primary"
    * otherwise → "unknown"
    """
    if oc_json_reader is None:
        return "unknown"
    try:
        oc = oc_json_reader(bot_id) or {}
    except Exception:
        return "unknown"
    if not isinstance(oc, dict):
        return "unknown"
    primary = (
        ((oc.get("agents") or {}).get("defaults") or {})
        .get("model", {}) or {}
    ).get("primary") or ""
    if not isinstance(primary, str):
        return "unknown"
    lc = primary.lower()
    if "haiku" in lc:
        return "auditor"
    if "sonnet" in lc or "opus" in lc:
        return "primary"
    return "unknown"


def role_for_bot(
    bot_id: str,
    config: dict,
    *,
    oc_json_reader: Callable[[str], dict | None] | None = None,
) -> str:
    """Resolve role: explicit > inferred > "unknown"."""
    explicit = _role_from_config(bot_id, config)
    if explicit is not None:
        return explicit
    return _role_from_inference(bot_id, oc_json_reader)


# ─────────────────────────────────────────────────────────────────────────────
# Baseline query
# ─────────────────────────────────────────────────────────────────────────────


def peer_baseline(
    bot_id: str,
    metric_name: str,
    *,
    config: dict,
    metric_reader: Callable[[str], float | None],
    bot_value: float | None = None,
    oc_json_reader: Callable[[str], dict | None] | None = None,
) -> PeerBaselineResult:
    """Compute distribution of ``metric_name`` across same-role peers.

    ``metric_reader``: callable(bot_id) → metric value or None. The
    caller supplies this so the toolkit doesn't have to know about
    every metric in the system. Bots that return None are dropped from
    the distribution (no data for them in the window).

    ``bot_value``: the subject bot's own value, if the caller knows it
    out-of-band. When None, ``metric_reader(bot_id)`` is used (most
    common case — read once for everyone). When non-None, the bot is
    still excluded from ``peer_values``.

    Returns a result with peer_values from all same-role bots except
    ``bot_id``. Empty peer_values is a valid result — the helper
    methods all return None when there's no data.
    """
    members = config.get("members") or []
    primary = config.get("primary")
    all_bots: list[str] = list(members)
    if primary and primary not in all_bots:
        all_bots.append(primary)

    target_role = role_for_bot(bot_id, config, oc_json_reader=oc_json_reader)

    peer_values: list[float] = []
    for peer in all_bots:
        if peer == bot_id:
            continue
        peer_role = role_for_bot(peer, config, oc_json_reader=oc_json_reader)
        if peer_role != target_role:
            continue
        try:
            v = metric_reader(peer)
        except Exception:
            continue
        if v is None:
            continue
        try:
            peer_values.append(float(v))
        except (TypeError, ValueError):
            continue

    if bot_value is None:
        try:
            bot_value = metric_reader(bot_id)
        except Exception:
            bot_value = None
        if bot_value is not None:
            try:
                bot_value = float(bot_value)
            except (TypeError, ValueError):
                bot_value = None

    return PeerBaselineResult(
        bot_id=bot_id,
        metric_name=metric_name,
        role=target_role,
        peer_values=peer_values,
        bot_value=bot_value,
    )
