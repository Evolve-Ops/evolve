"""provisioning_budget.py — bound a new bot's one-time provisioning spend.

A brand-new bot (``ledger``) spent **$30.26 across its first two days** —
entirely on building and then auditing its own starter apps, before delivering
a single unit of value. Its $10 daily cap tripped on both days, and the $5
warn alert fired, but **nothing halted the spend**: forge app-builds and the
audit pipeline run via admin-side daemons, *outside* the L1 cost-breaker's
heartbeat-disable scope, and operator-confirmed installs are deliberately
exempt from the daily cap. A per-day cap, at any value, can't bound a
multi-day standup.

This module is the missing backstop. It answers one question for a single
*provisioning* dispatch (a forge install build, or a fresh app's first audit):

    "Should this provisioning dispatch be allowed, or has this bot already
     spent enough on standing itself up that we should pause?"

Two independent refusal reasons (either trips):

  * **creation_ceiling** — the bot's cumulative spend during its activation
    window has crossed the one-time provisioning ceiling
    (``better_engine_config.NEW_BOT_PROVISIONING_CEILING_USD`` = $12, a
    product default; per-bot override allowed). This is the cumulative bound
    the daily cap can't express. Only gates while the bot is inside its
    activation window (``BetterEngineConfig.in_provisioning_window``); a
    graduated bot's later gallery installs are steady-state, governed by the
    daily cap, not this ceiling.

  * **daily_breaker** — the bot's L1 *cost* breaker is currently tripped.
    This is the piece that makes the existing daily cap actually GOVERN
    provisioning: when the breaker is tripped (the bot blew its daily hard
    cap), further provisioning dispatches pause too, instead of bleeding on
    past the trip the way they do today.

Design mirrors ``forge_cost_guard``: a pure :func:`decide` (no I/O, unit-
testable) + an I/O :func:`evaluate` wrapper that reads the ledger, the BE
config, and the breaker store, plus signal builders and a best-effort
:func:`emit_signal`. Two call sites consult it — the forge engine
(``run_forge_job``, admin/evolve side) and the app-audit runner
(``run_tier3``, bot side); both pass ``bot_id`` + ``shared_dir``.

**Fail-open is the load-bearing invariant.** A transient read failure
(ledger unreadable, BE config corrupt, breaker store import error) must
NEVER block a dispatch — provisioning continues, the existing daily cap +
forge Guard A/B still apply, and the failure is logged. The two failure
modes this module is built to survive are (i) the ceiling never enforces
(bleed continues) and (ii) the ceiling enforces but bricks a bot
half-provisioned with no signal. (i) is guarded by enforcing at the actual
spend chokepoints with real accumulated spend; (ii) by gating at the app/job
boundary (complete-current-then-stop) and always emitting an observable
Signal on refusal.

Finding: internal/finding-new-bot-activation-cost-2026-06-12.md (decision B).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_log = logging.getLogger(__name__)

PRODUCER = "provisioning_budget"
SIGNAL_TYPE = "provisioning_budget_exceeded"


# ── Spend measurement ─────────────────────────────────────────────────────────


def _turn_cost(turn: dict) -> float:
    """Recorded cost when non-zero; otherwise the token-based estimate.

    Mirrors ``spend_alert._turn_cost`` / ``usage_analytics.compute_summary``'s
    per-turn rule so this backstop's totals agree with the Usage page and the
    spend alerter to the penny.
    """
    try:
        from usage_analytics import _estimate_turn_cost  # type: ignore[import]
    except Exception:
        _estimate_turn_cost = lambda _t: 0.0  # type: ignore[assignment]  # noqa: E731
    recorded = float(turn.get("cost") or 0)
    return recorded if recorded else _estimate_turn_cost(turn)


def _parse_turn_ts(turn: dict) -> datetime | None:
    ts = (turn.get("ts") or "").strip()
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def window_spend_usd(
    bot_id: str,
    *,
    created_at: datetime,
    now: datetime,
    window_days: int,
    network_path: str | None = None,
) -> float | None:
    """Cumulative spend for ``bot_id`` since ``created_at`` (the standup spend).

    Returns the summed per-turn cost of every turn at-or-after ``created_at``,
    read from the same live turn JSONL the Usage page + spend alerter use
    (``usage_analytics.load_turns``). Returns ``None`` on a read failure so
    the caller fails open (never block a dispatch because the ledger is
    momentarily unreadable).

    We sum **all** turns in the window, not just forge/audit-tagged ones, on
    purpose: during a new bot's activation window its spend is dominated by
    provisioning (the ``ledger`` case was 100% — zero user chat), and counting
    everything is the conservative, fail-safe direction for a spend backstop
    that also dodges fragile source-tag filtering. User chat is never *gated*
    by this module (only provisioning dispatches are); it just counts toward
    the cumulative standup total.

    The lookback is bounded to the activation window (``window_days`` + 1, to
    cover the partial first/last day) — there is no reason to read older
    JSONL, and the ceiling only gates inside that window anyway.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    span_days = (now.date() - created_at.date()).days + 1
    days = max(1, min(span_days, window_days + 1))

    try:
        from usage_analytics import load_turns  # type: ignore[import]
    except Exception as exc:
        _log.debug("provisioning_budget: usage_analytics import failed: %s", exc)
        return None
    try:
        turns = load_turns(
            bot_id,
            days=days,
            end_date=now,
            network_path=network_path,
        )
    except Exception as exc:
        _log.debug("provisioning_budget: load_turns(%s) raised: %s", bot_id, exc)
        return None

    total = 0.0
    for t in turns:
        dt = _parse_turn_ts(t)
        if dt is None or dt < created_at:
            continue
        total += _turn_cost(t)
    return round(total, 6)


# ── Breaker state ─────────────────────────────────────────────────────────────


def cost_breaker_tripped(shared_dir: Path, bot_id: str) -> bool:
    """True iff a live (non-expired) L1 ``cost`` breaker is tripped for the bot.

    Mirrors ``spend_alert._breaker_already_tripped``. Fail-open: any import /
    read failure returns ``False`` (treat as not-tripped) so a mangled breaker
    file never blocks provisioning — the ceiling check is the primary bound and
    a corrupt breaker file is its own (separately surfaced) problem.
    """
    try:
        from breakers import store as _bstore  # type: ignore[import]
    except Exception as exc:
        _log.debug(
            "provisioning_budget: breakers.store import failed (%s); "
            "treating as not-tripped (fail-open)", exc,
        )
        return False
    try:
        rec = _bstore.read_trip(shared_dir, bot_id, "cost")
    except Exception as exc:
        _log.debug(
            "provisioning_budget: read_trip(%s) raised (%s); fail-open", bot_id, exc,
        )
        return False
    if rec is None:
        return False
    try:
        return not _bstore.is_expired(rec)
    except Exception:
        return False


# ── Pure decision ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProvisioningBudgetDecision:
    """Outcome of a provisioning-budget check for one dispatch.

    ``allowed=True`` ⇒ the caller proceeds normally (``failed_check`` empty,
    ``signal_payload`` None). ``allowed=False`` ⇒ the caller MUST NOT spend;
    ``reason`` is a one-line human summary and ``signal_payload`` is ready for
    :func:`emit_signal`. The numeric fields are populated whenever they could
    be computed (so an allowed decision can still log headroom).
    """

    allowed: bool
    failed_check: str = ""          # "creation_ceiling" | "daily_breaker" | ""
    reason: str = ""
    ceiling_usd: float = 0.0
    spent_usd: float | None = None
    remaining_usd: float | None = None
    in_window: bool = False
    breaker_tripped: bool = False
    signal_payload: Optional[dict] = None


def decide(
    *,
    spent_usd: float | None,
    ceiling_usd: float,
    in_window: bool,
    breaker_tripped: bool,
) -> tuple[bool, str, str]:
    """Pure refusal logic. Returns ``(allowed, failed_check, reason)``.

    Order of refusal:
      1. **creation_ceiling** (the headline bound) — only when the bot is in
         its activation window AND cumulative spend is known AND has reached
         the ceiling. A ``None`` ``spent_usd`` (ledger unreadable) skips this
         check (fail-open for the ceiling) — the breaker check below still runs.
      2. **daily_breaker** — the L1 cost breaker is tripped. Applies regardless
         of window: a tripped daily cost cap should pause provisioning on any
         bot (this is "the daily cap governs provisioning").

    Allowed otherwise.
    """
    if in_window and spent_usd is not None and ceiling_usd > 0 and spent_usd >= ceiling_usd:
        reason = (
            f"provisioning paused — cumulative standup spend ${spent_usd:.2f} "
            f"has reached the ${ceiling_usd:.2f} one-time provisioning ceiling"
        )
        return False, "creation_ceiling", reason
    if breaker_tripped:
        reason = (
            "provisioning paused — the bot's daily cost breaker is tripped "
            "(daily hard cap exceeded); provisioning waits for the breaker "
            "to reset"
        )
        return False, "daily_breaker", reason
    return True, "", ""


# ── I/O wrapper ───────────────────────────────────────────────────────────────


def evaluate(
    bot_id: str,
    shared_dir: Path,
    *,
    kind: str,
    job_id: str = "",
    remaining_label: str = "",
    now: datetime | None = None,
    network_path: str | None = None,
) -> ProvisioningBudgetDecision:
    """Evaluate the provisioning budget for one dispatch on ``bot_id``.

    Reads the ceiling + activation window + creation stamp from
    ``better_engine_config``, the cumulative window spend from the turn ledger,
    and the L1 cost-breaker state from the breaker store, then applies
    :func:`decide`.

    ``kind`` is a short label for the dispatch ("install build", "first audit")
    used in the refusal Signal. ``job_id`` / ``remaining_label`` are optional
    forensics for the Signal (the forge call site passes the failed job id and
    a "N of M apps remain" label; the audit side leaves them empty).

    **Fail-open**: any failure resolving config / window returns
    ``allowed=True`` — a backstop that can't read its inputs must not become a
    denial-of-service on provisioning. The decision still records what it
    could.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        from better_engine_config import (  # type: ignore[import]
            NEW_BOT_GRADUATION_DAYS,
            load as _load_be,
        )
        be = _load_be(shared_dir)
    except Exception as exc:
        _log.debug(
            "provisioning_budget: BE config load failed (%s); fail-open allow", exc,
        )
        return ProvisioningBudgetDecision(allowed=True)

    ceiling = be.provisioning_ceiling_usd(bot_id)
    in_window = be.in_provisioning_window(bot_id, now=now)
    created_at = be.bot_created_at(bot_id)

    breaker_tripped = cost_breaker_tripped(shared_dir, bot_id)

    spent: float | None = None
    if in_window and created_at is not None:
        spent = window_spend_usd(
            bot_id,
            created_at=created_at,
            now=now,
            window_days=NEW_BOT_GRADUATION_DAYS,
            network_path=network_path,
        )

    allowed, failed_check, reason = decide(
        spent_usd=spent,
        ceiling_usd=ceiling,
        in_window=in_window,
        breaker_tripped=breaker_tripped,
    )

    remaining_usd = (
        round(max(0.0, ceiling - spent), 6) if spent is not None else None
    )

    if allowed:
        return ProvisioningBudgetDecision(
            allowed=True,
            ceiling_usd=ceiling,
            spent_usd=spent,
            remaining_usd=remaining_usd,
            in_window=in_window,
            breaker_tripped=breaker_tripped,
        )

    payload = build_capped_signal(
        bot_id=bot_id,
        kind=kind,
        job_id=job_id,
        failed_check=failed_check,
        reason=reason,
        ceiling_usd=ceiling,
        spent_usd=spent,
        remaining_label=remaining_label,
    )
    return ProvisioningBudgetDecision(
        allowed=False,
        failed_check=failed_check,
        reason=reason,
        ceiling_usd=ceiling,
        spent_usd=spent,
        remaining_usd=remaining_usd,
        in_window=in_window,
        breaker_tripped=breaker_tripped,
        signal_payload=payload,
    )


# ── Signal payload + emission ─────────────────────────────────────────────────


def _make_signature(scope: str) -> str:
    """Stable dedup signature; degrades to a deterministic local string.

    Mirrors the trampoline in forge_cost_guard — analyzer's ``schema.signal``
    when importable, else a deterministic fallback so dedup still works in a
    broken-install / test environment.
    """
    try:
        import importlib
        return importlib.import_module("schema.signal").make_signature(
            PRODUCER, SIGNAL_TYPE, scope,
        )
    except Exception:
        return f"{PRODUCER}:{SIGNAL_TYPE}:{scope}"


def build_capped_signal(
    *,
    bot_id: str,
    kind: str,
    job_id: str,
    failed_check: str,
    reason: str,
    ceiling_usd: float,
    spent_usd: float | None,
    remaining_label: str = "",
) -> dict:
    """Signal payload for a provisioning-budget refusal.

    Signature is per-(bot, failed_check) so a ceiling refusal and a
    breaker refusal raise distinct Signals (different operator response
    paths), while repeated refusals under the same check on consecutive
    dispatches dedup into one firing whose body carries the latest
    cumulative figure + what remains unprovisioned. The Signal is the
    observable proof that provisioning was *capped* (not silently
    half-built) — the half-provisioned-safety requirement.
    """
    spent_str = f"${spent_usd:.2f}" if spent_usd is not None else "(unknown)"
    if failed_check == "creation_ceiling":
        title = (
            f"{bot_id}: provisioning paused — {spent_str} reached "
            f"${ceiling_usd:.2f} ceiling"
        )
        what = (
            "This bot's cumulative spend while standing itself up (forge "
            "app-builds + first audits) reached the one-time provisioning "
            "ceiling. Further provisioning dispatches are paused so the "
            "standup can't run away the way it did on `ledger` ($30 in two "
            "days). No money was spent on the refused dispatch — this is "
            "preventive. Any apps already built are complete; what remains "
            "is listed below and can be finished by raising the ceiling and "
            "retrying, or it resumes once the bot graduates its activation "
            "window."
        )
        knob = (
            f"`bots.{bot_id}.budget.provisioning_ceiling_usd` in "
            f"better-engine-config.json"
        )
    else:  # daily_breaker
        title = f"{bot_id}: provisioning paused — daily cost breaker tripped"
        what = (
            "The bot's L1 cost breaker is tripped (it exceeded its daily "
            "hard cap), so provisioning dispatches (forge builds + first "
            "audits) pause too — instead of bleeding past the trip the way "
            "they did before this backstop. They resume when the breaker "
            "resets (24h auto, or `evolve-admin breaker reset`)."
        )
        knob = (
            f"reset the breaker (`evolve-admin breaker reset {bot_id} cost`) "
            f"or raise `bots.{bot_id}.budget.per_bot_daily_hard_usd`"
        )

    remaining_line = f"\nRemaining: {remaining_label}" if remaining_label else ""
    body = (
        f"{reason}.\n"
        f"  dispatch:   {kind}" + (f" (job {job_id})" if job_id else "") + "\n"
        f"  spent:      {spent_str}\n"
        f"  ceiling:    ${ceiling_usd:.2f}\n"
        f"  failed:     {failed_check}"
        f"{remaining_line}\n\n"
        f"To allow more provisioning: {knob}."
    )
    fix_steps = (
        f"1. Open Cost → Bot detail for `{bot_id}` to see the standup spend\n"
        f"2. Decide whether the remaining provisioning is worth more spend\n"
        f"3. If yes: {knob}, then retry the paused builds from the Forge "
        f"Jobs page\n"
        f"4. If no: leave it — the bot keeps the apps it already built and "
        f"the rest stay paused (no further spend)"
    )
    return {
        "signature": _make_signature(f"{bot_id}/{failed_check}"),
        "producer": PRODUCER,
        "type": SIGNAL_TYPE,
        "flavor": "maintenance",
        "severity": "warn",
        "scope": "bot",
        "bot_id": bot_id,
        "category": "cost",
        "title": title,
        "body": body,
        "details": {
            "bot_id": bot_id,
            "job_id": job_id,
            "kind": kind,
            "failed_check": failed_check,
            "ceiling_usd": round(ceiling_usd, 4),
            "spent_usd": round(spent_usd, 4) if spent_usd is not None else None,
            "remaining_label": remaining_label,
            "vector": "cost",
            "magnitude": 2,
            "severity_active": True,
            "what_it_means": what,
            "fix_steps": fix_steps,
        },
    }


def emit_signal(shared_dir, payload: dict) -> bool:
    """Best-effort dispatch of ``payload`` to ``signals.store.observe()``.

    Returns True if written, False otherwise. Failures here MUST NOT
    propagate — the refusal is the load-bearing safety; the Signal is the
    alert. Pattern matches ``forge_cost_guard.emit_signal``.
    """
    try:
        import importlib
        signals_store = importlib.import_module("signals.store")
    except Exception as exc:
        _log.debug("provisioning_budget: signal store unavailable (%s)", exc)
        return False
    try:
        signals_store.observe(shared_dir, **payload)
        return True
    except Exception as exc:
        _log.warning("provisioning_budget: signal write failed: %s", exc)
        return False
