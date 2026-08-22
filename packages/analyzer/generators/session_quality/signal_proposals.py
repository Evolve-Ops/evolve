"""generators.session_quality.signal_proposals — Signal → Proposal factory.

Takes one ``session_quality`` Signal and emits one Investigation
Proposal. Investigation (claim=None) is the right shape: high
maintenance ratio is the symptom, but the *response* depends on what's
producing the maintenance (stuck loop, auth thrashing, scanner
re-scanning, persona refinement loops). The Proposal points at the
usual suspects; the operator applies the fix.
"""

from __future__ import annotations

from typing import Any

from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "session_quality"
DIMENSION = "cost"


DISMISS_SIG_SESSION_QUALITY = "session_quality:maintenance_ratio_high"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict — useful for tests."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def make_session_quality_proposal(signal: Any) -> Proposal:
    """`session_quality` Signal → Investigation Proposal."""
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    sig_id = _signal_dict_get(signal, "id") or ""
    details: dict = _signal_dict_get(signal, "details") or {}

    avg = float(details.get("maintenance_ratio_avg") or 0.0)
    threshold = float(details.get("threshold") or 0.0)
    window_days = int(details.get("window_days") or 7)
    qualifying_days = int(details.get("qualifying_days") or 0)

    problem = (
        f"{bot_name}: maintenance ratio {avg:.0%} over last {window_days}d "
        f"(threshold {threshold:.0%})"
    )
    headline = f"{bot_name} is mostly doing maintenance instead of work"
    summary = (
        f"{bot_name} spent {avg:.0%} of its sessions on maintenance "
        f"over the last {window_days} days (threshold {threshold:.0%}). "
        f"That's usually a sign productive sessions aren't completing — "
        f"a stuck loop, auth thrashing, or a scanner that keeps "
        f"re-scanning. The Sessions page shows the pattern."
    )
    explanation = (
        f"\"Maintenance\" sessions are housekeeping turns — config "
        f"reads, scan steps, retries — as opposed to \"productive\" "
        f"sessions that move work forward. A healthy bot's "
        f"maintenance share stays well under threshold; a high "
        f"share is the symptom of something not completing.\n\n"
        f"Diagnosis. {avg:.0%} maintenance over the last "
        f"{window_days} days ({qualifying_days} active days, "
        f"threshold {threshold:.0%}). Common causes: stuck loop, "
        f"auth/permission thrashing, scanner re-scanning, or a "
        f"persona-tuning loop where every SOUL edit triggers "
        f"follow-up maintenance turns.\n\n"
        f"Where to look. Sessions page filtered to this bot — recent "
        f"maintenance-tagged sessions should reveal a repeating "
        f"pattern. Cost tab → trigger-kind breakdown. Cron tab → "
        f"recent failures or rapid-repeat runs.\n\n"
        f"What could go wrong. If this bot genuinely does background "
        f"curation as its main job, the maintenance ratio is "
        f"expected to be high — dismiss this finding so the engine "
        f"stops nagging."
    )
    context = (
        f"{bot_name} spent {avg:.0%} of its sessions on maintenance "
        f"(configuration / housekeeping) over the last {window_days} days "
        f"({qualifying_days} active days, threshold {threshold:.0%}). A "
        f"healthy bot's maintenance ratio is well below the threshold — "
        f"high values usually mean productive sessions aren't completing, "
        f"not that the bot is doing legitimate background work.\n\n"
        f"**Common causes:**\n"
        f"- Stuck loop — a heartbeat or cron sequence retrying the same "
        f"failed action.\n"
        f"- Auth / permission thrashing — tool calls hitting access denials "
        f"and the agent retrying or reconfiguring on each turn.\n"
        f"- Scanner re-scanning — application scanner running too often or "
        f"failing to write manifests, so each pass redoes prior work.\n"
        f"- Persona-tuning loop — SOUL/AGENTS edits triggering follow-up "
        f"maintenance turns to integrate the change.\n\n"
        f"**Where to look:**\n"
        f"- Sessions page filtered to this bot — recent sessions tagged as "
        f"maintenance should reveal a repeating pattern.\n"
        f"- Cost tab → trigger-kind breakdown for this bot — if heartbeat "
        f"dominates, the heartbeat itself may be the offender.\n"
        f"- Cron tab → recent failures or rapid-repeat runs.\n\n"
        f"If the maintenance work is intentional (e.g. a bot that does "
        f"genuine background curation), dismiss this proposal."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"session_quality:{bot_id}"],
        provenance=Provenance(
            technique="session_quality.maintenance_ratio",
            signals={
                "maintenance_ratio_avg": round(avg, 4),
                "threshold": threshold,
                "window_days": window_days,
                "qualifying_days": qualifying_days,
            },
            confidence=0.85,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline[:120],
        motivating_signals=[sig_id],
        # ── Phase C-11 operator-first content (Tier 2 — UI manual) ──────
        summary=summary,
        explanation=explanation,
        action_label="Open Sessions page",
        manual_path=f"Cost → Sessions → {bot_name}",
        dismiss_signature=DISMISS_SIG_SESSION_QUALITY,
        dismiss_scope="kind",
    )
