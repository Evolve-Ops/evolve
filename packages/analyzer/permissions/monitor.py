"""permissions.monitor — drift detection across the three permission surfaces.

Spec: internal/spec-permission-posture-2026-05-10.md §3.3 (Signals) + §5.2
(drift detection).

Phase A scope: for each bot, read the live inventory, compare against
the operator-curated baseline, run denylist scans, and emit typed
Signals via signals.store.observe(). After the loop, sweep-resolve
any prior signals from this producer that didn't recur (the
"cleared condition → auto-archive" pattern).

Signal types (severity in parens; map to canonical {info,warn,alert}):
  - perm_config_drift (warn)          — observed config diverges from baseline
  - perm_config_dangerous_combo (alert) — known-bad combination observed
  - perm_approvals_denylist_match (alert) — approved pattern matches denylist
  - perm_approvals_volume_warn (info) — approvals count > warn threshold
  - perm_approvals_volume_alarm (warn) — approvals count > alarm threshold
  - perm_cron_uncapped_agent_turn (warn) — agentTurn cron lacks caps
  - perm_cron_denylist_match (alert) — cron payload matches denylist
  - perm_cron_added_silently (warn) — job present but not in cron_baseline
  - perm_openclaw_config_missing (info) — bot lacks openclaw.json

Producer name is "permission_monitor".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import inventory as _inv
from . import baseline as _bl
from . import denylist as _deny
from . import posture as _posture

# Optional signals dependency — degrades to no-op in unit tests.
try:
    from signals import store as _signals_store
    from schema.signal import make_signature as _make_signature
except ImportError:  # pragma: no cover
    _signals_store = None  # type: ignore[assignment]
    _make_signature = None  # type: ignore[assignment]


PRODUCER = "permission_monitor"

# All signal types this monitor owns — used to scope sweep_resolve so
# we don't accidentally clear another producer's signals if a future
# rename shares names.
OWNED_SIGNAL_TYPES: frozenset[str] = frozenset({
    "perm_config_drift",
    "perm_config_dangerous_combo",
    "perm_approvals_denylist_match",
    "perm_approvals_volume_warn",
    "perm_approvals_volume_alarm",
    "perm_cron_uncapped_agent_turn",
    "perm_cron_denylist_match",
    "perm_cron_added_silently",
    "perm_openclaw_config_missing",
    # Autonomy ladder (internal/spec-autonomy-ladder-2026-06-10.md §4.2.1
    # + §5.2). Produced via autonomy.coherence / autonomy.backfill;
    # this monitor is the emitter because it already reads all the
    # permission surfaces on the same cadence.
    "autonomy_posture_drift",
    "autonomy_backfill_review",
    # Phase B (spec §3.2–§3.4): rung-3 daily caps, the auto-demotion
    # reflex, and the promotion-streak producer. Same producer — the
    # 5-minute autonomy-limits daemon emits the first two between
    # audit passes with identical signatures, so the store dedups.
    "autonomy_limit_hit",
    "autonomy_demoted",
    "autonomy_promotion_candidate",
})


# Operator docs per signal_type (PR #1603 convention — what_it_means +
# fix_steps render specially in the Alerts UI). Bot id is substituted
# at emit time.
_OPERATOR_DOCS: dict[str, tuple[str, str]] = {
    "perm_openclaw_config_missing": (
        "The bot is listed in network.json but has no openclaw.json on "
        "disk. Permission posture cannot be evaluated and the gateway "
        "may not even be running. This usually means the bot was added "
        "to network.json but the deploy step never ran (or failed).",
        "1. Deploy the bot:\n"
        "   ssh pod_admin_user@mini sudo evolve-admin deploy <bot>\n"
        "2. If the deploy fails, check the deploy log for the actual "
        "error:\n"
        "   ssh pod_admin_user@mini sudo /bin/cat "
        "/Users/Shared/evolve/logs/admin-actions.jsonl | tail -20\n"
        "3. Once deploy succeeds, the next monitor pass will read the "
        "fresh openclaw.json and clear this Signal",
    ),
    "perm_config_drift": (
        "One or more fields on the bot's openclaw.json permission "
        "block disagree with the resolved baseline (pod_default + "
        "per-bot override). Drift is not automatically dangerous, but "
        "it means the live config no longer matches what the operator "
        "approved. Either the bot was modified out-of-band, or the "
        "baseline is stale relative to a recent approved change.",
        "1. Open Security → Permissions for the affected bot in the "
        "admin UI to see the field-by-field diff\n"
        "2. Decide: which side is correct?\n"
        "   - If the live config is right: update the baseline at "
        "`/Users/Shared/evolve/policy/permission-baseline.json` so it "
        "matches\n"
        "   - If the baseline is right: propose a config update via "
        "the UpdatePermissionConfig proposal flow on the same page\n"
        "3. The Signal auto-resolves once the diff clears on the "
        "next monitor pass",
    ),
    "perm_config_dangerous_combo": (
        "The bot is in the 'open' permission posture — a known-bad "
        "combination of permission settings has stripped all execution "
        "gates and isolation. The bot is currently a high-risk surface; "
        "any exec it runs has no approval gating and no sandbox. This "
        "is an immediate security finding, not a drift advisory.",
        "1. Open Security → Permissions for the affected bot\n"
        "2. Review `details.axes` on this Signal — identifies which "
        "axes are currently 'open' (exec, network, fs, etc.)\n"
        "3. Propose a Locked or Supervised level change via the "
        "permission-level selector on the Security page\n"
        "4. Approve the proposal to apply; the bot's gateway restarts "
        "automatically and the dangerous combo clears",
    ),
    "perm_approvals_denylist_match": (
        "An exec-approval entry on the bot matches a denylist rule in "
        "the permission baseline. Someone explicitly approved a "
        "command pattern that the baseline marks as never-approve. "
        "The approved entry is currently honored by the gateway, so "
        "this is an active exposure, not a hypothetical one.",
        "1. Open Security → Permissions → Exec approvals for the "
        "affected bot\n"
        "2. Find the pattern listed in `details.pattern` and revoke "
        "it via the UI (or edit `/Users/<bot>/.openclaw/exec-approvals.json` "
        "if the UI is unavailable)\n"
        "3. If the pattern needs to be approved long-term, update the "
        "baseline's `denied_approvals` list — but only after "
        "understanding why it was denied in the first place\n"
        "4. Restart the gateway to drop any cached approval state:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>",
    ),
    "perm_approvals_volume_warn": (
        "The bot has accumulated more exec approvals than the warn "
        "threshold. Each approved pattern is a permitted command "
        "shape, so a growing approval list is a growing attack "
        "surface. Frequently-approved patterns should usually be "
        "promoted to baseline defaults so the list stays small.",
        "1. Open Security → Permissions → Exec approvals for the "
        "affected bot — review the list\n"
        "2. Identify patterns that are clearly part of the bot's "
        "normal operation and promote them to "
        "`pod_default.exec_approvals` in the baseline\n"
        "3. Prune any patterns the bot no longer uses\n"
        "4. Raise the warn threshold via "
        "`pod_default.exec_approvals.max_agent_approvals_warn` if the "
        "current count is intentional",
    ),
    "perm_approvals_volume_alarm": (
        "The bot's exec-approval count is now above the alarm "
        "threshold. The approval list itself has become a meaningful "
        "attack surface — anyone who can write to "
        "exec-approvals.json can authorize new shapes invisibly. "
        "Escalation from `perm_approvals_volume_warn`; same root "
        "cause but past the higher bar.",
        "1. Audit the approvals list for the affected bot in "
        "Security → Permissions → Exec approvals\n"
        "2. Prune aggressively — anything not actively used in the "
        "last 30 days should come out\n"
        "3. Promote frequently-used patterns to baseline defaults so "
        "they don't bloat per-bot lists\n"
        "4. If the count is intentional (rare): raise "
        "`max_agent_approvals_alarm` in the baseline, but treat that "
        "as a deliberate accepted risk and document why",
    ),
    "perm_cron_uncapped_agent_turn": (
        "The bot has a cron job configured as an `agentTurn` (i.e. "
        "fires an actual LLM agent run on schedule) without both "
        "`maxTurns` and `maxBudgetUsd` caps. A bug or runaway in that "
        "cron's prompt can spawn unbounded turns/budget at the bot's "
        "primary-model rate. Cap enforcement is OpenClaw's "
        "primary safety net for headless agent runs.",
        "1. Open Maintenance → Cron jobs filtered to the affected bot "
        "and find the job named in this Signal's `details.name`\n"
        "2. Add caps via the cron editor: `maxTurns` (typically 5-10) "
        "and `maxBudgetUsd` (typically $0.50-$2)\n"
        "3. Save — the gateway picks up cron changes within ~1 min\n"
        "4. Verify via the next run: `details.has_turn_cap` and "
        "`details.has_budget_cap` should both be true",
    ),
    "perm_cron_denylist_match": (
        "A cron job payload matches a denylist rule in the permission "
        "baseline. The job is currently scheduled and will fire on "
        "its cadence — every fire executes whatever the denylist "
        "marked as never-allowed. Treat as active danger, not a "
        "drift advisory.",
        "1. Open Maintenance → Cron jobs filtered to the affected "
        "bot and remove the job listed in `details.job_id`\n"
        "2. Investigate how the job got added — check "
        "`/Users/Shared/evolve/logs/admin-actions.jsonl` for a "
        "matching cron-create entry\n"
        "3. If the denylist rule is wrong, update the baseline "
        "first — but only after the offending cron is removed\n"
        "4. Restart the gateway to drop the scheduled job from "
        "memory:\n"
        "   ssh pod_admin_user@mini sudo launchctl kickstart -k "
        "system/ai.openclaw.gateway.<bot>",
    ),
    "perm_cron_added_silently": (
        "A cron job is present in `cron/jobs.json` on the bot but "
        "isn't in the known-good baseline at "
        "`{shared_dir}/permissions/cron_baseline/<bot>.json`. Either "
        "the operator added it intentionally (and the baseline "
        "needs to refresh) or something added it out-of-band. "
        "Drift-tier; investigate the source before silencing.",
        "1. Open Maintenance → Cron jobs and identify the new job by "
        "`details.job_id`\n"
        "2. If the job was intentional: re-bootstrap the baseline "
        "by running:\n"
        "   ssh pod_admin_user@mini sudo evolve-admin permissions "
        "rebootstrap-cron <bot>\n"
        "3. If the job wasn't intentional: remove it via the UI and "
        "investigate the admin-actions log:\n"
        "   ssh pod_admin_user@mini sudo grep '<bot>' "
        "/Users/Shared/evolve/logs/admin-actions.jsonl | tail -50\n"
        "4. The Signal auto-resolves once the live state matches "
        "the baseline again",
    ),
    "autonomy_posture_drift": (
        "The bot's recorded autonomy setting for an integration (e.g. "
        "email at \"Drafts only\") no longer matches what the bot's "
        "live configuration actually enforces. Either openclaw.json "
        "was edited out-of-band, the integration's known tool surface "
        "changed, or a deploy raced the render. Until repaired, the "
        "setting shown on the Permissions page is a description, not "
        "a guarantee.",
        "1. Open Security → Permissions → Autonomy for the affected "
        "bot and expand the integration row — `details` lists the "
        "missing/unexpected entries\n"
        "2. Re-select the current level for that integration (the "
        "render re-applies on every posture write)\n"
        "3. Or redeploy the bot — the deploy pipeline re-renders the "
        "posture too:\n"
        "   ssh pod_admin_user@mini sudo evolve-admin deploy <bot>\n"
        "4. The Signal auto-resolves once enforcement matches the "
        "recorded setting on the next monitor pass",
    ),
    "autonomy_backfill_review": (
        "During backfill the bot was observed able to act on an "
        "integration more freely than the shipped default (e.g. it "
        "can send email without asking), and nobody has recorded "
        "that as a deliberate decision yet. Nothing was changed — "
        "the bot keeps working exactly as before. This is a "
        "suggestion to make the current state deliberate.",
        "1. Open Security → Permissions → Autonomy for the affected "
        "bot\n"
        "2. If the wider setting is intentional: confirm it (re-select "
        "the shown level) — the posture becomes operator-set and this "
        "Signal clears\n"
        "3. If not: click Restrict — one click, takes effect within "
        "seconds\n"
        "4. Either action resolves this Signal on the next monitor "
        "pass",
    ),
    "autonomy_limit_hit": (
        "The bot reached the daily action limit the operator set for "
        "this integration (\"Acts within limits\" → actions per day) "
        "and is paused from acting outward there until the UTC day "
        "rolls over. Reading, drafting, and conversation continue "
        "normally. The pause is enforced by denying the integration's "
        "outward tools; it lifts automatically tomorrow.",
        "1. Nothing is required — the pause clears itself at the next "
        "UTC day\n"
        "2. If the limit is routinely too low, raise actions-per-day "
        "for this integration on Security → Permissions → Autonomy "
        "(re-select \"Acts within limits\" with a larger daily limit)\n"
        "3. If the volume is unexpected, review what the bot has been "
        "doing — repeated cap hits escalate (see autonomy_demoted)",
    ),
    "autonomy_demoted": (
        "Evolve automatically reduced this integration from \"Acts "
        "within limits\" to \"Asks first\" — one step down, never "
        "further — because a safety trigger fired: either the bot "
        "kept attempting outward actions after its daily-limit pause, "
        "or a critical security finding named this integration. The "
        "previous setting (including its limits) is preserved and one "
        "click restores it after a confirmation.",
        "1. Read the trigger in this Signal's details "
        "(`trigger_signal_id` links the originating finding)\n"
        "2. If the trigger was a false alarm: click \"Restore previous "
        "level\" on this alert (it asks you to confirm — restoring is "
        "a promotion)\n"
        "3. If the trigger was real: keep the safer setting; the bot "
        "now asks before every outward action on this integration\n"
        "4. Any deliberate change to the integration's level on "
        "Security → Permissions → Autonomy also resolves this Signal",
    ),
    "autonomy_promotion_candidate": (
        "The bot has a sustained clean track record on this "
        "integration at \"Asks first\": it performed the listed number "
        "of outward actions across the listed span of days — each one "
        "after a go-ahead, per its operating rules — with no open "
        "autonomy incidents. This Signal is the "
        "operator-visible evidence trail; the improvement pipeline may "
        "turn it into a suggestion on the Improvements page. Nothing "
        "changes unless you approve that suggestion.",
        "1. No action needed — this is informational\n"
        "2. If you want the bot to act on its own within limits, "
        "either approve the matching suggestion on the Improvements "
        "page when it appears, or set it directly on Security → "
        "Permissions → Autonomy\n"
        "3. If you never want this suggested, dismiss the suggestion "
        "when it appears — the dismissal is remembered",
    ),
}


def _operator_docs(signal_type: str) -> tuple[str, str] | None:
    return _OPERATOR_DOCS.get(signal_type)


def cron_baseline_dir(shared_dir: Path) -> Path:
    return shared_dir / "permissions" / "cron_baseline"


def _load_cron_baseline(shared_dir: Path, bot_id: str) -> dict | None:
    p = cron_baseline_dir(shared_dir) / f"{bot_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_cron_baseline(shared_dir: Path, bot_id: str, job_ids: list[str]) -> None:
    """Capture the current set of cron job ids for a bot.

    Bootstrap calls this; the monitor itself doesn't write to the
    baseline (drift = signal, not silent absorption).
    """
    target = cron_baseline_dir(shared_dir) / f"{bot_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"job_ids": sorted(job_ids)}, indent=2))
    tmp.replace(target)


# ── config_intent integration ────────────────────────────────────────────────

def _filter_intentional_deviations(
    bot_id: str,
    diffs: dict[str, dict[str, Any]],
    shared_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Drop fields from ``diffs`` whose deviation matches an active
    config_intent record (spec internal/spec-config-intent-system-2026-05-21.md §4).

    Returns a new dict — callers should treat ``diffs`` as immutable
    here. The two-pass shape (compute, then filter) mirrors the same
    helper in
    ``packages/analyzer/generators/auth_drift_filler/signal_proposals.py``;
    keep this implementation in sync so monitor-side suppression and
    generator-side proposal-skip agree on the same field × value × intent
    triple.

    Fail-open behavior: a missing import, unreadable sidecar, or any
    other exception leaves ``diffs`` untouched. The safer direction is
    over-emitting an explainable drift signal than under-emitting an
    actual one — symmetric with the convention in
    ``config_intent.get_intent`` itself.
    """
    if not diffs:
        return diffs
    try:
        from evolve_admin.config_intent import get_intent, intent_still_valid
    except ImportError:
        return diffs

    filtered: dict[str, dict[str, Any]] = {}
    for field, diff in diffs.items():
        observed_val = diff.get("observed")
        try:
            intent = get_intent(bot_id, field, shared_dir=shared_dir)
        except Exception:  # noqa: BLE001 — monitor loop hygiene; fail open
            filtered[field] = diff
            continue
        if intent is None or intent.get("value") != observed_val:
            # No matching intent (or the operator changed the value back
            # after the intent was recorded — stale intent record). Real
            # drift; surface it.
            filtered[field] = diff
            continue
        try:
            still_valid = intent_still_valid(intent, shared_dir=shared_dir)
        except Exception:  # noqa: BLE001
            still_valid = True  # fail open toward suppression — operator
            # recorded the intent; don't undermine that based on a
            # transient validity-check error.
        if not still_valid:
            # Intent exists but its preconditions no longer hold
            # (Phase 5 plugin-coupled check). auth_drift_filler will emit
            # a stale_flagged proposal on its next sweep; surface the
            # drift here too so the operator sees something is wrong.
            filtered[field] = diff
            continue
        # Intentional deviation — sidecar audit trail + Phase 4
        # "Intentional Deviations" UI are the visibility surfaces;
        # the warn-severity drift signal would just be noise.
    return filtered


# ── Diff one bot ─────────────────────────────────────────────────────────────

def _diff_one_bot(
    inv: _inv.PermissionInventory,
    baseline: dict,
    shared_dir: Path,
) -> list[dict[str, Any]]:
    """Compute the list of Signal-worthy findings for one bot."""
    findings: list[dict[str, Any]] = []
    bot_id = inv.bot_id
    inv_dict = inv.to_dict()

    # ── Missing openclaw.json (info) ────────────────────────────────────────
    if inv.permission_config.read_error == "not_found":
        findings.append({
            "type": "perm_openclaw_config_missing",
            "severity": "info",
            "signature_scope": bot_id,
            "title": f"{bot_id}: openclaw.json not found",
            "body": (
                f"Bot {bot_id} is in network.json but has no "
                f"{inv.permission_config.openclaw_config_path}. Permission "
                f"posture cannot be evaluated."
            ),
            "details": {"bot_id": bot_id},
        })
        return findings

    # ── Config drift vs baseline (warn) ─────────────────────────────────────
    if inv.permission_config.read_error is None:
        expected = _bl.resolve(baseline, bot_id)
        observed = inv.permission_config.fields
        diffs: dict[str, dict[str, Any]] = {}
        for fld, expected_val in expected.items():
            observed_val = observed.get(fld)
            if observed_val != expected_val:
                diffs[fld] = {"expected": expected_val, "observed": observed_val}
        # Phase 5b of spec-config-intent-system-2026-05-21.md: drop fields the
        # operator has explicitly accepted via a config_intent record before
        # deciding whether to emit. Without this filter, intentional
        # deviations (a hardened bot's workspaceOnly=true, a bot whose
        # exec=full was set as the side-effect of a plugin install, etc.)
        # fire a noisy warn-severity signal on every monitor sweep even
        # though auth_drift_filler already suppresses the corresponding
        # revert proposal — the operator-facing surface stays noisy.
        # Audit visibility for intentional deviations comes from the
        # sidecar file itself (Phase 4 Security → Intentional Deviations UI).
        diffs = _filter_intentional_deviations(bot_id, diffs, shared_dir)
        if diffs:
            findings.append({
                "type": "perm_config_drift",
                "severity": "warn",
                "signature_scope": bot_id,
                "title": f"{bot_id}: permission config drifted from baseline",
                "body": (
                    f"{len(diffs)} field(s) on {bot_id} differ from the resolved "
                    f"baseline (pod_default + per-bot override). Review and either "
                    f"propose a config update or update the baseline."
                ),
                "details": {"bot_id": bot_id, "diffs": diffs},
            })

    # ── Dangerous combo (alert) ─────────────────────────────────────────────
    cp = _posture.classify(inv)
    if cp.get("score") == "open":
        findings.append({
            "type": "perm_config_dangerous_combo",
            "severity": "alert",
            "signature_scope": bot_id,
            "title": f"{bot_id}: dangerous permission combination",
            "body": (
                f"{bot_id} is in the 'open' posture: {cp.get('rationale', '')}. "
                "This combination removes all execution gates and isolation. "
                "Propose a Locked / Supervised level change via Security → Permissions."
            ),
            "details": {"bot_id": bot_id, "score": "open", "axes": cp.get("axes")},
        })

    # ── Approvals denylist matches (alert) ──────────────────────────────────
    deny_approvals = _bl.denylist_for(baseline, "approvals")
    for match in _deny.scan_approvals(inv_dict, deny_approvals):
        findings.append({
            "type": "perm_approvals_denylist_match",
            "severity": "alert",
            "signature_scope": f"{bot_id}:{match.context}:{match.pattern}",
            "title": f"{bot_id}: approved command matches denylist",
            "body": (
                f"Pattern {match.pattern!r} is approved on {bot_id} ({match.context}) "
                f"but matches denylist rule {match.rule!r}. An operator explicitly "
                "approved something the baseline marks as never-approve. Revoke "
                "via Security → Permissions."
            ),
            "details": {
                "bot_id": bot_id, "pattern": match.pattern,
                "rule": match.rule, "context": match.context,
            },
        })

    # ── Approvals volume (info / warn) ──────────────────────────────────────
    ea_cfg = (baseline.get("pod_default") or {}).get("exec_approvals") or {}
    warn_threshold = int(ea_cfg.get("max_agent_approvals_warn") or 50)
    alarm_threshold = int(ea_cfg.get("max_agent_approvals_alarm") or 200)
    for agent in inv.exec_approvals.agents:
        if agent.count > alarm_threshold:
            findings.append({
                "type": "perm_approvals_volume_alarm",
                "severity": "warn",
                "signature_scope": f"{bot_id}:agent:{agent.agent_id}",
                "title": f"{bot_id}: approval volume above alarm threshold",
                "body": (
                    f"Agent {agent.agent_id!r} on {bot_id} has {agent.count} "
                    f"accumulated approvals (threshold {alarm_threshold}). The "
                    "approval list itself is now a meaningful attack surface; "
                    "audit and prune."
                ),
                "details": {
                    "bot_id": bot_id, "agent_id": agent.agent_id,
                    "count": agent.count, "threshold": alarm_threshold,
                },
            })
        elif agent.count > warn_threshold:
            findings.append({
                "type": "perm_approvals_volume_warn",
                "severity": "info",
                "signature_scope": f"{bot_id}:agent:{agent.agent_id}",
                "title": f"{bot_id}: approval volume above warn threshold",
                "body": (
                    f"Agent {agent.agent_id!r} on {bot_id} has {agent.count} "
                    f"approvals (warn threshold {warn_threshold}). Consider "
                    "promoting frequently-approved patterns to defaults."
                ),
                "details": {
                    "bot_id": bot_id, "agent_id": agent.agent_id,
                    "count": agent.count, "threshold": warn_threshold,
                },
            })

    # ── Cron findings ───────────────────────────────────────────────────────
    si_cfg = (baseline.get("pod_default") or {}).get("scheduled_invocations") or {}
    require_caps = bool(si_cfg.get("agent_turn_max_turns_required"))
    deny_cron = _bl.denylist_for(baseline, "cron")

    # Track observed job ids for the silent-add check
    observed_job_ids = {j.id for j in inv.scheduled_invocations.jobs if j.id}
    baseline_ids: set[str] = set()
    cron_bl = _load_cron_baseline(shared_dir, bot_id)
    if cron_bl and isinstance(cron_bl.get("job_ids"), list):
        baseline_ids = set(cron_bl["job_ids"])

    for job in inv.scheduled_invocations.jobs:
        # Uncapped agentTurn
        if (job.payload_kind == "agentTurn" and job.enabled
                and require_caps
                and not (job.has_turn_cap and job.has_budget_cap)):
            findings.append({
                "type": "perm_cron_uncapped_agent_turn",
                "severity": "warn",
                "signature_scope": f"{bot_id}:cron:{job.id}",
                "title": f"{bot_id}: cron job {job.name!r} missing caps",
                "body": (
                    f"Job {job.name!r} ({job.id}) on {bot_id} is an agentTurn cron "
                    "without both maxTurns and maxBudgetUsd. Headless agent runs "
                    "with no cap can burn unbounded turns/budget. Add caps via "
                    "Security → Permissions → Details → add caps."
                ),
                "details": {
                    "bot_id": bot_id, "job_id": job.id, "name": job.name,
                    "has_turn_cap": job.has_turn_cap,
                    "has_budget_cap": job.has_budget_cap,
                },
            })

        # Cron denylist match — runs against the same string the inventory captured
        cron_matches = _deny.scan_cron(
            {"scheduled_invocations": {"jobs": [job.to_dict()]}}, deny_cron,
        )
        for match in cron_matches:
            findings.append({
                "type": "perm_cron_denylist_match",
                "severity": "alert",
                "signature_scope": f"{bot_id}:cron:{job.id}:denylist",
                "title": f"{bot_id}: cron payload matches denylist",
                "body": (
                    f"Cron job {job.name!r} on {bot_id} has a payload that matches "
                    f"denylist rule {match.rule!r}. Remove the job (Security → "
                    "Permissions → Details → remove) and review how it was added."
                ),
                "details": {
                    "bot_id": bot_id, "job_id": job.id, "rule": match.rule,
                    "payload": match.pattern,
                },
            })

        # Added silently — only when a baseline exists; absence of a baseline
        # for the bot means bootstrap hasn't run yet, so we can't tell.
        if baseline_ids and job.id and job.id not in baseline_ids:
            findings.append({
                "type": "perm_cron_added_silently",
                "severity": "warn",
                "signature_scope": f"{bot_id}:cron:{job.id}:added",
                "title": f"{bot_id}: cron job {job.name!r} appeared without proposal",
                "body": (
                    f"Job {job.id!r} on {bot_id} is in cron/jobs.json but not in the "
                    "known-good baseline at "
                    f"{cron_baseline_dir(shared_dir)}/{bot_id}.json. If you added it "
                    "intentionally, re-bootstrap the baseline; otherwise investigate."
                ),
                "details": {
                    "bot_id": bot_id, "job_id": job.id, "name": job.name,
                    "baseline_path": str(cron_baseline_dir(shared_dir) / f"{bot_id}.json"),
                },
            })

    # Stash observed ids on the findings list as a side-channel for the
    # caller — they aren't a finding but help with metrics.
    _ = observed_job_ids  # noqa: F841

    return findings


# ── Autonomy ladder findings (U4.1) ──────────────────────────────────────────

# The autonomy types are swept separately from the rest of
# OWNED_SIGNAL_TYPES: their sweep is scoped to bots whose autonomy
# checks RAN successfully this pass. A crashed check must read as
# "never ran", not "condition cleared" — otherwise one transient
# failure auto-resolves a still-true safety Signal and the alert flaps
# (the feedback_distinguish_tooling_failure_from_findings rule).
AUTONOMY_SIGNAL_TYPES: frozenset[str] = frozenset({
    "autonomy_posture_drift",
    "autonomy_backfill_review",
    "autonomy_limit_hit",
    "autonomy_demoted",
    "autonomy_promotion_candidate",
})


def _autonomy_findings(
    bot_id: str, shared_dir: Path, config: "dict[str, Any] | None",
    *, create_entries: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Backfill + coherence + limits + reflex + streaks for one bot.

    Spec internal/spec-autonomy-ladder-2026-06-10.md §4.2.1 (drift), §5.2
    (observe-first backfill), §1.3 (rung-3 caps), §3.2 (streaks), §3.3
    (auto-demotion). Lazy-imported and exception-safe: a broken
    autonomy module must not take the permission monitor down with it.

    The Phase B passes here are the **slow backstop** — the 5-minute
    ``ai.evolve.evolve.autonomy-limits`` daemon (run_autonomy_limits.py)
    runs the same limits + reflex evaluation between audit passes.
    Both paths derive identical signatures, so the Signal store dedups.

    ``create_entries=False`` (the dry-run path) previews findings
    without persisting inferred posture entries, pause state, or
    demotions.

    Returns ``(findings, ran_ok)``. ``ran_ok=False`` means tooling
    failure — the caller must exclude this bot from the autonomy-type
    sweep so its existing Signals are kept, not auto-resolved.
    """
    findings: list[dict[str, Any]] = []
    try:
        from autonomy import backfill as _ab
        from autonomy import coherence as _ac
        from autonomy import limits as _alimits
        from autonomy import reflex as _areflex
        from autonomy import streaks as _astreaks
    except ImportError:  # pragma: no cover — partial installs
        return findings, False
    import sys
    ran_ok = True
    try:
        findings.extend(_ab.ensure_backfilled(
            shared_dir, bot_id, config, create_missing=create_entries,
        ))
    except Exception as exc:  # noqa: BLE001 — monitor loop hygiene
        ran_ok = False
        print(f"[permissions/monitor] {bot_id}: autonomy backfill failed: {exc}",
              file=sys.stderr)
    # Limits BEFORE reflex: the reflex's escalation trigger reads the
    # pause state the limits pass just wrote.
    try:
        limit_findings, limits_ok = _alimits.evaluate_bot(
            shared_dir, bot_id, config, enforce=create_entries,
        )
        findings.extend(limit_findings)
        ran_ok = ran_ok and limits_ok
    except Exception as exc:  # noqa: BLE001
        ran_ok = False
        print(f"[permissions/monitor] {bot_id}: autonomy limits failed: {exc}",
              file=sys.stderr)
    try:
        reflex_findings, reflex_ok = _areflex.run_bot(
            shared_dir, bot_id, config, act=create_entries,
        )
        findings.extend(reflex_findings)
        ran_ok = ran_ok and reflex_ok
    except Exception as exc:  # noqa: BLE001
        ran_ok = False
        print(f"[permissions/monitor] {bot_id}: autonomy reflex failed: {exc}",
              file=sys.stderr)
    try:
        streak_findings, streaks_ok = _astreaks.candidates(shared_dir, bot_id)
        findings.extend(streak_findings)
        ran_ok = ran_ok and streaks_ok
    except Exception as exc:  # noqa: BLE001
        ran_ok = False
        print(f"[permissions/monitor] {bot_id}: autonomy streaks failed: {exc}",
              file=sys.stderr)
    # Coherence LAST: limits/reflex may have re-rendered above; checking
    # afterwards avoids flagging a drift the same pass just repaired.
    try:
        findings.extend(_ac.check_bot(bot_id, shared_dir, config))
    except Exception as exc:  # noqa: BLE001 — monitor loop hygiene
        ran_ok = False
        print(f"[permissions/monitor] {bot_id}: autonomy coherence failed: {exc}",
              file=sys.stderr)
    return findings, ran_ok


def emit_findings(
    shared_dir: Path, findings: "list[dict[str, Any]]",
) -> set[str]:
    """Observe every finding into the Signal store under this monitor's
    producer name; return the kept signatures (the sweep input).

    Shared by :func:`run` and the 5-minute autonomy-limits daemon
    (``run_autonomy_limits.py``) so both emitters attach identical
    signatures, operator docs, and remediations — the store dedups
    across the two cadences by signature.
    """
    kept: set[str] = set()
    if _signals_store is None or _make_signature is None:
        return kept
    for f in findings:
        signature = _make_signature(PRODUCER, f["type"], f["signature_scope"])
        kept.add(signature)
        details = dict(f.get("details") or {})
        docs = _operator_docs(f["type"])
        if docs is not None:
            details["what_it_means"], details["fix_steps"] = docs
        remediation = None
        rem_raw = f.get("remediation")
        if isinstance(rem_raw, dict) and rem_raw.get("kind"):
            # autonomy_demoted carries its one-click restore here
            # (spec §3.3 — the alert carries the restore).
            try:
                from schema.signal import Remediation as _Remediation
                remediation = _Remediation.from_dict(rem_raw)
            except Exception:  # noqa: BLE001 — alert still fires without it
                remediation = None
        try:
            _signals_store.observe(
                shared_dir,
                signature=signature,
                producer=PRODUCER,
                type=f["type"],
                flavor="maintenance",
                severity=f["severity"],
                scope="bot",
                bot_id=f.get("bot_id"),
                title=f["title"],
                body=f["body"],
                details=details,
                remediation=remediation,
            )
        except Exception:  # noqa: BLE001 — one bad signal can't break the rest
            continue
    return kept


# ── Public entry point ───────────────────────────────────────────────────────

def run(
    shared_dir: Path,
    bot_ids: list[str],
    config: "dict[str, Any] | None" = None,
    *,
    emit_signals: bool = True,
) -> dict[str, Any]:
    """Run the permission monitor across the given bots.

    Side effects:
      - Writes inventory cache files to {shared_dir}/permissions/inventory/<bot>.json.
      - Ensures the baseline file exists at {shared_dir}/policy/permission-baseline.json
        (default seed on first read).
      - Emits Signals via signals.store.observe() for each finding when
        ``emit_signals=True``.
      - Sweep-resolves any prior signals from PRODUCER no longer present.

    Returns a summary dict:
      {bots_checked, findings, swept_resolved}
    """
    _bl.write_default_if_missing(shared_dir)
    baseline = _bl.load(shared_dir)

    findings: list[dict[str, Any]] = []
    kept_signatures: set[str] = set()

    autonomy_swept_bots: set[str] = set()
    for bot_id in bot_ids:
        inv = _inv.read_inventory(bot_id, config)
        _posture.annotate(inv)
        _inv.write_inventory(inv, shared_dir)
        for finding in _diff_one_bot(inv, baseline, shared_dir):
            finding["bot_id"] = inv.bot_id
            findings.append(finding)
        autonomy_findings, autonomy_ok = _autonomy_findings(
            bot_id, shared_dir, config, create_entries=emit_signals,
        )
        if autonomy_ok:
            autonomy_swept_bots.add(bot_id)
        for finding in autonomy_findings:
            finding["bot_id"] = bot_id
            findings.append(finding)

    swept_resolved = 0
    if emit_signals and _signals_store is not None and _make_signature is not None:
        kept_signatures.update(emit_findings(shared_dir, findings))
        try:
            swept = _signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                reason="auto-resolve: permission condition cleared on next monitor run",
                types=set(OWNED_SIGNAL_TYPES - AUTONOMY_SIGNAL_TYPES),
            )
            swept_resolved = len(swept)
        except Exception:  # noqa: BLE001
            swept_resolved = 0
        # Autonomy types sweep separately, scoped to bots whose autonomy
        # checks ran successfully this pass — a crashed check must keep
        # its bot's Signals firing, not auto-resolve them (see
        # AUTONOMY_SIGNAL_TYPES note above).
        if autonomy_swept_bots:
            try:
                swept = _signals_store.sweep_resolve(
                    shared_dir,
                    producer=PRODUCER,
                    kept_signatures=kept_signatures,
                    reason="auto-resolve: permission condition cleared on next monitor run",
                    types=set(AUTONOMY_SIGNAL_TYPES),
                    bot_ids=autonomy_swept_bots,
                )
                swept_resolved += len(swept)
            except Exception as exc:  # noqa: BLE001 — sweep failure is non-fatal
                import sys
                print(f"[permissions/monitor] autonomy sweep failed: {exc}",
                      file=sys.stderr)

    return {
        "bots_checked": len(bot_ids),
        "findings": findings,
        "swept_resolved": swept_resolved,
    }
