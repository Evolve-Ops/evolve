"""Coherence Pass C3 — capability check (LLM, on-demand) — PR 16.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5.

Pass C3 is the most expensive coherence pass. It asks:

    "Given only this manifest (no code), could a competent developer
    build something that accomplishes the stated goal?"

This is the only pass that runs without the code as input — it checks
whether the *design* itself makes sense.

## When it fires (spec §6.5)

1. **On charter change.** When ``description``, ``usage.how_to_use``,
   or ``success_criteria.observable_outcomes`` is edited, Pass C3
   runs against the new manifest before the change is persisted.
   This is the pre-deploy coherence gate for manifest edits.
2. **On forge approval.** Same gate, before forge builds an app from
   the manifest.
3. **On-demand.** Operator clicks "Check coherence" on the manifest
   view; runs Pass A + C3 together.

## Cost (spec §6.5)

~5k tokens per run. Rate-limited to 1 run per app per day to prevent
thrashing. The rate limit is enforced here as a pre-flight gate that
the LLM caller checks before spending tokens.

## Output

A one-shot finding with severity ``incoherent | feasible | unclear``
plus a one-paragraph rationale. Written to
``manifest.coherence.last_capability_check`` (NOT ``findings[]``,
which is the recurring-pass log).

This module exposes the prompt template, the rate-limit check, and
the output persistence helper — the LLM call itself is the caller's
responsibility (uses whatever bot-side LLM client; honors per-bot
inference doctrine).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# ── Severities (spec §6.5) ──────────────────────────────────────────

SEVERITY_INCOHERENT  = "incoherent"
SEVERITY_FEASIBLE    = "feasible"
SEVERITY_UNCLEAR     = "unclear"

_VALID_SEVERITIES = frozenset({
    SEVERITY_INCOHERENT, SEVERITY_FEASIBLE, SEVERITY_UNCLEAR,
})


# Rate limit: 1 run per app per day (spec §6.5).
DAILY_RUN_CAP = 1


# Charter fields that trigger C3 on edit (spec §6.5 #1).
CHARTER_FIELDS = frozenset({
    "description",
    "usage.how_to_use",
    "success_criteria.observable_outcomes",
})


# ── Prompt template ─────────────────────────────────────────────────

C3_SYSTEM_PROMPT = """\
You are checking whether an application manifest describes something
that could plausibly work. You have ONLY the manifest — no code, no
deployment, no runtime state. Your job is to decide whether the
manifest's stated goal could be achieved by a competent developer
working from this manifest alone.

Return a JSON object with:
  - severity: one of "incoherent", "feasible", "unclear"
  - rationale: one paragraph (under 300 words) explaining the verdict

"incoherent" means the manifest contradicts itself or claims something
no possible implementation could deliver (e.g., "summarize Telegram
messages" with no Telegram integration or read inputs declared, or
crons[*] that schedules an action that doesn't exist).

"feasible" means the manifest hangs together — the scope, declared
integrations, inputs, outputs, and triggers all align with the stated
goal.

"unclear" means the manifest is ambiguous or missing critical detail
to judge feasibility. Use this when the answer would need the code.

Be conservative on "incoherent" — only when the gap is obvious from
the manifest text alone.
"""

C3_USER_PROMPT_TEMPLATE = """\
Manifest:
```json
{manifest_json}
```

Decide: incoherent / feasible / unclear, and why."""


def build_user_prompt(manifest: dict) -> str:
    """Build the C3 user prompt. Trims manifest fields that don't help
    the design-level check (file SHAs, observation logs, coherence
    findings from prior passes)."""
    trimmed = {
        k: v for k, v in manifest.items()
        if k not in {
            "files", "volatile_paths",
            "coherence", "reconciliation",
            "provenance", "observation_log",
        }
    }
    return C3_USER_PROMPT_TEMPLATE.format(
        manifest_json=json.dumps(trimmed, indent=2),
    )


# ── Rate limit ───────────────────────────────────────────────────────

def is_rate_limited(
    manifest: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """Spec §6.5: 1 run per app per day. Reads
    ``manifest.coherence.last_capability_check.checked_at``.
    """
    coherence = manifest.get("coherence") or {}
    last = coherence.get("last_capability_check")
    if not isinstance(last, dict):
        return False
    checked_at = last.get("checked_at")
    if not isinstance(checked_at, str) or not checked_at:
        return False
    try:
        ts = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    n = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (n - ts) < timedelta(hours=24)


# ── Output persistence ──────────────────────────────────────────────

@dataclass
class CapabilityCheck:
    """The one-shot finding C3 produces."""
    severity: str
    rationale: str
    checked_at: str = ""
    triggered_by: str = ""

    def to_dict(self) -> dict:
        return {
            "severity":     self.severity,
            "rationale":    self.rationale,
            "checked_at":   self.checked_at,
            "triggered_by": self.triggered_by,
        }


def parse_llm_response(text: str) -> CapabilityCheck | None:
    """Parse the JSON response from the LLM into a CapabilityCheck.

    Returns None when the response is unparseable or the severity
    isn't one of the valid values — the caller can fall back to
    'unclear' or retry.
    """
    # Trim markdown fence if present.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Find the inner block.
        lines = cleaned.split("\n")
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1])
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].lstrip()
    try:
        obj = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    severity = (obj.get("severity") or "").strip().lower()
    if severity not in _VALID_SEVERITIES:
        return None
    rationale = (obj.get("rationale") or "").strip()
    if not rationale:
        return None
    return CapabilityCheck(severity=severity, rationale=rationale)


def write_capability_check(
    manifest: dict,
    check: CapabilityCheck,
) -> None:
    """Persist the capability check to ``manifest.coherence.last_capability_check``.

    Replaces any prior check (this is a one-shot, not append).
    """
    coherence = manifest.setdefault("coherence", {})
    coherence["last_capability_check"] = check.to_dict()


# ── Charter-change trigger detection (spec §6.5 #1) ─────────────────

def detect_charter_change(
    before: dict, after: dict,
) -> set[str]:
    """Return the set of charter fields that changed between two
    manifest snapshots. Spec §6.5 #1: changes to these fields trigger
    C3 before persistence.

    Path semantics: nested fields like ``usage.how_to_use`` are
    looked up by walking dotted segments.
    """
    changed: set[str] = set()
    for field in CHARTER_FIELDS:
        before_val = _get_nested(before, field)
        after_val = _get_nested(after, field)
        if before_val != after_val:
            changed.add(field)
    return changed


def _get_nested(obj: dict, dotted: str) -> Any:
    """Walk a dotted path through nested dicts. Returns None when any
    segment is missing or not a dict."""
    cur: Any = obj
    for seg in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def should_run_c3(
    *,
    before: dict | None = None,
    after: dict | None = None,
    trigger: str = "",
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether C3 should fire.

    Args:
        before: prior manifest (or None for forge approval / on-demand)
        after: new manifest (the one to be persisted)
        trigger: "charter_change" | "forge_approval" | "on_demand"
        now: clock for rate-limit check

    Returns:
        (should_run, reason) — reason is a short human-readable string.
    """
    if trigger not in {"charter_change", "forge_approval", "on_demand"}:
        return (False, f"unknown trigger {trigger!r}")
    if after is None:
        return (False, "no manifest to check")
    if is_rate_limited(after, now=now):
        return (False, "rate-limited (already ran within 24h)")
    if trigger == "charter_change":
        if before is None:
            return (False, "charter_change requires prior manifest")
        changed = detect_charter_change(before, after)
        if not changed:
            return (False, "no charter fields changed")
        return (True, f"charter fields changed: {sorted(changed)}")
    return (True, f"trigger={trigger}")
