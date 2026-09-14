#!/usr/bin/env python3
"""
skill_audit.py — Per-bot skill audit (Stage 3a Discovery + Stage 3b Triage).

Workstream B-skills (spec-audit-extensions §4.1 + §4.4). Mirrors
:mod:`app_audit_tier3` but scoped to skills (Gmail, Calendar, Slack,
Notion, etc.) rather than apps. Skills are pod-wide code that gets
installed per-bot; each install module owns a detailed docstring + a
public interface. We treat that docstring + introspection as the
*synthetic contract* for the skill (spec §4.4 Option B).

Two-stage shape, identical to the app audit:

  * **Stage 3a (Discovery)** — assemble what we know about a single skill
    on a single bot (docstring + signatures + config + credential state +
    recent invocation signals) and ask the LLM to surface places the
    install is likely to fail the next time an app calls it.

  * **Stage 3b (Triage)** — for each observation, decide
    dismiss / auto_fix / propose. In v1 the runner demotes every
    auto_fix to propose (calibration mode default-on), so the LLM is
    just classifying severity / actionability, not authorizing changes.

The runner consumes the :class:`SkillAuditOutput`, writes outbox records
of kind ``skill_finding`` (Stage 3b → Proposal via admin poller) and
trail entries under ``skill_audits/<skill>/trail.jsonl``.

This module DOES NOT decide cadence, schedule runs, or write the trail —
those are runner concerns. It's a pure transformation
``(skill_id, bot_state) → observations → triage decisions``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _iso_now
from platform_profile import get_profile

# Shared scaffolding with app_audit_tier3 — same dispatch helper, same
# parse-tolerant JSON extractor, same outcome vocabulary so executor
# and poller code paths can branch on kind, not wiring.
from app_audit_tier3 import (  # noqa: E402
    OUTCOME_AUTO_FIX,
    OUTCOME_DISMISS,
    OUTCOME_PROPOSE,
    VALID_OUTCOMES,
    VALID_SEVERITIES,
    _dispatch_via_oc,
    _parse_json_array,
    _coerce_decision,
)


logger = logging.getLogger(__name__)


# ── Vocabulary ──────────────────────────────────────────────────────────────

# Skill-audit categories. Narrower than app-audit because skills have a
# narrower contract (no how_to_use block — just a docstring + a public
# interface).
VALID_SKILL_CATEGORIES = (
    "credential_state",     # token missing / expired / refresh failing
    "config_drift",         # openclaw.json missing expected keys for this skill
    "scope_drift",          # OAuth scopes don't cover what the skill claims
    "code_vs_docstring",    # public interface diverges from docstring contract
    "permission_drift",     # macOS TCC / ACL missing for skills that need it
    "manifest_thin",        # docstring too thin to audit; nudge to expand
    "invocation_pattern",   # recent failure signals point at this skill
    "stale_dependency",     # underlying plugin/provider no longer present
)


# Model timeouts — same envelopes as app audit. Skills are smaller inputs
# so we rarely come close, but the ceiling guards against a stuck call
# holding the runner's lockfile forever.
# Matches app_audit_tier3 — the 2026-05-20 bleed showed 600s let a single
# hung agent leak $1 of Sonnet per dispatch before the wrapper noticed.
# 180s is generous for any legitimate response from this dispatch.
_DISCOVERY_TIMEOUT_S = 180
_TRIAGE_TIMEOUT_S = 180

# Per-stage input ceilings — keep prompts compact.
_MAX_DOCSTRING_BYTES = 16_000
_MAX_TRAIL_LINES = 30


# ── Data shapes ─────────────────────────────────────────────────────────────


@dataclass
class SkillObservation:
    """One Stage-3a observation about a skill's install / credential state."""
    obs_id: str
    category: str       # one of VALID_SKILL_CATEGORIES
    severity: str       # one of VALID_SEVERITIES
    description: str
    evidence: list[str] = field(default_factory=list)
    suggested_action: str = ""

    def signature(self, bot_id: str, skill_id: str) -> str:
        """Stable signature for dedup against the accepted-findings list.

        Mirrors :class:`app_audit_tier3.Observation.signature` — canonical
        whitespace, line numbers stripped so a code shuffle doesn't bust
        dedup. Producer-tagged so the signal store can route independently
        from app audits.
        """
        import hashlib
        canon_desc = " ".join((self.description or "").lower().split())
        canon_evidence = sorted(
            re.sub(r":\d+", "", str(ev)) for ev in (self.evidence or [])
        )
        h = hashlib.sha256()
        h.update(f"skill_audit:{self.category}:".encode())
        h.update(canon_desc.encode())
        h.update(b"|".join(e.encode() for e in canon_evidence))
        digest = h.hexdigest()[:16]
        return f"skill_audit:{self.category}:{bot_id}:{skill_id}:{digest}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkillTriageDecision:
    """One Stage-3b decision about what to do with a SkillObservation."""
    obs_id: str
    outcome: str           # one of VALID_OUTCOMES
    rationale: str = ""
    transformation: str = ""   # reserved for future auto-fix transformations

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkillAuditOutput:
    """Aggregate skill-audit result. Mirrors app-audit's AuditOutput."""
    audit_run_id: str
    bot_id: str
    skill_id: str
    status: str            # "ok" | "with_findings" | "failed"
    started_at: str
    completed_at: str
    full_audit: bool
    observations: list[SkillObservation] = field(default_factory=list)
    decisions: list[SkillTriageDecision] = field(default_factory=list)
    tokens_used: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_run_id": self.audit_run_id,
            "bot_id": self.bot_id,
            "skill_id": self.skill_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "full_audit": self.full_audit,
            "observations": [o.to_dict() for o in self.observations],
            "decisions": [d.to_dict() for d in self.decisions],
            "tokens_used": self.tokens_used,
            "error": self.error,
        }


# ── Synthetic-contract extraction ───────────────────────────────────────────
#
# We read the install module's docstring + public interface as the contract
# to audit against (Option B from spec §4.4). The install module lives at
# ``packages/admin/evolve_admin/skills/<id>_install.py`` in the deploy
# checkout (``/Users/Shared/evolve-repo/...``). The runner is the bot user
# with read access to the deploy checkout (world-readable), so we read
# directly.


# ``{repo}`` is the deploy checkout (platform-keyed: /Users/Shared/evolve-repo
# on macOS, /var/lib/evolve/repo on Linux); ``{cwd}`` covers dev/test checkouts.
_INSTALL_MODULE_CANDIDATES = (
    "{repo}/packages/admin/evolve_admin/skills/{skill}_install.py",
    "{cwd}/packages/admin/evolve_admin/skills/{skill}_install.py",
)


def find_install_module(skill_id: str, *, cwd: str | None = None) -> Path | None:
    """Locate the install module for *skill_id*, or None if not found.

    Tries the deploy checkout first, then the dev path. The dev path uses
    the caller's cwd so tests can plant fixtures locally without overriding
    the deploy location.
    """
    repo = get_profile().deploy_checkout_default
    cwd = cwd or repo
    for tpl in _INSTALL_MODULE_CANDIDATES:
        p = Path(tpl.format(skill=skill_id, cwd=cwd, repo=repo))
        if p.exists():
            return p
    return None


def extract_synthetic_contract(install_path: Path) -> dict[str, Any]:
    """Parse the install module into a synthetic-contract dict.

    Returns::

      {
        "module_docstring": str,        # first module-level docstring
        "public_symbols": [str, ...],   # names that don't start with _
        "function_signatures": [{"name", "params", "doc"}, ...],
        "constants": [{"name", "value_summary"}, ...],
        "size_bytes": int,
        "is_thin": bool,                # True if docstring < 200 chars
      }

    Tolerant of parse errors — returns a minimal dict if the file isn't
    importable Python. We're not executing the module, just reading it as
    AST, which is enough for the synthetic contract.
    """
    import ast

    try:
        text = install_path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "module_docstring": "",
            "public_symbols": [],
            "function_signatures": [],
            "constants": [],
            "size_bytes": 0,
            "is_thin": True,
            "parse_error": f"read failed: {exc}",
        }

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return {
            "module_docstring": "",
            "public_symbols": [],
            "function_signatures": [],
            "constants": [],
            "size_bytes": len(text),
            "is_thin": True,
            "parse_error": f"SyntaxError: {exc.msg}",
        }

    docstring = (ast.get_docstring(tree) or "")[:_MAX_DOCSTRING_BYTES]

    public_symbols: list[str] = []
    function_signatures: list[dict[str, Any]] = []
    constants: list[dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            public_symbols.append(node.name)
            function_signatures.append({
                "name": node.name,
                "params": [a.arg for a in node.args.args],
                "doc": (ast.get_docstring(node) or "")[:400],
            })
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            public_symbols.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id.startswith("_") or not target.id.isupper():
                    continue
                # Avoid evaluating arbitrary code; capture a string summary
                # via ast.unparse (3.9+) or degraded fallback.
                try:
                    value_summary = ast.unparse(node.value)[:160]
                except (AttributeError, ValueError):
                    value_summary = type(node.value).__name__
                constants.append({"name": target.id, "value_summary": value_summary})

    return {
        "module_docstring": docstring,
        "public_symbols": public_symbols,
        "function_signatures": function_signatures,
        "constants": constants,
        "size_bytes": len(text),
        "is_thin": len(docstring.strip()) < 200,
    }


# ── Bot-state shape ─────────────────────────────────────────────────────────


@dataclass
class BotSkillState:
    """State of one skill on one bot, assembled by the runner before dispatch.

    Tests construct this directly to bypass file I/O.
    """
    bot_id: str
    skill_id: str
    plugin_enabled: bool | None = None          # None = couldn't determine
    plugin_entry: dict[str, Any] | None = None  # raw openclaw.json entry
    oauth_profile: dict[str, Any] | None = None
    oauth_profile_status: str | None = None     # active | needs_oauth | unknown
    credentials_age_days: int | None = None     # how long since the token landed
    credentials_expire_days: int | None = None  # negative if already expired
    scopes_present: list[str] = field(default_factory=list)
    scopes_expected: list[str] = field(default_factory=list)
    recent_failures: list[dict[str, Any]] = field(default_factory=list)
    apps_using_skill: list[str] = field(default_factory=list)
    has_oauth: bool = True                      # False for credential-less skills
    extra: dict[str, Any] = field(default_factory=dict)

    def to_prompt_dict(self) -> dict[str, Any]:
        """Compact dict for the prompt — keeps tokens cheap."""
        return {
            "skill_id": self.skill_id,
            "plugin_enabled": self.plugin_enabled,
            "has_oauth": self.has_oauth,
            "oauth_profile_status": self.oauth_profile_status,
            "credentials_age_days": self.credentials_age_days,
            "credentials_expire_days": self.credentials_expire_days,
            "scopes_present": sorted(self.scopes_present),
            "scopes_expected": sorted(self.scopes_expected),
            "scope_drift": sorted(
                set(self.scopes_expected) - set(self.scopes_present)
            ),
            "recent_failures": self.recent_failures[:10],
            "apps_using_skill": self.apps_using_skill[:20],
            "extra": self.extra,
        }


# ── Prompt templates ────────────────────────────────────────────────────────


_STAGE_3A_SYSTEM = """You are an internal skill-audit reviewer for a bot called {bot_id}.

You audit one SKILL at a time (gmail, calendar, slack, notion, etc.). A
skill is a capability that apps invoke — when a skill is broken, every
app that depends on it fails the next time it's called.

You have:
  - The skill's synthetic CONTRACT: install module's docstring, public
    interface (functions), constants, and module size.
  - The skill's STATE on this bot: plugin-enabled flag, credential state
    (expiry, scopes), recent failure signals, apps that depend on this
    skill.

Your job: produce a JSON list of OBSERVATIONS where this skill is likely
to fail the next time an app calls it. Be specific, not vague.

GOOD observations:
  - "Credential expired 12 days ago — any app that calls gmail will fail
    until token is regenerated. Apps depending: morning-briefing, journal."
  - "Docstring says the skill requires gmail.readonly scope, but the
    OAuth profile carries gmail.send instead — write-only token can't
    read inbox."
  - "Plugin entry for `slack` is present but enabled=false; calendar app
    can't post if slack remains disabled."

BAD observations (do NOT emit):
  - "this could be more secure" — vague style.
  - "consider adding more tests" — not the audit layer's job.
  - "the install module could be shorter" — not actionable.

Output ONLY a JSON array. Each element:
{{
  "obs_id": "obs-N" (sequential),
  "category": one of {valid_categories},
  "severity": "critical" | "major" | "minor" | "info",
  "description": one-paragraph human-readable summary,
  "evidence": list of "config.field" / "file:line" / "signal:id" refs,
  "suggested_action": brief phrase like "regenerate token" or "expand docstring"
}}

If the skill looks healthy, emit an empty array `[]`.

ACCEPTED FINDINGS:
The operator has previously accepted these finding signatures:
{accepted_block}
Observations whose signature would match any of these should be SKIPPED.
When `full_audit=true` is set in the input, this list is empty — re-evaluate
from scratch.

No prose, no markdown fences. Just the JSON array.
"""


_STAGE_3B_SYSTEM = """You are the triage half of an internal skill-audit reviewer.

Given a list of Stage-3a OBSERVATIONS about a skill, decide what to do
with each. Pick exactly one outcome per observation:

  * dismiss — noise; not actionable; minor style.
  * auto_fix — safe transformation the system can apply without operator
    review. v1: do NOT pick auto_fix for anything touching credentials,
    OAuth profiles, or skill code.
  * propose — operator should review. Default for anything substantive
    (expired credentials, scope drift, plugin disabled).

Output ONLY a JSON array. One entry per input observation, same order:
[
  {{
    "obs_id": "obs-1",
    "outcome": "dismiss" | "auto_fix" | "propose",
    "rationale": one-line reason,
    "transformation": "" (leave empty in v1 — no skill auto-fixes shipped)
  }},
  ...
]

No prose, no markdown fences. Just the JSON array.
"""


def stage_3a_prompt(
    contract: dict[str, Any],
    state: BotSkillState,
    trail_tail: list[dict] | None = None,
    full_audit: bool = False,
) -> str:
    """Render the user-message body for Stage 3a Discovery (skills)."""
    return json.dumps({
        "contract": {
            "module_docstring": contract.get("module_docstring", ""),
            "public_symbols": contract.get("public_symbols", []),
            "function_signatures": contract.get("function_signatures", []),
            "constants": contract.get("constants", []),
            "is_thin": contract.get("is_thin", False),
            "parse_error": contract.get("parse_error", ""),
        },
        "state": state.to_prompt_dict(),
        "trail_tail": (trail_tail or [])[-_MAX_TRAIL_LINES:],
        "full_audit": full_audit,
    }, indent=2, default=str)


def stage_3b_prompt(observations: list[SkillObservation]) -> str:
    return json.dumps(
        {"observations": [o.to_dict() for o in observations]},
        indent=2,
    )


# ── Coercion ────────────────────────────────────────────────────────────────


def _coerce_skill_observation(raw: dict, idx: int) -> SkillObservation | None:
    """Normalize an LLM-emitted observation; drops invalid category/severity."""
    if not isinstance(raw, dict):
        return None
    category = str(raw.get("category", "")).strip().lower()
    if category not in VALID_SKILL_CATEGORIES:
        return None
    severity = str(raw.get("severity", "info")).strip().lower()
    if severity not in VALID_SEVERITIES:
        severity = "info"
    description = str(raw.get("description", "")).strip()
    if not description:
        return None
    evidence = raw.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [str(evidence)]
    return SkillObservation(
        obs_id=str(raw.get("obs_id", f"obs-{idx}")),
        category=category,
        severity=severity,
        description=description,
        evidence=[str(e) for e in evidence],
        suggested_action=str(raw.get("suggested_action", "")).strip(),
    )


# ── Public entry point ──────────────────────────────────────────────────────


def run_skill_audit(
    *,
    skill_id: str,
    bot_id: str,
    state: BotSkillState,
    contract: dict[str, Any] | None = None,
    install_module_path: Path | None = None,
    audit_run_id: str,
    full_audit: bool = False,
    accepted_signatures: set[str] | None = None,
    openclaw_bin: str | None = None,
    trail_tail: list[dict] | None = None,
    shared_dir: Path | None = None,
) -> SkillAuditOutput:
    """End-to-end skill audit for a single (bot, skill) pair.

    Steps:
      1. Resolve contract (extract from install module if not passed in).
      2. Stage 3a Discovery → list of SkillObservations.
      3. Filter observations whose signature is in accepted_signatures.
      4. Stage 3b Triage → list of SkillTriageDecisions.
      5. Return SkillAuditOutput.

    The runner is responsible for passing in *state* (assembled from the
    bot's own openclaw.json + auth-profiles.json + signal store). The
    contract is auto-extracted from install_module_path when not passed.

    Failures of either stage are captured into status="failed" + error;
    partial observations are preserved so the trail still shows what we
    extracted before the dispatch broke.
    """
    started_at = _iso_now()
    out = SkillAuditOutput(
        audit_run_id=audit_run_id,
        bot_id=bot_id,
        skill_id=skill_id,
        status="ok",
        started_at=started_at,
        completed_at=started_at,
        full_audit=full_audit,
    )

    # ── Contract resolution ───────────────────────────────────────────────
    if contract is None:
        if install_module_path is None:
            install_module_path = find_install_module(skill_id)
        if install_module_path is None:
            out.status = "failed"
            out.error = f"no install module found for skill {skill_id!r}"
            out.completed_at = _iso_now()
            return out
        contract = extract_synthetic_contract(install_module_path)

    accepted_set = accepted_signatures or set()

    # ── Stage 3a Discovery ────────────────────────────────────────────────
    accepted_block = (
        json.dumps(sorted(accepted_set), indent=2) if accepted_set else "[]"
    )
    sys3a = _STAGE_3A_SYSTEM.format(
        bot_id=bot_id,
        valid_categories=list(VALID_SKILL_CATEGORIES),
        accepted_block=accepted_block,
    )
    user3a = stage_3a_prompt(contract, state, trail_tail, full_audit)
    text3a, tokens3a, err3a = _dispatch_via_oc(
        sys3a, user3a,
        timeout_s=_DISCOVERY_TIMEOUT_S,
        openclaw_bin=openclaw_bin,
        bot_id=bot_id, shared_dir=shared_dir,
    )
    out.tokens_used += tokens3a
    if err3a:
        out.status = "failed"
        out.error = f"stage 3a: {err3a}"
        out.completed_at = _iso_now()
        return out

    raw_obs = _parse_json_array(text3a)
    observations: list[SkillObservation] = []
    for i, raw in enumerate(raw_obs):
        obs = _coerce_skill_observation(raw, i)
        if obs is None:
            continue
        if not full_audit and obs.signature(bot_id, skill_id) in accepted_set:
            continue
        observations.append(obs)
    out.observations = observations

    if not observations:
        out.status = "ok"
        out.completed_at = _iso_now()
        return out

    # ── Stage 3b Triage ───────────────────────────────────────────────────
    text3b, tokens3b, err3b = _dispatch_via_oc(
        _STAGE_3B_SYSTEM, stage_3b_prompt(observations),
        timeout_s=_TRIAGE_TIMEOUT_S,
        openclaw_bin=openclaw_bin,
        bot_id=bot_id, shared_dir=shared_dir,
    )
    out.tokens_used += tokens3b
    if err3b:
        # Fall through with default-propose for every observation so we
        # don't silently lose findings on a triage timeout.
        out.status = "failed"
        out.error = f"stage 3b: {err3b}"
        out.decisions = [
            SkillTriageDecision(obs_id=o.obs_id, outcome=OUTCOME_PROPOSE,
                                rationale="triage failed; defaulting to propose")
            for o in observations
        ]
        out.completed_at = _iso_now()
        return out

    raw_decisions = _parse_json_array(text3b)
    decisions: list[SkillTriageDecision] = []
    by_obs = {o.obs_id: o for o in observations}
    seen_ids: set[str] = set()
    for i, raw in enumerate(raw_decisions):
        fallback = observations[i].obs_id if i < len(observations) else f"obs-{i}"
        # Reuse the app-audit coercer for decisions — same fields, same
        # validation. We then upcast to the skill decision dataclass below.
        dec = _coerce_decision(raw, fallback)
        if dec is None:
            continue
        if dec.obs_id not in by_obs:
            continue
        seen_ids.add(dec.obs_id)
        decisions.append(SkillTriageDecision(
            obs_id=dec.obs_id,
            outcome=dec.outcome,
            rationale=dec.rationale,
            transformation=dec.transformation,
        ))
    # Backfill any observations Stage 3b didn't return decisions for —
    # default to propose so we never silently lose findings.
    for o in observations:
        if o.obs_id not in seen_ids:
            decisions.append(SkillTriageDecision(
                obs_id=o.obs_id, outcome=OUTCOME_PROPOSE,
                rationale="missing triage decision; defaulted to propose",
            ))
    out.decisions = decisions
    out.status = "with_findings"
    out.completed_at = _iso_now()
    return out
