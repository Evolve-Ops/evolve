"""model_liveness — prove a model actually DISPATCHES before anything routes to it.

Spec: [internal/spec-model-auto-upgrade-2026-07-30.md](../../internal/spec-model-auto-upgrade-2026-07-30.md)
§Liveness guard — **Phase 2 of auto-upgrade**, and independently a standing
provider-health monitor. Phase 1 (``model_auto_upgrade``, #3515) judges every
detected version upgrade against conditions 1-5+7 and leaves each eligible
decision carrying ``pending_guards=("liveness",)``; this module is the guard
that fills that seam. Phase 3 (the apply path) does not exist yet — **nothing
here mutates any config, rung, or routing. Read + dispatch only.**

Why the probe exists — the #3489 signature
──────────────────────────────────────────
A model can be present in the catalog, report ``available: true, missing:
false`` from the capability/``models list`` resolver, and still fail every
real request. PR #3489: provider blocks registered with only a ``models``
list and no explicit ``api`` were dispatched to the OpenAI Responses
transport — a Google key and an xai key were being POSTed to
api.openai.com and 401ing. The 401 reads as key expiry; the credential was
fine and aimed at the wrong vendor. Two providers' rungs were dead
fleet-wide for WEEKS because the only thing that exercised the real
dispatch path was a real turn.

Therefore the probe MUST NOT ask the resolver anything. It performs one
trivial round-trip through the same embedded-agent path a real turn uses:

    sudo -H -u <bot> openclaw agent --json --agent main \
        --session-id <fresh probe id> --model <target> \
        --thinking off --timeout <s> --message "<tiny ping>"

Invocation shape verified against the installed OpenClaw (2026.7.1-2):
``openclaw agent`` runs the turn via the Gateway — the same dispatch +
provider-transport resolution an inbound channel message rides (the path
that was broken) — and ``--model <id>`` is a documented per-run override
accepting ``provider/model`` or a bare id. ``--deliver`` is deliberately
absent, so the reply goes nowhere; a fresh session id per probe keeps real
sessions untouched and the turn context empty. There is no output-cap flag
on ``openclaw agent`` (checked against the installed CLI), so cost is
bounded from the prompt side: a one-line message requesting a one-word
reply, thinking off.

The subprocess goes through ``oc_cli.oc_command`` (the mandated wrapper for
every cross-bot OC CLI invocation — sudo resolution, bot-home cwd for the
Node ``uv_cwd()`` EACCES class, misinvocation Signals), with ``cache_ttl=0``
because a liveness answer must never be a cached one.

The verdict must prove the TARGET answered
──────────────────────────────────────────
OpenClaw is known to reject model overrides it does not authorize and
silently route the turn elsewhere (the 2026.7 subagent-override incident).
A probe that only checks "the turn succeeded" would read such a reroute as
GREEN — proving nothing about the target. So the verdict compares the model
OC *reports served the turn* (``result.meta.agentMeta.model``) against the
requested target (bare ids, date-suffix-insensitive):

  * ``ok``               — turn completed AND the target itself answered.
  * ``dispatch_failed``  — the turn errored: CLI failure, timeout, non-JSON,
                           error status, or an empty reply. The #3489 class.
  * ``rerouted``         — the turn completed on a DIFFERENT model. Routing
                           to the target does not actually reach it.
  * ``unverified``       — the turn completed but OC did not say which model
                           served it (shape drift). Proves nothing — fail
                           safe: never treated as ok, never fired as "model
                           down" either.

Signal (the standalone provider-health monitor)
───────────────────────────────────────────────
Producer ``model_liveness_probe``; type ``model_dispatch_failed``;
pod-scoped, one Signal per target model (signature keyed on the bare model
id, stable across runs — ``observe()`` find-or-creates, so a still-dead
model bumps its existing Signal). ``dispatch_failed`` and ``rerouted`` fire;
only targets THIS RUN probed and found ``ok`` are swept via
``sweep_resolve`` at the end — every other active signature (``unverified``
probes, targets a partial run never touched: ``--model`` spot-checks,
cap-dropped targets, bots whose resolution broke) rides in the keep-set, so
a probe that cannot see, and a run that did not look, never archive a live
outage — the same fail-safe-unreadable posture as the drift monitors.

Shape-drift posture, decided deliberately: a turn whose envelope is
unparseable or whose reply text is empty judges ``dispatch_failed`` (fires),
not ``unverified``. False-RED is the chosen failure direction — for the
guard both read "do not apply", and on the monitor side a CLI envelope
rename would page all probed models at once, which is loud, obviously
correlated, and fixed in one place; a false GREEN or a silent skip would be
neither. Only the narrow completed-turn-but-model-unreported case is
``unverified``, because there a real reply exists and "down" would be a lie.

The default run probes only the models the pod actually routes to: each
bot's five role resolutions (``primary_bot.resolve_roles_with_provenance``,
credential-aware — i.e. the ``models[0]``-reachable targets), deduped
pod-wide by bare id, primary bot first, capped at
:data:`MAX_PROBES_PER_RUN`. Dropped targets are named in the run summary —
a bounded run must say what it did not check.

Probes cost real tokens — and NOT "a few dozen": ``openclaw agent`` runs
the bot's full embedded agent, so every probe pays the bot's whole
system-prompt/bootstrap prefix as input (order 10k-27k tokens on this
fleet's measurements), plus a one-word output. **MEASURED, not estimated**
(evolve-vps, August 2026): ~$0.89 per scheduled run across 4 resolved
targets, of which ``claude-opus-4-8`` alone is ~$0.75 (84%). That is
~$27/month at the daily cadence — the earlier "~8 turns/day, roughly
low-single-digit dollars/month" in this docstring was wrong by an order of
magnitude, and it is what let the real cost go unexamined.

Two things make each probe more expensive than the token count suggests:

- the prefix is billed as a CACHE WRITE at 1.25x base input rate, and
- ``cache_read`` was 0 on every probe for the whole month — a fresh
  session id per probe plus a ~24h cadence versus a 5-minute cache TTL
  means the cache can never be hit, so the 1.25x is paid every time.

The spend is bounded by design — probes only the role-resolved
(``models[0]``-reachable) targets deduped pod-wide, capped at
:data:`MAX_PROBES_PER_RUN` per run, one-line prompt / one-word reply /
thinking off, DAILY cadence — but "bounded" is not "cheap", and it lands on
each probing bot's own cost accounting where it is indistinguishable from
real usage. On 2026-08-19 the ``darwin`` bot tripped its $5.00/day breaker
on this monitor alone after a deploy bug force-ran it 6x (fixed: the
deploy bounce no longer restarts timer-activated oneshot jobs); month to
date it was $27.50 of darwin's $33.10 total spend. The fresh per-probe
session ids also accrue small OC session files in the bot's state;
accepted — the alternative (one stable session) grows turn context
instead, which costs more.

Testing: every entry point takes an injected ``runner`` (and the target
enumeration an injected ``roles_resolver``), so tests use fakes and never
shell out or touch the network.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import model_discovery as _md
from evolve_config import CANONICAL_SHARED_DIR, get_members, get_primary, load_config
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "model_liveness_probe"
SIGNAL_TYPE = "model_dispatch_failed"

#: The pending-guard name Phase 1 leaves on every eligible decision
#: (``model_auto_upgrade._judge``) and :func:`apply_liveness_guard` clears.
LIVENESS_GUARD = "liveness"

# Hold codes for the guard helper — same contract as the Phase 1 HOLD_*
# codes (stable machine-readable code + one plain operator sentence).
# Deliberately NOT in ``model_auto_upgrade.DURABLE_HOLD_CODES``: a failed or
# incomplete probe is transient — the next cycle re-probes.
HOLD_LIVENESS_FAILED = "liveness_probe_failed"
HOLD_LIVENESS_UNVERIFIED = "liveness_probe_unverified"

# ── Probe cost/cadence constants (explicit by design — spec §Liveness guard,
#    chip constraint "cadence/caps explicit") ────────────────────────────────

#: Upper bound on real-token probes in one monitor run. Targets beyond the
#: cap are reported as dropped in the run summary, never silently omitted.
MAX_PROBES_PER_RUN = 8

# ── INTERIM COST BRAKE (2026-08-19) — MEANT TO BE REVERTED ───────────────────
# Models the SCHEDULED sweep declines to probe. Purely a cost measure, not a
# capability judgement: claude-opus-4-8 is ~$0.75 of the run's ~$0.89 (84%),
# and the daily standing sweep is the only thing paying it.
#
# It stays fully reachable on the spot-check path — `python3 -m model_liveness
# --model claude-opus-4-8` probes it exactly as before — because this list is
# applied ONLY where the routed set is built, never to explicit --model targets.
#
# Fail-safe by construction: an unprobed model's existing outage Signal is
# KEPT, not swept (see emit_signals — the keep-set is every active signature
# minus the probed-ok ones), so dropping a model here cannot silently archive
# a live outage. The tradeoff is real and one-directional: a dispatch break in
# an excluded model goes undetected by this monitor until someone spot-checks.
#
# Revert by emptying this tuple. A liveness redesign is in design now and will
# likely delete the standing daily sweep outright, at which point this brake
# and the debate about which models it covers both go away.
SCHEDULED_PROBE_EXCLUSIONS: tuple[str, ...] = ("claude-opus-4-8",)

#: Scheduled cadence (documented here; enforced by the launchd/systemd job
#: installed via ``analyzer_monitor_jobs``). Daily: provider transport
#: breakage is a slow-moving, weeks-long-silent class — the spec's bar is
#: "a one-week-max detected outage", and daily beats it at ~8 tiny turns/day.
CADENCE_SECONDS = 24 * 3600

#: OC-side turn deadline (--timeout). A liveness ping needs seconds; a minute
#: absorbs cold-start latency without letting a hung provider hold the run.
DEFAULT_TIMEOUT_SECONDS = 60

#: Extra headroom the *subprocess* deadline gets over the OC-side one, so a
#: turn OC itself times out still reports through the JSON envelope rather
#: than as a killed process.
SUBPROCESS_GRACE_SECONDS = 15

#: The whole probe request. One line in, one word out, no thinking budget.
PROBE_MESSAGE = "Connectivity check. Reply with the single word: OK"

#: OC agent id — v1 pods are 1:1 bot↔agent, same constant the evo proxy pins.
DEFAULT_AGENT = "main"

# Verdict codes.
VERDICT_OK = "ok"
VERDICT_DISPATCH_FAILED = "dispatch_failed"
VERDICT_REROUTED = "rerouted"
VERDICT_UNVERIFIED = "unverified"


def _norm_model(model_id: str) -> str:
    """Canonical comparison form: bare id, lowercased, date suffix stripped —
    so ``anthropic/claude-opus-5`` matches a reported ``claude-opus-5-20260115``
    (the same alias/snapshot equivalence the freshness engine uses)."""
    return _md._strip_date_suffix(_md._bare_id(model_id or "").lower())


def _provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0].lower() if "/" in (model_id or "") else ""


# ── Runner seam ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProbeRequest:
    """One agent-turn invocation, fully specified — what the runner executes."""

    bot_id: str
    model: str                # qualified or bare id, passed to --model verbatim
    message: str = PROBE_MESSAGE
    session_id: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @property
    def cli_args(self) -> list[str]:
        """The exact ``openclaw`` argv tail (shape verified against the
        installed OC 2026.7.1-2 ``agent --help``). Centralized so tests can
        assert the invocation and a CLI migration has one place to edit."""
        return [
            "agent",
            "--agent", DEFAULT_AGENT,
            "--session-id", self.session_id,
            "--model", self.model,
            "--thinking", "off",
            "--timeout", str(int(self.timeout_seconds)),
            "--json",
            "--message", self.message,
        ]


@dataclass
class AgentRunResult:
    """What one runner invocation produced: the parsed ``--json`` envelope
    (or None when the CLI failed) plus the transport-level error strings."""

    payload: Any = None
    errors: list[str] = field(default_factory=list)
    latency_ms: int | None = None


#: The injectable seam. Tests supply fakes; the default shells out through
#: ``oc_cli`` (never subprocess directly — repo convention).
AgentRunner = Callable[[ProbeRequest], AgentRunResult]


def oc_agent_runner(req: ProbeRequest) -> AgentRunResult:
    """The real runner: one gateway agent turn as the bot user via
    ``oc_cli.oc_command`` (sudo + bot-home cwd + misinvocation signals handled
    there). ``cache_ttl=0`` — a liveness verdict must never be cached."""
    import oc_cli

    errs: list[str] = []
    t0 = time.monotonic()
    payload = oc_cli.oc_command(
        req.bot_id,
        req.cli_args,
        timeout=int(req.timeout_seconds) + SUBPROCESS_GRACE_SECONDS,
        cache_ttl=0,
        _err_out=errs,
    )
    ms = int((time.monotonic() - t0) * 1000)
    return AgentRunResult(payload=payload, errors=errs, latency_ms=ms)


# ── Verdict ──────────────────────────────────────────────────────────────────


@dataclass
class ProbeVerdict:
    """The structured outcome of probing one (bot, model) target."""

    bot_id: str
    model: str                     # the requested target, as configured
    code: str                      # VERDICT_* value
    observed_model: str | None = None
    error: str | None = None       # short stable class, e.g. "timeout", "http:401"
    detail: str = ""               # one-line human-readable cause
    latency_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.code == VERDICT_OK

    @property
    def proven_dead(self) -> bool:
        """True when the probe POSITIVELY showed the target does not serve
        (fires the monitor Signal). ``unverified`` is deliberately excluded —
        a probe that could not see must neither fire nor clear."""
        return self.code in (VERDICT_DISPATCH_FAILED, VERDICT_REROUTED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "model": self.model,
            "code": self.code,
            "ok": self.ok,
            "observed_model": self.observed_model,
            "error": self.error,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


def _envelope_text(payload: Mapping[str, Any]) -> str:
    """First payload text out of the ``openclaw agent --json`` envelope
    (same shape ``evo.proxy._parse_agent_json`` documents)."""
    result = payload.get("result")
    payloads = result.get("payloads") if isinstance(result, Mapping) else None
    first = payloads[0] if isinstance(payloads, list) and payloads else None
    text = first.get("text") if isinstance(first, Mapping) else None
    return text if isinstance(text, str) else ""


def _envelope_model(payload: Mapping[str, Any]) -> str | None:
    """The model OC reports actually served the turn
    (``result.meta.agentMeta.model``), or None when absent."""
    result = payload.get("result")
    meta = result.get("meta") if isinstance(result, Mapping) else None
    agent_meta = meta.get("agentMeta") if isinstance(meta, Mapping) else None
    model = agent_meta.get("model") if isinstance(agent_meta, Mapping) else None
    return model if isinstance(model, str) and model else None


def judge_run(req: ProbeRequest, res: AgentRunResult) -> ProbeVerdict:
    """Apply the verdict rules to one runner result. Pure — fully unit-testable
    against synthetic envelopes."""
    verdict = ProbeVerdict(
        bot_id=req.bot_id, model=req.model,
        code=VERDICT_DISPATCH_FAILED, latency_ms=res.latency_ms,
    )
    if res.payload is None:
        cause = res.errors[-1] if res.errors else "no output from the agent CLI"
        verdict.error = "cli-failure"
        verdict.detail = (
            f"agent turn produced no result ({cause}) — the request never "
            f"completed through the gateway dispatch path"
        )
        return verdict
    if not isinstance(res.payload, Mapping):
        verdict.error = "bad-envelope"
        verdict.detail = "agent CLI returned JSON that is not an object"
        return verdict

    status = res.payload.get("status")
    if isinstance(status, str) and status and status != "ok":
        err_text = str(res.payload.get("error") or "").strip()
        verdict.error = f"status:{status}"
        verdict.detail = (
            f"agent turn ended with status {status!r}"
            + (f": {err_text[:200]}" if err_text else "")
        )
        return verdict

    text = _envelope_text(res.payload).strip()
    observed = _envelope_model(res.payload)
    verdict.observed_model = observed
    if not text:
        verdict.error = "empty-reply"
        verdict.detail = (
            "agent turn returned no reply text — the model produced nothing "
            "(dispatch or provider failure swallowed upstream)"
        )
        return verdict
    if observed is None:
        verdict.code = VERDICT_UNVERIFIED
        verdict.error = "model-unreported"
        verdict.detail = (
            "the turn completed but OC did not report which model served it — "
            "this proves nothing about the target"
        )
        return verdict
    if _norm_model(observed) != _norm_model(req.model):
        verdict.code = VERDICT_REROUTED
        verdict.error = "rerouted"
        verdict.detail = (
            f"the turn was served by {observed} instead of the requested "
            f"{req.model} — the gateway will not actually route to the target"
        )
        return verdict

    verdict.code = VERDICT_OK
    verdict.error = None
    verdict.detail = f"target answered ({len(text)} chars, model {observed})"
    return verdict


def probe_model(
    bot_id: str,
    model: str,
    *,
    runner: AgentRunner | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeVerdict:
    """ONE trivial round-trip through the embedded-agent path for ``model``
    as ``bot_id``, judged. This is the whole Phase 2 primitive.

    A fresh, obviously-synthetic session id per probe keeps real sessions
    untouched and the turn's context empty (no accumulated probe history).
    """
    req = ProbeRequest(
        bot_id=bot_id,
        model=model,
        session_id=f"evolve-liveness-{uuid.uuid4().hex[:12]}",
        timeout_seconds=timeout_seconds,
    )
    run = (runner or oc_agent_runner)(req)
    return judge_run(req, run)


# ── Phase 1 seam: clear / keep the pending liveness guard ────────────────────


def apply_liveness_guard(decision: Any, verdict: ProbeVerdict) -> Any:
    """Fold a probe verdict into an eligible Phase 1 decision.

    Takes a ``model_auto_upgrade.UpgradeDecision`` whose ``pending_guards``
    carries ``"liveness"`` and returns a NEW decision (the input is never
    mutated — same purity contract as the engine):

      * verdict ``ok``  → the ``liveness`` pending guard is cleared;
        ``eligible`` stands. Phase 3 may apply a decision only when
        ``pending_guards`` is empty AND ``eligible`` is True.
      * ``dispatch_failed`` / ``rerouted`` → the guard RAN and the target is
        not servable: ``eligible`` becomes False and a :data:`HOLD_LIVENESS_FAILED`
        hold is appended — surfaced exactly like conditions 1-5+7 (stable code
        + one operator-facing sentence). ``pending_guards`` empties (the guard
        is no longer pending; it has an answer).
      * ``unverified`` → the guard did NOT complete: held with
        :data:`HOLD_LIVENESS_UNVERIFIED` and ``liveness`` STAYS pending, so
        the decision reads as "guard still unanswered", never as "checked".

    Raises ``ValueError`` when the verdict is for a different model than the
    decision's target — clearing a guard with someone else's probe is a
    programming error, not a runtime condition.
    """
    from model_auto_upgrade import HoldReason

    if _norm_model(verdict.model) != _norm_model(decision.latest_model):
        raise ValueError(
            f"verdict is for {verdict.model!r}, decision targets "
            f"{decision.latest_model!r} — refusing to apply a mismatched probe"
        )

    new_bare = _md._bare_id(decision.latest_model)
    out = dataclasses.replace(
        decision,
        roles=list(decision.roles),
        holds=list(decision.holds),
        scopes=list(decision.scopes),
        enabled_scopes=list(decision.enabled_scopes),
        cost_evidence=dict(decision.cost_evidence),
    )
    remaining = tuple(g for g in out.pending_guards if g != LIVENESS_GUARD)

    if verdict.ok:
        out.pending_guards = remaining
        return out

    if verdict.code == VERDICT_UNVERIFIED:
        out.eligible = False
        out.pending_guards = decision.pending_guards  # liveness stays pending
        out.holds.append(HoldReason(
            code=HOLD_LIVENESS_UNVERIFIED,
            message=(
                f"A test request could not confirm which model answered, so "
                f"there is no proof {new_bare} actually works. The update "
                f"waits for a check that completes."
            ),
            detail={"verdict": verdict.to_dict()},
        ))
        return out

    if verdict.code == VERDICT_REROUTED:
        message = (
            f"A test request addressed to {new_bare} was answered by "
            f"{verdict.observed_model} instead — the pod cannot actually "
            f"route to it yet. The update waits until the model answers "
            f"for itself."
        )
    else:
        message = (
            f"{new_bare} may be listed as available, but a real test request "
            f"through the agent path failed ({verdict.detail}). Updating to a "
            f"model that does not answer would break this tier, so it waits "
            f"until it does."
        )
    out.eligible = False
    out.pending_guards = remaining
    out.holds.append(HoldReason(
        code=HOLD_LIVENESS_FAILED,
        message=message,
        detail={"verdict": verdict.to_dict()},
    ))
    return out


# ── Target selection (the standalone monitor's scope) ────────────────────────


@dataclass(frozen=True)
class ProbeTarget:
    """One (bot, model) the pod actually routes to."""

    bot_id: str
    model: str                    # qualified id as configured
    roles: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"bot_id": self.bot_id, "model": self.model,
                "roles": list(self.roles)}


#: roles_resolver seam: (network, bot_id) → the per-role resolution view
#: (``primary_bot.resolve_roles_with_provenance`` shape). Injectable so tests
#: never touch primary_bot's file reads.
RolesResolver = Callable[[Mapping[str, Any], str], Mapping[str, Mapping[str, Any]]]


def _credentialed_providers_for(
    network: Mapping[str, Any], bot_id: str,
) -> set[str] | None:
    """The bot's credentialed provider set, or None when it cannot be read
    (pre-install, ACL drift). None makes role resolution fail-open to the
    rung's literal ``models[0]`` — still a routed target, just not
    credential-filtered."""
    try:
        from evolve_admin.deploy import get_bot_user  # type: ignore
        from evolve_admin.provisioning import (  # type: ignore
            _read_auth_profile_providers,
        )

        user = get_bot_user(bot_id, dict(network))
        return set(_read_auth_profile_providers(user))
    except Exception:
        return None


def default_roles_resolver(
    network: Mapping[str, Any], bot_id: str,
) -> Mapping[str, Mapping[str, Any]]:
    """The real resolver: the SAME defaults ← pod ← bot merge the gateway
    routes from, credential-aware, so ``resolvedModel`` per role is exactly
    the ``models[0]``-reachable target a real turn would hit."""
    from primary_bot import resolve_roles_with_provenance

    return resolve_roles_with_provenance(
        dict(network), bot_id,
        credentialed_providers=_credentialed_providers_for(network, bot_id),
    )


def routed_probe_targets(
    network: Mapping[str, Any],
    *,
    roles_resolver: RolesResolver | None = None,
    max_targets: int = MAX_PROBES_PER_RUN,
    exclude: Sequence[str] = (),
) -> tuple[list[ProbeTarget], list[ProbeTarget]]:
    """The models the pod actually routes to, as probe targets.

    Walks every pod bot (primary first — its config is the pod-scope merge),
    collects each role's resolved model, and dedupes pod-wide by bare id: the
    first bot that routes to a model probes it. A pod-defaults bot resolves
    identically to the pod merge, so dedup collapses the fleet to the distinct
    routed set; a Custom bot contributes only the models it alone routes to.

    ``exclude`` drops models by bare id BEFORE the cap is applied, so an
    excluded model never consumes a probe slot that a real target wanted.
    Empty by default — the caller supplies the policy; see
    :data:`SCHEDULED_PROBE_EXCLUSIONS`, which ``run_probes`` applies to the
    routed path only.

    Returns ``(targets, dropped)`` — ``dropped`` is everything past
    ``max_targets`` (cost cap), returned rather than discarded so the caller
    can say what was not checked.
    """
    resolver = roles_resolver or default_roles_resolver
    bots: list[str] = []
    primary = get_primary(dict(network))
    if primary:
        bots.append(primary)
    for member in get_members(dict(network)) or []:
        if member not in bots:
            bots.append(member)

    seen: dict[str, ProbeTarget] = {}
    order: list[str] = []
    for bot_id in bots:
        try:
            view = resolver(network, bot_id)
        except Exception:
            continue  # a bot whose resolution breaks contributes nothing
        for role, info in (view or {}).items():
            model = (info or {}).get("resolvedModel")
            if not isinstance(model, str) or not model:
                continue
            key = _norm_model(model)
            if key in seen:
                if seen[key].bot_id == bot_id and role not in seen[key].roles:
                    seen[key] = dataclasses.replace(
                        seen[key], roles=(*seen[key].roles, role),
                    )
                continue
            seen[key] = ProbeTarget(bot_id=bot_id, model=model, roles=(role,))
            order.append(key)

    excluded = {_norm_model(m) for m in exclude}
    targets = [seen[k] for k in order if k not in excluded]
    return targets[:max_targets], targets[max_targets:]


# ── Signal emission (producer sweep pattern) ─────────────────────────────────


def _signature(model: str) -> str:
    return make_signature(PRODUCER, SIGNAL_TYPE, _norm_model(model))


def _signal_title(v: ProbeVerdict) -> str:
    bare = _md._bare_id(v.model)
    cause = v.error or "unknown failure"
    return f"model does not dispatch — {bare} ({cause})"


def _signal_body(v: ProbeVerdict) -> str:
    bare = _md._bare_id(v.model)
    provider = _provider_of(v.model) or "its provider"
    return (
        f"A liveness probe — one real agent turn through the same gateway "
        f"dispatch path every user message rides — failed for {bare} "
        f"(probed as {v.bot_id}):\n\n"
        f"  {v.detail}\n\n"
        f"The models resolver may still report this model available; that "
        f"disagreement is exactly the #3489 failure signature (a provider "
        f"block dispatching to the wrong transport 401s like an expired key "
        f"while `models list` stays green). Every rung routing to {bare} is "
        f"effectively dead until {provider} dispatch works, and fallback "
        f"chains will walk past it to costlier models."
    )


def _active_signatures(shared_dir: Path) -> set[str]:
    """Every currently-active (firing|snoozed) signature of this producer's
    type. Best-effort: unreadable store → empty set, which makes the sweep
    below resolve nothing extra (fail-safe direction)."""
    try:
        return {
            s.signature
            for s in signals_store.iter_signals(shared_dir)
            if s.producer == PRODUCER and s.type == SIGNAL_TYPE
            and s.state in ("firing", "snoozed")
        }
    except Exception:  # noqa: BLE001 — a blind read must not widen the sweep
        return set()


def emit_signals(
    shared_dir: Path,
    verdicts: Iterable[ProbeVerdict],
    *,
    dry_run: bool = False,
) -> set[str]:
    """Fire one pod-wide Signal per proven-dead target; sweep-resolve the
    recovered ones.

    The sweep may resolve ONLY signatures this run positively verified ok:
    the keep-set is (every active signature of this type) minus (the probed
    ``ok`` verdicts). That closes the partial-coverage hole — an outage
    Signal for a target this run did NOT probe (an explicit ``--model``
    spot-check, a cap-dropped target, a bot whose resolution broke, a
    zero-target run) survives untouched, exactly like ``sweep_resolve``'s
    ``bot_ids`` caveat demands of partial scans. ``unverified`` targets are
    likewise kept WITHOUT an observe — a probe that could not see must
    neither page nor archive.

    Returns the kept (not-swept) signature set.
    """
    verdicts = list(verdicts)
    kept: set[str] = _active_signatures(shared_dir) - {
        _signature(v.model) for v in verdicts if v.ok
    }
    for v in verdicts:
        signature = _signature(v.model)
        if v.ok:
            continue
        kept.add(signature)
        if dry_run or not v.proven_dead:
            continue
        try:
            signals_store.observe(
                shared_dir,
                signature=signature,
                producer=PRODUCER,
                type=SIGNAL_TYPE,
                severity="alert",
                flavor="maintenance",
                scope="pod",
                title=_signal_title(v),
                body=_signal_body(v),
                details={
                    "model": v.model,
                    "provider": _provider_of(v.model),
                    "probe_bot": v.bot_id,
                    "verdict": v.to_dict(),
                    "probe": "one real agent turn via the gateway dispatch path",
                    # Severity-framework fields (mirrors evo_path_probe): a
                    # routed model silently unservable — cost + capability
                    # impact fleet-wide.
                    "vector": "operations",
                    "magnitude": 3,
                    "severity_active": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 — emit is best-effort
            print(
                f"[model_liveness] observe() failed for {signature}: {exc}",
                file=sys.stderr,
            )
    if not dry_run:
        try:
            signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                types={SIGNAL_TYPE},
                reason="model liveness probe recovered",
            )
        except Exception as exc:  # noqa: BLE001 — sweep is best-effort
            print(f"[model_liveness] sweep_resolve() failed: {exc}", file=sys.stderr)
    return kept


# ── Orchestration ────────────────────────────────────────────────────────────


def run_probes(
    shared_dir: Path,
    network: Mapping[str, Any],
    *,
    runner: AgentRunner | None = None,
    roles_resolver: RolesResolver | None = None,
    targets: Sequence[ProbeTarget] | None = None,
    max_targets: int = MAX_PROBES_PER_RUN,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Probe the routed targets, emit/sweep Signals, return a run summary.

    A run that fires nothing still lists every target it checked, every
    verdict, and every target the cost cap dropped — bounded coverage must
    say what it did not cover.
    """
    dropped: list[ProbeTarget] = []
    excluded: list[str] = []
    if targets is None:
        # Routed (scheduled) path only — an explicit --model target is never
        # filtered, so the spot-check path keeps full reach.
        excluded = list(SCHEDULED_PROBE_EXCLUSIONS)
        target_list, dropped = routed_probe_targets(
            network, roles_resolver=roles_resolver, max_targets=max_targets,
            exclude=SCHEDULED_PROBE_EXCLUSIONS,
        )
    else:
        target_list = list(targets)[:max_targets]
        dropped = list(targets)[max_targets:]

    verdicts = [
        probe_model(
            t.bot_id, t.model, runner=runner, timeout_seconds=timeout_seconds,
        )
        for t in target_list
    ]
    kept = emit_signals(shared_dir, verdicts, dry_run=dry_run)
    dead = [v for v in verdicts if v.proven_dead]
    return {
        "producer": PRODUCER,
        "ok": not dead,
        "probed": len(verdicts),
        "verdicts": [v.to_dict() for v in verdicts],
        "dead_count": len(dead),
        "unverified_count": sum(
            1 for v in verdicts if v.code == VERDICT_UNVERIFIED
        ),
        "dropped_by_cap": [t.to_dict() for t in dropped],
        # Bounded coverage must say what it did not cover — a cost brake that
        # does not name itself in the summary reads as full coverage.
        "excluded_from_schedule": excluded,
        "signals_firing": sorted(kept),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Model liveness probe — one real agent turn per routed model, "
            "through the same gateway dispatch path user messages ride. "
            "Read + dispatch only; never mutates any config."
        ),
    )
    parser.add_argument(
        "--shared-dir", type=Path, default=CANONICAL_SHARED_DIR,
        help="Pod shared directory (default: platform-keyed canonical shared dir).",
    )
    parser.add_argument(
        "--network", default=None,
        help="Path to network.json (default: platform-keyed canonical config).",
    )
    parser.add_argument(
        "--bot", default=None,
        help="With --model: the bot to dispatch the probe as (default: primary).",
    )
    parser.add_argument(
        "--model", default=None,
        help="Probe ONE explicit model instead of the routed set.",
    )
    parser.add_argument(
        "--max-probes", type=int, default=MAX_PROBES_PER_RUN,
        help=f"Per-run probe cap (default {MAX_PROBES_PER_RUN}); dropped "
        f"targets are named in the summary.",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--gate", action="store_true",
        help="Exit non-zero when any probed model is dead. The default "
        "(scheduled) run exits 0 even on RED so it does not also trip "
        "cron_exit_monitor — the Signal is the alert.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Probe and print; do not write Signals or sweep.",
    )
    args = parser.parse_args(argv)

    try:
        network = load_config(args.network)
    except Exception as exc:  # noqa: BLE001 — config unreadable
        summary = {
            "producer": PRODUCER, "skipped": True,
            "reason": f"could not read network.json: {exc}",
        }
        print(json.dumps(summary, indent=2), flush=True)
        return 0

    explicit_targets: list[ProbeTarget] | None = None
    if args.model:
        bot_id = args.bot or get_primary(network)
        if not bot_id:
            summary = {
                "producer": PRODUCER, "skipped": True,
                "reason": "no bot resolved to dispatch the probe as",
            }
            print(json.dumps(summary, indent=2), flush=True)
            return 0
        explicit_targets = [ProbeTarget(bot_id=bot_id, model=args.model)]

    summary = run_probes(
        args.shared_dir,
        network,
        targets=explicit_targets,
        max_targets=args.max_probes,
        timeout_seconds=args.timeout,
        dry_run=args.dry_run,
    )
    # The summary is the LAST thing printed on a successful run, so its stdout
    # mtime is monitor_coverage's producer-liveness signal. Keep it last.
    print(json.dumps(summary, indent=2), flush=True)

    if args.gate and not summary["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
