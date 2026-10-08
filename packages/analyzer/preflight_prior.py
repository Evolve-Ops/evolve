#!/usr/bin/env python3
"""preflight_prior.py — learn each bot's routing prior from its own turns.

Decision: ``internal/decision-evolve-overhead-2026-09-07.md`` D-OH2/D-OH4.

Why this exists
===============

Evolve used to answer "which rung should this bot's next user turn run
on?" with a model call, per turn, at the user's latency: a haiku
classifier in front of the turn, a tier classifier behind it, a judge
beside it. Two days of turns (decision §2) priced the whole benefit of
that machinery at ~$3-4/day pod-wide, all of it from moving heartbeats
and cron to the cheap rung — which the trigger already tells you, for
free. And one of those classifiers recursed into ~3,000 calls in an
afternoon.

The question those calls were asking is a question about a bot, not
about a message, and it is better answered overnight than in front of a
person. This job reads the last 14 days of a bot's OWN turns — which the
analyzer already walks for cost rollups — and writes the answer to
``network.json`` where the plugin's preflight router reads it as a
cheap, cached, local file read:

    bots.<id>.preflight.bot_prior      "fast" | "standard" | "power"
    bots.<id>.preflight.prior_evidence { turns, confidence, surfaces,
                                         hours, computed_at, window_days }

Two weeks of a bot's actual traffic beats a haiku guess at one message,
costs nothing, sits nowhere near the user, and cannot recurse into
itself.

What "the prior" actually measures
==================================

The rung the bot's **user turns already run on**. Not what a model
thinks they deserve — what they got, and were not corrected away from.
That framing is deliberate and it is what makes this safe to ship under
the standing rule (*nothing changes which model answers*): a prior
computed from what already happens, applied to what happens next, is
close to a no-op by construction. It earns its keep on the bots where
the answer is stable and lets Evolve stop asking.

Refusal is the default. A bot gets NO prior — and stays on its primary,
exactly as today — when any of these hold:

  * fewer than ``--min-turns`` user turns in the window (default 50);
  * the modal rung's share is below ``--min-confidence`` (default 0.8);
  * every turn's model is unrecognised (no rung config for this bot).

Per-surface entries follow the same rules with their own, lower turn
floor: a surface with thin evidence simply gets no entry and falls back
to the bot-wide value.

Hours are recorded as evidence, not as a routing input. The rule
consumes trigger kind and surface today (see
``packages/plugin/src/observer/routingRule.ts``); the hour histogram is
written so a later rule can use it without re-deriving a fortnight of
turns. Recording a signal is not the same as routing on it, and the
distinction is the whole reason this file can claim the standing rule.

Writing is opt-in, twice
========================

``--apply`` is required, AND ``network.json::cascade.prior.enabled``
must be true. Deploying this code changes nothing on a live pod until an
operator flips that switch — the fail-closed posture D-OH5 asks of every
Evolve switch. Without ``--apply`` the job computes and reports; with
``--apply`` but the switch off, it says so and writes nothing.

Idempotent: the same window over the same turns produces the same
document, and an unchanged prior is not rewritten (``computed_at`` alone
would otherwise churn network.json nightly for no reason).

Usage:
    python3 preflight_prior.py --shared-dir /Users/Shared/evolve
    python3 preflight_prior.py --shared-dir ... --json
    python3 preflight_prior.py --shared-dir ... --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

#: Days of turns the prior is computed from. Two weeks is long enough to
#: cross a couple of work rhythms and short enough that a bot whose job
#: changed last Tuesday is not pinned to what it used to do.
DEFAULT_WINDOW_DAYS = 14

#: Minimum user turns before a bot may have a prior at all.
DEFAULT_MIN_TURNS = 50

#: Minimum share of user turns on the modal rung. Mirrors
#: ``routingRule.DEFAULT_PRIOR_MIN_CONFIDENCE`` — the plugin refuses
#: anything below this too, so the two ends agree even if one is
#: mis-configured.
DEFAULT_MIN_CONFIDENCE = 0.8

#: Minimum user turns on ONE surface before that surface gets its own entry.
DEFAULT_MIN_SURFACE_TURNS = 20

#: The sources that count as a user turn. Everything else — heartbeat,
#: cron, subagent, and Evolve's own summariser/classifier calls — is
#: routed by the trigger rule and contributes nothing to the prior.
USER_SOURCES = frozenset({"user", "human"})

#: Roles the prior may name. ``max`` is pull-only (never reachable by a
#: rule, a prior or a classifier), so a bot whose turns ran on the max
#: rung yields no prior rather than a prior that could never be honoured.
PRIOR_ROLES = ("fast", "standard", "power")


# ── Turn reading ─────────────────────────────────────────────────────────────


def turns_paths(shared_dir: Path, bot_id: str, window_days: int,
                today: date | None = None) -> list[Path]:
    """The turns files covering the window, newest day last.

    Missing days are normal (a bot that was quiet, or not yet deployed)
    and are simply absent from the returned list.
    """
    end = today or datetime.now(timezone.utc).date()
    out: list[Path] = []
    for offset in range(window_days - 1, -1, -1):
        d = end - timedelta(days=offset)
        p = shared_dir / bot_id / "turns" / f"turns-{d.isoformat()}.jsonl"
        if p.exists():
            out.append(p)
    return out


def iter_user_turns(paths: list[Path]) -> list[dict[str, Any]]:
    """Every user turn in the given files.

    Malformed lines are skipped, not fatal: a turns file is appended to
    by a live gateway, so a torn final line is an ordinary event and not
    a reason to refuse a fortnight of evidence.
    """
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            if str(rec.get("source") or "").lower() not in USER_SOURCES:
                continue
            rows.append(rec)
    return rows


# ── Model → role ─────────────────────────────────────────────────────────────


def build_role_index(models_block: dict[str, Any] | None) -> dict[str, str]:
    """Map each configured model id (lowercased) → its role.

    Reads the ``rungs`` / ``roles`` shape documented in
    ``docs/model-roles.md``. A model listed in more than one rung takes
    the first role that claims it in ``PRIOR_ROLES`` order, which is the
    cheap end first — the same conservative tiebreak
    ``ModelRouter.getRoleForModel`` applies via its role-preference rank.
    """
    block = models_block or {}
    rungs = {
        str(r.get("id")): [m for m in (r.get("models") or []) if isinstance(m, str)]
        for r in (block.get("rungs") or [])
        if isinstance(r, dict) and r.get("id")
    }
    roles = block.get("roles") or {}
    index: dict[str, str] = {}
    for role in PRIOR_ROLES:
        slug = roles.get(role)
        if not isinstance(slug, str):
            continue
        for model in rungs.get(slug, []):
            key = model.lower()
            index.setdefault(key, role)
    return index


def role_for_model(model: str | None, index: dict[str, str]) -> str | None:
    """Resolve one turn's model to a role.

    Exact match first, then the suffix/containment fallback
    ``ModelRouter.getRoleForModel`` uses — a turns file records whatever
    string the provider echoed back, which is not always the string the
    catalog lists (``claude-sonnet-4-6`` vs ``anthropic/claude-sonnet-4-6``).
    Longest match wins so a rung listing both a family and a specific
    build resolves to the specific one.
    """
    if not model:
        return None
    lower = model.lower()
    exact = index.get(lower)
    if exact:
        return exact
    best: tuple[int, str] | None = None
    for candidate, role in index.items():
        if len(candidate) < 6:
            continue
        if lower.endswith(candidate) or candidate.endswith(lower) or candidate in lower:
            if best is None or len(candidate) > best[0]:
                best = (len(candidate), role)
    return best[1] if best else None


def models_block_for_bot(network: dict[str, Any], bot_id: str) -> dict[str, Any]:
    """The bot's effective models block: pod base, bot override on top.

    A shallow per-key overlay, matching what the plugin's
    ``mergeModelCatalog`` produces for the two keys this job reads.
    A bot with no override inherits the pod's rungs, which is the
    common case.
    """
    base = network.get("models") or {}
    override = ((network.get("bots") or {}).get(bot_id) or {}).get("models") or {}
    merged = dict(base)
    for key in ("rungs", "roles"):
        if override.get(key):
            merged[key] = override[key]
    return merged


# ── The prior ────────────────────────────────────────────────────────────────


def utc_hour(ts: Any) -> str | None:
    """The UTC hour a turn was written at, as a string, or None.

    Read off the timestamp in UTC and never localised: turn files are
    UTC-named and UTC-stamped, and a reader that applies local time
    silently shifts a bot's evening into someone else's morning.

    Returns None for a timestamp that will not parse — a torn line or a
    shape from an older writer. The caller drops it from the histogram
    rather than guessing an hour; an unparseable timestamp is evidence
    we do not have, not an hour of zero.
    """
    text = ts if isinstance(ts, str) else ""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(parsed.astimezone(timezone.utc).hour)


def _modal(counter: Counter[str], total: int, min_confidence: float
           ) -> tuple[str, float] | None:
    """The modal role and its share, or None when nothing clears the bar."""
    if total <= 0:
        return None
    role, count = max(counter.items(), key=lambda kv: (kv[1], kv[0]))
    share = count / total
    if share < min_confidence:
        return None
    return role, share


def compute_prior(
    rows: list[dict[str, Any]],
    role_index: dict[str, str],
    *,
    min_turns: int = DEFAULT_MIN_TURNS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    min_surface_turns: int = DEFAULT_MIN_SURFACE_TURNS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    computed_at: str | None = None,
) -> dict[str, Any]:
    """Compute one bot's prior from its user turns.

    Returns a dict that always carries ``turns``, ``resolved`` and
    ``refused`` (the reason, or None). ``bot_prior`` is present only
    when the bot earned one — absence is the answer, not an omission,
    and the caller writes nothing in that case.
    """
    overall: Counter[str] = Counter()
    per_surface: dict[str, Counter[str]] = {}
    hours: Counter[str] = Counter()
    unresolved = 0

    for rec in rows:
        role = role_for_model(rec.get("model"), role_index)
        if role is None:
            unresolved += 1
            continue
        overall[role] += 1
        surface = str(rec.get("channel") or "unknown").strip().lower() or "unknown"
        per_surface.setdefault(surface, Counter())[role] += 1
        hour = utc_hour(rec.get("ts"))
        if hour is not None:
            hours[hour] += 1

    resolved = sum(overall.values())
    result: dict[str, Any] = {
        "turns": len(rows),
        "resolved": resolved,
        "unresolved": unresolved,
        "refused": None,
    }

    if resolved < min_turns:
        result["refused"] = f"too few resolved user turns ({resolved} < {min_turns})"
        return result

    modal = _modal(overall, resolved, min_confidence)
    if modal is None:
        top = max(overall.items(), key=lambda kv: kv[1]) if overall else ("none", 0)
        result["refused"] = (
            f"no rung clears the confidence bar "
            f"(best {top[0]} at {top[1] / resolved:.2f} < {min_confidence:.2f})"
        )
        return result

    role, confidence = modal
    surfaces: dict[str, str] = {}
    for surface, counter in sorted(per_surface.items()):
        total = sum(counter.values())
        if total < min_surface_turns:
            continue
        s_modal = _modal(counter, total, min_confidence)
        # Only record a surface that DISAGREES with the bot-wide value —
        # an entry that repeats the fallback is noise in the evidence and
        # one more thing to keep in sync.
        if s_modal and s_modal[0] != role:
            surfaces[surface] = s_modal[0]

    result["bot_prior"] = role
    result["prior_evidence"] = {
        "turns": resolved,
        "confidence": round(confidence, 4),
        "window_days": window_days,
        "surfaces": surfaces,
        # Evidence only — the rule does not route on the hour. See the
        # module docstring.
        "hours": dict(sorted(hours.items(), key=lambda kv: int(kv[0]))),
        "computed_at": computed_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    }
    return result


# ── network.json ─────────────────────────────────────────────────────────────


def prior_writes_enabled(network: dict[str, Any]) -> bool:
    """Is the operator's prior-write switch on? Default OFF, fail closed."""
    return ((network.get("cascade") or {}).get("prior") or {}).get("enabled") is True


def _unchanged(existing: dict[str, Any] | None, role: str,
               evidence: dict[str, Any]) -> bool:
    """True when the stored prior already says exactly this.

    ``computed_at`` is excluded from the comparison: it changes every
    run by construction, and letting it drive a write would churn
    network.json nightly on a pod where nothing moved.
    """
    if not isinstance(existing, dict):
        return False
    if existing.get("bot_prior") != role:
        return False
    stored = dict(existing.get("prior_evidence") or {})
    fresh = dict(evidence)
    stored.pop("computed_at", None)
    fresh.pop("computed_at", None)
    return stored == fresh


def apply_prior(network_path: Path, bot_id: str, role: str,
                evidence: dict[str, Any]) -> bool:
    """Write one bot's prior into network.json. Returns True if it wrote."""
    from evolve_config import _patch_network_json

    try:
        network = json.loads(network_path.read_text())
    except (OSError, ValueError):
        network = {}
    existing = ((network.get("bots") or {}).get(bot_id) or {}).get("preflight")
    if _unchanged(existing, role, evidence):
        return False

    preflight = dict(existing or {})
    preflight["bot_prior"] = role
    preflight["prior_evidence"] = evidence
    _patch_network_json(network_path, ["bots", bot_id, "preflight"], preflight)
    return True


# ── CLI ──────────────────────────────────────────────────────────────────────


def _bot_ids(network: dict[str, Any]) -> list[str]:
    bots = network.get("bots")
    return sorted(bots.keys()) if isinstance(bots, dict) else []


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Learn each bot's routing prior from its own turns.")
    # The default shared dir is platform-derived (macOS vs the Linux
    # port), never a literal — see platform_profile.get_profile().
    from platform_profile import get_profile

    ap.add_argument("--shared-dir", default=get_profile().shared_dir_default)
    ap.add_argument("--network", default=None,
                    help="network.json path (default: <shared-dir>/network.json)")
    ap.add_argument("--bot", action="append", default=None,
                    help="limit to this bot (repeatable)")
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--min-turns", type=int, default=DEFAULT_MIN_TURNS)
    ap.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    ap.add_argument("--min-surface-turns", type=int, default=DEFAULT_MIN_SURFACE_TURNS)
    ap.add_argument("--apply", action="store_true",
                    help="write priors into network.json (also needs "
                         "cascade.prior.enabled)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of lines")
    args = ap.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    network_path = Path(args.network) if args.network else shared_dir / "network.json"
    try:
        network = json.loads(network_path.read_text())
    except (OSError, ValueError) as exc:
        print(f"preflight-prior: cannot read {network_path}: {exc}", file=sys.stderr)
        return 2

    writes_enabled = prior_writes_enabled(network)
    bot_ids = args.bot or _bot_ids(network)
    report: list[dict[str, Any]] = []

    for bot_id in bot_ids:
        paths = turns_paths(shared_dir, bot_id, args.window_days)
        rows = iter_user_turns(paths)
        index = build_role_index(models_block_for_bot(network, bot_id))
        prior = compute_prior(
            rows, index,
            min_turns=args.min_turns,
            min_confidence=args.min_confidence,
            min_surface_turns=args.min_surface_turns,
            window_days=args.window_days,
        )
        prior["bot_id"] = bot_id
        prior["days_with_turns"] = len(paths)

        wrote = False
        if "bot_prior" in prior and args.apply and writes_enabled:
            wrote = apply_prior(
                network_path, bot_id, prior["bot_prior"], prior["prior_evidence"],
            )
        prior["written"] = wrote
        report.append(prior)

        # One line per bot, whatever the outcome — a refusal is a result,
        # and a silent bot is indistinguishable from a bot the job never
        # reached.
        if not args.json:
            if "bot_prior" in prior:
                ev = prior["prior_evidence"]
                surfaces = ", ".join(f"{k}={v}" for k, v in ev["surfaces"].items())
                print(
                    f"preflight-prior: {bot_id} → {prior['bot_prior']} "
                    f"(confidence {ev['confidence']:.2f}, {ev['turns']} user turns "
                    f"over {ev['window_days']}d"
                    + (f", surfaces: {surfaces}" if surfaces else "")
                    + ") "
                    + ("written" if wrote
                       else "unchanged" if args.apply and writes_enabled
                       else "not written (--apply + cascade.prior.enabled required)")
                )
            else:
                print(
                    f"preflight-prior: {bot_id} → no prior "
                    f"({prior['refused']}) — stays on its primary"
                )

    if args.json:
        print(json.dumps(report, indent=2))
    elif args.apply and not writes_enabled:
        print(
            "preflight-prior: --apply given but cascade.prior.enabled is not true "
            "in network.json — nothing was written. This switch fails closed by "
            "design (D-OH5)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(run())
