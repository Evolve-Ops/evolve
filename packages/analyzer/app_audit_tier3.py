#!/usr/bin/env python3
"""
app_audit_tier3.py — Tier 3 semantic audit (LLM, two-stage).

Imported by ``app_audit_runner.py`` when running on the bot. Owns the
LLM-driven half of audits: read the manifest's claims, read the actual
code, decide where reality diverges, decide what to do about it.

Two-stage design (spec §4):
  - **Stage 3a (Discovery)** — broad, noisy. Emits raw observations.
  - **Stage 3b (Triage)** — narrow, decisive. Picks dismiss / auto_fix /
    propose for each observation.

The bot owns dispatch — both stages call the bot's local OpenClaw agent
via subprocess (the runner already runs as the bot user, so no sudo).
Outputs land in two places: the bot's per-app audit JSON (full history)
and outbox records the admin's poller ingests into pod-wide stores.

This module does NOT decide whether an audit is due, schedule runs, or
write the trail — those are runner concerns. It's a pure transformation:
manifest + context → observations → triage decisions.

See internal/spec-app-audit-2026-05-16.md §4-5.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal as _signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _iso_now

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────────


# Stage 3b outcome vocabulary. Stable; written into outbox records.
OUTCOME_DISMISS = "dismiss"
OUTCOME_AUTO_FIX = "auto_fix"
OUTCOME_PROPOSE = "propose"
VALID_OUTCOMES = (OUTCOME_DISMISS, OUTCOME_AUTO_FIX, OUTCOME_PROPOSE)

# Categories Stage 3a is allowed to emit. The triage prompt and the
# poller-side proposal renderers both branch on these.
VALID_CATEGORIES = (
    "drift",                # code no longer matches manifest's stated behavior
    "missing_functionality", # manifest claims a feature the code doesn't implement
    "broken_path",          # paths / references that exist but won't actually work
    "code_smell",           # bad pattern that's likely a future bug
    "behavior_mismatch",    # code does something different than usage.how_to_use describes
    "manifest_mismatch",    # coherence Pass C2 (spec §6.4): manifest's declared
                             # shape (inputs/outputs/trigger) contradicts what the
                             # code actually does
    "dead_code",            # code path no longer reachable / unused
    "manifest_drift",       # manifest field stale vs reality (description out of date, etc.)
)

VALID_SEVERITIES = ("critical", "major", "minor", "info")


# Wrapper timeouts. Sonnet responses typically return in <60s; openclaw
# agent startup adds ~30s. 180s is a generous cap that still bounds
# per-incident damage when the agent process hangs after firing the LLM
# call. The earlier 600/300s caps let a single bleed leak ~$5 because
# the Sonnet call had already billed by the time the wrapper noticed.
# See internal/forensic-team_bot_a-apply-bleed-2026-05-21.md.
_DISCOVERY_TIMEOUT_S = 180
_TRIAGE_TIMEOUT_S = 180

# Hard cap on the message body we send to ``openclaw agent --message``.
# A 200k-char body indicates an out-of-control prompt; refuse rather than
# fire a $1+ Sonnet call that's almost certain to time out.
_MESSAGE_MAX_CHARS = 200_000


# ── Observation + decision data shapes ──────────────────────────────────────


@dataclass
class Observation:
    """One Stage 3a output — a single divergence between manifest and reality."""
    obs_id: str
    category: str       # one of VALID_CATEGORIES
    severity: str       # one of VALID_SEVERITIES
    description: str    # one-paragraph human-readable summary
    evidence: list[str] = field(default_factory=list)   # file:line refs, manifest field paths
    suggested_action: str = ""   # raw LLM suggestion — Stage 3b decides for real

    def signature(self, bot_id: str, app_id: str) -> str:
        """Stable signature for dedup against `manifest.audit_accepted[]`.

        Hash inputs: category, sorted evidence keys, normalized description.
        We canonicalize description by lowercasing + collapsing whitespace
        so trivial rewording doesn't bust the signature; the operator's
        "I've accepted this" decision should survive re-runs of the same
        finding.
        """
        import hashlib
        canon_desc = " ".join((self.description or "").lower().split())
        # Strip line numbers from evidence refs so a code shuffle that moves
        # the cited line doesn't bust the signature.
        canon_evidence = sorted(
            re.sub(r":\d+", "", str(ev)) for ev in (self.evidence or [])
        )
        h = hashlib.sha256()
        h.update(f"tier3:{self.category}:".encode())
        h.update(canon_desc.encode())
        h.update(b"|".join(e.encode() for e in canon_evidence))
        digest = h.hexdigest()[:16]
        return f"app_audit_tier3:{self.category}:{bot_id}:{app_id}:{digest}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TriageDecision:
    """One Stage 3b output — what to do about a Stage 3a observation."""
    obs_id: str
    outcome: str           # one of VALID_OUTCOMES
    rationale: str = ""    # one-line why
    # When outcome == auto_fix, the LLM may name a transformation kind.
    # Whitelist enforced post-hoc in the executor — see §5.2.
    transformation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditOutput:
    """Aggregate Tier-3 result for one app."""
    audit_run_id: str
    bot_id: str
    app_id: str
    status: str            # "ok" | "with_findings" | "failed"
    started_at: str
    completed_at: str
    full_audit: bool
    observations: list[Observation] = field(default_factory=list)
    decisions: list[TriageDecision] = field(default_factory=list)
    tokens_used: int = 0
    error: str = ""

    def outcomes_by_kind(self) -> dict[str, int]:
        counts = {"dismiss": 0, "auto_fix": 0, "propose": 0, "conflict_notice": 0}
        for d in self.decisions:
            if d.outcome in counts:
                counts[d.outcome] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_run_id": self.audit_run_id,
            "bot_id": self.bot_id,
            "app_id": self.app_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "full_audit": self.full_audit,
            "observations": [o.to_dict() for o in self.observations],
            "decisions": [d.to_dict() for d in self.decisions],
            "tokens_used": self.tokens_used,
            "error": self.error,
        }


# ── Input assembly ──────────────────────────────────────────────────────────


# Hard ceilings on how much we feed to the LLM. Per-file caps avoid
# blowing the context window when one giant file dominates the app.
_MAX_FILES = 25
_MAX_FILE_BYTES = 30_000
_MAX_TRAIL_LINES = 60


def assemble_inputs(
    manifest: dict, workspace: Path, *, full_audit: bool,
) -> dict[str, Any]:
    """Gather everything Stage 3a needs from local state.

    The runner runs as the bot, so we have direct read access to every
    file the bot owns. No sudo, no admin roundtrip. Returns a dict ready
    to pass into the Stage 3a prompt builder.

    ``full_audit=True`` excludes ``manifest.audit_accepted[]`` so Stage 3a
    re-evaluates from scratch. Otherwise, accepted signatures are passed
    in and the prompt tells Stage 3a to skip matching observations.
    """
    files_payload: list[dict] = []
    for rec in (manifest.get("files") or [])[:_MAX_FILES]:
        if not isinstance(rec, dict):
            continue
        path = (rec.get("path") or "").lstrip("/")
        if not path:
            continue
        full = workspace / path
        try:
            data = full.read_text(errors="replace")[:_MAX_FILE_BYTES]
        except (OSError, UnicodeDecodeError):
            continue
        files_payload.append({
            "path": path,
            "layer": rec.get("layer", ""),
            "purpose": rec.get("purpose", ""),
            "truncated": (full.stat().st_size > _MAX_FILE_BYTES) if full.exists() else False,
            "content": data,
        })

    # Trail tail — last N entries.
    audits_dir = workspace / "evolve" / "audits" / (manifest.get("id") or "")
    trail_path = audits_dir / "trail.jsonl"
    trail_lines: list[dict] = []
    if trail_path.exists():
        try:
            lines = trail_path.read_text().splitlines()[-_MAX_TRAIL_LINES:]
            for ln in lines:
                try:
                    trail_lines.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    accepted = [] if full_audit else (manifest.get("audit_accepted") or [])

    return {
        "manifest": _manifest_for_prompt(manifest),
        "files": files_payload,
        "trail_tail": trail_lines,
        "accepted_signatures": [a.get("signature") for a in accepted if isinstance(a, dict) and a.get("signature")],
        "customization_guidance": _extract_customization_guidance(manifest),
        "full_audit": full_audit,
    }


def _manifest_for_prompt(manifest: dict) -> dict:
    """Strip volatile and deprecated fields from the manifest before the LLM.

    We don't want last_audit / last_structural_verify timestamps in the
    prompt — they're operational state, not claims to audit, and changing
    them shouldn't change the audit's outputs.

    ``test_cases`` and ``test_command`` are deprecated as of PR #2488
    (2026-06-08, app-test surface removal): the dataclass fields are kept
    for one schema cycle but the runner that satisfied them is gone, so
    every "manifest claims test_cases[N]=X, code does Y" finding is noise
    about a field the operator can no longer act on. Redacted from the
    LLM-facing snapshot until the fields drop from the schema entirely.
    """
    skip = {
        "last_audit", "last_structural_verify", "last_verification",
        "last_test_run", "last_test_output", "last_test_exit_code",
        "install_job", "improvement_history",
        "test_cases", "test_command",
    }
    return {k: v for k, v in manifest.items() if k not in skip}


# Match "## Customization Guidance", "### Customization Notes", etc. — flexible
# enough to catch the variants build_spec authors actually use, anchored by
# "Customization" + one of three nouns.
_CUSTOMIZATION_HEADING_RE = re.compile(
    r"^\s*#{2,}\s*Customization\s+(?:Guidance|Notes|Points)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_customization_guidance(manifest: dict) -> str:
    """Pull the build_spec's ``## Customization Guidance`` section, if any.

    The forge gallery convention is for build_specs to include a section
    naming the per-bot customizations the spec explicitly invites
    (TAG_ALIASES extensions, category renames, default overrides, etc.).
    Without surfacing this to the auditor, invited divergence reads as
    drift — the dominant false-positive on the personal-bot task-manager
    forge run (spec-forge-side-effects-2026-06-02.md §8.1).

    Returns the matched heading + body, trimmed at the next ``##`` heading
    or end of build_spec. Returns ``""`` when the manifest has no
    build_spec, no matching section, or a non-string build_spec.
    """
    spec = manifest.get("build_spec") or ""
    if not isinstance(spec, str) or not spec:
        return ""
    match = _CUSTOMIZATION_HEADING_RE.search(spec)
    if not match:
        return ""
    # Cut at the next top-level heading (`##` followed by a non-`#`); deeper
    # headings (`###`, etc.) stay in scope as subsections of the guidance.
    tail = spec[match.end():]
    next_h = re.search(r"^\s*##\s+\S", tail, re.MULTILINE)
    body = tail[: next_h.start()] if next_h else tail
    return (spec[match.start(): match.end()] + body).strip()


# ── Prompts ─────────────────────────────────────────────────────────────────


_STAGE_3A_SYSTEM = """You are an internal app-audit reviewer for a bot called {bot_id}.

You audit one app at a time. The app's MANIFEST tells you what the app is
supposed to do (look at description, identity.purpose, usage.how_to_use,
success_criteria, files[].purpose). The app's CODE is what's actually there.

Your job: produce a JSON list of OBSERVATIONS where the code has diverged
from the manifest's claims, or where the code has rotted in ways tests
wouldn't catch. Be specific, not vague.

GOOD observations:
  - "manifest.usage.how_to_use says the bot should run `journal.py --mood X`
    when the user mentions feelings, but journal.py has no --mood flag"
  - "scripts/morning-briefing.py imports `requests` which isn't in
    requirements.python_packages — fragile if the bot env changes"
  - "identity.scope_excludes mentions 'long-form journal entries' but
    journal.py has a 2000-char free-text capture path"

BAD observations (do NOT emit):
  - "this function name is short" / "could be cleaner" — style only
  - "what if the disk fails" — generic robustness speculation
  - "consider adding more tests" — that's the testing layer's job
  - re-noting structural problems already caught by Tier 2 (file missing,
    sha drift, etc.) — those are someone else's job

CUSTOMIZATION GUIDANCE:
The input includes a `customization_guidance` field. If non-empty, it
quotes the build_spec's `## Customization Guidance` section — the
specific divergences from the canonical spec that this app was BUILT
TO ENCOURAGE on a per-bot basis (TAG_ALIASES extensions, category
renames, default overrides, prefix changes, etc.).

Divergence covered by customization_guidance is NOT drift. Specifically:
  - Do NOT flag TAG_ALIASES (or similar alias/dict) entries the guidance
    explicitly invites the bot to customize for its domain.
  - Do NOT flag category renames, category additions, or prefix changes
    the guidance says to adapt.
  - Do NOT flag default value overrides (e.g. light=green, urgency=2)
    the guidance lists as customizable.

If unsure whether a divergence is invited or genuine drift, SKIP the
observation. The operator triages every finding by hand; false negatives
cost less than false positives here.

COHERENCE CHECK (Pass C2):
For each scheduled_actions[*] entry in the manifest, read the
implementing code path and verify the implementation is consistent
with the declared inputs, outputs, summary, and trigger. Flag cases
where the code exists but doesn't actually accomplish the claim —
e.g., "sends daily briefing" but the script only logs to stdout, or
"summarizes the inbox" but the script never reads the inbox file.

Emit a finding with category `behavior_mismatch` when the script
runs but doesn't deliver the claimed behavior. Emit
`manifest_mismatch` when the manifest's declared shape (input
kinds, output integration, trigger) contradicts what the code
actually does. Include the action `id` in the finding for
deduplication.

Be conservative — only flag when the mismatch is unambiguous. The
goal is to catch egregious lies between manifest and code, not to
quibble about wording. This is spec §6.4 — the LLM-backed coherence
pass that complements pure-Python Pass A + Pass C1.

BROKEN_PATH RIGOR:
When emitting a `broken_path` observation, the description MUST name
three things explicitly:
  (a) the specific code path you claim is broken (function name +
      approximate line range),
  (b) the input that triggers it (CLI invocation, manifest field, or
      caller context — be concrete),
  (c) confirmation that the path runs in normal operation — NOT a dead
      branch, NOT a code arm reached only under operator flags like
      `--dry-run`, and NOT a defensive `if foo: ...` whose condition is
      false in normal use.

If you cannot substantiate all three in the description text, do not
emit `broken_path`. Either downgrade to `code_smell` (lower severity)
or skip the observation. Findings asserting "X doesn't update Y" must
prove X actually has a reachable path that should update Y in normal
operation, not in a dry-run / unreachable branch.

Output ONLY a JSON array. Each element:
{{
  "obs_id": "obs-N" (sequential),
  "category": one of {valid_categories},
  "severity": "critical" | "major" | "minor" | "info",
  "description": one-paragraph human-readable summary,
  "evidence": list of "file:line" or "manifest.field" refs supporting it,
  "suggested_action": brief phrase like "update manifest" or "fix the code"
}}

If the app looks fine, emit an empty array `[]`.

ACCEPTED FINDINGS:
The operator has previously accepted these finding signatures:
{accepted_block}
Observations whose signature would match any of these should be SKIPPED
(don't emit them). When `full_audit=true` is set in the input, this list
is empty — re-evaluate from scratch.

No prose, no markdown fences. Just the JSON array.
"""


_STAGE_3B_SYSTEM = """You are the triage half of an internal app-audit reviewer.

Given a list of Stage-3a OBSERVATIONS, decide what to do with each.
Pick exactly one outcome per observation:

  • dismiss — noise; not actionable; routine restatement; minor style.
    The operator never sees these (they only land in the audit JSON).

  • auto_fix — safe transformation the system can apply without
    operator review. The runner has a hard whitelist (see
    `transformation` below); your job is to flag the obs as "yes this
    is safe to fix automatically", not to pick the transformation
    yourself. v1: do NOT pick auto_fix for anything that touches
    application code or user data files.

  • propose — operator should look at it. This is the default for
    anything substantive — semantic drift, behavior mismatch, missing
    functionality, suspicious code paths. The operator will see a
    Proposal in their dashboard.

When outcome is auto_fix, you may set `transformation` to one of:
  - "manifest_path_update"   — a file got renamed; update manifest.files
  - "manifest_metadata_fix"  — stale field like last_reviewed, dead docs link
  - "dead_orphan_file"       — file not in any manifest, not modified in 90d
  - "stale_cron_removal"     — crontab entry whose manifest cron was removed

For anything else, leave `transformation` empty and pick `propose` instead.

Output ONLY a JSON array. One entry per input observation, same order:
[
  {{
    "obs_id": "obs-1",
    "outcome": "dismiss" | "auto_fix" | "propose",
    "rationale": one-line reason,
    "transformation": "" or one of the kinds above
  }},
  ...
]

No prose, no markdown fences. Just the JSON array.
"""


def stage_3a_prompt(inputs: dict) -> str:
    """Render the user-message body for Stage 3a Discovery."""
    return json.dumps({
        "manifest": inputs["manifest"],
        "files": inputs["files"],
        "trail_tail": inputs["trail_tail"],
        "customization_guidance": inputs.get("customization_guidance", ""),
        "full_audit": inputs.get("full_audit", False),
    }, indent=2, default=str)


def stage_3b_prompt(observations: list[Observation]) -> str:
    """Render the user-message body for Stage 3b Triage."""
    return json.dumps(
        {"observations": [o.to_dict() for o in observations]},
        indent=2,
    )


# ── Dispatch (OpenClaw agent) ───────────────────────────────────────────────
#
# The bot runs `openclaw agent --local --agent main --json --message <text>`.
# stdout is structured JSON; we extract the assistant's message body.


@dataclass
class _DispatchResult:
    text: str
    tokens: int            # input + output, recovered from TurnObserver on timeout
    cost_usd: float        # 0 if unknown
    error: str             # "" on success
    timed_out: bool        # True iff wrapper killed the process tree


def _build_audit_agent_cmd(
    *,
    binary: str,
    body: str,
    timeout_s: int,
    session_id: str,
    model: str | None = None,
) -> list[str]:
    """Construct the argv for an audit ``openclaw agent`` dispatch.

    Pulled out of ``_dispatch_via_oc_full`` so the per-dispatch ``--model``
    plumbing is unit-testable without standing up subprocess fakes (mirrors
    ``bot_forge._build_agent_cmd``).

    ``model=None`` emits no ``--model`` flag, so openclaw falls back to the
    bot's ``agents.defaults.model`` — today's behavior for every recurring
    audit. The FIRST audit of a just-built app passes the pod's resolved
    ``standard``-role model here so a never-audited app's provisioning audit
    doesn't inherit the bot's ``power`` rung (decision C —
    internal/finding-new-bot-activation-cost-2026-06-12.md).
    """
    cmd = [
        binary, "agent",
        "--local", "--agent", "main",
        "--session-id", session_id,
        "--json",
        "--timeout", str(timeout_s),
        "--message", body,
    ]
    if model:
        cmd.extend(["--model", model])
    return cmd


def _dispatch_via_oc(system_prompt: str, user_message: str, *, timeout_s: int,
                    openclaw_bin: str | None = None,
                    bot_id: str | None = None,
                    shared_dir: Path | None = None,
                    model: str | None = None) -> tuple[str, int, str]:
    """Send (system, user) to the local OpenClaw agent. Returns (text, tokens, error).

    Thin wrapper preserving the legacy 3-tuple return for callers that
    don't care about cost recovery. Use ``_dispatch_via_oc_full`` for the
    full result including recovered cost and timed-out flag.

    ``model`` pins this dispatch to a specific model (the resolved
    ``standard`` role on a first audit); ``None`` inherits the bot's agent
    default — today's behavior.
    """
    res = _dispatch_via_oc_full(
        system_prompt, user_message,
        timeout_s=timeout_s, openclaw_bin=openclaw_bin,
        bot_id=bot_id, shared_dir=shared_dir, model=model,
    )
    return res.text, res.tokens, res.error


def _dispatch_via_oc_full(system_prompt: str, user_message: str, *, timeout_s: int,
                          openclaw_bin: str | None = None,
                          bot_id: str | None = None,
                          shared_dir: Path | None = None,
                          model: str | None = None) -> _DispatchResult:
    """Send (system, user) to the local OpenClaw agent.

    NB: ``openclaw agent`` has no ``--system`` flag (verified against 2026.4.29
    and 2026.5.12 — only ``--message``, ``--model``, ``--channel`` etc. exist).
    Passing one bails openclaw out with "unknown option '--system'" before the
    agent ever runs, which is what masqueraded as a brave-plugin failure prior
    to 2026-05-18 (the deprecation banner for brave's providerAuthEnvVars
    metadata fills the first ~270 chars of stderr, and the truncation at 400
    chars buried the real error). We now fold the framing into one ``--message``
    body and surface the *last* non-warning line of stderr when dispatch fails.

    Three behaviours matter beyond returning the assistant text:

    1. **Process-group kill on timeout.** ``openclaw`` forks ``openclaw-agent``
       worker processes. ``subprocess.run(timeout=N)`` only SIGKILLs the
       immediate child, so the agent worker keeps running and eventually
       finishes its (already billed) LLM call before idling forever. We
       launch with ``start_new_session=True`` and ``os.killpg`` the whole
       group on timeout so the agent dies with its parent. This is the fix
       for the zombie-accumulation observed in the 2026-05-20 forensics.

    2. **Cost recovery from TurnObserver.** When the agent fires Sonnet
       and we kill it before getting the response, the wrapper has no
       token count — historically that was reported as ``tokens_used=0``
       even though Anthropic billed us for the full call. We now scan
       ``{shared_dir}/{bot_id}/turns/turns-<today>.jsonl`` (written by
       the OC plugin's TurnObserver on agent_end) for any cost event the
       agent emitted between dispatch start and now, and populate tokens
       + cost from there. Best-effort: if we killed before agent_end fired,
       the cost event isn't in the file and we'll still under-report.

    3. **Message size cap.** A 200k-char message body is almost certainly
       a runaway prompt; refuse before firing the LLM call.
    """
    binary = openclaw_bin or _resolve_openclaw_bin()
    if not binary:
        return _DispatchResult("", 0, 0.0, "openclaw binary not found on PATH", False)

    # Fold system + user into a single message body, separated by a marker the
    # agent reads as plain context. The audit framing tells the agent exactly
    # what shape of JSON to return; that instruction must arrive on the same
    # turn as the inputs.
    body = f"{system_prompt}\n\n---\n\n{user_message}"

    if len(body) > _MESSAGE_MAX_CHARS:
        return _DispatchResult(
            "", 0, 0.0,
            f"message body {len(body)} chars exceeds cap {_MESSAGE_MAX_CHARS}; refusing dispatch",
            False,
        )

    # Per-dispatch ephemeral session-id. Without this, every `--agent main` call
    # with no override accumulates into the shared `agent:main:main` session on
    # disk (e.g. /Users/<bot>/.openclaw/agents/main/sessions/<uuid>.jsonl) — over
    # weeks of audits the message history grows past the model's compaction
    # threshold and the `openclaw agent` worker hangs after the response is
    # generated, holding a session lock and refusing to exit (observed on atlas
    # 2026-06-07 with a 64-message / 3.5MB primary session). A fresh UUID per
    # dispatch routes each one-shot to its own session file, matching how
    # bot_forge.py already isolates forge dispatches.
    cmd = _build_audit_agent_cmd(
        binary=binary, body=body, timeout_s=timeout_s,
        session_id=str(uuid.uuid4()), model=model,
    )
    started_at = time.time()
    proc: subprocess.Popen | None = None
    try:
        # start_new_session=True puts the child in its own process group so
        # killpg can take down the whole tree (openclaw → openclaw-agent
        # workers → plugin processes) on timeout. Without this, the
        # background workers orphan and keep running.
        #
        # cwd="/tmp" because openclaw calls libuv's uv_cwd() at startup; when
        # the runner inherits a CWD the evolve user can't getcwd() (admin-ui
        # sudo-kicks the runner from /Users/pod_admin_user/...), openclaw exits 1
        # with "EACCES: permission denied, uv_cwd" before parsing argv,
        # surfacing as "openclaw exit=1: [openclaw] Help: openclaw --help"
        # (the last non-warning stderr line). Same family as the
        # sudo-evolve-python sys.path issue — neutralize via /tmp.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True, cwd="/tmp",
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            stdout, stderr = proc.communicate(timeout=10)
            tokens, cost = _recover_cost_from_turn_observer(
                bot_id, shared_dir, started_at,
            )
            return _DispatchResult(
                "", tokens, cost,
                f"dispatch timeout after {timeout_s}s; killed process group "
                f"(recovered tokens={tokens}, cost=${cost:.4f} from TurnObserver)",
                True,
            )
    except OSError as exc:
        if proc is not None:
            _kill_process_group(proc)
        return _DispatchResult("", 0, 0.0, f"dispatch OSError: {exc}", False)

    if proc.returncode != 0:
        # Even on non-timeout error, the agent may have fired the LLM call
        # before exiting. Recover any cost so we don't silently under-report.
        tokens, cost = _recover_cost_from_turn_observer(
            bot_id, shared_dir, started_at,
        )
        return _DispatchResult(
            "", tokens, cost,
            f"openclaw exit={proc.returncode}: {_summarize_stderr(stderr)}",
            False,
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Some openclaw modes print plain text — fall back to raw stdout.
        return _DispatchResult(stdout.strip(), 0, 0.0, "", False)

    text, tokens = _extract_text_and_tokens(payload)
    # Cost from the openclaw payload would be ideal, but it's not part of the
    # documented usage shape. Recover from TurnObserver as a sanity check.
    _, cost = _recover_cost_from_turn_observer(bot_id, shared_dir, started_at)
    return _DispatchResult(text, tokens, cost, "", False)


def _extract_text_and_tokens(payload: dict) -> tuple[str, int]:
    """Pull assistant text + token count from ``openclaw agent --json`` output.

    Current shape (2026.5.22) is ``{"payloads":[{"text": "..."}], "meta":
    {"agentMeta": {"usage": {"input": N, "output": N, ...}}}}``. Older
    versions (pre-2026.5) emitted ``{"text": "...", "usage": {"input_tokens":
    N, "output_tokens": N}}`` at the top level. Both shapes are handled so a
    mid-upgrade pod doesn't see Stage 3a silently return empty observations
    (status=ok, tokens=0, findings=0 — looks fine, does nothing).
    """
    # New shape: payloads[0].text + meta.agentMeta.usage.{input,output}
    text = ""
    payloads = payload.get("payloads")
    if isinstance(payloads, list) and payloads:
        first = payloads[0] if isinstance(payloads[0], dict) else {}
        text = (first.get("text") or "").strip()
    if not text:
        # Old shape fallback.
        text = (payload.get("text") or payload.get("message") or "").strip()

    tokens = 0
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    agent_meta = meta.get("agentMeta") if isinstance(meta.get("agentMeta"), dict) else {}
    usage_new = agent_meta.get("usage") if isinstance(agent_meta.get("usage"), dict) else {}
    if usage_new:
        tokens = int(usage_new.get("input", 0)) + int(usage_new.get("output", 0))
    if not tokens:
        usage_old = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        tokens = int(usage_old.get("input_tokens", 0)) + int(usage_old.get("output_tokens", 0))
    return text, tokens


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole session, escalate to SIGKILL if it doesn't die.

    On POSIX, ``start_new_session=True`` made proc.pid the session leader,
    which on macOS/Linux means proc.pid is also the process-group ID. So
    ``killpg(pid, signal)`` hits the whole tree. We send SIGTERM first to
    give in-flight network I/O a chance to flush (so the cost event has a
    better shot of landing in TurnObserver's file), then SIGKILL after a
    short grace period.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (_signal.SIGTERM, _signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


def _recover_cost_from_turn_observer(
    bot_id: str | None,
    shared_dir: Path | None,
    started_at: float,
) -> tuple[int, float]:
    """Scan TurnObserver-written cost events for calls fired since ``started_at``.

    Returns ``(tokens_total, cost_usd_total)``. Returns (0, 0.0) when bot_id
    or shared_dir aren't known, the turns file doesn't exist, or no matching
    event landed. We accept events with ts within a ±5s window around
    started_at to handle clock-skew between the wrapper and the agent
    process; without that, an event written at exactly started_at can be
    missed by strict greater-than comparison.
    """
    if not bot_id or shared_dir is None:
        return 0, 0.0
    today = datetime.now(timezone.utc).date().isoformat()
    turns_path = Path(shared_dir) / bot_id / "turns" / f"turns-{today}.jsonl"
    if not turns_path.exists():
        return 0, 0.0
    # Brief wait so the in-flight agent_end write has a chance to flush.
    # TurnObserver writes synchronously inside handleTurn but the OS file
    # buffer can lag; 0.5s covers the typical case without delaying the
    # caller noticeably.
    time.sleep(0.5)
    cutoff = started_at - 5.0
    tokens_total = 0
    cost_total = 0.0
    try:
        with turns_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Filter to ONLY the events that ``openclaw agent --local``
                # produces: channel="unknown" because the CLI agent has no
                # channel context. Without this filter, an audit dispatch
                # running concurrently with live Slack/Telegram traffic
                # would over-attribute those user turns to the audit cost.
                # The reviewer agent caught this during the 2026-05-21
                # second-pass review.
                if rec.get("channel") != "unknown":
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, str):
                    continue
                try:
                    rec_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if rec_dt.timestamp() < cutoff:
                    continue
                tokens_total += int(rec.get("input_tokens", 0) or 0)
                tokens_total += int(rec.get("output_tokens", 0) or 0)
                cost_total += float(rec.get("cost", 0) or 0)
    except (OSError, PermissionError):
        return 0, 0.0
    return tokens_total, cost_total


def _summarize_stderr(stderr: str) -> str:
    """Return the actionable line(s) of openclaw stderr for error reporting.

    openclaw prints a "Config warnings:" block on every invocation for any
    deprecated config keys (e.g. brave's ``providerAuthEnvVars`` on 4.29).
    The block can be several hundred chars and historically buried the real
    error under a fixed 400-char cap. We also need to step around openclaw's
    fixed boilerplate footer on startup failures, which looks like::

        [openclaw] Could not start the CLI.
        [openclaw] Reason: EACCES: permission denied, uv_cwd
        [openclaw] Debug: set OPENCLAW_DEBUG=1 to include the stack trace.
        [openclaw] Try: openclaw doctor
        [openclaw] Help: openclaw --help

    The original "last non-empty line" heuristic surfaced
    ``[openclaw] Help: openclaw --help`` — true to the CLI's convention but
    useless for diagnosis. The actually-useful diagnostic is the
    ``Reason:`` line. We prefer it explicitly.

    Steps:
      1. Strip lines that belong to a "Config warnings:" block.
      2. Drop boilerplate ``[openclaw] {Debug|Try|Help}: ...`` lines that
         appear on every startup failure and never carry the cause.
      3. Prefer the first ``Reason:`` / ``Error:`` line if one exists —
         that's openclaw's diagnostic line. Fall back to the last
         remaining non-empty line for non-startup errors that don't use
         the Reason convention.
      4. Cap at 600 chars (not 400) so multi-line errors still survive.
      5. If everything stripped out (stderr is warnings-only — rare; means
         openclaw exited non-zero for a reason that didn't reach stderr),
         return a short hint pointing the operator at the warning preamble.
    """
    if not stderr:
        return ""

    # A "Config warnings:" block runs from its header until the first blank
    # line or a line that doesn't look like a bullet/continuation. Drop it.
    cleaned: list[str] = []
    in_warning_block = False
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("config warnings:"):
            in_warning_block = True
            continue
        if in_warning_block:
            # Bullets and indented continuations belong to the block.
            if stripped.startswith(("-", "*")) or line.startswith(" "):
                continue
            if not stripped:
                in_warning_block = False
                continue
            in_warning_block = False  # this line is real stderr — keep it
        if stripped:
            cleaned.append(stripped)

    if not cleaned:
        return "(config warnings only; no error line — check stderr in logs)"

    # Drop the openclaw startup-failure boilerplate footer (Debug/Try/Help)
    # so the Reason: line below isn't outranked by useless guidance.
    _boilerplate_prefixes = (
        "[openclaw] Debug:",
        "[openclaw] Try:",
        "[openclaw] Help:",
    )
    cleaned = [ln for ln in cleaned if not ln.startswith(_boilerplate_prefixes)]
    if not cleaned:
        return "(openclaw startup boilerplate only; no cause line — check stderr in logs)"

    # Prefer the first explicit Reason:/Error: line — that's the
    # diagnostic openclaw printed before the boilerplate footer.
    for ln in cleaned:
        # Match `[openclaw] Reason: ...`, `Reason: ...`, `[openclaw] Error: ...`, `Error: ...`.
        lowered = ln.lower()
        if (
            "] reason:" in lowered
            or lowered.startswith("reason:")
            or "] error:" in lowered
            or lowered.startswith("error:")
        ):
            return ln[:600]

    # No Reason/Error line — fall back to the last non-empty line.
    return cleaned[-1][:600]


def _resolve_openclaw_bin() -> str | None:
    """Find the openclaw binary (Homebrew + standard paths).

    Delegates to the single shared resolver in ``platform_profile`` so this
    dispatcher path (App Audit Tier 3, Coherence Pass A, Repair-with-atlas)
    sees the SAME candidate list as deploy / setup_wizard / safe_upgrade. The
    old two-candidate copy here missed the node_modules .mjs entrypoint and
    the Linux paths, so on the mini (which() → None under the stripped-PATH
    LaunchDaemon) it returned None and dispatch failed with "openclaw binary
    not found on PATH". Returns None when nothing exists — the caller emits
    the unreachable-dispatcher message.
    """
    from platform_profile import find_openclaw_cli
    return find_openclaw_cli()


# ── Output parsing ──────────────────────────────────────────────────────────


def _parse_json_array(text: str) -> list[dict]:
    """Extract a JSON array from an LLM response.

    Strips accidental code fences and prose around the array. Returns [] on
    unparseable input — the caller decides whether to flag run_failed.
    """
    if not text:
        return []
    stripped = text.strip()
    # Code-fence strip
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped.strip())
    # Try direct
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass
    # Hunt for the first array
    m = re.search(r"\[[\s\S]*\]", stripped)
    if m:
        try:
            parsed = json.loads(m.group(0))
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _coerce_observation(raw: dict, idx: int) -> Observation | None:
    """Normalize one LLM-emitted observation dict into an Observation.

    Drops entries with invalid category/severity — Stage 3a is told the
    valid sets and we don't want garbage flowing downstream.
    """
    if not isinstance(raw, dict):
        return None
    category = str(raw.get("category", "")).strip().lower()
    if category not in VALID_CATEGORIES:
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
    return Observation(
        obs_id=str(raw.get("obs_id", f"obs-{idx}")),
        category=category,
        severity=severity,
        description=description,
        evidence=[str(e) for e in evidence],
        suggested_action=str(raw.get("suggested_action", "")).strip(),
    )


def _coerce_decision(raw: dict, fallback_obs_id: str) -> TriageDecision | None:
    if not isinstance(raw, dict):
        return None
    outcome = str(raw.get("outcome", "")).strip().lower()
    if outcome not in VALID_OUTCOMES:
        outcome = OUTCOME_PROPOSE  # Conservative default — surface to operator.
    return TriageDecision(
        obs_id=str(raw.get("obs_id", fallback_obs_id)),
        outcome=outcome,
        rationale=str(raw.get("rationale", "")).strip(),
        transformation=str(raw.get("transformation", "")).strip(),
    )


# ── Public entry point ─────────────────────────────────────────────────────


def run_tier3_for_app(
    *,
    manifest: dict,
    workspace: Path,
    bot_id: str,
    audit_run_id: str,
    full_audit: bool,
    openclaw_bin: str | None = None,
    accepted_signatures: set[str] | None = None,
    shared_dir: Path | None = None,
    model: str | None = None,
) -> AuditOutput:
    """End-to-end Tier-3 audit for a single app.

    Steps:
      1. Assemble inputs (manifest, files, trail tail, accepted list).
      2. Stage 3a Discovery → list of Observations.
      3. Filter observations whose signature appears in accepted_signatures
         (defense-in-depth alongside Stage 3a's prompt-side filter).
      4. Stage 3b Triage → list of TriageDecisions.
      5. Return AuditOutput.

    Returns an AuditOutput. If a stage fails, status="failed" with the
    error captured; partial observations are still preserved so the trail
    shows what we did manage to extract.

    ``model`` pins both stages to a specific model. The runner passes the
    pod's resolved ``standard``-role model on a just-built app's FIRST audit
    (provisioning) so it doesn't inherit the bot's ``power`` rung; ``None``
    (every recurring audit) inherits the bot's agent default — today's
    behavior. Decision C, internal/finding-new-bot-activation-cost-2026-06-12.md.
    """
    app_id = manifest.get("id") or "unknown"
    started_at = _iso_now()
    out = AuditOutput(
        audit_run_id=audit_run_id,
        bot_id=bot_id,
        app_id=app_id,
        status="ok",
        started_at=started_at,
        completed_at=started_at,
        full_audit=full_audit,
    )

    inputs = assemble_inputs(manifest, workspace, full_audit=full_audit)
    accepted_set = accepted_signatures or set(inputs.get("accepted_signatures") or [])

    # ── Stage 3a Discovery ───────────────────────────────────────────────────
    accepted_block = (
        json.dumps(sorted(accepted_set), indent=2) if accepted_set else "[]"
    )
    sys3a = _STAGE_3A_SYSTEM.format(
        bot_id=bot_id,
        valid_categories=list(VALID_CATEGORIES),
        accepted_block=accepted_block,
    )
    text3a, tokens3a, err3a = _dispatch_via_oc(
        sys3a, stage_3a_prompt(inputs),
        timeout_s=_DISCOVERY_TIMEOUT_S,
        openclaw_bin=openclaw_bin,
        bot_id=bot_id,
        shared_dir=shared_dir,
        model=model,
    )
    out.tokens_used += tokens3a
    if err3a:
        out.status = "failed"
        out.error = f"stage 3a: {err3a}"
        out.completed_at = _iso_now()
        return out

    raw_obs = _parse_json_array(text3a)
    observations: list[Observation] = []
    for i, raw in enumerate(raw_obs):
        obs = _coerce_observation(raw, i)
        if obs is None:
            continue
        # Defense-in-depth: drop accepted observations even if Stage 3a
        # ignored the instruction to filter them out itself.
        if not full_audit and obs.signature(bot_id, app_id) in accepted_set:
            continue
        observations.append(obs)
    out.observations = observations

    # No observations → no triage needed.
    if not observations:
        out.status = "ok"
        out.completed_at = _iso_now()
        return out

    # ── Stage 3b Triage ──────────────────────────────────────────────────────
    text3b, tokens3b, err3b = _dispatch_via_oc(
        _STAGE_3B_SYSTEM,
        stage_3b_prompt(observations),
        timeout_s=_TRIAGE_TIMEOUT_S,
        openclaw_bin=openclaw_bin,
        bot_id=bot_id,
        shared_dir=shared_dir,
        model=model,
    )
    out.tokens_used += tokens3b
    if err3b:
        out.status = "failed"
        out.error = f"stage 3b: {err3b}"
        # Fall through to default-propose so observations aren't lost.
        out.decisions = [
            TriageDecision(obs_id=o.obs_id, outcome=OUTCOME_PROPOSE,
                           rationale="triage failed; defaulting to propose")
            for o in observations
        ]
        out.completed_at = _iso_now()
        return out

    raw_decisions = _parse_json_array(text3b)
    decisions: list[TriageDecision] = []
    by_obs_id = {o.obs_id: o for o in observations}
    seen_obs_ids: set[str] = set()
    for i, raw in enumerate(raw_decisions):
        fallback = observations[i].obs_id if i < len(observations) else f"obs-{i}"
        dec = _coerce_decision(raw, fallback)
        if dec is None:
            continue
        if dec.obs_id not in by_obs_id:
            # LLM emitted a decision for an obs_id we don't recognize — skip.
            continue
        seen_obs_ids.add(dec.obs_id)
        decisions.append(dec)
    # Backfill: any observation Stage 3b didn't return a decision for
    # defaults to propose. Safety bias: surface > silently lose.
    for o in observations:
        if o.obs_id not in seen_obs_ids:
            decisions.append(TriageDecision(
                obs_id=o.obs_id, outcome=OUTCOME_PROPOSE,
                rationale="missing triage decision; defaulted to propose",
            ))
    out.decisions = decisions
    out.status = "with_findings"
    out.completed_at = _iso_now()
    return out
