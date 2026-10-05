"""Coherence Pass C2 — implementation coherence (LLM) — PR 16.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §6.4.

Pass C2 folds into Tier 3's existing Stage 3a discovery prompt — no new
audit run, no new cost beyond the existing monthly cadence. This module
exposes:

- ``C2_PROMPT_SECTION`` — the text block to splice into the Tier 3
  discovery prompt
- ``parse_c2_findings`` — pulls C2-style findings out of the Tier 3
  triage output and normalizes them into the existing observation
  shape (``behavior_mismatch`` / ``manifest_mismatch``)

Findings flow through Stage 3b triage and into Proposals exactly like
every other Tier 3 finding — no new outbox shape.
"""

from __future__ import annotations

from typing import Any


# ── Prompt section spliced into Tier 3 Stage 3a ─────────────────────

# The exact wording from spec §6.4, framed as a "during discovery,
# also check…" instruction.
C2_PROMPT_SECTION = """\
## Coherence check (Pass C2)

For each ``scheduled_actions[*]`` entry in the manifest, read the
implementing code path and verify the implementation is consistent
with the declared inputs, outputs, summary, and trigger. Flag cases
where the code exists but doesn't actually accomplish the claim —
e.g., "sends daily briefing" but the script only logs to stdout, or
"summarizes the inbox" but the script never reads the inbox file.

Emit a finding with category ``behavior_mismatch`` when the script
runs but doesn't deliver the claimed behavior. Emit
``manifest_mismatch`` when the manifest's declared shape (input
kinds, output integration, trigger) contradicts what the code
actually does. Include the action ``id`` in the finding for
deduplication.

Be conservative — only flag when the mismatch is unambiguous. The
goal is to catch egregious lies between manifest and code, not to
quibble about wording.
"""


# Categories C2 produces (matches existing Tier 3 categories).
CATEGORY_BEHAVIOR_MISMATCH  = "behavior_mismatch"
CATEGORY_MANIFEST_MISMATCH  = "manifest_mismatch"

_C2_CATEGORIES = frozenset({
    CATEGORY_BEHAVIOR_MISMATCH,
    CATEGORY_MANIFEST_MISMATCH,
})


def parse_c2_findings(tier3_findings: list[dict]) -> list[dict]:
    """Pull C2-style findings out of a Tier 3 triage output list.

    Tier 3 emits a heterogeneous findings list (cost, audit, behavior,
    manifest, security…). C2 is the subset whose category is
    behavior_mismatch or manifest_mismatch.

    Returns the subset, unchanged in shape — callers route them to the
    same observation pipeline as other Tier 3 findings.
    """
    out: list[dict] = []
    for f in tier3_findings or []:
        if not isinstance(f, dict):
            continue
        category = (f.get("category") or "").strip().lower()
        if category in _C2_CATEGORIES:
            out.append(f)
    return out


def annotate_finding_as_c2(finding: dict) -> dict:
    """Stamp a finding as originating from Pass C2.

    Tier 3 may not naturally tag the origin pass; this lets callers
    mark a finding as C2 for downstream dashboards / debugging.
    Returns a shallow copy with ``coherence_pass: "C2"`` set.
    """
    out = dict(finding)
    out["coherence_pass"] = "C2"
    return out
