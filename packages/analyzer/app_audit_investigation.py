#!/usr/bin/env python3
"""
app_audit_investigation.py — Bot-side investigation runner for ``evo fail``.

Workstream C of the audit-extensions sprint. Spec:
internal/spec-audit-extensions-2026-05-17.md §5.

When a user types ``evo fail <description>`` in their bot's messaging
thread, the admin handler writes an ``investigation-<id>.json`` request
to the bot's audit_inbox/ and kicks the runner. The runner's inbox
dispatcher routes ``kind == "investigation"`` to this module, which
runs a two-stage investigation:

  * **Stage 1 — Triage**: read the user's description, recent signals
    (24h), recent trail entries from app/skill/provider audits, and
    watchdog events. Identify the most-likely culprit element (app,
    skill, provider, scheduled_action, infrastructure). Produce a
    candidate list with confidence scores.

  * **Stage 2 — Diagnosis**: run a focused audit on the top candidate
    framed by the user's complaint. Produce a plain-language diagnosis
    + suggested fix + confidence.

The runner then writes one outbox record of kind
``investigation_diagnosis`` that the admin's poller routes to the
user's notification queue (NOT a Proposal — direct-reply design).
A no-diagnosis result still emits ``investigation_diagnosis`` with
``diagnosis: null``; the reply template renders the §5.4 form.

The escalation path (``evo fail flag``) flows through a separate
record kind ``investigation_unresolved``, written admin-side, that
DOES become a Proposal in the arbiter for operator review.

No auto-fix; this module never mutates anything outside the trail
and outbox.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _iso_now


logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────────


# Element-type vocabulary for triage. The triage LLM picks one of these
# (or null when it can't pick). The diagnosis stage branches on this to
# decide what context to assemble.
VALID_ELEMENT_TYPES = (
    "app",
    "skill",
    "provider",
    "scheduled_action",
    "infrastructure",
)

# Triage confidence buckets — same vocabulary as the spec.
VALID_CONFIDENCE = ("low", "medium", "high")

# Window for recent-signal lookup.
SIGNAL_WINDOW_HOURS = 24

# Caps to keep LLM context small and predictable.
_MAX_SIGNALS = 30
_MAX_TRAIL_ENTRIES_PER_ELEMENT = 8
_MAX_WATCHDOG_LINES = 30
_MAX_MANIFEST_FILES = 25
_MAX_FILE_BYTES = 12_000

# Timeouts mirror the audit_tier3 module — investigation is similarly LLM-
# bound and we don't want a stuck call holding the lockfile forever.
_STAGE_1_TIMEOUT_S = 240
_STAGE_2_TIMEOUT_S = 480


# ── Data shapes ─────────────────────────────────────────────────────────────


@dataclass
class TriageCandidate:
    """One row of Stage 1 triage output."""
    element_type: str       # one of VALID_ELEMENT_TYPES
    element_id: str         # e.g. app-id, skill name, provider name
    confidence: str         # one of VALID_CONFIDENCE
    justification: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TriageResult:
    """Stage 1 output as a whole."""
    candidates: list[TriageCandidate] = field(default_factory=list)
    top_candidate: TriageCandidate | None = None
    rationale: str = ""
    tokens_used: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "top_candidate": self.top_candidate.to_dict() if self.top_candidate else None,
            "rationale": self.rationale,
            "tokens_used": self.tokens_used,
            "error": self.error,
        }


@dataclass
class Diagnosis:
    """Stage 2 output: a plain-language explanation + fix proposal."""
    diagnosis: str | None       # None for the no-diagnosis path
    suggested_fix: str = ""
    confidence: str = "low"     # low | medium | high
    evidence: list[str] = field(default_factory=list)
    what_i_checked: list[str] = field(default_factory=list)
    tokens_used: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InvestigationOutput:
    """Aggregate Workstream-C investigation result."""
    investigation_id: str
    bot_id: str
    user_description: str
    requesting_user: str
    requested_at: str
    started_at: str
    completed_at: str
    triage: TriageResult
    diagnosis: Diagnosis
    chosen_candidate: TriageCandidate | None = None
    related_signal_ids: list[str] = field(default_factory=list)
    status: str = "ok"          # ok | no_diagnosis | failed
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "bot_id": self.bot_id,
            "user_description": self.user_description,
            "requesting_user": self.requesting_user,
            "requested_at": self.requested_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "triage": self.triage.to_dict(),
            "diagnosis": self.diagnosis.to_dict(),
            "chosen_candidate":
                self.chosen_candidate.to_dict() if self.chosen_candidate else None,
            "related_signal_ids": list(self.related_signal_ids),
            "status": self.status,
            "error": self.error,
        }


# ── Context assembly ────────────────────────────────────────────────────────


def gather_recent_signals(
    *, shared_dir: Path, bot_id: str, hours: int = SIGNAL_WINDOW_HOURS,
) -> tuple[list[dict], list[str]]:
    """Return (signal_summaries, signal_ids) from the last *hours* hours.

    Reads firing + snoozed Signals for this bot, plus today's signal log
    if available. Best-effort: missing shared_dir or unreadable files
    yields an empty list rather than crashing the investigation.
    """
    summaries: list[dict] = []
    signal_ids: list[str] = []
    seen_ids: set[str] = set()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    # Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    # Phase B): firing + snoozed Signals come from
    # ``signals.store.iter_signals``. When the store is unavailable (test
    # context, fresh bot, broken PYTHONPATH), fall back to raw file
    # iteration so we still get something for the LLM.
    try:
        from signals import store as signals_store  # type: ignore
    except Exception:
        signals_store = None  # type: ignore

    def _iter_signal_dicts():
        """Yield (signal_dict, fallback_stem) for firing+snoozed signals."""
        if signals_store is not None:
            # Materialize via the store API first so a mid-iteration error
            # doesn't half-yield before the raw-read fallback runs.
            try:
                via_store = [
                    (sig.to_dict(), sig.id)
                    for sig in signals_store.iter_signals(
                        shared_dir, subdirs=("firing", "snoozed")
                    )
                ]
            except Exception:
                via_store = None
            if via_store is not None:
                yield from via_store
                return
        # Fallback: store unavailable / unreadable — read subdirs directly.
        firing_dir = shared_dir / "signals" / "firing"  # store-access-lint: analyzer-unavailable fallback
        snoozed_dir = shared_dir / "signals" / "snoozed"  # store-access-lint: analyzer-unavailable fallback
        for d in (firing_dir, snoozed_dir):
            if not d.exists():
                continue
            try:
                files = sorted(d.glob("*.json"))
            except OSError:
                continue
            for f in files:
                try:
                    raw = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(raw, dict):
                    yield raw, f.stem

    for sig, fallback_stem in _iter_signal_dicts():
        # Bot-scoped or scope=pod relevant
        sig_bot = sig.get("bot_id")
        if sig_bot and sig_bot != bot_id and sig.get("scope") not in ("pod", "integration"):
            continue
        sid = sig.get("id") or fallback_stem
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        signal_ids.append(sid)
        summaries.append({
            "id": sid,
            "producer": sig.get("producer"),
            "type": sig.get("type"),
            "severity": sig.get("severity"),
            "title": sig.get("title"),
            "body": (sig.get("body") or "")[:200],
            "first_seen": sig.get("first_seen"),
            "state": sig.get("state"),
        })
        if len(summaries) >= _MAX_SIGNALS:
            break

    # Today's signal log (newly created + transitions within the window)
    if len(summaries) < _MAX_SIGNALS:
        today = now.strftime("%Y-%m-%d")
        log_path = shared_dir / "signals" / "log" / f"{today}.jsonl"
        if log_path.exists():
            try:
                for ln in log_path.read_text().splitlines()[-_MAX_SIGNALS * 2:]:
                    try:
                        entry = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    sid = entry.get("signal_id") or entry.get("id")
                    if not sid or sid in seen_ids:
                        continue
                    ts_raw = entry.get("ts") or ""
                    try:
                        ts = datetime.fromisoformat(
                            ts_raw.replace("Z", "+00:00")
                        )
                    except (ValueError, AttributeError):
                        ts = now
                    if ts < cutoff:
                        continue
                    seen_ids.add(sid)
                    signal_ids.append(sid)
                    summaries.append({
                        "id": sid,
                        "log_entry": entry,
                    })
                    if len(summaries) >= _MAX_SIGNALS:
                        break
            except OSError:
                pass

    return summaries, signal_ids


def gather_recent_trail_entries(workspace: Path) -> dict[str, list[dict]]:
    """Walk app/skill/provider trail.jsonl files; return last-N per element.

    The return shape is keyed by ``"app/<id>"``, ``"skill/<id>"``,
    ``"provider/<id>"`` so the triage prompt can show recent activity
    grouped by element.
    """
    out: dict[str, list[dict]] = {}
    evolve_dir = workspace / "evolve"
    if not evolve_dir.exists():
        return out

    for kind, sub in (
        ("app", "audits"),
        ("skill", "skill_audits"),
        ("provider", "provider_audits"),
    ):
        root = evolve_dir / sub
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for ed in entries:
            if not ed.is_dir() or ed.name.startswith("_") or ed.name.startswith("."):
                continue
            trail = ed / "trail.jsonl"
            if not trail.exists():
                continue
            try:
                lines = trail.read_text().splitlines()[-_MAX_TRAIL_ENTRIES_PER_ELEMENT:]
            except OSError:
                continue
            parsed: list[dict] = []
            for ln in lines:
                try:
                    parsed.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
            if parsed:
                out[f"{kind}/{ed.name}"] = parsed
    return out


def gather_recent_watchdog_events(
    *, shared_dir: Path, bot_id: str,
) -> list[dict]:
    """Tail today's watchdog JSONL — filtered to this bot. Best-effort."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = shared_dir / "watchdog" / f"{today}.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()[-_MAX_WATCHDOG_LINES * 2:]
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines:
        try:
            entry = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("bot_id") and entry.get("bot_id") != bot_id:
            continue
        out.append(entry)
        if len(out) >= _MAX_WATCHDOG_LINES:
            break
    return out


def _list_manifests(workspace: Path) -> list[dict]:
    """Return parsed manifests under the bot workspace, best-effort."""
    out: list[dict] = []
    md = workspace / "manifests"
    if not md.exists():
        return out
    try:
        files = sorted(md.iterdir())
    except OSError:
        return out
    for f in files:
        if f.suffix != ".json" or f.name.startswith("_") or f.name.startswith("."):
            continue
        try:
            data = json.loads(f.read_text())
            if isinstance(data, dict):
                out.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _find_manifest_for_app(workspace: Path, app_id: str) -> dict | None:
    for m in _list_manifests(workspace):
        if m.get("id") == app_id:
            return m
    return None


# ── Triage (Stage 1) ────────────────────────────────────────────────────────


_STAGE_1_SYSTEM = """You are an internal failure-investigation triage agent for a bot called {bot_id}.

A user has reported a failure in conversation. Your job is to identify the
single most likely element that caused it.

You have access to:
  - The user's description (verbatim, as they typed it).
  - Recent signals from the last 24 hours (failed/partial monitors, anomalies).
  - Recent audit trail entries for apps, skills, and providers on this bot.
  - Recent watchdog events for this bot.
  - The bot's app manifests (description + identity + scheduled_actions[] when
    schema v13 is populated).

Pick from these element types:
  - app                — one of the installed applications on this bot
  - skill              — a per-bot skill (gmail, calendar, slack, etc.)
  - provider           — an OAuth provider (google, slack, etc.)
  - scheduled_action   — a heartbeat/cron-fired routine (manifest v13)
  - infrastructure     — pod-level infra (daemons, ACLs, network.json)

For each plausible candidate, return a confidence score (low / medium /
high) and one-sentence justification. Then pick ONE top_candidate — the
element you'd investigate first.

If no candidate is plausible (the description doesn't map to anything you
can see), return top_candidate: null and explain in rationale.

Output ONLY this JSON shape — no prose, no markdown fences:
{{
  "candidates": [
    {{"element_type": "...", "element_id": "...",
      "confidence": "low|medium|high", "justification": "..."}}
  ],
  "top_candidate": {{
    "element_type": "...", "element_id": "...",
    "confidence": "low|medium|high", "justification": "..."
  }} OR null,
  "rationale": "one paragraph naming what tipped you toward the top pick"
}}
"""


def build_stage_1_inputs(
    *,
    user_description: str,
    requested_at: str,
    bot_id: str,
    workspace: Path,
    shared_dir: Path,
) -> tuple[dict, list[str]]:
    """Assemble the triage stage's user-message payload.

    Returns ``(payload_dict, related_signal_ids)``. The signal IDs are
    captured so the investigation record can carry them; the payload is
    what the LLM sees.
    """
    signals, signal_ids = gather_recent_signals(
        shared_dir=shared_dir, bot_id=bot_id,
    )
    trail = gather_recent_trail_entries(workspace)
    watchdog = gather_recent_watchdog_events(
        shared_dir=shared_dir, bot_id=bot_id,
    )

    manifests = _list_manifests(workspace)
    manifest_summary = []
    for m in manifests:
        if m.get("status") in ("hidden", "dormant", "deprecated"):
            continue
        entry = {
            "id": m.get("id"),
            "display_name": m.get("display_name") or m.get("name"),
            "description": (m.get("description") or "")[:200],
            "purpose": ((m.get("identity") or {}).get("purpose") or "")[:200],
        }
        # Schema v13 — fall back gracefully when the scanner hasn't re-run.
        sched = m.get("scheduled_actions") or []
        if isinstance(sched, list) and sched:
            entry["scheduled_actions"] = [
                {
                    "id": a.get("id"),
                    "trigger": a.get("trigger"),
                    "summary": a.get("summary"),
                }
                for a in sched if isinstance(a, dict)
            ][:5]
        heartbeat = m.get("heartbeat_evidence")
        if isinstance(heartbeat, dict):
            entry["heartbeat_evidence"] = heartbeat
        manifest_summary.append(entry)

    payload = {
        "user_description": user_description,
        "requested_at": requested_at,
        "bot_id": bot_id,
        "recent_signals": signals,
        "recent_trail_entries": trail,
        "recent_watchdog": watchdog,
        "manifests": manifest_summary,
    }
    return payload, signal_ids


def parse_triage_output(raw: str) -> TriageResult:
    """Parse the Stage-1 LLM response into a TriageResult.

    Tolerant: malformed JSON or missing fields yield empty candidates +
    a "raw" rationale fragment so the investigation can still complete
    with the no-diagnosis path. We never raise from this function.
    """
    text = (raw or "").strip()
    # Strip code fences if the LLM ignored the prompt.
    text = re.sub(r"^```(?:json)?", "", text)
    text = re.sub(r"```$", "", text).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return TriageResult(
            candidates=[],
            top_candidate=None,
            rationale=f"(triage output not parseable; raw head={text[:200]!r})",
            error="triage_output_not_json",
        )
    if not isinstance(obj, dict):
        return TriageResult(
            candidates=[],
            top_candidate=None,
            rationale="(triage output was not an object)",
            error="triage_output_not_object",
        )

    candidates: list[TriageCandidate] = []
    for c in obj.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        et = (c.get("element_type") or "").strip().lower()
        if et not in VALID_ELEMENT_TYPES:
            continue
        conf = (c.get("confidence") or "").strip().lower()
        if conf not in VALID_CONFIDENCE:
            conf = "low"
        candidates.append(TriageCandidate(
            element_type=et,
            element_id=(c.get("element_id") or "").strip()[:120],
            confidence=conf,
            justification=(c.get("justification") or "")[:300],
        ))

    top_raw = obj.get("top_candidate")
    top: TriageCandidate | None = None
    if isinstance(top_raw, dict):
        et = (top_raw.get("element_type") or "").strip().lower()
        if et in VALID_ELEMENT_TYPES:
            conf = (top_raw.get("confidence") or "").strip().lower()
            if conf not in VALID_CONFIDENCE:
                conf = "low"
            top = TriageCandidate(
                element_type=et,
                element_id=(top_raw.get("element_id") or "").strip()[:120],
                confidence=conf,
                justification=(top_raw.get("justification") or "")[:300],
            )

    return TriageResult(
        candidates=candidates,
        top_candidate=top,
        rationale=(obj.get("rationale") or "")[:1000],
    )


# ── Diagnosis (Stage 2) ─────────────────────────────────────────────────────


_STAGE_2_SYSTEM = """You are the diagnosis stage of an internal failure-investigation agent.

Stage 1 identified a top candidate for the user's reported failure. Your
job is to diagnose it: produce a plain-language explanation suitable
for showing the user directly in their messaging thread, plus a concrete
suggested fix and a confidence score.

Guidelines:
  - **Plex-test the diagnosis.** Write it for someone who installs Plex
    and runs Home Assistant; no jargon, no internal terms.
  - **Be honest about confidence.** If you can't be sure, return
    diagnosis: null and explain what you checked. A confident wrong
    diagnosis is worse than an honest "I couldn't pinpoint one cause."
  - **Cite evidence.** File paths, signal IDs, trail timestamps,
    config keys. Specifics, not "the system seems off."
  - **Propose ONE concrete fix.** A user can act on one step;
    "investigate further" is not a fix.

Output ONLY this JSON shape:
{{
  "diagnosis": "one or two short paragraphs, plain language, OR null",
  "suggested_fix": "one concrete action the user/operator can take",
  "confidence": "low|medium|high",
  "evidence": ["file:line", "signal:abc12345", ...],
  "what_i_checked": ["bullet 1", "bullet 2", ...]
}}

No prose, no markdown fences.
"""


def build_stage_2_inputs(
    *,
    user_description: str,
    chosen: TriageCandidate,
    bot_id: str,
    workspace: Path,
    shared_dir: Path,
    related_signal_ids: list[str],
) -> dict:
    """Assemble the diagnosis stage's user-message payload.

    Strategy: gather everything we have for the chosen element. For
    apps, that means the manifest + the trail tail + a sample of files.
    For skills/providers, the trail tail + recent failure signals.
    For scheduled_action, the parent app's manifest with the scheduled-
    action entry highlighted. For infrastructure, the relevant infra
    state we can probe.
    """
    payload: dict[str, Any] = {
        "user_description": user_description,
        "bot_id": bot_id,
        "chosen_candidate": chosen.to_dict(),
        "related_signal_ids": related_signal_ids,
    }

    if chosen.element_type == "app":
        manifest = _find_manifest_for_app(workspace, chosen.element_id)
        if manifest is not None:
            payload["element_details"] = {
                "manifest": _trim_manifest(manifest),
                "trail_tail": _trail_tail_for_app(workspace, chosen.element_id),
                "code_sample": _code_sample_for_app(workspace, manifest),
            }
        else:
            payload["element_details"] = {
                "manifest": None,
                "note": f"no manifest found for app {chosen.element_id!r}",
            }
    elif chosen.element_type in ("skill", "provider"):
        sub = "skill_audits" if chosen.element_type == "skill" else "provider_audits"
        element_dir = workspace / "evolve" / sub / chosen.element_id
        payload["element_details"] = {
            "trail_tail": _trail_tail_at(element_dir / "trail.jsonl"),
            "accepted": _read_json_or_none(element_dir / "accepted.json"),
        }
    elif chosen.element_type == "scheduled_action":
        # Search every manifest for a scheduled_actions[] entry matching id.
        match: dict | None = None
        owning_app: str | None = None
        for m in _list_manifests(workspace):
            for sa in m.get("scheduled_actions") or []:
                if isinstance(sa, dict) and sa.get("id") == chosen.element_id:
                    match = sa
                    owning_app = m.get("id")
                    break
            if match is not None:
                break
        payload["element_details"] = {
            "scheduled_action": match,
            "owning_app": owning_app,
        }
    elif chosen.element_type == "infrastructure":
        # No bot-side state to gather; LLM works from the user description
        # plus the triage rationale.
        payload["element_details"] = {
            "note": (
                "Pod infrastructure — bot-side investigation has limited "
                "visibility. Likely candidates: daemons, ACLs, sudoers, "
                "network.json schema, repo-puller."
            ),
        }
    else:
        payload["element_details"] = {"note": "(unknown element type)"}

    return payload


def _trim_manifest(manifest: dict) -> dict:
    """Strip volatile fields so the prompt stays stable across runs."""
    skip = {
        "last_audit", "last_structural_verify", "last_verification",
        "last_test_run", "last_test_output", "last_test_exit_code",
        "improvement_history", "install_job",
    }
    return {k: v for k, v in manifest.items() if k not in skip}


def _trail_tail_for_app(workspace: Path, app_id: str) -> list[dict]:
    return _trail_tail_at(
        workspace / "evolve" / "audits" / app_id / "trail.jsonl",
    )


def _trail_tail_at(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()[-30:]
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _code_sample_for_app(workspace: Path, manifest: dict) -> list[dict]:
    """Best-effort: read a few files referenced by the manifest."""
    out: list[dict] = []
    files = manifest.get("files") or []
    for rec in files[:_MAX_MANIFEST_FILES]:
        if not isinstance(rec, dict):
            continue
        path = (rec.get("path") or "").lstrip("/")
        if not path:
            continue
        # Skip data layer to keep prompt focused on code.
        if rec.get("layer") in ("data", "state"):
            continue
        full = workspace / path
        try:
            data = full.read_text(errors="replace")[:_MAX_FILE_BYTES]
        except (OSError, UnicodeDecodeError):
            continue
        out.append({
            "path": path,
            "purpose": rec.get("purpose", ""),
            "content": data,
        })
        if len(out) >= 5:
            break
    return out


def _read_json_or_none(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def parse_diagnosis_output(raw: str) -> Diagnosis:
    """Parse Stage-2 LLM response. Tolerant of malformed shapes."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?", "", text)
    text = re.sub(r"```$", "", text).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return Diagnosis(
            diagnosis=None,
            suggested_fix="",
            confidence="low",
            what_i_checked=[f"(diagnosis output not parseable; head={text[:200]!r})"],
            error="diagnosis_output_not_json",
        )
    if not isinstance(obj, dict):
        return Diagnosis(
            diagnosis=None,
            error="diagnosis_output_not_object",
        )

    diagnosis = obj.get("diagnosis")
    if diagnosis is not None and not isinstance(diagnosis, str):
        diagnosis = str(diagnosis)
    if isinstance(diagnosis, str):
        diagnosis = diagnosis.strip() or None

    confidence = (obj.get("confidence") or "").strip().lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    suggested = (obj.get("suggested_fix") or "").strip()
    evidence = [
        str(e)[:200] for e in (obj.get("evidence") or []) if e
    ]
    checked = [
        str(e)[:200] for e in (obj.get("what_i_checked") or []) if e
    ]
    return Diagnosis(
        diagnosis=diagnosis,
        suggested_fix=suggested[:500],
        confidence=confidence,
        evidence=evidence[:10],
        what_i_checked=checked[:10],
    )


# ── Top-level orchestrator ──────────────────────────────────────────────────


def run_investigation(
    *,
    investigation_id: str,
    bot_id: str,
    workspace: Path,
    shared_dir: Path,
    user_description: str,
    requesting_user: str,
    requested_at: str,
    dispatch_fn=None,
) -> InvestigationOutput:
    """Run the two-stage investigation end-to-end.

    ``dispatch_fn`` is the LLM dispatcher. Signature::
        dispatch_fn(system_prompt: str, user_message: str, *, timeout_s: int)
            -> tuple[str, int, str]   # (text, tokens, error)

    When ``None``, we lazy-import ``app_audit_tier3._dispatch_via_oc``
    (the same dispatcher Tier 3 uses). Tests inject a stub.
    """
    started_at = _iso_now()

    if dispatch_fn is None:
        try:
            from app_audit_tier3 import _dispatch_via_oc as _real_dispatch
            dispatch_fn = _real_dispatch
        except Exception as exc:
            return InvestigationOutput(
                investigation_id=investigation_id,
                bot_id=bot_id,
                user_description=user_description,
                requesting_user=requesting_user,
                requested_at=requested_at,
                started_at=started_at,
                completed_at=_iso_now(),
                triage=TriageResult(error=f"dispatch_import_failed: {exc}"),
                diagnosis=Diagnosis(diagnosis=None, error="dispatch_unavailable"),
                status="failed",
                error=str(exc),
            )

    # ── Stage 1 ─────────────────────────────────────────────────────────
    triage_inputs, signal_ids = build_stage_1_inputs(
        user_description=user_description,
        requested_at=requested_at,
        bot_id=bot_id,
        workspace=workspace,
        shared_dir=shared_dir,
    )
    stage1_sys = _STAGE_1_SYSTEM.format(bot_id=bot_id)
    stage1_user = json.dumps(triage_inputs, indent=2, default=str)
    triage = TriageResult()
    try:
        text, tokens, err = dispatch_fn(
            stage1_sys, stage1_user, timeout_s=_STAGE_1_TIMEOUT_S,
        )
        triage = parse_triage_output(text)
        triage.tokens_used = tokens
        if err:
            triage.error = err
    except Exception as exc:
        triage = TriageResult(error=f"stage1_dispatch_failed: {exc}")

    chosen = triage.top_candidate

    # ── Stage 2 ─────────────────────────────────────────────────────────
    if chosen is None or (triage.error and not triage.candidates):
        diagnosis = Diagnosis(
            diagnosis=None,
            confidence="low",
            what_i_checked=_default_what_i_checked(triage, signal_ids),
            error=triage.error,
        )
        status = "no_diagnosis"
        completed_at = _iso_now()
        return InvestigationOutput(
            investigation_id=investigation_id,
            bot_id=bot_id,
            user_description=user_description,
            requesting_user=requesting_user,
            requested_at=requested_at,
            started_at=started_at,
            completed_at=completed_at,
            triage=triage,
            diagnosis=diagnosis,
            chosen_candidate=None,
            related_signal_ids=signal_ids,
            status=status,
        )

    diag_inputs = build_stage_2_inputs(
        user_description=user_description,
        chosen=chosen,
        bot_id=bot_id,
        workspace=workspace,
        shared_dir=shared_dir,
        related_signal_ids=signal_ids,
    )
    stage2_user = json.dumps(diag_inputs, indent=2, default=str)
    diagnosis = Diagnosis(diagnosis=None)
    try:
        text, tokens, err = dispatch_fn(
            _STAGE_2_SYSTEM, stage2_user, timeout_s=_STAGE_2_TIMEOUT_S,
        )
        diagnosis = parse_diagnosis_output(text)
        diagnosis.tokens_used = tokens
        if err and not diagnosis.error:
            diagnosis.error = err
    except Exception as exc:
        diagnosis = Diagnosis(
            diagnosis=None,
            confidence="low",
            what_i_checked=_default_what_i_checked(triage, signal_ids),
            error=f"stage2_dispatch_failed: {exc}",
        )

    if diagnosis.diagnosis is None or not diagnosis.diagnosis.strip():
        status = "no_diagnosis"
        if not diagnosis.what_i_checked:
            diagnosis.what_i_checked = _default_what_i_checked(triage, signal_ids)
    else:
        status = "ok"

    completed_at = _iso_now()
    return InvestigationOutput(
        investigation_id=investigation_id,
        bot_id=bot_id,
        user_description=user_description,
        requesting_user=requesting_user,
        requested_at=requested_at,
        started_at=started_at,
        completed_at=completed_at,
        triage=triage,
        diagnosis=diagnosis,
        chosen_candidate=chosen,
        related_signal_ids=signal_ids,
        status=status,
    )


def _default_what_i_checked(
    triage: TriageResult, signal_ids: list[str],
) -> list[str]:
    """Fallback bullet list when the LLM didn't fill what_i_checked."""
    out = [
        f"Recent signals on this bot ({len(signal_ids)} found in the last 24h)",
        "Recent audit trail entries for apps, skills, and providers",
        "Recent watchdog events",
    ]
    if triage.candidates:
        out.append(
            "Considered "
            + ", ".join(
                f"{c.element_type}:{c.element_id or '?'} ({c.confidence})"
                for c in triage.candidates[:4]
            )
        )
    return out


# ── Trail + outbox helpers (called from the runner) ────────────────────────


def render_investigation_trail_entry(out: InvestigationOutput) -> dict:
    """Render the investigation as one operator-facing trail.jsonl line.

    The trail entry is the durable structured record per spec §5.6
    deliverable 6. Operators can deep-link to it from a Proposal or
    pull it via the investigations history form.
    """
    triage_summary = []
    for c in out.triage.candidates:
        triage_summary.append({
            "element_type": c.element_type,
            "element_id": c.element_id,
            "confidence": c.confidence,
        })
    return {
        "ts": out.completed_at,
        "kind": "investigation",
        "investigation_id": out.investigation_id,
        "user_description": out.user_description,
        "requesting_user": out.requesting_user,
        "triage_candidates": triage_summary,
        "chosen_candidate":
            out.chosen_candidate.to_dict() if out.chosen_candidate else None,
        "evidence": out.diagnosis.evidence,
        "diagnosis": out.diagnosis.diagnosis,
        "suggested_fix": out.diagnosis.suggested_fix,
        "confidence": out.diagnosis.confidence,
        "status": out.status,
        "related_signal_ids": out.related_signal_ids,
        "tokens_total": out.triage.tokens_used + out.diagnosis.tokens_used,
        "error": out.error or out.diagnosis.error or out.triage.error,
    }


def render_outbox_record(
    out: InvestigationOutput, *, runner_version: str,
) -> dict:
    """Return the outbox record the admin's poller picks up.

    Kind is always ``investigation_diagnosis``; the poller routes both
    diagnosed and no-diagnosis outcomes here (one notification, two
    templates). The reply template branches on ``confidence`` and
    ``diagnosis is None``.
    """
    return {
        "kind": "investigation_diagnosis",
        "ts": out.completed_at,
        "runner_version": runner_version,
        "producer": "app_audit_investigation",
        "investigation_id": out.investigation_id,
        "bot_id": out.bot_id,
        "user_description": out.user_description,
        "requesting_user": out.requesting_user,
        "requested_at": out.requested_at,
        "completed_at": out.completed_at,
        "status": out.status,
        "diagnosis": out.diagnosis.diagnosis,
        "suggested_fix": out.diagnosis.suggested_fix,
        "confidence": out.diagnosis.confidence,
        "evidence": out.diagnosis.evidence,
        "what_i_checked": out.diagnosis.what_i_checked,
        "chosen_candidate":
            out.chosen_candidate.to_dict() if out.chosen_candidate else None,
        "triage_candidates": [c.to_dict() for c in out.triage.candidates],
        "related_signal_ids": out.related_signal_ids,
        "error": out.error or out.diagnosis.error or out.triage.error,
    }


# ── Notification template (admin-side renderer) ─────────────────────────────


def render_notification_detail(
    record: dict, *, is_pod_admin: bool, trail_link: str | None = None,
) -> str:
    """Render the in-thread reply body for an investigation_diagnosis record.

    Two templates per spec §5.6 deliverable 7:
      - High/medium confidence + diagnosis present → the diagnosed form.
      - Low confidence OR no diagnosis → the §5.4 honest "couldn't pinpoint"
        form, listing what was checked.

    The reply is short — 3-6 sentences max. Only pod admins see the
    trail link (regular users don't get UI links).
    """
    diagnosis = (record.get("diagnosis") or "").strip()
    confidence = (record.get("confidence") or "low").lower()
    suggested_fix = (record.get("suggested_fix") or "").strip()
    user_desc = (record.get("user_description") or "").strip()

    if diagnosis and confidence in ("medium", "high"):
        parts: list[str] = []
        parts.append("I checked.")
        # Echo the user's complaint so the reply lands as a clear answer.
        if user_desc:
            parts.append(f"On `{user_desc}`:")
        parts.append(diagnosis)
        if suggested_fix:
            parts.append(f"Suggested fix: {suggested_fix}")
        parts.append(
            "If this doesn't resolve it, reply `evo fail flag` to escalate "
            "to the operator."
        )
        if is_pod_admin and trail_link:
            parts.append(f"Full trail: {trail_link}")
        return "\n\n".join(parts)

    # No-diagnosis / low-confidence path.
    checked = record.get("what_i_checked") or []
    parts = [
        "I checked but couldn't pinpoint a single cause."
    ]
    if checked:
        bullets = "\n".join(f"  • {c}" for c in checked[:5])
        parts.append(f"Here's what I looked at:\n{bullets}")
    if suggested_fix:
        parts.append(f"One thing to try: {suggested_fix}")
    parts.append(
        "If you want me to flag this for the operator, reply `evo fail flag`."
    )
    if is_pod_admin and trail_link:
        parts.append(f"Full trail: {trail_link}")
    return "\n\n".join(parts)
