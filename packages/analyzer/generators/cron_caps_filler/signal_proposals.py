"""generators.cron_caps_filler.signal_proposals — Signal → Proposal factory.

One factory: ``make_uncapped_cron_proposal`` turns a single
``perm_cron_uncapped_agent_turn`` Signal into an ``UpsertCronJob``
Proposal that adds the missing ``maxTurns`` + ``maxBudgetUsd`` caps to
the affected job's payload.

The factory needs the *full* job dict to build a valid UpsertCronJob
(the applier replaces existing-by-id, so the proposal must round-trip
every field). The Signal carries only ``bot_id`` / ``job_id`` /
``name`` / cap flags, so ``observe.py`` reads the bot's jobs.json,
locates the matching job, and hands the dict in here. If the job
isn't found at observe time (job removed mid-flight), no proposal is
emitted for that signal.

Defaults match the admin UI's "Add caps" button (maxTurns=20,
maxBudgetUsd=1.00) so manual and automated paths produce equivalent
edits. Operators can tune defaults per-bot via
``cron_caps_filler.bots.<bot_id>.{default_max_turns,default_max_budget_usd}``
in the generator's config (per-bot overrides handled by
``_resolve_gen_config`` in generator_runner).
"""

from __future__ import annotations

import copy
from typing import Any

from schema.proposal import (
    Proposal,
    Provenance,
    RiskTag,
    UpsertCronJob,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "cron_caps_filler"
DIMENSION = "safety"

# Defaults match the admin UI's "Add caps" button (web/index.html prompt
# initialValue). Keeping these in sync means manual + automated paths
# converge on the same caps unless the operator explicitly overrides.
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_BUDGET_USD = 1.00


# ── Dismiss signature (Phase A.5 + Phase C-2) ───────────────────────────────
#
# Per-job granularity: dismissing caps on job X doesn't suppress finding
# uncapped on job Y for the same bot. The store layer handles per-bot
# scoping; the signature carries the job_id to keep distinct findings
# distinct.
def dismiss_signature_for_job(job_id: str) -> str:
    return f"{GENERATOR_ID}:uncapped_agent_turn:{job_id}"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict — useful for tests."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def _payload_has_turn_cap(payload: dict) -> bool:
    """Mirror the applier's ``_job_has_required_caps`` turn-cap check.

    Returns True if the payload already carries a positive turn cap
    under any of the accepted field names. Used so the filler is
    idempotent on the rare case where only one of the two caps is
    missing — we don't overwrite an operator-set cap with our default.
    """
    for key in ("maxTurns", "turnCap"):
        v = payload.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return True
    return False


def _payload_has_budget_cap(payload: dict) -> bool:
    """Same shape as turn-cap; checks maxBudgetUsd / budgetUsd."""
    for key in ("maxBudgetUsd", "budgetUsd"):
        v = payload.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return True
    return False


def make_uncapped_cron_proposal(
    signal: Any,
    *,
    job: dict,
    default_max_turns: int = DEFAULT_MAX_TURNS,
    default_max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
) -> Proposal:
    """`perm_cron_uncapped_agent_turn` + job dict → UpsertCronJob proposal.

    Takes the live job dict (read from jobs.json by the caller) and
    produces a fresh copy with caps merged into ``payload``. Only the
    missing cap is set — an operator who had set one of the two
    manually keeps that value; the proposal completes the other.
    The applier requires both caps to be present (its auto-reject
    fires on uncapped agentTurn), so the resulting job dict is
    apply-ready.
    """
    if not isinstance(job, dict):
        raise ValueError("job must be a dict")
    job_id = job.get("id")
    if not job_id:
        raise ValueError("job dict must include a non-empty 'id'")

    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)

    # Deep copy so we never mutate the caller's job dict; the proposal
    # owns its payload from this point on.
    new_job = copy.deepcopy(job)
    payload = new_job.setdefault("payload", {})
    if not isinstance(payload, dict):
        # Pathological shape; replace rather than fail — better to ship a
        # well-formed proposal than to error on a malformed input.
        payload = {}
        new_job["payload"] = payload

    set_turn = False
    set_budget = False
    if not _payload_has_turn_cap(payload):
        payload["maxTurns"] = int(default_max_turns)
        set_turn = True
    if not _payload_has_budget_cap(payload):
        payload["maxBudgetUsd"] = float(default_max_budget_usd)
        set_budget = True

    name = new_job.get("name") or job_id

    # Build a human-readable summary of which caps the proposal sets.
    # This goes into ``problem`` so it surfaces in evo's chat paraphrase
    # and in the Recommendations row — operators reviewing the proposal
    # need to see what's actually being set, not just "fix the thing".
    set_pieces: list[str] = []
    if set_turn:
        set_pieces.append(f"maxTurns={default_max_turns}")
    if set_budget:
        set_pieces.append(f"maxBudgetUsd=${default_max_budget_usd:.2f}")
    set_summary = " + ".join(set_pieces) if set_pieces else "no caps to add"

    problem = (
        f"{bot_name}: cron job {name!r} ({job_id}) is an agentTurn payload "
        f"with no turn/budget cap. Proposal sets {set_summary}."
    )
    headline = f"Add the standard caps to {bot_name}'s {name!r} cron"

    # ── Phase C-2 (2026-06-04 protocol) — operator-first content ────────────
    # Tier 1 — auto-apply UpsertCronJob. The "Add the standard caps"
    # button writes both maxTurns and maxBudgetUsd in one go; manual
    # path lands on the per-bot cron editor.
    summary = (
        f"One of {bot_name}'s cron jobs ({name!r}) runs without a turn or "
        f"budget cap. If something goes wrong — a runaway loop or a "
        f"flaky tool — there's nothing to stop it from spending unbounded "
        f"money. Adding the standard caps (max {default_max_turns} turns "
        f"per fire, ${default_max_budget_usd:.2f} budget) keeps an incident "
        f"bounded."
    )
    explanation = (
        f"Cron jobs on this pod run as agent turns — the bot wakes up, "
        f"does some work, and goes back to sleep. Without caps, a single "
        f"fire can keep looping until the model produces a response, "
        f"which under the right (wrong) conditions can be a very long "
        f"time and a lot of money.\n\n"
        f"Diagnosis. Permission Monitor scanned this bot's cron jobs and "
        f"found {name!r} configured as an agentTurn payload with no "
        f"maxTurns or maxBudgetUsd. The fix is adding both — the applier "
        f"requires both caps before it'll allow the job to keep running, "
        f"and the engine refuses uncapped agentTurns at the gateway.\n\n"
        f"The defaults. {default_max_turns} turns and "
        f"${default_max_budget_usd:.2f} match the admin UI's manual "
        f"'Add caps' button, so the auto-applied result is identical to "
        f"what you'd get clicking through yourself. You can edit either "
        f"value before approving if your job legitimately needs more "
        f"headroom.\n\n"
        f"What could go wrong. If this job is supposed to run long-lived "
        f"work — a batch process that genuinely needs more than "
        f"{default_max_turns} turns per fire — the cap will cut it off "
        f"mid-run. Raise the cap before approving, or dismiss and add "
        f"caps manually with the right value."
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"perm_cron_uncapped_agent_turn:{bot_id}:{job_id}"],
        provenance=Provenance(
            technique="cron_caps_filler.uncapped_agent_turn",
            signals={
                "bot_id": bot_id,
                "job_id": job_id,
                "set_max_turns": set_turn,
                "set_max_budget_usd": set_budget,
                "default_max_turns": int(default_max_turns),
                "default_max_budget_usd": float(default_max_budget_usd),
            },
            confidence=0.95,
        ),
        problem=problem,
        action=UpsertCronJob(bot_id=bot_id, job=new_job),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["cron_config"],
        ),
        # Typed config edit; the applier verifies + writes. We don't
        # declare a metric claim — the verification is "applier returned
        # ok + permission_monitor stops re-firing the signal".
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase C-2 operator-first content (Tier 1 — auto-apply) ──────
        summary=summary,
        explanation=explanation,
        action_label="Add the standard caps",
        manual_path=f"Cron Jobs → {bot_name} → {name!r}",
        dismiss_signature=dismiss_signature_for_job(job_id),
        dismiss_scope="kind",
    )
