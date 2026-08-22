"""fit_review.runner — the bot-side Fit Reviewer pass (Bite 3).

Spec: docs/spec-fit-reviewer-2026-06-12.md §3 + §7 (Bite 3).

Runs **in-bot, on the bot account, with the bot's own LLM credentials** — the
only locus that can read the bot's real transcripts (principle-per-bot-inference).
It mirrors the App-Audit precedent exactly (``app_audit_runner`` →
``app_audit_tier3`` → outbox → admin poller): a pure-Python gate decides whether
to spend one bounded in-bot LLM call, the call's structured output is written to a
per-bot outbox, and the admin-side poller (Bite 4, sibling chip) ingests the
*structured output only* — never the raw transcript.

The five structural brakes that keep this from becoming "the 138" (spec §8) are
realized here in order:

  1. **Opt-out first.** A user who opted out of observation is not reviewed — we
     exit before reading purpose or tuples (privacy before everything).
  2. **Purpose-anchored + targeting floor (pure Python, no LLM).** The targeting
     report (Bite 1) must return ``targets_found`` — a declared purpose AND a
     purpose-aligned, above-floor, gallery-matched need. ``no_purpose`` /
     ``no_candidate`` / ``gap_no_gallery_match`` ⇒ **no LLM call** (this is the
     line that makes "review the team PM bot" and "review the thin personal bot"
     diverge correctly).
  3. **One bounded reflection.** Exactly one call, only when (2) passed.
  4. **Cite-or-don't + bounded action space.** Enforced in ``reflection`` — a
     non-verbatim quote or an off-shortlist pkg_id ⇒ no candidate written.
     (identity: see applications.app_identity.resolve_app_id — every pkg_id
     in this module is a gallery catalog key; see fit_review/__init__.py.)
  5. **Deterministic value + altitude.** Computed here from the targeting
     support (Gate B), never LLM-asserted.

A run that does not produce a grounded suggestion writes **nothing** to the
outbox — it records the decision + reason in the per-bot trail instead. So every
outbox file is a positive, cited candidate (the Bite-4 poller never sees a
``no_candidate`` record).

Cadence: weekly (like ``app_suggester``). NO new launchd job — the pass
piggybacks the existing hourly bot-side Tier-3 audit tick via ``run_if_due``,
which is cadence-gated by a per-bot sentinel (see ``app_audit_runner`` wiring).
``python3 -m fit_review.runner --bot-id <bot> [--force]`` is the manual /
proof-run surface.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from evolve_config import CANONICAL_SHARED_DIR
from fit_review.reflection import ReflectionContext, reflect
from fit_review.targeting import (
    DECISION_TARGETS_FOUND,
    STRONG_EMERGENT_SESSIONS,  # Gate B "high" bar (spec §3.6) == strong-emergent floor
    SUPPORT_FLOOR_SESSIONS,  # Gate B "medium" bar == support floor
    build_targeting_report_for_bot,
)

logger = logging.getLogger(__name__)

# The candidate record discriminator + altitude. The runner EMITS the contract
# dict that Bite 4's ``fit_review.candidate.parse_candidate`` reads (the field
# names match that reader's canonical aliases). The Fit Reviewer always emits at
# L2 (capability); altitude is deterministic, never LLM-asserted (spec §5.1).
CANDIDATE_KIND = "fit_review_candidate"
ALTITUDE_L2 = 2

# Weekly cadence (the brief sets weekly for this sibling pass; spec §3.4's
# monthly recommendation is the conservative ceiling — weekly is cheap because
# the LLM only fires for above-floor bots).
DEFAULT_CADENCE_DAYS = 7

# Recent USER turns to feed the reflection (the capture buffer is already bounded
# by policy to ≤200 turns / ≤48h; we cap how many we pass).
DEFAULT_TRANSCRIPT_LIMIT = 50

# OpenClaw agent dispatch timeout for the one reflection call. Same generous cap
# as the audit dispatch — Sonnet returns in <60s; agent startup adds ~30s.
_REFLECTION_TIMEOUT_S = 180

# Sentinel meaning "resolve the standard-role model" for the ``model`` arg, so
# callers can pass an explicit ``None`` (= inherit bot default) distinctly.
_RESOLVE_MODEL = object()


# ─────────────────────────────────────────────────────────────────────────────
# Paths. Two homes, by role:
#   * bot-local STATE (cadence sentinel + decision trail) lives in the bot's own
#     workspace — the bot writes it, nobody else needs it.
#   * the integration ARTIFACT (the candidate) goes to {shared_dir}/fit_review/
#     outbox/<run_id>/<bot_id>.json — exactly where the Bite-4 poller drains it
#     (fit_review_poller._outbox_root). The runner runs in-bot as the bot user and
#     writes there directly, the same posture as the OC plugin's TurnObserver
#     (which writes {shared_dir}/<bot_id>/turns/... in-bot).
# ─────────────────────────────────────────────────────────────────────────────


def _bot_workspace() -> Path:
    """Return the bot's workspace path (same resolution as app_audit_runner)."""
    home = Path.home()
    oc_json = home / ".openclaw" / "openclaw.json"
    try:
        cfg = json.loads(oc_json.read_text())
        ws = cfg.get("agents", {}).get("defaults", {}).get("workspace")
        if ws:
            return Path(ws)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("fit_review: workspace resolve fell back to default: %s", exc)
    return home / ".openclaw" / "workspace"


def _state_dir(workspace: Path) -> Path:
    """Bot-local state (cadence sentinel + decision trail)."""
    return workspace / "evolve" / "fit_review"


def _trail_path(workspace: Path) -> Path:
    return _state_dir(workspace) / "trail.jsonl"


def _sentinel_path(workspace: Path) -> Path:
    return _state_dir(workspace) / "last_run.json"


def _outbox_dir(shared_dir: Path) -> Path:
    """The integration artifact home — where Bite 4's poller drains candidates."""
    return Path(shared_dir) / "fit_review" / "outbox"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ─────────────────────────────────────────────────────────────────────────────
# Inputs: network.json, the transcript capture buffer, the opt-out gate
# ─────────────────────────────────────────────────────────────────────────────


def _load_network(shared_dir: Path, network_path: Path | None = None) -> dict[str, Any]:
    """Read network.json (for the purpose anchor + opt-out flag). Reads
    ``{shared_dir}/network.json`` by default; the bot has read access to the
    shared tree. Returns ``{"bots": {}}`` on any failure (degrade gracefully)."""
    path = Path(network_path) if network_path else Path(shared_dir) / "network.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"bots": {}}
    except (OSError, json.JSONDecodeError):
        return {"bots": {}}


def _read_recent_transcript(
    shared_dir: Path, bot_id: str, *, limit: int = DEFAULT_TRANSCRIPT_LIMIT
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(turns, opted_out)`` from the recent-transcript capture buffer.

    Reads ``{shared_dir}/metrics/{bot_id}/recent-transcripts.json`` directly —
    the same file ``evolve_admin.pod_state.turns.recent_turns`` and
    ``RecentTranscriptCapture.ts`` own (we read it directly to avoid importing
    the admin tree in-bot). Each turn is ``{session_id, turn_index, ts, text}``
    with USER text only (assistant replies are not captured — a privacy
    invariant, and exactly what cite-or-don't needs).

    ``opted_out=True`` when the buffer file is ABSENT — the capture plugin never
    writes it for a bot whose user opted out (``securityScanning=false``). That
    distinguishes "opted out" from "file present but empty" (no recent activity).
    """
    path = Path(shared_dir) / "metrics" / bot_id / "recent-transcripts.json"
    if not path.exists():
        return [], True  # no buffer ⇒ opted out / never captured
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], False
    if not isinstance(raw, list):
        return [], False
    capped = max(1, min(int(limit), DEFAULT_TRANSCRIPT_LIMIT))
    tail = [t for t in raw[-capped:] if isinstance(t, dict)]
    return tail, False


def _is_opted_out(
    network: dict[str, Any], bot_id: str, transcript_opted_out: bool
) -> bool:
    """True iff the bot's user has opted out of observation.

    Two signals, either of which means opt-out:
      * the capture buffer is absent (``transcript_opted_out``), and
      * ``network.bots[bot_id].securityScanning is False`` (explicit opt-out).
    ``securityScanning`` defaults to True (observe), so only an explicit ``False``
    counts. Honoring DNT from v1 (spec §3.1).
    """
    if transcript_opted_out:
        return True
    bot = ((network or {}).get("bots") or {}).get(bot_id) or {}
    return bot.get("securityScanning") is False


# ─────────────────────────────────────────────────────────────────────────────
# LLM dispatch (production) + model resolution
# ─────────────────────────────────────────────────────────────────────────────


def _make_oc_llm_call(bot_id: str, shared_dir: Path) -> Callable[[str, str, str | None, int], str]:
    """Build the production LLM callable: one ``openclaw agent`` dispatch.

    Reuses ``app_audit_tier3._dispatch_via_oc`` (battle-tested: process-group
    kill on timeout, cost recovery from TurnObserver, message-size cap). Lazy
    import so importing this module in tests never drags in the subprocess
    machinery. A dispatch error returns ``""`` ⇒ the reflection declines (nothing
    written) rather than raising.
    """
    from app_audit_tier3 import _dispatch_via_oc

    def _call(system: str, user: str, model: str | None, max_tokens: int) -> str:
        text, _tokens, err = _dispatch_via_oc(
            system,
            user,
            timeout_s=_REFLECTION_TIMEOUT_S,
            bot_id=bot_id,
            shared_dir=shared_dir,
            model=model,
        )
        if err:
            logger.warning("fit_review: OC dispatch error: %s", err)
            return ""
        return text

    return _call


def _resolve_reflection_model(bot_id: str) -> str | None:
    """Resolve the model for the reflection — the pod's ``standard`` role.

    The reflection is "the one place judgment matters" (spec §3.4) — standard or
    power, never Haiku. We pin standard (cost discipline; the bot's default may
    be the power rung). Goes through the role/tier resolver so a per-bot / pod
    override is honored and no provider/model literal appears here. Returns
    ``None`` (⇒ inherit the bot's agent default) when resolution fails. Mirrors
    ``app_audit_runner._resolve_first_audit_model``.
    """
    try:
        from evolve_config import load_config
        from models import resolve_tier

        return resolve_tier("standard", load_config(), bot_id=bot_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "fit_review: standard-role resolve failed, inheriting bot default: %s",
            exc,
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic value (Gate B) + context assembly
# ─────────────────────────────────────────────────────────────────────────────


def compute_value(
    report, chosen_pkg_id: str | None, cited_session_ids: list[str]
) -> tuple[dict[str, Any], dict[str, int]]:
    """Compute the value tier from the targeting support — NEVER from the LLM.

    Gate B (spec §3.6):
        high   if purpose_aligned AND distinct_sessions ≥ 8  AND no_current_coverage
        medium if purpose_aligned AND distinct_sessions ≥ 3
        low    otherwise

    ``distinct_sessions`` / ``distinct_days`` are taken across the candidate
    domains the chosen app covers (the strongest supporting domain). ``basis`` is
    the literal trace; ``evidence_refs`` cite the grounding sessions. No dollar
    field (capability value is not dollar-denominated).
    """
    match = next(
        (g for g in report.shortlist if g.pkg_id == chosen_pkg_id), None
    )
    matched_domains = list(match.matched_domains) if match else []
    cands = [c for c in report.candidates if c.noun in matched_domains]
    if not cands:
        # Defensive: the chosen pkg should always cover ≥1 candidate domain (the
        # shortlist is built from candidate domains). Fall back to all candidates.
        cands = list(report.candidates)

    max_sessions = max((c.distinct_sessions for c in cands), default=0)
    max_days = max((c.distinct_days for c in cands), default=0)
    purpose_aligned = any(c.alignment == "confirmed" for c in cands)
    no_coverage = all(not c.covered for c in cands)

    if purpose_aligned and max_sessions >= STRONG_EMERGENT_SESSIONS and no_coverage:
        tier = "high"
    elif purpose_aligned and max_sessions >= SUPPORT_FLOOR_SESSIONS:
        tier = "medium"
    else:
        tier = "low"

    domains_str = ", ".join(matched_domains) or "(none)"
    basis = (
        f"purpose_aligned={purpose_aligned}; covers [{domains_str}]; "
        f"{max_sessions} distinct sessions / {max_days} distinct days; "
        f"no_current_coverage={no_coverage}"
    )
    # value_estimate mirrors schema.proposal.ValueEstimate exactly so the poller
    # maps it onto the Proposal with no translation.
    value_estimate = {
        "tier": tier,
        "basis": basis,
        # de-dup, preserve order
        "evidence_refs": list(dict.fromkeys(s for s in cited_session_ids if s)),
    }
    support = {"distinct_sessions": max_sessions, "days": max_days}
    return value_estimate, support


def _candidate_record(
    *,
    bot_id: str,
    archetype: str | None,
    recommended_need: str,
    suggested_gallery_pkg_id: str | None,
    cited_evidence: list,
    value_estimate: dict[str, Any],
    support: dict[str, int],
    targeting_decision: str,
    run_id: str,
    created_at: str,
    record_id: str,
) -> dict[str, Any]:
    """Build the on-disk candidate dict — the exact shape Bite 4's
    ``fit_review.candidate.parse_candidate`` reads. ``kind``/``record_id`` are
    envelope (the reader ignores them; it derives run_id/bot_id from the path)."""
    return {
        "kind": CANDIDATE_KIND,
        "record_id": record_id,
        "bot_id": bot_id,
        "archetype": archetype,
        "recommended_need": recommended_need,
        "suggested_gallery_pkg_id": suggested_gallery_pkg_id,
        "cited_evidence": [
            {"quote": e.quote, "session_id": e.session_id, "ts": e.ts}
            for e in cited_evidence
        ],
        "value_estimate": value_estimate,
        "altitude": ALTITUDE_L2,
        "targeting_decision": targeting_decision,
        "support": {
            "distinct_sessions": int(support.get("distinct_sessions", 0)),
            "days": int(support.get("days", 0)),
        },
        "run_id": run_id,
        "created_at": created_at,
    }


def _assemble_candidate_briefs(report) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in report.candidates:
        out.append(
            {
                "noun": c.noun,
                "distinct_sessions": c.distinct_sessions,
                "distinct_days": c.distinct_days,
                "frustration_share": round(c.frustration_share, 3),
                "alignment": c.alignment,
                "top_verbs": [list(v) for v in c.top_verbs[:3]],
            }
        )
    return out


def _assemble_shortlist(report) -> list[dict[str, Any]]:
    return [
        {
            "pkg_id": g.pkg_id,
            "name": g.name,
            "matched_domains": list(g.matched_domains),
        }
        for g in report.shortlist
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Outbox + trail writes
# ─────────────────────────────────────────────────────────────────────────────


def _write_candidate_outbox(
    shared_dir: Path, run_id: str, bot_id: str, record: dict[str, Any]
) -> Path:
    """Atomic temp+rename write of one candidate to
    ``{shared_dir}/fit_review/outbox/<run_id>/<bot_id>.json`` (where Bite 4's
    poller drains it)."""
    outbox = _outbox_dir(shared_dir) / run_id
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{bot_id}.json"
    fd, tmp = tempfile.mkstemp(
        dir=str(outbox), prefix=f".{bot_id}-", suffix=".json.tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def _cap_trail(trail: Path, *, soft_cap: int = 1000, keep: int = 500) -> None:
    """Soft line-cap the per-bot trail: when it exceeds ``soft_cap`` lines, keep
    the most recent ``keep``. Mirrors app_audit_runner._prune_investigations.

    The trail has no reader (the Bite-4 poller drains ``outbox/``, never the
    trail) — it is pure per-run observability, so a rolling tail is plenty and
    leaving it unbounded was a write-only leak. Best-effort; never raises."""
    try:
        lines = trail.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > soft_cap:
        try:
            trail.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
        except OSError:
            pass


def _append_trail(workspace: Path, entry: dict[str, Any]) -> None:
    """Append one decision record to the per-bot trail (observability for every
    run, including the non-emitting ones). Best-effort; never raises."""
    d = _state_dir(workspace)
    try:
        d.mkdir(parents=True, exist_ok=True)
        trail = _trail_path(workspace)
        with trail.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        _cap_trail(trail)
    except OSError as exc:
        logger.warning("fit_review: trail append failed: %s", exc)


def _record_skip(
    workspace: Path,
    *,
    run_id: str,
    bot_id: str,
    decision: str,
    reason: str,
    created_at: str,
    targeting_decision: str | None = None,
) -> dict[str, Any]:
    """Record a non-emitting run in the trail (NOT the outbox) and return the
    result dict. ``decision`` ∈ {opted_out, no_purpose, no_candidate,
    gap_no_gallery_match, declined}."""
    entry = {
        "ts": created_at,
        "kind": "fit_review_run",
        "run_id": run_id,
        "bot_id": bot_id,
        "decision": decision,
        "reason": reason,
        "wrote_candidate": False,
    }
    if targeting_decision:
        entry["targeting_decision"] = targeting_decision
    _append_trail(workspace, entry)
    return {
        "run_id": run_id,
        "decision": decision,
        "wrote_candidate": False,
        "reason": reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# The pass
# ─────────────────────────────────────────────────────────────────────────────


def run_fit_review_for_bot(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    network: dict[str, Any] | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    llm_call: Callable[[str, str, str | None, int], str] | None = None,
    catalog: list[dict[str, Any]] | None = None,
    targeting_report=None,
    transcript: list[dict[str, Any]] | None = None,
    model: Any = _RESOLVE_MODEL,
    transcript_limit: int = DEFAULT_TRANSCRIPT_LIMIT,
) -> dict[str, Any]:
    """Run one Fit Reviewer pass for ``bot_id``. Returns a result dict.

    Test seams (all optional — production reads them from disk):
      * ``network``        — network.json dict (else read from shared_dir).
      * ``transcript``     — recent USER turns (else read the capture buffer;
                             ``None`` also drives the opt-out check).
      * ``targeting_report`` — a prebuilt TargetingReport (else build from disk).
      * ``llm_call``       — the reflection callable (else the OpenClaw dispatch).
      * ``model``          — model id; default resolves the standard role.

    Writes a candidate to the outbox iff the reflection produced a grounded, cited
    suggestion; otherwise records the decision in the trail and writes nothing.
    """
    run_id = run_id or _new_id("fitrev-run")
    now = now or datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")

    if network is None:
        network = _load_network(shared_dir)

    # ── Brake 1: opt-out first (privacy before any purpose/tuple read). ───────
    if transcript is None:
        turns, transcript_opted_out = _read_recent_transcript(
            shared_dir, bot_id, limit=transcript_limit
        )
    else:
        turns, transcript_opted_out = list(transcript), False
    if _is_opted_out(network, bot_id, transcript_opted_out):
        return _record_skip(
            workspace,
            run_id=run_id,
            bot_id=bot_id,
            decision="opted_out",
            reason=(
                "user-observation opt-out — not reviewed (no capture buffer / "
                "securityScanning=false)"
            ),
            created_at=created_at,
        )

    # ── Brake 2: targeting floor (pure Python — the cheap gate, NO LLM). ──────
    if targeting_report is None:
        targeting_report = build_targeting_report_for_bot(
            bot_id, network=network, shared_dir=shared_dir, catalog=catalog
        )
    decision = targeting_report.decision
    if decision != DECISION_TARGETS_FOUND:
        # no_purpose / no_candidate / gap_no_gallery_match ⇒ the expensive
        # reflection NEVER fires. This is the line that prevents the 138.
        return _record_skip(
            workspace,
            run_id=run_id,
            bot_id=bot_id,
            decision=decision,
            reason=targeting_report.reason,
            created_at=created_at,
        )

    # ── Brake 3: the one bounded reflection. ──────────────────────────────────
    ctx = ReflectionContext(
        bot_id=bot_id,
        archetype=targeting_report.archetype,
        mission=targeting_report.mission,
        candidate_briefs=_assemble_candidate_briefs(targeting_report),
        shortlist=_assemble_shortlist(targeting_report),
        transcript_turns=turns,
    )
    if llm_call is None:
        llm_call = _make_oc_llm_call(bot_id, shared_dir)
    if model is _RESOLVE_MODEL:
        model = _resolve_reflection_model(bot_id)

    result = reflect(ctx, llm_call=llm_call, model=model)

    # ── Brake 4: cite-or-don't already enforced in reflect(); declined ⇒ none. ─
    if not result.is_suggestion:
        return _record_skip(
            workspace,
            run_id=run_id,
            bot_id=bot_id,
            decision="declined",
            reason=result.reason,
            created_at=created_at,
            targeting_decision=decision,
        )

    # ── Brake 5: deterministic value + altitude; assemble + write candidate. ──
    cited_sessions = [e.session_id for e in result.cited_evidence if e.session_id]
    value_estimate, support = compute_value(
        targeting_report, result.suggested_gallery_pkg_id, cited_sessions
    )
    record = _candidate_record(
        bot_id=bot_id,
        archetype=targeting_report.archetype,
        recommended_need=result.recommended_need,
        suggested_gallery_pkg_id=result.suggested_gallery_pkg_id,
        cited_evidence=result.cited_evidence,
        value_estimate=value_estimate,
        support=support,
        targeting_decision=decision,
        run_id=run_id,
        created_at=created_at,
        record_id=_new_id("fitrev"),
    )
    path = _write_candidate_outbox(shared_dir, run_id, bot_id, record)
    _append_trail(
        workspace,
        {
            "ts": created_at,
            "kind": "fit_review_run",
            "run_id": run_id,
            "bot_id": bot_id,
            "decision": "suggest",
            "wrote_candidate": True,
            "suggested_gallery_pkg_id": result.suggested_gallery_pkg_id,
            "value_tier": value_estimate["tier"],
            "cited_evidence_count": len(result.cited_evidence),
            "outbox_path": str(path),
        },
    )
    return {
        "run_id": run_id,
        "decision": "suggest",
        "wrote_candidate": True,
        "outbox_path": str(path),
        "suggested_gallery_pkg_id": result.suggested_gallery_pkg_id,
        "value_tier": value_estimate["tier"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cadence wrapper (weekly; piggybacks the hourly tier-3 tick — no new launchd job)
# ─────────────────────────────────────────────────────────────────────────────


def _is_due(
    workspace: Path, now: datetime, interval_days: int
) -> tuple[bool, str]:
    """Return (due, reason) from the per-bot weekly sentinel."""
    sentinel = _sentinel_path(workspace)
    if not sentinel.exists():
        return True, "never run"
    try:
        data = json.loads(sentinel.read_text(encoding="utf-8"))
        last_raw = (data.get("last_run_at") or "").strip()
        last = datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
    except (OSError, json.JSONDecodeError, ValueError, AttributeError, TypeError):
        return True, "unparseable / missing sentinel"
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    interval = timedelta(days=interval_days)
    if now - last >= interval:
        return True, f"overdue (last={last_raw})"
    return False, f"not due (next in {(last + interval - now).days}d)"


def _stamp_sentinel(workspace: Path, now: datetime, result: dict[str, Any]) -> None:
    sentinel = _sentinel_path(workspace)
    payload = {
        "last_run_at": now.isoformat(timespec="seconds"),
        "last_decision": result.get("decision"),
        "last_run_id": result.get("run_id"),
    }
    try:
        _state_dir(workspace).mkdir(parents=True, exist_ok=True)
        tmp = sentinel.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(sentinel))
    except OSError as exc:
        logger.warning("fit_review: sentinel stamp failed: %s", exc)


def run_if_due(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    now: datetime | None = None,
    interval_days: int = DEFAULT_CADENCE_DAYS,
    network: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the pass iff the weekly sentinel says it's due (or ``force``).

    Called by the bot-side Tier-3 audit tick (``app_audit_runner``) so the Fit
    Reviewer rides the existing hourly schedule with NO new launchd job — the
    sentinel gates it to weekly. Always stamps the sentinel after a run so even a
    non-emitting bot is only re-targeted weekly.
    """
    now = now or datetime.now(timezone.utc)
    if not force:
        due, reason = _is_due(workspace, now, interval_days)
        if not due:
            return {"ran": False, "reason": reason}
    result = run_fit_review_for_bot(
        workspace, bot_id=bot_id, shared_dir=shared_dir, now=now, network=network
    )
    _stamp_sentinel(workspace, now, result)
    return {"ran": True, **result}


# ─────────────────────────────────────────────────────────────────────────────
# CLI (manual / proof-run surface)
# ─────────────────────────────────────────────────────────────────────────────


def _human(result: dict[str, Any]) -> str:
    lines = [f"decision: {result.get('decision', result.get('reason', '?'))}"]
    if result.get("ran") is False:
        return f"skipped (cadence): {result.get('reason')}"
    if result.get("wrote_candidate"):
        lines.append(f"  wrote candidate → {result.get('outbox_path')}")
        lines.append(f"  pkg_id: {result.get('suggested_gallery_pkg_id')}")
        lines.append(f"  value tier: {result.get('value_tier')}")
    else:
        lines.append(f"  reason: {result.get('reason')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fit Reviewer — Bite 3 in-bot reflection runner."
    )
    ap.add_argument("--bot-id", required=True)
    ap.add_argument("--shared-dir", type=Path, default=CANONICAL_SHARED_DIR)
    ap.add_argument("--network", type=Path, default=None, help="network.json path")
    ap.add_argument(
        "--force",
        action="store_true",
        help="ignore the weekly cadence sentinel (manual 'review now')",
    )
    ap.add_argument("--json", action="store_true", help="emit the result as JSON")
    args = ap.parse_args(argv)

    workspace = _bot_workspace()
    network = _load_network(args.shared_dir, args.network)
    result = run_if_due(
        workspace,
        bot_id=args.bot_id,
        shared_dir=args.shared_dir,
        network=network,
        force=args.force,
    )
    print(json.dumps(result, indent=2) if args.json else _human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
