#!/usr/bin/env python3
"""
provider_audit.py — Per-bot OAuth-provider audit (Stage 3a + 3b).

Workstream B-skills (spec-audit-extensions §4.2 + §4.4). Sibling to
:mod:`skill_audit`, scoped to OAuth providers (Google Workspace, Slack
OAuth, Discord OAuth, Telegram OAuth, ...). Providers are a narrower
surface than skills: mostly OAuth-config + token-state + scope set, with
little behavioral logic. The audit reflects this — categories are tighter
and the synthetic contract is smaller (the provider's module docstring +
its ``integration_ids`` / ``skill_id`` fields).

Some overlap with skill audit when a skill uses a provider — both audits
run independently and the operator may see two findings for the same root
cause (one from the skill side, one from the provider side). That's by
design (spec §4.2): provider-side surfaces are the credentials layer; the
operator dispatches a single token-refresh fix that resolves both.

This module DOES NOT decide cadence, schedule runs, or write the trail —
those are runner concerns.
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

from app_audit_tier3 import (  # noqa: E402
    OUTCOME_PROPOSE,
    VALID_OUTCOMES,
    VALID_SEVERITIES,
    _dispatch_via_oc,
    _parse_json_array,
    _coerce_decision,
)


logger = logging.getLogger(__name__)


# ── Vocabulary ──────────────────────────────────────────────────────────────


# Provider-audit categories. Tighter than skill audit because the provider
# surface is just OAuth wiring + token state.
VALID_PROVIDER_CATEGORIES = (
    "token_state",          # token missing / expired / refresh failing
    "scope_coverage",       # provider scopes don't cover dependent skills' claims
    "config_drift",         # auth-profiles.json structure has drifted
    "provider_disabled",    # provider registered but not currently in use
    "code_vs_docstring",    # public interface diverges from docstring contract
    "manifest_thin",        # docstring too thin to audit
    "client_credentials",   # OAuth client (app reg.) missing / misconfigured
    "upstream_schema",      # provider's upstream API has changed in a way we'd notice
)


# Matches app_audit_tier3 — the 2026-05-20 bleed showed 600s let a single
# hung agent leak $1 of Sonnet per dispatch before the wrapper noticed.
# 180s is generous for any legitimate response from this dispatch.
_DISCOVERY_TIMEOUT_S = 180
_TRIAGE_TIMEOUT_S = 180

_MAX_DOCSTRING_BYTES = 12_000
_MAX_TRAIL_LINES = 30


# ── Data shapes ─────────────────────────────────────────────────────────────


@dataclass
class ProviderObservation:
    """One Stage-3a observation about an OAuth provider's state."""
    obs_id: str
    category: str       # one of VALID_PROVIDER_CATEGORIES
    severity: str       # one of VALID_SEVERITIES
    description: str
    evidence: list[str] = field(default_factory=list)
    suggested_action: str = ""

    def signature(self, bot_id: str, provider_id: str) -> str:
        import hashlib
        canon_desc = " ".join((self.description or "").lower().split())
        canon_evidence = sorted(
            re.sub(r":\d+", "", str(ev)) for ev in (self.evidence or [])
        )
        h = hashlib.sha256()
        h.update(f"provider_audit:{self.category}:".encode())
        h.update(canon_desc.encode())
        h.update(b"|".join(e.encode() for e in canon_evidence))
        digest = h.hexdigest()[:16]
        return f"provider_audit:{self.category}:{bot_id}:{provider_id}:{digest}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderTriageDecision:
    obs_id: str
    outcome: str
    rationale: str = ""
    transformation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderAuditOutput:
    audit_run_id: str
    bot_id: str
    provider_id: str
    status: str            # "ok" | "with_findings" | "failed"
    started_at: str
    completed_at: str
    full_audit: bool
    observations: list[ProviderObservation] = field(default_factory=list)
    decisions: list[ProviderTriageDecision] = field(default_factory=list)
    tokens_used: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_run_id": self.audit_run_id,
            "bot_id": self.bot_id,
            "provider_id": self.provider_id,
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


# ``{repo}`` is the deploy checkout (platform-keyed: /Users/Shared/evolve-repo
# on macOS, /var/lib/evolve/repo on Linux); ``{cwd}`` covers dev/test checkouts.
_PROVIDER_MODULE_CANDIDATES = (
    "{repo}/packages/admin/evolve_admin/oauth/providers/{provider}_provider.py",
    "{cwd}/packages/admin/evolve_admin/oauth/providers/{provider}_provider.py",
)


def find_provider_module(provider_id: str, *, cwd: str | None = None) -> Path | None:
    """Locate the provider module file, or None."""
    repo = get_profile().deploy_checkout_default
    cwd = cwd or repo
    # provider_id may be the integration name (e.g. "gmail") or the
    # provider key proper (e.g. "google_workspace"). Try both shapes.
    candidates = [provider_id]
    if provider_id.endswith("_workspace"):
        candidates.append(provider_id.replace("_workspace", ""))
    for cand in candidates:
        for tpl in _PROVIDER_MODULE_CANDIDATES:
            p = Path(tpl.format(provider=cand, cwd=cwd, repo=repo))
            if p.exists():
                return p
    return None


def extract_provider_contract(provider_path: Path) -> dict[str, Any]:
    """Same AST-based extraction as skill_audit, scoped to provider modules."""
    import ast
    try:
        text = provider_path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "module_docstring": "",
            "public_symbols": [],
            "function_signatures": [],
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
            "size_bytes": len(text),
            "is_thin": True,
            "parse_error": f"SyntaxError: {exc.msg}",
        }

    docstring = (ast.get_docstring(tree) or "")[:_MAX_DOCSTRING_BYTES]
    public_symbols: list[str] = []
    function_signatures: list[dict[str, Any]] = []
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
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            public_symbols.append(node.name)

    return {
        "module_docstring": docstring,
        "public_symbols": public_symbols,
        "function_signatures": function_signatures,
        "size_bytes": len(text),
        "is_thin": len(docstring.strip()) < 200,
    }


# ── Bot-state shape ─────────────────────────────────────────────────────────


@dataclass
class BotProviderState:
    """State of one OAuth provider on one bot."""
    bot_id: str
    provider_id: str
    profile_present: bool = False
    profile_status: str | None = None           # active | needs_oauth | expired | ...
    token_age_days: int | None = None
    token_expire_days: int | None = None        # negative if past expiry
    scopes_present: list[str] = field(default_factory=list)
    scopes_needed_by_skills: dict[str, list[str]] = field(default_factory=dict)
    refresh_token_present: bool = False
    last_refresh_age_days: int | None = None
    oauth_client_configured: bool = True
    dependent_skills: list[str] = field(default_factory=list)
    recent_failures: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "profile_present": self.profile_present,
            "profile_status": self.profile_status,
            "token_age_days": self.token_age_days,
            "token_expire_days": self.token_expire_days,
            "scopes_present": sorted(self.scopes_present),
            "scopes_needed_by_skills": self.scopes_needed_by_skills,
            "scope_drift_per_skill": {
                skill: sorted(set(needed) - set(self.scopes_present))
                for skill, needed in self.scopes_needed_by_skills.items()
            },
            "refresh_token_present": self.refresh_token_present,
            "last_refresh_age_days": self.last_refresh_age_days,
            "oauth_client_configured": self.oauth_client_configured,
            "dependent_skills": self.dependent_skills[:20],
            "recent_failures": self.recent_failures[:10],
            "extra": self.extra,
        }


# ── Prompt templates ────────────────────────────────────────────────────────


_STAGE_3A_SYSTEM = """You are an internal OAuth-provider audit reviewer for a bot called {bot_id}.

You audit one PROVIDER at a time (google_workspace, slack, discord, ...).
A provider is the credentials layer: it owns OAuth tokens, scopes, and
refresh state. When a provider is broken, every skill that depends on it
breaks. Skills then cascade into apps.

You have:
  - The provider's synthetic CONTRACT: docstring + public interface.
  - The provider's STATE on this bot: profile present/active/expired,
    token age + expiry, scopes present vs needed-by-dependent-skills,
    refresh token presence, recent failure signals.

Your job: produce a JSON list of OBSERVATIONS where this provider is
likely to fail the next time a skill calls it. Be specific.

GOOD observations:
  - "Token expires in 4 days; skill gmail will fail any morning briefing
    that runs after that."
  - "Profile carries gmail.readonly but skill `calendar` needs
    calendar.events scope — calendar invocations error."
  - "Refresh token last rotated 89 days ago, near upstream's hard 90-day
    limit. Expect refresh failure soon."

BAD observations (do NOT emit):
  - "OAuth in general is risky" — not actionable.
  - "the provider could be modular" — style only.

Output ONLY a JSON array. Each element:
{{
  "obs_id": "obs-N",
  "category": one of {valid_categories},
  "severity": "critical" | "major" | "minor" | "info",
  "description": one-paragraph human-readable summary,
  "evidence": list of refs (profile field, scope, etc.),
  "suggested_action": brief phrase like "regenerate token" or "expand scopes"
}}

If the provider looks healthy, emit an empty array `[]`.

ACCEPTED FINDINGS:
{accepted_block}
Skip observations whose signature matches any of these. Empty list when
`full_audit=true`.

No prose, no markdown fences. Just the JSON array.
"""


_STAGE_3B_SYSTEM = """You are the triage half of an OAuth-provider audit.

Pick exactly one outcome per Stage-3a observation:

  * dismiss — noise; not actionable; minor style.
  * auto_fix — safe transformation the system can apply without operator
    review. v1: do NOT pick auto_fix for anything touching credentials,
    tokens, or OAuth client state.
  * propose — operator should review. Default for substantive findings
    (expiring token, scope drift, missing refresh).

Output ONLY a JSON array, same order as input observations:
[
  {{ "obs_id": "obs-1",
     "outcome": "dismiss" | "auto_fix" | "propose",
     "rationale": one-line reason,
     "transformation": "" (no provider auto-fixes in v1) }},
  ...
]

No prose, no markdown fences. Just the JSON array.
"""


def stage_3a_prompt(
    contract: dict[str, Any],
    state: BotProviderState,
    trail_tail: list[dict] | None = None,
    full_audit: bool = False,
) -> str:
    return json.dumps({
        "contract": {
            "module_docstring": contract.get("module_docstring", ""),
            "public_symbols": contract.get("public_symbols", []),
            "function_signatures": contract.get("function_signatures", []),
            "is_thin": contract.get("is_thin", False),
            "parse_error": contract.get("parse_error", ""),
        },
        "state": state.to_prompt_dict(),
        "trail_tail": (trail_tail or [])[-_MAX_TRAIL_LINES:],
        "full_audit": full_audit,
    }, indent=2, default=str)


def stage_3b_prompt(observations: list[ProviderObservation]) -> str:
    return json.dumps({"observations": [o.to_dict() for o in observations]}, indent=2)


# ── Coercion ────────────────────────────────────────────────────────────────


def _coerce_provider_observation(raw: dict, idx: int) -> ProviderObservation | None:
    if not isinstance(raw, dict):
        return None
    category = str(raw.get("category", "")).strip().lower()
    if category not in VALID_PROVIDER_CATEGORIES:
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
    return ProviderObservation(
        obs_id=str(raw.get("obs_id", f"obs-{idx}")),
        category=category,
        severity=severity,
        description=description,
        evidence=[str(e) for e in evidence],
        suggested_action=str(raw.get("suggested_action", "")).strip(),
    )


# ── Public entry point ──────────────────────────────────────────────────────


def run_provider_audit(
    *,
    provider_id: str,
    bot_id: str,
    state: BotProviderState,
    contract: dict[str, Any] | None = None,
    provider_module_path: Path | None = None,
    audit_run_id: str,
    full_audit: bool = False,
    accepted_signatures: set[str] | None = None,
    openclaw_bin: str | None = None,
    trail_tail: list[dict] | None = None,
    shared_dir: Path | None = None,
) -> ProviderAuditOutput:
    """End-to-end OAuth-provider audit for a single (bot, provider) pair.

    Same orchestration as :func:`skill_audit.run_skill_audit` — assemble
    inputs, Stage 3a, filter accepted, Stage 3b, backfill missing
    decisions. Returns a :class:`ProviderAuditOutput`.
    """
    started_at = _iso_now()
    out = ProviderAuditOutput(
        audit_run_id=audit_run_id,
        bot_id=bot_id,
        provider_id=provider_id,
        status="ok",
        started_at=started_at,
        completed_at=started_at,
        full_audit=full_audit,
    )

    if contract is None:
        if provider_module_path is None:
            provider_module_path = find_provider_module(provider_id)
        if provider_module_path is None:
            out.status = "failed"
            out.error = f"no provider module found for {provider_id!r}"
            out.completed_at = _iso_now()
            return out
        contract = extract_provider_contract(provider_module_path)

    accepted_set = accepted_signatures or set()

    accepted_block = (
        json.dumps(sorted(accepted_set), indent=2) if accepted_set else "[]"
    )
    sys3a = _STAGE_3A_SYSTEM.format(
        bot_id=bot_id,
        valid_categories=list(VALID_PROVIDER_CATEGORIES),
        accepted_block=accepted_block,
    )
    text3a, tokens3a, err3a = _dispatch_via_oc(
        sys3a, stage_3a_prompt(contract, state, trail_tail, full_audit),
        timeout_s=_DISCOVERY_TIMEOUT_S, openclaw_bin=openclaw_bin,
        bot_id=bot_id, shared_dir=shared_dir,
    )
    out.tokens_used += tokens3a
    if err3a:
        out.status = "failed"
        out.error = f"stage 3a: {err3a}"
        out.completed_at = _iso_now()
        return out

    raw_obs = _parse_json_array(text3a)
    observations: list[ProviderObservation] = []
    for i, raw in enumerate(raw_obs):
        obs = _coerce_provider_observation(raw, i)
        if obs is None:
            continue
        if not full_audit and obs.signature(bot_id, provider_id) in accepted_set:
            continue
        observations.append(obs)
    out.observations = observations
    if not observations:
        out.status = "ok"
        out.completed_at = _iso_now()
        return out

    text3b, tokens3b, err3b = _dispatch_via_oc(
        _STAGE_3B_SYSTEM, stage_3b_prompt(observations),
        timeout_s=_TRIAGE_TIMEOUT_S, openclaw_bin=openclaw_bin,
        bot_id=bot_id, shared_dir=shared_dir,
    )
    out.tokens_used += tokens3b
    if err3b:
        out.status = "failed"
        out.error = f"stage 3b: {err3b}"
        out.decisions = [
            ProviderTriageDecision(obs_id=o.obs_id, outcome=OUTCOME_PROPOSE,
                                   rationale="triage failed; defaulting to propose")
            for o in observations
        ]
        out.completed_at = _iso_now()
        return out

    raw_decisions = _parse_json_array(text3b)
    decisions: list[ProviderTriageDecision] = []
    by_obs = {o.obs_id: o for o in observations}
    seen: set[str] = set()
    for i, raw in enumerate(raw_decisions):
        fallback = observations[i].obs_id if i < len(observations) else f"obs-{i}"
        dec = _coerce_decision(raw, fallback)
        if dec is None or dec.obs_id not in by_obs:
            continue
        seen.add(dec.obs_id)
        decisions.append(ProviderTriageDecision(
            obs_id=dec.obs_id, outcome=dec.outcome,
            rationale=dec.rationale, transformation=dec.transformation,
        ))
    for o in observations:
        if o.obs_id not in seen:
            decisions.append(ProviderTriageDecision(
                obs_id=o.obs_id, outcome=OUTCOME_PROPOSE,
                rationale="missing triage decision; defaulted to propose",
            ))
    out.decisions = decisions
    out.status = "with_findings"
    out.completed_at = _iso_now()
    return out
