"""edr.triage.dev_issue — firing ``dev_issue`` Signals → routed Proposals.

The EDR triage generator (design: internal/edr/design-edr-2026-06-11.md §6).
Per firing ``dev_issue`` Signal it writes ONE Proposal through the REAL
imported ``arbiter.store`` carrying classification, severity, the
agent-able-vs-human routing verdict, a target aspect, and the
``motivating_signals[]`` link back to the Signal. Pure Python — no LLM in
this bite (``rsi-low-cost-preference``: LLM is escalation, not default).

This module is a TIER INTERFACE — the first coordinator-tier function
(internal/edr/design-edr-tiered-oversight-2026-06-11.md §4 Move 1): the work
ledger IS the proposal store, so triaging a Signal *is* writing typed
coordination state. Every rule below lives in DATA tables at module top,
not branchy logic, so the mandate is inspectable.

Where the triage verdict lives in the stored Proposal (MACHINE-READABLE)
─────────────────────────────────────────────────────────────────────────
``Proposal.provenance.signals["triage"]``. ``Provenance.signals`` is the
schema's designated free-form, technique-specific evidence dict
(packages/analyzer/schema/provenance.py — "Keys and shapes vary by
technique"), so the verdict is a structured JSON object in the stored
record — no analyzer-schema change, nothing buried in prose. P1.3 (the
dispatch bridge) reads exactly this block::

    proposal.provenance.signals["triage"] == {
        "schema": 1,
        "classification": "bug" | "feature" | "noise" | "needs-classification",
        "severity": "info" | "warn" | "alert",   # the SIGNAL severity scale
        "agent_able": bool,                       # defaults to False (human)
        "route": "<aspect name>" | "unrouted (operator)",
        "proof_artifact": "<verbatim Proof: payload>" | None,
        "reason": "<why this agent-able verdict>",
        "issue_number": int | None,
        "issue_url": str,
        "labels": [str, ...],
    }

The human-facing mirror of the same verdict goes in ``problem`` /
``action.context`` / ``admin_surface_summary`` — prose for operators,
never the parse surface.

Classification rules (DATA: ``CLASSIFICATION_RULES``; first row whose
label-set intersects the issue's labels wins; default
``needs-classification`` → a human decides)
─────────────────────────────────────────────────────────────────────────
  noise    ← duplicate | wontfix | invalid   (highest precedence: an
             issue marked duplicate stays noise even if also labeled bug)
  bug      ← bug | regression
  feature  ← enhancement | feature

Severity rules (DATA: ``SEVERITY_RULES``; the SIGNAL severity scale
``info < warn < alert`` from packages/analyzer/schema/signal.py; first
matching row wins; conservative — default stays at the ``info`` floor)
─────────────────────────────────────────────────────────────────────────
  alert  ← security | critical
  warn   ← bug | regression
  info   ← everything else

Triage's severity verdict is recorded IN THE PROPOSAL (the triage block
+ the urgency mapping below). The Signal is never mutated — it keeps the
adapter's no-judgment ``severity="info"`` floor.

Proposal.urgency mapping (DATA: ``SEVERITY_TO_URGENCY`` + two documented
overrides)
─────────────────────────────────────────────────────────────────────────
  security label        → ``security_critical``   (overrides everything)
  classification noise  → ``hygiene``             (unless security)
  severity alert        → ``operational_urgent``
  severity warn | info  → ``improvement``

The agent-able-vs-human verdict — DEFAULTS TO HUMAN (design §6.1)
─────────────────────────────────────────────────────────────────────────
``agent_able=True`` ONLY when BOTH hold:
  (a) the issue carries the explicit label ``edr:agent-able`` — a HUMAN's
      judgment, per the R0→R1 autonomy rung (§5.3); and
  (b) the issue body contains a parseable proof-artifact declaration — a
      line starting ``Proof:`` with a non-empty payload (e.g.
      ``Proof: pytest packages/admin/tests/test_x.py::test_y goes
      red->green``), per the §5.2 proof-artifact discipline.

Label without a ``Proof:`` line → human, with reason "agent-able label
present but no falsifiable Proof: line". Everything else → human. No
heuristic guessing in v1: the agent-able envelope widens later from a
MEASURED landing rate (§5.3), not optimism. ``proof_artifact`` is the
first ``Proof:`` payload, verbatim (whitespace-trimmed), and is only
recorded on agent-able verdicts — it is the dispatch contract for P1.3.

Routing rules (DATA: ``ROUTING_RULES``; first row whose hint appears in
the issue's lowercased labels or title tokens wins; default
``unrouted (operator)``)
─────────────────────────────────────────────────────────────────────────
Aspect names mirror the registry table in internal/META-session-guide.md
(mirrored as data — the markdown is never parsed). Title tokens keep
internal hyphens ("multi-pod" is one token), so multi-word hints match
only deliberately.

Idempotency
─────────────────────────────────────────────────────────────────────────
The proposal id is DETERMINISTIC: ``edr-triage-gh-<issue number>``
(fallback ``edr-triage-sig-<signal id>`` for a dev_issue Signal that
lacks ``details["number"]``). ``run()`` checks ``find_proposal`` across
ALL subdirs first — re-runs write nothing, and a previously
rejected/archived proposal is NOT recreated (the human's verdict stands).

CLI
─────────────────────────────────────────────────────────────────────────
    python -m edr.triage --shared-dir <dir>

Processes all firing ``dev_issue`` Signals in the EDR-local store and
prints one summary line::

    triage: signals N, proposals_written M, skipped_existing K, agent_able A, human H

``agent_able`` / ``human`` count the verdicts on proposals WRITTEN THIS
RUN (skipped-existing proposals keep their stored verdicts and are not
re-counted). ``--shared-dir`` must be an EDR-local store — never a pod's
shared dir.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

# The analyzer is the installed evolve-analyzer package (flat layout), so
# its subpackages import by their bare names — the same modules the shipped
# admin server and daemons load. Import, never fork.
import arbiter.store as arbiter_store
import signals.store as signals_store
from schema.proposal import Investigation, Proposal, Provenance, RiskTag
from schema.signal import Signal

# ── Constants (identity) ─────────────────────────────────────────────────────

BOT_ID = "edr"  # EDR's domain is Evolve itself; no pod bot involved.
GENERATOR_ID = "edr_triage_dev_issue"
DIMENSION = "dev_remediation"
TECHNIQUE = "edr_triage"
SIGNAL_TYPE = "dev_issue"
TRIAGE_SCHEMA_VERSION = 1

AGENT_ABLE_LABEL = "edr:agent-able"
PROOF_LINE_PREFIX = "Proof:"

# ── DATA: classification (first matching row wins; see module docstring) ─────

CLASSIFICATION_RULES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"duplicate", "wontfix", "invalid"}), "noise"),
    (frozenset({"bug", "regression"}), "bug"),
    (frozenset({"enhancement", "feature"}), "feature"),
)
DEFAULT_CLASSIFICATION = "needs-classification"  # → a human decides

# ── DATA: severity on the signal scale (first matching row wins) ─────────────

SEVERITY_RULES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"security", "critical"}), "alert"),
    (frozenset({"bug", "regression"}), "warn"),
)
DEFAULT_SEVERITY = "info"  # conservative floor — same as the adapter's

# ── DATA: Proposal.urgency mapping ───────────────────────────────────────────

SECURITY_LABEL = "security"
SECURITY_URGENCY = "security_critical"
NOISE_URGENCY = "hygiene"
SEVERITY_TO_URGENCY: dict[str, str] = {
    "alert": "operational_urgent",
    "warn": "improvement",
    "info": "improvement",
}

# ── DATA: provenance confidence per classification outcome ──────────────────
# Deterministic label-matching is high-confidence when a rule fired; the
# fall-through bucket is explicitly low-confidence (a human classifies).

CLASSIFICATION_CONFIDENCE: dict[str, float] = {
    DEFAULT_CLASSIFICATION: 0.4,
}
DEFAULT_CONFIDENCE = 0.9

# ── DATA: routing (first matching row wins; hints are single tokens —
#    internal hyphens preserved — matched against labels + title tokens).
#    Aspect names mirror internal/META-session-guide.md §Aspect registry. ─────────

_ASPECT_MODEL_TIERS = "Model-tier system"
_ASPECT_MULTI_PLATFORM = "Multi-platform (roadmap Phase 8)"
_ASPECT_DILIGENCE = "Diligence remediation (roadmap 80→100)"
_ASPECT_USER_VALUE = (
    "User-value roadmap (functioning → astoundingly useful, U0–U5)"
)
_ASPECT_MULTI_POD = 'Multi-pod hub ("Pods")'
_ASPECT_EDR = "EDR (Evolve Development Rig)"

ROUTING_RULES: tuple[tuple[str, str], ...] = (
    ("edr", _ASPECT_EDR),
    ("triage", _ASPECT_EDR),
    ("multi-pod", _ASPECT_MULTI_POD),
    ("pods", _ASPECT_MULTI_POD),
    ("model", _ASPECT_MODEL_TIERS),
    ("tier", _ASPECT_MODEL_TIERS),
    ("rung", _ASPECT_MODEL_TIERS),
    ("provider", _ASPECT_MODEL_TIERS),
    ("linux", _ASPECT_MULTI_PLATFORM),
    ("windows", _ASPECT_MULTI_PLATFORM),
    ("platform", _ASPECT_MULTI_PLATFORM),
    ("security", _ASPECT_DILIGENCE),
    ("sudoers", _ASPECT_DILIGENCE),
    ("audit", _ASPECT_DILIGENCE),
    ("wizard", _ASPECT_USER_VALUE),
    ("delivery", _ASPECT_USER_VALUE),
    ("autonomy", _ASPECT_USER_VALUE),
    ("manifest", _ASPECT_USER_VALUE),
)
DEFAULT_ROUTE = "unrouted (operator)"

# ── Verdict reasons (machine-readable strings; P1.3 may branch on these) ─────

REASON_AGENT_ABLE = "edr:agent-able label + falsifiable Proof: line present"
REASON_LABEL_NO_PROOF = (
    "agent-able label present but no falsifiable Proof: line"
)
REASON_DEFAULT_HUMAN = (
    "default-to-human: no edr:agent-able label (design §6.1)"
)


# ── Pure classification functions (DATA-table lookups, unit-testable) ────────


def classify(labels: list[str]) -> str:
    """Classification from labels (CLASSIFICATION_RULES; first row wins)."""
    label_set = {label.lower() for label in labels}
    for rule_labels, classification in CLASSIFICATION_RULES:
        if label_set & rule_labels:
            return classification
    return DEFAULT_CLASSIFICATION


def severity_for(labels: list[str]) -> str:
    """Severity on the signal scale (SEVERITY_RULES; first row wins)."""
    label_set = {label.lower() for label in labels}
    for rule_labels, severity in SEVERITY_RULES:
        if label_set & rule_labels:
            return severity
    return DEFAULT_SEVERITY


def urgency_for(labels: list[str], severity: str, classification: str) -> str:
    """Proposal.urgency from the triage verdict (see module docstring)."""
    label_set = {label.lower() for label in labels}
    if SECURITY_LABEL in label_set:
        return SECURITY_URGENCY
    if classification == "noise":
        return NOISE_URGENCY
    return SEVERITY_TO_URGENCY[severity]


def extract_proof_artifact(body: str) -> str | None:
    """First ``Proof:`` line's payload, verbatim (trimmed), or None.

    A line qualifies when it starts with ``Proof:`` (after leading
    whitespace) and carries a non-empty payload. The §5.2 discipline: a
    proof artifact must be falsifiable, so an empty ``Proof:`` is no
    declaration at all.
    """
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(PROOF_LINE_PREFIX):
            payload = stripped[len(PROOF_LINE_PREFIX):].strip()
            if payload:
                return payload
    return None


def agent_able_verdict(
    labels: list[str], body: str
) -> tuple[bool, str | None, str]:
    """The load-bearing routing decision → (agent_able, proof, reason).

    DEFAULTS TO HUMAN. True only when the explicit ``edr:agent-able``
    label (a human's judgment) AND a parseable ``Proof:`` declaration
    are BOTH present. No heuristic guessing (design §6.1).
    """
    if AGENT_ABLE_LABEL not in labels:
        return False, None, REASON_DEFAULT_HUMAN
    proof = extract_proof_artifact(body)
    if proof is None:
        return False, None, REASON_LABEL_NO_PROOF
    return True, proof, REASON_AGENT_ABLE


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def route_for(labels: list[str], title: str) -> str:
    """Target aspect from ROUTING_RULES (first hint hit wins)."""
    candidates = {label.lower() for label in labels}
    candidates.update(_TOKEN_RE.findall((title or "").lower()))
    for hint, aspect in ROUTING_RULES:
        if hint in candidates:
            return aspect
    return DEFAULT_ROUTE


def triage_signal(signal: Signal) -> dict[str, Any]:
    """Full verdict for one dev_issue Signal — the machine-readable block
    stored at ``Proposal.provenance.signals["triage"]`` (see docstring)."""
    details = signal.details or {}
    labels = [str(label) for label in (details.get("labels") or [])]
    classification = classify(labels)
    severity = severity_for(labels)
    agent_able, proof, reason = agent_able_verdict(labels, signal.body)
    return {
        "schema": TRIAGE_SCHEMA_VERSION,
        "classification": classification,
        "severity": severity,
        "agent_able": agent_able,
        "route": route_for(labels, signal.title),
        "proof_artifact": proof,
        "reason": reason,
        "issue_number": details.get("number"),
        "issue_url": details.get("url", ""),
        "labels": labels,
    }


# ── Proposal construction ────────────────────────────────────────────────────


def proposal_id_for(signal: Signal) -> str:
    """Deterministic id: ``edr-triage-gh-<number>`` (the idempotency key).

    A dev_issue Signal without ``details["number"]`` (non-GitHub source)
    falls back to ``edr-triage-sig-<signal id>`` — still deterministic
    per Signal, so re-runs stay idempotent.
    """
    number = (signal.details or {}).get("number")
    if number is not None:
        return f"edr-triage-gh-{number}"
    return f"edr-triage-sig-{signal.id}"


def build_proposal(signal: Signal, verdict: dict[str, Any]) -> Proposal:
    """One triaged Proposal for one Signal.

    The machine-readable verdict goes in ``provenance.signals["triage"]``
    (the parse surface for P1.3); the prose fields mirror it for
    operators. Action is an ``Investigation`` — triage routes, it never
    dispatches (dispatch is P1.3, behind the R1 human gate).
    """
    issue_ref = (
        f"GitHub issue #{verdict['issue_number']}"
        if verdict["issue_number"] is not None
        else f"dev_issue Signal {signal.id}"
    )
    disposition = (
        "agent-able (dispatch candidate, human-gated at R1)"
        if verdict["agent_able"]
        else "human"
    )
    context_lines = [
        f"EDR triage of {issue_ref}: {signal.title}",
        f"URL: {verdict['issue_url'] or 'n/a'}",
        f"Classification: {verdict['classification']}"
        f" | severity: {verdict['severity']}",
        f"Route: {verdict['route']} | disposition: {disposition}",
        f"Reason: {verdict['reason']}",
    ]
    if verdict["proof_artifact"]:
        context_lines.append(f"Proof artifact: {verdict['proof_artifact']}")
    return Proposal(
        id=proposal_id_for(signal),
        bot_id=BOT_ID,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[signal.id],
        provenance=Provenance(
            technique=TECHNIQUE,
            signals={"triage": verdict},
            confidence=CLASSIFICATION_CONFIDENCE.get(
                verdict["classification"], DEFAULT_CONFIDENCE
            ),
        ),
        problem=(
            f"{issue_ref} needs a routed owner: "
            f"{verdict['classification']} ({verdict['severity']}), "
            f"routed {verdict['route']}, disposition {disposition}."
        ),
        action=Investigation(context="\n".join(context_lines)),
        risk_tag=RiskTag(
            blast_radius="bot", reversibility="manual", touches=[]
        ),
        urgency=urgency_for(
            verdict["labels"], verdict["severity"], verdict["classification"]
        ),
        admin_surface_summary=f"EDR triage: {issue_ref} → {verdict['route']}",
        human_title=f"EDR triage: {signal.title}",
        status="pending",
        motivating_signals=[signal.id],
    )


# ── Runner + CLI ─────────────────────────────────────────────────────────────


def run(shared_dir: Path) -> dict[str, int]:
    """Triage every firing ``dev_issue`` Signal in the store.

    Idempotent: a Signal whose deterministic proposal id already exists
    in ANY subdir (pending/snoozed/applied/archived) is skipped — re-runs
    write nothing, and a rejected/archived proposal is never recreated.
    ``agent_able`` / ``human`` count verdicts on proposals written THIS
    run only.
    """
    shared_dir = Path(shared_dir)
    firing = [
        signal
        for signal in signals_store.iter_active(shared_dir, state="firing")
        if signal.type == SIGNAL_TYPE
    ]

    written = skipped = agent_able_count = human_count = 0
    for signal in firing:
        if arbiter_store.find_proposal(
            shared_dir, proposal_id_for(signal)
        ) is not None:
            skipped += 1
            continue
        verdict = triage_signal(signal)
        arbiter_store.write_proposal(build_proposal(signal, verdict), shared_dir)
        written += 1
        if verdict["agent_able"]:
            agent_able_count += 1
        else:
            human_count += 1

    return {
        "signals": len(firing),
        "proposals_written": written,
        "skipped_existing": skipped,
        "agent_able": agent_able_count,
        "human": human_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m edr.triage",
        description=(
            "Triage firing dev_issue Signals in an EDR store into routed "
            "Proposals (classification, severity, agent-able-vs-human "
            "defaulting to human, target aspect)."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        required=True,
        type=Path,
        help=(
            "EDR store root (the dir holding signals/ and proposals/). "
            "Required — use an EDR-local dir, never a pod's shared dir."
        ),
    )
    args = parser.parse_args(argv)

    summary = run(args.shared_dir)
    print(
        f"triage: signals {summary['signals']}, "
        f"proposals_written {summary['proposals_written']}, "
        f"skipped_existing {summary['skipped_existing']}, "
        f"agent_able {summary['agent_able']}, "
        f"human {summary['human']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
