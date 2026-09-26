"""generators.cron_caps_filler.observe — Detector entry point.

Reads firing ``perm_cron_uncapped_agent_turn`` Signals from the
``permission_monitor`` producer and dispatches each to the factory in
``signal_proposals.py``. Unlike most generators in this package, the
factory needs the full cron-job dict (the applier requires it for an
UpsertCronJob round-trip) — so this observer reads
``/Users/<bot>/.openclaw/cron/jobs.json``, locates the job by id, and
hands it to the factory.

If the job is no longer present in jobs.json (operator removed it
between signal emission and this run), no proposal is emitted —
let permission_monitor's sweep_resolve archive the now-stale signal
on its own cadence. The drop is silent because re-emitting the
proposal anyway would just fail at apply time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generators._signal_consumer import iter_firing_signals
from generators.cron_caps_filler.signal_proposals import (
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_MAX_TURNS,
    dismiss_signature_for_job,
    make_uncapped_cron_proposal,
)
from schema.proposal import Proposal


GENERATOR_ID = "cron_caps_filler"
DIMENSION = "safety"

# Producer name on Signals emitted by permission_monitor. This generator
# subscribes to a single signal type from that producer.
PERMISSION_MONITOR_PRODUCER = "permission_monitor"

CONSUMED_SIGNAL_TYPE = "perm_cron_uncapped_agent_turn"


@dataclass
class CronCapsFillerContext:
    """Per-bot run context.

    ``bot_id`` filters signals; ``shared_dir`` locates the Signal store.
    ``home_override`` is an optional per-bot home override used in tests
    so we don't have to write to ``/Users/<bot>/.openclaw`` during a
    unit-test run. In production it stays None and the writer reads
    from the real per-bot home (the ``evolve`` user has ACL read access).

    ``default_max_turns`` / ``default_max_budget_usd`` are the caps the
    filler will set on jobs missing them. The runner factory merges
    per-bot overrides from the generator's config record into these
    fields (see ``_make_cron_caps_filler_ctx`` in generator_runner).
    """

    bot_id: str
    shared_dir: Path
    home_override: Path | None = None
    default_max_turns: int = DEFAULT_MAX_TURNS
    default_max_budget_usd: float = DEFAULT_MAX_BUDGET_USD
    # Phase A.5 — universal dismiss suppression. Per-job granularity,
    # so dismissing caps on job X doesn't suppress finding-uncapped on
    # job Y for the same bot.
    consult_dismissals: bool = True
    # Phase 5 of spec-config-intent-system-2026-05-21.md — investigate
    # before propose. When an operator has recorded that they
    # deliberately left a specific cron job uncapped (because the job
    # legitimately needs long-running execution, etc.), suppress the
    # cap-proposing emission. Mirrors the auth_drift_filler /
    # cache_ttl_tuner / permission_monitor pattern. Disabled in tests
    # that want to exercise the legacy "always propose" path.
    consult_config_intent: bool = True


def _read_jobs_for_bot(bot_id: str, home_override: Path | None) -> list[dict]:
    """Read the bot's cron/jobs.json and return the jobs list.

    Uses ``permissions.writer.read_cron_jobs`` which handles the
    bot-owned-file read pattern (direct ACL read with sudo /bin/cat
    fallback). Returns [] on any failure or empty file — better to
    skip-then-resolve than to crash the generator run.
    """
    try:
        from permissions.writer import read_cron_jobs
    except ImportError:
        return []
    try:
        obj = read_cron_jobs(bot_id, home_override=home_override)
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    jobs = obj.get("jobs")
    if not isinstance(jobs, list):
        return []
    return [j for j in jobs if isinstance(j, dict)]


def _find_job(jobs: list[dict], job_id: str) -> dict | None:
    """Locate a job by id; case-sensitive exact match on the ``id`` field."""
    for job in jobs:
        if job.get("id") == job_id:
            return job
    return None


def observe(ctx: CronCapsFillerContext) -> list[Proposal]:
    """Read firing permission_monitor signals and produce one proposal each.

    Per-signal failures are swallowed so a bad payload on one signal
    doesn't torpedo the rest of the run.
    """
    proposals: list[Proposal] = []
    # Cache the bot's jobs list — we read it once per observe() call;
    # all per-signal lookups are in-memory after that.
    bot_jobs = _read_jobs_for_bot(ctx.bot_id, ctx.home_override)

    for sig in iter_firing_signals(
        ctx.shared_dir,
        ctx.bot_id,
        PERMISSION_MONITOR_PRODUCER,
        CONSUMED_SIGNAL_TYPE,
    ):
        details: dict = getattr(sig, "details", None) or {}
        job_id = details.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        job = _find_job(bot_jobs, job_id)
        if job is None:
            # Job evaporated between signal and this run — let the
            # next permission_monitor pass archive the signal.
            continue
        # Phase A.5 dismiss-suppression gate. Per-job signature, so
        # dismissing caps on one cron doesn't suppress findings for
        # the bot's other uncapped crons.
        if ctx.consult_dismissals and _is_dismissed(
            ctx.shared_dir, ctx.bot_id, job_id,
        ):
            continue
        # Phase 5 intent-suppression gate. The intent's field_path is
        # synthetic — ``commands.cron.<job_id>.caps`` doesn't appear in
        # openclaw.json, but it's the natural slot to record
        # "this job is intentionally uncapped" once. value=None is the
        # canonical representation (no caps present); intent_still_valid
        # gates the Phase 5 plugin-coupled extension and is exercised
        # by the test that mocks it to False.
        if ctx.consult_config_intent and _is_intent_explained(
            ctx.shared_dir, ctx.bot_id, job_id,
        ):
            continue
        try:
            proposal = make_uncapped_cron_proposal(
                sig,
                job=job,
                default_max_turns=ctx.default_max_turns,
                default_max_budget_usd=ctx.default_max_budget_usd,
            )
            proposals.append(proposal)
        except Exception:
            continue

    return proposals


def _is_dismissed(shared_dir: Path, bot_id: str, job_id: str) -> bool:
    """Return True if the operator has dismissed caps on this specific
    cron for this bot. Fail-open on any read failure."""
    try:
        from arbiter.dismissals import is_suppressed
    except ImportError:
        return False
    try:
        return is_suppressed(
            shared_dir,
            signature=dismiss_signature_for_job(job_id),
            bot_id=bot_id,
        )
    except Exception:
        return False


def _intent_field_path(job_id: str) -> str:
    """Return the synthetic config_intent field path for ``job_id``'s
    cap-state. Centralized so the suppression check, the test fixtures,
    and any future operator-facing UI surface all reach for the same
    key.

    ``commands.cron.<job_id>.caps`` is synthetic — it doesn't appear
    in openclaw.json — but it satisfies config_intent's allowed-prefix
    check (``commands.``) and reads naturally on the Intentional
    Deviations page.
    """
    return f"commands.cron.{job_id}.caps"


def _is_intent_explained(
    shared_dir: Path, bot_id: str, job_id: str,
) -> bool:
    """Return True if a config_intent records that this cron job is
    intentionally uncapped.

    The canonical shape (set by the operator via the admin UI's
    Intentional Deviations editor or by ``evolve-admin intent set``):

      bot_id      = <bot>
      field_path  = commands.cron.<job_id>.caps
      value       = None         (None means "no caps applied")
      reason      = "<why this cron legitimately needs to run uncapped>"

    The check is fail-open: any import or read failure returns False
    so the generator falls back to its legacy "always propose" path —
    over-emitting an explainable signal is the safer direction than
    silently dropping a real one. Symmetric with the equivalent gates
    in auth_drift_filler, cache_ttl_tuner, and permission_monitor.
    """
    try:
        from evolve_admin.config_intent import get_intent, intent_still_valid
    except ImportError:
        return False
    try:
        intent = get_intent(
            bot_id, _intent_field_path(job_id), shared_dir=shared_dir,
        )
    except Exception:  # noqa: BLE001 — generator loop hygiene
        return False
    if intent is None:
        return False
    # ``value=None`` is the canonical "intentionally uncapped" marker.
    # Other values mean the operator wrote SOMETHING but it doesn't
    # match the "leave this alone" semantics — fall through and let
    # the generator propose normally.
    if intent.get("value") is not None:
        return False
    try:
        return bool(intent_still_valid(intent, shared_dir=shared_dir))
    except Exception:  # noqa: BLE001
        return True  # fail open toward suppression — operator recorded
        # the intent; don't undermine that based on a validity-check
        # transient error.
