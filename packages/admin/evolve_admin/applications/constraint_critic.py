"""constraint_critic.py — LLM verifier that maps each declared constraint
to an implementation site.

Spec: internal/spec-forge-side-effects-2026-06-02.md §13.2. PR 6 of that
spec adds this as a critic-cycle check.

The 2026-06-02 audit on personal-bot ea-pack surfaced three findings
from one root cause: the bot LLM read the manifest's declarations
(``constraints.boundaries[0]``: "Fail silently with log entry",
``identity.scope_includes[1]``: "Configurable timing for all scheduled
behaviors via bot config") and the generated code quietly omitted
compliance. The smoke test only exercised happy paths and didn't
notice. The orphan-function check (§13.4) catches the smoking-gun
``ea_config()`` orphan; this critic catches the broader pattern of
declared-but-not-implemented constraints.

Two passes:

  1. **Extraction** — pure-Python collection of every item from
     ``manifest.constraints.boundaries[]``, ``manifest.constraints.safety[]``,
     and ``manifest.identity.scope_includes[]``. No LLM needed; this is
     just walking the manifest dict.

  2. **Verification** — for each extracted item, ask the LLM "point to
     the file:line that implements this, or say 'absent' / 'unclear'."
     Returns a list of findings. ``absent`` becomes a critic-cycle
     finding that routes back to the bot LLM.

The customization_guidance trick from PR 0's audit-calibration work is
reused: the build_spec's ``## Customization Guidance`` section is
injected into the prompt so the critic doesn't flag spec-blessed
deviations as drift.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


# Reuse the same extractor as the audit (PR 0). Importing here would
# create an admin → analyzer dep we'd rather avoid; pasting the
# 20-line regex+slice is cheaper than the coupling.
_CUSTOMIZATION_HEADING_RE = re.compile(
    r"^\s*#{2,}\s*Customization\s+(?:Guidance|Notes|Points)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_customization_guidance(build_spec: str) -> str:
    if not isinstance(build_spec, str) or not build_spec:
        return ""
    m = _CUSTOMIZATION_HEADING_RE.search(build_spec)
    if not m:
        return ""
    tail = build_spec[m.end():]
    next_h = re.search(r"^\s*##\s+\S", tail, re.MULTILINE)
    body = tail[: next_h.start()] if next_h else tail
    return (build_spec[m.start(): m.end()] + body).strip()


@dataclass
class ConstraintItem:
    """One declared constraint or scope clause to verify."""
    source: str   # "constraints.boundaries" | "constraints.safety" | "identity.scope_includes"
    index: int    # position in source list
    text: str     # the actual clause text


@dataclass
class ConstraintFinding:
    """One verdict on a ConstraintItem.

    Verdicts:
      enforced — the LLM pointed to a file:line that implements it
      absent   — no implementation found in the code; bot LLM omitted it
      unclear  — LLM can't tell from the code alone (e.g. system-level
                 or runtime-environmental constraint)
    """
    source: str
    index: int
    text: str
    verdict: str                # enforced | absent | unclear
    evidence: str = ""          # file:line + one-line justification
    severity: str = "should-fix"  # critic-cycle severity (passed back to bot LLM)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "index": self.index,
            "text": self.text,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "severity": self.severity,
        }


# ── Extraction (pure) ───────────────────────────────────────────────────────


def extract_constraint_items(manifest: dict) -> list[ConstraintItem]:
    """Walk the manifest and produce the verification work list.

    Empty / missing sections are silently skipped. The three sources
    have slightly different semantics:

      ``constraints.boundaries[]``  — hard rules the implementation
        must respect (e.g. "atomic writes only", "fail silently when X")
      ``constraints.safety[]``      — safety invariants (e.g. "never
        delete tasks, mark cancelled instead")
      ``identity.scope_includes[]`` — features the app is supposed to
        offer (e.g. "configurable timing via bot config")

    All three are LLM-verifiable as "is there code that does this?"
    """
    items: list[ConstraintItem] = []
    constraints = manifest.get("constraints") or {}
    if isinstance(constraints, dict):
        for source_key in ("boundaries", "safety"):
            values = constraints.get(source_key) or []
            if isinstance(values, list):
                for idx, val in enumerate(values):
                    if isinstance(val, str) and val.strip():
                        items.append(ConstraintItem(
                            source=f"constraints.{source_key}",
                            index=idx,
                            text=val.strip(),
                        ))
    identity = manifest.get("identity") or {}
    if isinstance(identity, dict):
        scopes = identity.get("scope_includes") or []
        if isinstance(scopes, list):
            for idx, val in enumerate(scopes):
                if isinstance(val, str) and val.strip():
                    items.append(ConstraintItem(
                        source="identity.scope_includes",
                        index=idx,
                        text=val.strip(),
                    ))
    return items


# ── Verification (LLM-driven) ───────────────────────────────────────────────


_CRITIC_SYSTEM_PROMPT = """You are a senior engineer auditing whether a manifest's declared constraints \
and scope items are actually implemented in the generated code.

For each item in the input list, you must produce exactly one verdict:

  enforced — the code clearly implements this constraint. Point to a specific
             file and line range (e.g. "scripts/tasks.py:42-58") and state
             in one sentence WHY that code satisfies the constraint.

  absent   — you can find no code that implements this constraint. The bot
             LLM declared the constraint but skipped the implementation. This
             is the dominant failure mode the personal-bot ea-pack audit
             caught: ea_config() was defined but never called, "configurable
             timing" was hardcoded, "fail silently" was ignored.

  unclear  — the constraint is true-but-unverifiable from code alone (a
             system invariant like "uses Python 3.11+", a runtime promise
             like "completes in <2s", or a value judgment like "elegant").

CUSTOMIZATION GUIDANCE:
The input includes a ``customization_guidance`` field. When non-empty, it
quotes the build_spec's "## Customization Guidance" section — explicit
divergences from the canonical spec that this bot was BUILT to make
(category renames, default overrides, etc.). Do NOT flag a constraint
as `absent` if the only "violation" is a spec-blessed customization.

Output ONLY a JSON array, one entry per input item, same order:
[
  {
    "verdict": "enforced" | "absent" | "unclear",
    "evidence": "file:line + one-sentence justification (required for enforced)"
  },
  ...
]

No prose, no markdown fences. Just the JSON array."""


def verify_constraints(
    manifest: dict,
    implementation_files: dict[str, str],
    call_llm: Callable[[str, str], str],
    *,
    items: list[ConstraintItem] | None = None,
) -> list[ConstraintFinding]:
    """Verify every declared constraint / scope item has an implementation.

    Parameters
    ----------
    manifest
        Forge manifest dict. ``constraints.boundaries[]``,
        ``constraints.safety[]``, and ``identity.scope_includes[]`` are
        the verification sources.
    implementation_files
        Map of {workspace-relative path → text content}. The critic LLM
        searches these for the implementation site.
    call_llm
        Callable that takes ``(system_prompt, user_message)`` and returns
        the LLM's response text. Injected for testability — production
        wires this to forge_engine._call_llm with the resolved critic
        target (#3466: any credentialed provider).
    items
        Pre-computed work list (optional). When omitted,
        ``extract_constraint_items`` is called.

    Returns
    -------
    list[ConstraintFinding]
        One finding per input item. ``verdict == "absent"`` findings are
        the ones that block forge approval until the bot LLM addresses
        them; ``unclear`` items get logged but don't block; ``enforced``
        items are listed so operators can audit the critic's reasoning.

        Empty list when there are no constraints or scope items declared
        (and thus nothing to verify).
    """
    if items is None:
        items = extract_constraint_items(manifest)
    if not items:
        return []

    build_spec = manifest.get("build_spec") or ""
    customization = _extract_customization_guidance(build_spec)

    # Cap file payload size to avoid blowing the prompt budget. Each
    # file gets up to ~6 KB; bigger ones are head-truncated with a
    # marker. The critic looks for top-level shape; the tail is rarely
    # load-bearing for "is this constraint implemented?"
    _MAX_FILE_BYTES = 6000
    files_payload = {}
    for path, content in (implementation_files or {}).items():
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if len(content) > _MAX_FILE_BYTES:
            files_payload[path] = content[:_MAX_FILE_BYTES] + "\n# … [truncated]"
        else:
            files_payload[path] = content

    user_payload = json.dumps({
        "constraint_items": [
            {"source": it.source, "index": it.index, "text": it.text}
            for it in items
        ],
        "customization_guidance": customization,
        "files": files_payload,
    }, indent=2)

    try:
        response = call_llm(_CRITIC_SYSTEM_PROMPT, user_payload)
    except Exception as exc:
        # LLM call failed — surface as unclear for every item rather
        # than blocking the forge. The operator sees a structured error
        # in the critic findings and can re-run.
        err = f"constraint critic LLM call failed: {type(exc).__name__}: {exc}"
        return [
            ConstraintFinding(
                source=it.source, index=it.index, text=it.text,
                verdict="unclear", evidence=err, severity="info",
            )
            for it in items
        ]

    verdicts = _parse_verdict_array(response, expected=len(items))
    findings: list[ConstraintFinding] = []
    for it, v in zip(items, verdicts):
        verdict = (v.get("verdict") or "unclear").strip().lower()
        if verdict not in ("enforced", "absent", "unclear"):
            verdict = "unclear"
        evidence = (v.get("evidence") or "").strip()
        # "absent" findings carry critic-cycle weight: they block
        # approval until the bot LLM addresses them. "unclear" is
        # informational; "enforced" is positive evidence.
        severity = {
            "absent": "should-fix",
            "unclear": "info",
            "enforced": "info",
        }[verdict]
        findings.append(ConstraintFinding(
            source=it.source, index=it.index, text=it.text,
            verdict=verdict, evidence=evidence, severity=severity,
        ))
    return findings


# ── Privacy-block verifier (manifest-v7 Slice 2) ────────────────────────────
#
# Critique-phase check: "does the privacy block match what the blueprint
# actually collects" (internal/spec-manifest-v7-slicing-2026-06-10.md §4.1).
# Two directions, asymmetric severity:
#
#   undeclared_collection — the code persists/transmits user data the
#     privacy block doesn't declare. The dangerous direction (the consent
#     notice under-states what the app does) → "should-fix".
#
#   declared_not_found — privacy declares a collection no code performs.
#     Over-declaration is conservative (consent over-states) → "info",
#     listed so the operator can tidy the block.
#
# Advisory like the rest of Phase 2.5 — findings surface on the approval
# panel; they don't block.


@dataclass
class PrivacyBlockFinding:
    """One privacy-vs-implementation verdict.

    Kinds:
      declared_present      — declared item; code that collects it was found
      declared_not_found    — declared item; no collecting code found
      undeclared_collection — code collects user data the block omits
      unclear               — can't tell from code alone
    """
    kind: str
    text: str                 # the declared item, or a description of the undeclared collection
    evidence: str = ""        # file:line + one-line justification
    severity: str = "info"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "evidence": self.evidence,
            "severity": self.severity,
        }


_PRIVACY_CRITIC_SYSTEM_PROMPT = """You are a privacy auditor checking whether an app manifest's declared \
privacy block matches what the generated code actually collects.

"Collects" means: persists, logs, or transmits information that originates
from a user (message contents, names/identifiers, timestamps of user
actions, locations, health/financial details, files the user sends, …).
Internal app state that encodes no user-originated information (config,
retry counters, its own schedule) does NOT count.

You receive:
  declared_items        — the privacy.user_data_collected[] strings
  blueprint_and_spec    — what the app is supposed to do
  files                 — the implementation

Produce ONLY a JSON object:
{
  "declared_verdicts": [          // exactly one per declared item, same order
    {
      "verdict": "present" | "not_found" | "unclear",
      "evidence": "file:line + one sentence (required for present)"
    }, ...
  ],
  "undeclared_collections": [     // empty array when the block is honest
    {
      "description": "what user data the code collects that is NOT declared",
      "evidence": "file:line + one sentence"
    }, ...
  ]
}

Be conservative on undeclared_collections: report only collection you can
point at in the code, not speculation about what the app could do.
No prose, no markdown fences. Just the JSON object."""


def verify_privacy_block(
    manifest: dict,
    implementation_files: dict[str, str],
    call_llm: Callable[[str, str], str],
) -> list[PrivacyBlockFinding]:
    """Check the manifest's privacy{} block against the implementation.

    Returns one finding per declared ``privacy.user_data_collected`` item
    plus one ``undeclared_collection`` finding per collection the LLM can
    evidence in code that the block omits. Empty list when the manifest
    has no privacy block (absence = "not yet declared" — gating absent
    blocks is the validator's call, not the critic's).
    """
    privacy = manifest.get("privacy")
    if not isinstance(privacy, dict) or not privacy:
        return []
    declared = privacy.get("user_data_collected")
    declared_items = [
        s.strip() for s in declared if isinstance(s, str) and s.strip()
    ] if isinstance(declared, list) else []

    _MAX_FILE_BYTES = 6000
    files_payload: dict[str, str] = {}
    for path, content in (implementation_files or {}).items():
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if len(content) > _MAX_FILE_BYTES:
            files_payload[path] = content[:_MAX_FILE_BYTES] + "\n# … [truncated]"
        else:
            files_payload[path] = content

    blueprint = manifest.get("blueprint")
    user_payload = json.dumps({
        "declared_items": declared_items,
        "privacy_block": privacy,
        "blueprint_and_spec": {
            "blueprint": blueprint if isinstance(blueprint, dict) else {},
            "build_spec": manifest.get("build_spec") or "",
        },
        "files": files_payload,
    }, indent=2)

    try:
        response = call_llm(_PRIVACY_CRITIC_SYSTEM_PROMPT, user_payload)
    except Exception as exc:
        err = f"privacy critic LLM call failed: {type(exc).__name__}: {exc}"
        return [
            PrivacyBlockFinding(
                kind="unclear", text=item, evidence=err, severity="info",
            )
            for item in declared_items
        ]

    parsed = _parse_json_object(response)
    raw_verdicts = parsed.get("declared_verdicts")
    raw_verdicts = raw_verdicts if isinstance(raw_verdicts, list) else []
    raw_undeclared = parsed.get("undeclared_collections")
    raw_undeclared = raw_undeclared if isinstance(raw_undeclared, list) else []

    findings: list[PrivacyBlockFinding] = []
    for idx, item in enumerate(declared_items):
        v = raw_verdicts[idx] if idx < len(raw_verdicts) and isinstance(raw_verdicts[idx], dict) else {}
        verdict = (v.get("verdict") or "unclear").strip().lower()
        kind = {
            "present": "declared_present",
            "not_found": "declared_not_found",
        }.get(verdict, "unclear")
        findings.append(PrivacyBlockFinding(
            kind=kind,
            text=item,
            evidence=(v.get("evidence") or "").strip(),
            severity="info",
        ))
    for entry in raw_undeclared:
        if not isinstance(entry, dict):
            continue
        desc = (entry.get("description") or "").strip()
        if not desc:
            continue
        findings.append(PrivacyBlockFinding(
            kind="undeclared_collection",
            text=desc,
            evidence=(entry.get("evidence") or "").strip(),
            severity="should-fix",
        ))
    return findings


def _parse_json_object(response: str) -> dict:
    """Permissive JSON object extractor — object-shaped sibling of
    ``_parse_verdict_array``. Returns {} when no object can be parsed."""
    text = (response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            parsed = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_verdict_array(response: str, *, expected: int) -> list[dict]:
    """Permissive JSON array extractor — strips fenced blocks, hunts
    inside prose if the LLM forgot the "no preamble" instruction."""
    text = (response or "").strip()
    # Strip fenced blocks
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    # Try direct parse
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Hunt for the first [ ... ] in the text
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return [{} for _ in range(expected)]
        try:
            parsed = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return [{} for _ in range(expected)]
    if not isinstance(parsed, list):
        return [{} for _ in range(expected)]
    # Pad / truncate to expected length
    out = [v if isinstance(v, dict) else {} for v in parsed[:expected]]
    while len(out) < expected:
        out.append({})
    return out
