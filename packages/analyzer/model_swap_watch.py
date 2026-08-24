"""model_swap_watch — Signal producer for behavior that diverges after a
model swap.

Motivated by the 2026-08-14 group-chat silence incident (design:
``internal/design-model-swap-behavior-guard-2026-08-19.md``). A fleet-wide bulk
tier update moved six bots' Workhorse rung to a new model. The admin write
path checked that the model *string* landed and reported success. Nothing
checked whether the bots still BEHAVED correctly, so a bot sitting in four
``requireMention: false`` Slack channels — where OpenClaw runs a full agent
turn on every message and the model must emit the bare ``NO_REPLY`` sentinel
to stay quiet — began posting its should-I-reply deliberation into those
channels instead. Days of visible noise before anyone connected it to the
swap.

What it measures
================

The **terse-reply rate**: the fraction of a bot's turns whose
``output_tokens`` are at or below :data:`TERSE_MAX_OUTPUT_TOKENS`. A silent
turn (the ``NO_REPLY`` sentinel) is a handful of output tokens; a leaked
deliberation is dozens to hundreds. The rate is a *proxy* for "how often is
this bot choosing to stay quiet" — it does not read message text and cannot
distinguish a sentinel from any other terse reply. That is deliberate: the
proxy needs no new instrumentation at all. ``output_tokens``,
``model_selected`` and ``ts`` are already on every ``turn_annotation`` record
in ``{shared_dir}/annotations/<bot_id>/<date>.jsonl``.

For each swap in the model-swap ledger (``model_swap_ledger``), the monitor
splits that bot's annotations at the swap instant and compares two arms:

  * **target** — turns whose ``model_selected`` is the rung's previous or new
    model. This is the arm the swap moved.
  * **control** — every other model the same bot ran in the same windows
    (its other rungs). Untouched by the swap.

A Signal fires only when the target arm's terse rate **collapses** while the
control arm **holds steady**. That differential is what makes the check
robust: a pod-wide change (a busier week, a new app, a channel added) moves
both arms and is correctly ignored; only a change isolated to the rung that
was swapped fires.

Calibration
===========

The thresholds below were fitted against the live pod's annotations, then
this module's own ``measure_arms`` + ``evaluate`` were run over that data with
a synthetic ledger entry for the 2026-08-14 swap. The verdicts below are what
the shipped code produced, not a hand calculation:

===========  =======  ==============================  ==========================
bot          verdict  target terse rate               control terse rate
===========  =======  ==============================  ==========================
team-bot-a   FIRES    76.4% → 28.1%                   15.6% → 14.6%
team-bot-c   FIRES    24.8% →  0.0%                    0.0% →  0.0%
bot-d        quiet    n=15 pre / 1 post (below floor)  94.8% → 94.2%
bot-e        quiet    n=7 pre / 5 post (below floor)   90.2% → 100.0%
team-bot-b   quiet    n=0 pre / 1 post (below floor)    0.0% →  0.0%
===========  =======  ==============================  ==========================

Both bots with real target-arm volume show the collapse; every control arm is
flat to within a point; the low-volume bots fall below
:data:`MIN_TARGET_TURNS` and correctly do not fire. ``team-bot-a`` is the bot from
the incident. Rates are over the windows the
monitor actually uses (see WINDOW_DAYS), so they differ slightly from a
whole-month comparison.

Signal shape
============

  * ``model_swap_behavior_divergence`` — one per ``(bot_id, tier)``. Scope
    ``bot``, producer ``model_swap_watch``. Carries the swap record (so the
    Signal names the exact ``from`` → ``to`` and the ledger timestamp), both
    arms' rates and sample counts, and the rollback command. Auto-resolves
    via ``sweep_resolve`` once the rate recovers or the swap ages out of
    :data:`LOOKBACK_DAYS`.

There is deliberately no ``unreadable`` companion Signal. Absent annotations
are the normal state for a quiet bot, not a blind spot: this monitor cannot
distinguish "cannot read" from "this bot had no turns", so a per-bot
unreadable Signal would fire constantly on idle bots. What it does instead is
report ``skipped`` reasons in the run summary, so a run that measured nothing
is visibly distinct from a run that measured everything and found nothing.

Cadence: daily. The divergence accumulates over days, and the monitor only
looks at swaps that are already :data:`SETTLE_DAYS` old (a swap needs a
post-window before it can be judged) — there is nothing an hourly tick would
catch sooner.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from model_swap_ledger import latest_swaps_by_rung  # noqa: E402
from signals import store as signals_store  # noqa: E402
from schema.signal import make_signature  # noqa: E402

PRODUCER = "model_swap_watch"
DIVERGENCE_TYPE = "model_swap_behavior_divergence"

# A turn at or below this many output tokens counts as "terse". A NO_REPLY
# sentinel is ~3 tokens; the leaked deliberations from the incident ran 50-300.
# Fitted on live data (see the calibration table above) — the band separates
# both affected rungs cleanly while leaving every control arm flat.
TERSE_MAX_OUTPUT_TOKENS = 40

# Days of annotations to read either side of the swap instant.
WINDOW_DAYS = 7
# A swap younger than this has too little post-window to judge.
SETTLE_DAYS = 2
# Stop watching a swap once it is this old — by then the post-window is fully
# populated and a still-firing Signal has been seen, or the behavior is the
# new normal and re-litigating it is noise.
LOOKBACK_DAYS = 30
# Minimum turns in the target arm on EACH side. Below this the rate is noise
# (the low-volume bots in the calibration table).
MIN_TARGET_TURNS = 30
# Don't fire when the bot was barely staying quiet to begin with — there is no
# meaningful silence behavior to lose.
MIN_PRE_TERSE_RATE = 0.10
# Fire when the target arm keeps at most this fraction of its prior rate.
COLLAPSE_RATIO = 0.6
# ...but only if the control arm held within this relative band. A control arm
# that moved too is a pod-wide change, not a model regression.
CONTROL_STABLE_BAND = 0.25


def _parse_ts(value):
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _model_key(model):
    """Bare model name — ``anthropic/claude-sonnet-5`` → ``claude-sonnet-5``.

    Annotations record whatever OpenClaw resolved, which is sometimes
    provider-qualified and sometimes not (both spellings appear in the live
    data for the same model). Comparing bare names keeps the target/control
    split from silently mis-binning turns.
    """
    return str(model or "").split("/")[-1].strip().lower()


def iter_annotations(shared_dir: Path, bot_id: str, start, end):
    """Yield ``turn_annotation`` records for ``bot_id`` with ``start <= ts <= end``.

    Reads only the day files the window spans. Unreadable or malformed files
    are skipped — the run summary reports how many turns were actually read,
    so a partial read is visible rather than passed off as a measurement.
    """
    base = Path(shared_dir) / "annotations" / bot_id
    day = start.date()
    while day <= end.date():
        path = base / f"{day.isoformat()}.jsonl"
        day += timedelta(days=1)
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
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "turn_annotation":
                continue
            ts = _parse_ts(rec.get("ts"))
            if ts is None or ts < start or ts > end:
                continue
            yield rec


def measure_arms(shared_dir: Path, bot_id: str, swap_at, target_models,
                 window_days: int = WINDOW_DAYS, now=None) -> dict:
    """Terse-reply counts for the target and control arms, either side of a swap.

    Returns ``{"pre": {"target": (terse, n), "control": (terse, n)},
    "post": {...}}``. Turns exactly at ``swap_at`` land in the post window.
    """
    now = now or datetime.now(timezone.utc)
    targets = {_model_key(m) for m in (target_models or []) if _model_key(m)}
    out = {
        "pre": {"target": [0, 0], "control": [0, 0]},
        "post": {"target": [0, 0], "control": [0, 0]},
    }
    pre_start = swap_at - timedelta(days=window_days)
    post_end = min(swap_at + timedelta(days=window_days), now)
    for rec in iter_annotations(shared_dir, bot_id, pre_start, post_end):
        ts = _parse_ts(rec.get("ts"))
        era = "pre" if ts < swap_at else "post"
        arm = "target" if _model_key(rec.get("model_selected")) in targets else "control"
        cell = out[era][arm]
        cell[1] += 1
        if (rec.get("output_tokens") or 0) <= TERSE_MAX_OUTPUT_TOKENS:
            cell[0] += 1
    return {era: {arm: tuple(v) for arm, v in arms.items()} for era, arms in out.items()}


def _rate(cell):
    """Terse fraction for a ``(terse, n)`` cell, or None when n == 0."""
    terse, n = cell
    return (terse / n) if n else None


def evaluate(arms: dict) -> dict:
    """Decide whether a measured swap diverged. Never raises.

    Returns a verdict dict with ``diverged`` plus the rates and the reason it
    did or did not fire — the reason is carried into the run summary so a
    non-firing swap is explainable without re-running the numbers by hand.
    """
    pre_t, post_t = arms["pre"]["target"], arms["post"]["target"]
    pre_c, post_c = arms["pre"]["control"], arms["post"]["control"]
    verdict = {
        "diverged": False,
        "reason": "",
        "pre_target_rate": _rate(pre_t), "pre_target_n": pre_t[1],
        "post_target_rate": _rate(post_t), "post_target_n": post_t[1],
        "pre_control_rate": _rate(pre_c), "pre_control_n": pre_c[1],
        "post_control_rate": _rate(post_c), "post_control_n": post_c[1],
    }
    if pre_t[1] < MIN_TARGET_TURNS or post_t[1] < MIN_TARGET_TURNS:
        verdict["reason"] = (
            f"below sample floor (target n={pre_t[1]} pre / {post_t[1]} post, "
            f"need {MIN_TARGET_TURNS} each side)"
        )
        return verdict

    # Past the floor gate both denominators are non-zero, so divide directly
    # rather than through _rate() — its Optional return is for the control arm,
    # where an empty window is a real and meaningful case.
    pre_rate = pre_t[0] / pre_t[1]
    post_rate = post_t[0] / post_t[1]
    if pre_rate < MIN_PRE_TERSE_RATE:
        verdict["reason"] = (
            f"no silence behavior to lose (pre-swap terse rate {pre_rate:.1%} "
            f"< {MIN_PRE_TERSE_RATE:.0%})"
        )
        return verdict

    if post_rate > pre_rate * COLLAPSE_RATIO:
        verdict["reason"] = (
            f"terse rate held ({pre_rate:.1%} → {post_rate:.1%}; fires below "
            f"{pre_rate * COLLAPSE_RATIO:.1%})"
        )
        return verdict

    # The control arm is what separates "this model regressed" from "the whole
    # pod's traffic changed". With no control turns we cannot tell the two
    # apart, so we do NOT fire — a differential check without its differential
    # is just the noisy absolute check this monitor exists to avoid.
    pre_c_rate, post_c_rate = _rate(pre_c), _rate(post_c)
    if pre_c_rate is None or post_c_rate is None:
        verdict["reason"] = (
            "no control arm (bot ran only the swapped model in one window) — "
            "cannot separate a model regression from a pod-wide change"
        )
        return verdict
    if pre_c_rate > 0 and abs(post_c_rate - pre_c_rate) / pre_c_rate > CONTROL_STABLE_BAND:
        verdict["reason"] = (
            f"control arm moved too ({pre_c_rate:.1%} → {post_c_rate:.1%}) — "
            "looks pod-wide, not model-specific"
        )
        return verdict

    verdict["diverged"] = True
    verdict["reason"] = (
        f"target {pre_rate:.1%} → {post_rate:.1%} while control "
        f"{pre_c_rate:.1%} → {post_c_rate:.1%}"
    )
    return verdict


def _divergence_signal(swap: dict, verdict: dict) -> dict:
    """Signal payload for one diverged (bot, tier) swap."""
    bot_id, tier = swap["bot_id"], swap["tier"]
    prev = ", ".join(swap.get("previous_models") or []) or "(unknown)"
    new = ", ".join(swap.get("new_models") or []) or "(unknown)"
    pre_r, post_r = verdict["pre_target_rate"], verdict["post_target_rate"]
    pre_c, post_c = verdict["pre_control_rate"], verdict["post_control_rate"]
    return dict(
        signature=make_signature(PRODUCER, DIVERGENCE_TYPE, f"{bot_id}:{tier}"),
        producer=PRODUCER,
        type=DIVERGENCE_TYPE,
        scope="bot",
        bot_id=bot_id,
        incident_key=f"{PRODUCER}:{bot_id}:{tier}",
        title=(
            f"{bot_id}: terse-reply rate collapsed after the {tier} model swap "
            f"({pre_r:.0%} → {post_r:.0%})"
        ),
        body=(
            f"`{bot_id}`'s **{tier}** rung was changed on {swap.get('ts')} "
            f"(`{prev}` → `{new}`, via {swap.get('source', 'unknown')}). Since "
            f"then the share of that rung's turns that produce a terse reply "
            f"(≤{TERSE_MAX_OUTPUT_TOKENS} output tokens) fell from "
            f"{pre_r:.1%} to {post_r:.1%}, while the bot's OTHER rungs held "
            f"steady over the same windows ({pre_c:.1%} → {post_c:.1%}). A "
            "change isolated to the rung that moved is a model behavior "
            "change, not a change in what the bot is being asked.\n\n"
            "The terse-reply rate is a proxy for how often the bot chooses to "
            "stay quiet. In a group channel the bot must emit the bare "
            "`NO_REPLY` sentinel to say nothing; a model that instead writes "
            "out its should-I-reply reasoning posts that reasoning to the "
            "channel. That is the 2026-08-14 incident this monitor exists to "
            "catch — it ran for five days in front of the operator's "
            "colleagues before anyone traced it to the swap.\n\n"
            "**Check first:** read the bot's recent messages in its busiest "
            "shared channel. If it is narrating its silence, or replying to "
            "messages that were not addressed to it, this is that failure.\n\n"
            "**Undo:** `sudo evolve-admin models rollback "
            f"{bot_id} --tier {tier}` restores `{prev}` from the model-swap "
            "ledger. Run it with `--dry-run` first to see the exact write.\n\n"
            "If the new model is behaving correctly and the rate change is "
            "expected (the bot legitimately got busier on that rung), dismiss "
            "this Signal — it will not re-fire for the same swap."
        ),
        details=dict(
            swap=swap,
            terse_max_output_tokens=TERSE_MAX_OUTPUT_TOKENS,
            window_days=WINDOW_DAYS,
            **{k: v for k, v in verdict.items() if k != "diverged"},
            rollback_command=f"sudo evolve-admin models rollback {bot_id} --tier {tier}",
            what_it_means=(
                "A model swap was verified as a string (the id landed in the "
                "tier) but never as a behavior. This bot's turns on the "
                "swapped rung now produce a substantive reply far more often "
                "than before, while its unswapped rungs did not change — the "
                "signature of a model that stopped honoring the silent-reply "
                "sentinel in group channels."
            ),
            fix_steps=(
                "1. Read the bot's recent messages in its busiest shared "
                "channel; look for replies that narrate staying quiet, or "
                "replies to messages not addressed to the bot.\n"
                "2. If confirmed, roll the rung back: `sudo evolve-admin "
                f"models rollback {bot_id} --tier {tier}` (add --dry-run "
                "first).\n"
                "3. If the new model is wanted, keep it and dismiss this "
                "Signal — POD_CONDUCT rule 14 states the silence contract "
                "the model has to meet, and it is injected into every "
                "session.\n"
                "4. The Signal auto-resolves once the rate recovers, or when "
                f"the swap ages past {LOOKBACK_DAYS} days."
            ),
        ),
    )


def run(shared_dir: Path, *, dry_run: bool = False, now=None) -> dict:
    """Evaluate every recent swap in the ledger; emit + sweep Signals."""
    now = now or datetime.now(timezone.utc)
    shared_dir = Path(shared_dir)

    kept: set[str] = set()
    evaluated: list[dict] = []
    skipped: list[dict] = []
    signals_fired = 0

    for (bot_id, tier), swap in sorted(latest_swaps_by_rung(shared_dir).items()):
        swap_at = _parse_ts(swap.get("ts"))
        if swap_at is None:
            skipped.append({"bot_id": bot_id, "tier": tier, "reason": "unparseable ts"})
            continue
        age_days = (now - swap_at).total_seconds() / 86400.0
        if age_days < SETTLE_DAYS:
            skipped.append({"bot_id": bot_id, "tier": tier,
                            "reason": f"too fresh ({age_days:.1f}d < {SETTLE_DAYS}d)"})
            continue
        if age_days > LOOKBACK_DAYS:
            skipped.append({"bot_id": bot_id, "tier": tier,
                            "reason": f"aged out ({age_days:.0f}d > {LOOKBACK_DAYS}d)"})
            continue

        targets = list(swap.get("previous_models") or []) + list(swap.get("new_models") or [])
        arms = measure_arms(shared_dir, bot_id, swap_at, targets, now=now)
        verdict = evaluate(arms)
        evaluated.append({"bot_id": bot_id, "tier": tier, **verdict})

        if not verdict["diverged"]:
            continue
        sig = _divergence_signal(swap, verdict)
        kept.add(sig["signature"])
        if dry_run:
            print(json.dumps({"would_observe": sig}, default=str), flush=True)
        else:
            try:
                signals_store.observe(shared_dir, **sig)
                signals_fired += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[model_swap_watch] observe failed for {bot_id}/{tier}: {exc}",
                      flush=True)

    signals_resolved = 0
    if not dry_run:
        # Sweep over every swap we EVALUATED this run. A swap we skipped (too
        # fresh, aged out, unparseable) was not measured, so its Signal must
        # survive — sweeping on an unmeasured swap would auto-resolve a live
        # regression the moment it aged past the lookback.
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                types={DIVERGENCE_TYPE},
                bot_ids={e["bot_id"] for e in evaluated},
                reason="auto-resolve: terse-reply rate recovered after the model swap",
            )
            signals_resolved += len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[model_swap_watch] sweep_resolve failed: {exc}", flush=True)

    summary = {
        "swaps_evaluated": len(evaluated),
        "swaps_skipped": len(skipped),
        "diverged": sum(1 for e in evaluated if e["diverged"]),
        "signals_fired": signals_fired,
        "signals_resolved": signals_resolved,
        "evaluations": evaluated,
        "skipped": skipped,
        "ran_at": now.isoformat(),
    }
    print(json.dumps(summary, default=str), flush=True)
    return summary


def main(argv: "list[str] | None" = None) -> int:
    from platform_profile import get_profile

    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "--shared-dir",
        default=str(get_profile().shared_dir_default),
        help="Path to the Evolve shared dir (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Signals that would be observed but don't write them.",
    )
    args = parser.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    if not shared_dir.exists():
        print(json.dumps({"status": "skipped",
                          "reason": f"shared dir not found at {shared_dir}"}), flush=True)
        return 0

    run(shared_dir, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
