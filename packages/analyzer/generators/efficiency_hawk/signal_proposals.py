"""generators.efficiency_hawk.signal_proposals — Signal → Proposal factories.

Builds Proposals from cost_watchdog Signals. One factory per signal type;
each is pure string templating, no LLM. Every Proposal sets
``motivating_signals=[signal.id]`` so the inverse link on the Signal
points back here.

Investigation-style proposals (claim=None) — these surface a finding,
operator decides the fix. The charter's ``claim_metric_known`` invariant
only fires when claim is set, so leaving it None is intentional.
"""

from __future__ import annotations

from typing import Any, Callable

from evolve_config import bot_label
from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


GENERATOR_ID = "efficiency_hawk"
DIMENSION = "efficiency"


# ── Dismiss signatures (Phase A.5 + Phase C-5) ──────────────────────────────
#
# Stable per-finding signatures the dismissals store keys on. Per-bot
# scoping happens at the store layer. Three of these embed a sub-id
# (cron, file) so dismissing one specific resource doesn't suppress
# findings on adjacent resources of the same kind.
DISMISS_SIG_DAILY_SPEND_HIGH = "efficiency_hawk:daily_spend_high"
DISMISS_SIG_AUTOMATION_DOMINANCE = "efficiency_hawk:automation_dominance"
DISMISS_SIG_SESSION_TOKEN_OUTLIER = "efficiency_hawk:session_token_outlier"
DISMISS_SIG_HEARTBEAT_NO_OVERRIDE = "efficiency_hawk:heartbeat_no_model_override"


def dismiss_signature_for_cron_wakes(cron_id: str) -> str:
    return f"efficiency_hawk:cron_wakes_agent:{cron_id}"


def dismiss_signature_for_cron_overactive(cron_id: str) -> str:
    return f"efficiency_hawk:cron_overactive:{cron_id}"


def dismiss_signature_for_context_bloat(filename: str) -> str:
    return f"efficiency_hawk:context_bloat:{filename}"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict — useful for tests."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def make_daily_spend_proposal(signal: Any) -> Proposal:
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    cost = float(details.get("cost_usd") or 0.0)
    threshold = float(details.get("threshold_usd") or 0.0)
    severity = _signal_dict_get(signal, "severity") or "warn"
    urgency = "operational_urgent" if severity == "alert" else "substrate_warn"

    problem = f"{bot_name}: daily spend ${cost:.2f} exceeds ${threshold:.2f} threshold"
    headline = f"{bot_name} blew through its daily spend cap"

    # ── Phase C-5 (2026-06-04 protocol) — operator-first content ────────────
    summary = (
        f"{bot_name} spent ${cost:.2f} today, over the ${threshold:.2f} cap "
        f"you set for it ({int(details.get('event_count', 0))} LLM "
        f"calls). The Cost tab's trigger-kind breakdown is the fastest "
        f"way to see whether the spend is user-driven or automation, "
        f"and which app or cron pushed it over."
    )
    explanation = (
        f"Daily spend caps act as a check on cost — when a bot's "
        f"workload shifts (new app, more users, a slower or smarter "
        f"primary model), spend can grow before anyone notices. The "
        f"cap fires so you see the change.\n\n"
        f"Diagnosis. ${cost:.2f} today against a ${threshold:.2f} "
        f"threshold. The cap doesn't say whether the spend is bad — it "
        f"says the spend changed. Three usual culprits: a new app's "
        f"traffic, a heartbeat or cron firing more than expected, or "
        f"a model swap that bumped per-call cost.\n\n"
        f"Where to look. Open the Cost tab and switch to the "
        f"trigger-kind breakdown for {bot_name}. If user-turn share is "
        f"up, it's traffic; if heartbeat or cron share jumped, look at "
        f"the Sessions page for repeats; if neither moved, the bot's "
        f"primary model may have changed.\n\n"
        f"What could go wrong. If the spend is intentional (a "
        f"deliberate new workload), raising the threshold via "
        f"network.json is the right call — the engine then stops "
        f"nagging you about an expected cost. Reverting an intentional "
        f"workload regresses the thing you wanted."
    )
    context = (
        f"{bot_name} spent ${cost:.2f} on LLM calls today, over the "
        f"${threshold:.2f} configured threshold "
        f"({details.get('event_count', 0)} events). "
        f"\n\nFirst things to check:\n"
        f"  - trigger_kind breakdown: is this user_turn-driven or automation?\n"
        f"  - per-cron breakdown: is one cron over-firing?\n"
        f"  - per-session top spikes: any single session unusually expensive?\n\n"
        f"Tune the threshold per-bot via "
        f"network.json `cost_watchdog.bots.{bot_id}.daily_spend_usd` "
        f"if this spend is expected for this bot's role."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"daily_spend_high:{bot_id}"],
        provenance=Provenance(
            technique="efficiency_hawk.daily_spend_high",
            signals={
                "cost_usd": round(cost, 4),
                "threshold_usd": threshold,
                "event_count": int(details.get("event_count") or 0),
                "date": details.get("date"),
            },
            confidence=0.9,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency=urgency,
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase C-5 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Cost tab",
        manual_path=f"Cost tab → {bot_name} → trigger-kind breakdown",
        dismiss_signature=DISMISS_SIG_DAILY_SPEND_HIGH,
        dismiss_scope="kind",
        # Per-finding surface override — daily-spend high is anomaly
        # triage, not RSI. Routes to Alerts even though the charter is
        # surface=improvement. See internal/spec-rsi-proposal-eligibility-
        # 2026-06-05.md (audit row: make_daily_spend_proposal).
        surface="firing",
    )


def make_automation_dominance_proposal(signal: Any) -> Proposal:
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    auto = int(details.get("automation_count") or 0)
    user = int(details.get("user_turn_count") or 0)
    ratio = float(details.get("automation_ratio") or 0.0)
    window_days = int(details.get("window_days") or 3)
    top_kinds: dict = details.get("top_automation_kinds") or {}
    top_str = ", ".join(f"{k}={n}" for k, n in top_kinds.items()) or "n/a"

    problem = (
        f"{bot_name}: {ratio:.0%} of turns are automation "
        f"({auto}/{auto + user} over {window_days}d)"
    )
    headline = f"{bot_name} is mostly talking to itself"

    # ── Phase C-5 operator-first content (Tier 2 — UI manual) ───────────────
    summary = (
        f"{bot_name} ran {auto} automation turns vs {user} user turns "
        f"over the last {window_days} days — {ratio:.0%} of the bot's "
        f"activity is the bot waking itself up. The Cost tab's "
        f"trigger-kind breakdown shows which heartbeat or cron is "
        f"driving the share."
    )
    explanation = (
        f"Bots run two kinds of turns: user turns (you said something) "
        f"and automation turns (a heartbeat fired, a cron woke up, an "
        f"app triggered work). Both cost money. When automation "
        f"dominates, the bot is doing background work it might not "
        f"need to do — or doing the same work too often.\n\n"
        f"Diagnosis. Top automation sources over the window: "
        f"{top_str}. The dominant source is usually the right place "
        f"to look first — a heartbeat firing every minute when 5 "
        f"would do, or a cron firing on every tick instead of on a "
        f"schedule.\n\n"
        f"What to do, in order of impact. (1) Lower the cadence of "
        f"the dominant source if it doesn't need to run that often. "
        f"(2) For shell-only crons, set `sessionTarget: \"isolated\"` "
        f"so they run without waking the agent. (3) Move maintenance "
        f"work outside OpenClaw entirely (a launchd plist) when no "
        f"AI is needed.\n\n"
        f"What could go wrong. If the automation is load-bearing — a "
        f"continuously-running classifier or sandbox bot whose job IS "
        f"to be 90% automation — raising the per-bot threshold tells "
        f"the engine to stop nagging. Don't reduce cadence on work "
        f"the bot's purpose depends on."
    )
    context = (
        f"{bot_name} has spent the last {window_days} days running "
        f"{auto} automation turns vs {user} user turns. The bot is "
        f"effectively talking to itself.\n\n"
        f"Top automation sources by turn count: {top_str}\n\n"
        f"Likely fixes (in order of impact):\n"
        f"  - Identify the dominant automation source above and reduce its cadence\n"
        f"  - For shell-only crons, set sessionTarget=\"isolated\" so they don't wake the agent\n"
        f"  - Lower the heartbeat cadence in openclaw.json if the bot doesn't need fast wake\n"
        f"  - Move maintenance work outside openclaw entirely (launchd plist) when no AI is needed\n\n"
        f"Tune the trigger threshold per-bot via "
        f"`cost_watchdog.bots.{bot_id}.automation_ratio` if this ratio is expected."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"automation_dominance:{bot_id}"],
        provenance=Provenance(
            technique="efficiency_hawk.automation_dominance",
            signals={
                "automation_count": auto,
                "user_turn_count": user,
                "automation_ratio": ratio,
                "window_days": window_days,
                "top_automation_kinds": dict(top_kinds),
            },
            confidence=0.85,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase C-5 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Cost tab",
        manual_path=f"Cost tab → {bot_name} → trigger-kind breakdown",
        dismiss_signature=DISMISS_SIG_AUTOMATION_DOMINANCE,
        dismiss_scope="kind",
    )


def make_cron_wakes_agent_proposal(signal: Any) -> Proposal:
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    cron_id = details.get("cron_id") or "?"
    cron_name = details.get("cron_name") or cron_id
    cadence = details.get("cadence") or "?"
    session_target = details.get("session_target") or "main"
    shell = details.get("shell") or ""

    problem = (
        f"{bot_name}: cron '{cron_name}' is shell-only but wakes the main agent"
    )
    headline = f"Stop {bot_name}'s '{cron_name}' cron from waking the agent"

    # ── Phase C-5 operator-first content (Tier 5 — paste-to-bot) ────────────
    summary = (
        f"{bot_name}'s '{cron_name}' cron runs a shell command but is "
        f"configured to wake the main agent on every fire. That spawns "
        f"a heartbeat turn at API rates for work that doesn't need any "
        f"AI involvement — pure waste. Setting `sessionTarget: "
        f"\"isolated\"` runs the shell without waking the agent."
    )
    explanation = (
        f"Crons on this pod can either run in an isolated subprocess "
        f"(just executes the shell) or wake the main agent (executes "
        f"the shell AND triggers an LLM turn). Isolated runs cost "
        f"nothing extra. Agent-waking runs cost a heartbeat turn each "
        f"fire — fine when the bot actually needs to react, wasteful "
        f"otherwise.\n\n"
        f"Diagnosis. '{cron_name}' fires every {cadence} with "
        f"`payload.kind=systemEvent` (shell-only) but sessionTarget="
        f"\"{session_target}\" with wakeMode=\"now\". The shell runs "
        f"correctly; the agent also wakes and bills a turn it doesn't "
        f"contribute to.\n\n"
        f"What to do. The bot knows what this cron is for — paste the "
        f"instruction below and it can confirm whether the agent needs "
        f"to react to the shell output or just run the command. If "
        f"the latter, it can flip `sessionTarget: \"isolated\"` on the "
        f"cron. If the shell is fully process-management (healthcheck, "
        f"log rotation), converting to a launchd plist outside "
        f"OpenClaw entirely is even cleaner.\n\n"
        f"What could go wrong. Flipping to isolated means the agent "
        f"won't see the shell's output unless it reads the log "
        f"itself. If the bot was relying on the heartbeat to surface "
        f"shell failures (it shouldn't be, but check), surface them "
        f"via your existing alerting instead."
    )

    manual_instruction = (
        f"One of your crons ('{cron_name}', id `{cron_id}`) runs a "
        f"shell command but wakes you on every fire. The shell is: "
        f"`{shell}`. Confirm whether you actually need to react to "
        f"this shell, or whether it just needs to run. If it just "
        f"needs to run, propose flipping `sessionTarget` to "
        f"\"isolated\" via `openclaw cron edit --id {cron_id}`. "
        f"Explain the trade-off of either decision."
    )
    context = (
        f"Cron '{cron_name}' (id `{cron_id}`) runs every {cadence} on bot "
        f"{bot_name}. Its payload is a shell command "
        f"(`payload.kind=systemEvent`):\n\n"
        f"```sh\n{shell}\n```\n\n"
        f"But it's targeted at sessionTarget=\"{session_target}\" with "
        f"wakeMode=\"now\", so each fire wakes the main agent and may spawn "
        f"a heartbeat turn — at API rates this is pure waste for shell-only work.\n\n"
        f"Two fixes:\n"
        f"  - **Recommended**: set `sessionTarget: \"isolated\"` on the cron. "
        f"The shell still runs; the main agent isn't woken.\n"
        f"  - **Stronger**: convert this to a launchd plist outside openclaw. "
        f"Appropriate when the work is purely process-management (gateway "
        f"healthcheck, log rotation, etc.) and never needs AI involvement.\n\n"
        f"Edit via `openclaw cron edit --id {cron_id}` (run as the bot user)."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"cron_wakes_agent:{bot_id}/{cron_id}"],
        provenance=Provenance(
            technique="efficiency_hawk.cron_wakes_agent",
            signals={
                "cron_id": cron_id,
                "cron_name": cron_name,
                "cadence": cadence,
                "session_target": session_target,
                "wake_mode": details.get("wake_mode"),
                "shell": shell,
            },
            confidence=0.95,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase C-5 operator-first content (Tier 5 — paste-to-bot) ────
        summary=summary,
        explanation=explanation,
        manual_instruction=manual_instruction,
        dismiss_signature=dismiss_signature_for_cron_wakes(cron_id),
        dismiss_scope="kind",
        # Per-finding surface override — "cron wakes an agent" is a
        # sys-admin config nit, not RSI. Routes to Alerts. See
        # internal/spec-rsi-proposal-eligibility-2026-06-05.md (audit row:
        # make_cron_wakes_agent_proposal).
        surface="drift",
    )


def make_cron_overactive_proposal(signal: Any) -> Proposal:
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    cron_id = details.get("cron_id") or "?"
    cron_name = details.get("cron_name") or cron_id
    actual = int(details.get("actual_fires") or 0)
    expected = float(details.get("expected_fires") or 0.0)
    window_hours = int(details.get("window_hours") or 24)
    every_ms = int(details.get("every_ms") or 0)

    declared_min = every_ms / 60_000 if every_ms else 0
    problem = (
        f"{bot_name}: cron '{cron_name}' fired {actual}× in {window_hours}h "
        f"(expected ~{expected:.0f})"
    )
    headline = f"{bot_name}'s '{cron_name}' cron is firing too often"

    # ── Phase C-5 operator-first content (Tier 5 — paste-to-bot) ────────────
    summary = (
        f"{bot_name}'s '{cron_name}' cron fired {actual} times in the "
        f"last {window_hours} hours — about {actual / max(1, expected):.1f}× "
        f"the declared cadence of every {declared_min:g} minutes. "
        f"That's either a duplicate registration or a scheduler bug; "
        f"both cost real money per extra fire."
    )
    explanation = (
        f"Crons are supposed to fire on a fixed schedule. When the "
        f"actual rate is well above the declared rate, something is "
        f"wrong — and each extra fire costs at least an LLM call (if "
        f"the cron wakes the agent) plus the shell work.\n\n"
        f"Diagnosis. Declared cadence: every {declared_min:g}min "
        f"(~{expected:.0f} fires expected in {window_hours}h). "
        f"Actual: {actual} fires. The two usual causes: (1) a "
        f"duplicate cron registration with the same name or shell "
        f"payload — both copies fire on their own schedules and the "
        f"total doubles up. (2) The scheduler isn't respecting the "
        f"`schedule.everyMs` setting (less common; usually a clock-"
        f"skew bug or stale runs log).\n\n"
        f"What to do. Paste the instruction below — the bot can "
        f"check its own cron list for duplicates and inspect the "
        f"runs log for inter-fire intervals. If duplicates exist, it "
        f"can propose removing the redundant one.\n\n"
        f"What could go wrong. Removing the wrong cron registration "
        f"breaks whatever depended on it. If both registrations have "
        f"the same shell payload, removing either is safe; if they "
        f"have different payloads under the same name, the duplicate "
        f"is actually two distinct jobs and they need new names, not "
        f"deletion."
    )

    manual_instruction = (
        f"Your cron '{cron_name}' (id `{cron_id}`) fired {actual} "
        f"times in {window_hours}h but should have fired ~"
        f"{expected:.0f} times. Run `openclaw cron list` and look "
        f"for duplicate entries with the same name or shell payload. "
        f"Inspect "
        f"`/Users/{bot_id}/.openclaw/cron/runs/{cron_id}.jsonl` for "
        f"tight inter-fire intervals. Report what you find and "
        f"propose a fix (remove duplicate, fix scheduler, or "
        f"reconcile two-jobs-one-name)."
    )
    context = (
        f"Cron '{cron_name}' (id `{cron_id}`) on bot {bot_name} is firing "
        f"{actual}× per {window_hours}h, well over the declared cadence "
        f"of every {declared_min:g}min ({expected:.0f} expected fires).\n\n"
        f"This usually means one of:\n"
        f"  - A clock-skew or scheduler bug — the runner is not respecting "
        f"`schedule.everyMs`\n"
        f"  - A duplicate cron registration with the same target\n"
        f"  - The runs log file has accumulated stale entries from a "
        f"previous higher cadence (less likely)\n\n"
        f"Inspect `/Users/{bot_id}/.openclaw/cron/runs/{cron_id}.jsonl` "
        f"for tight inter-fire intervals. If duplicate registration, "
        f"`openclaw cron list` will show two entries with the same name "
        f"or shell payload."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"cron_overactive:{bot_id}/{cron_id}"],
        provenance=Provenance(
            technique="efficiency_hawk.cron_overactive",
            signals={
                "cron_id": cron_id,
                "cron_name": cron_name,
                "actual_fires": actual,
                "expected_fires": expected,
                "ratio": details.get("ratio"),
                "window_hours": window_hours,
                "every_ms": every_ms,
            },
            confidence=0.9,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="operational_urgent",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase C-5 operator-first content (Tier 5 — paste-to-bot) ────
        summary=summary,
        explanation=explanation,
        manual_instruction=manual_instruction,
        dismiss_signature=dismiss_signature_for_cron_overactive(cron_id),
        dismiss_scope="kind",
        # Per-finding surface override — cron-overactive is a threshold
        # trip on a sys-admin control. Maintenance, not RSI. Routes to
        # Alerts. See internal/spec-rsi-proposal-eligibility-2026-06-05.md
        # (audit row: make_cron_overactive_proposal).
        surface="drift",
    )


def make_context_bloat_proposal(signal: Any) -> Proposal:
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    filename = details.get("filename") or "?"
    size_kb = float(details.get("size_kb") or 0.0)
    threshold_kb = float(details.get("threshold_kb") or 0.0)

    problem = (
        f"{bot_name}: {filename} is {size_kb:.0f} KB "
        f"(threshold {threshold_kb:.0f} KB)"
    )
    headline = f"Trim {bot_name}'s {filename}"

    # ── Phase C-5 operator-first content (Tier 5 — paste-to-bot) ────────────
    summary = (
        f"`{filename}` in {bot_name}'s workspace is {size_kb:.0f} KB — "
        f"larger than the {threshold_kb:.0f} KB target. Files in "
        f"workspace ship into the model's context on every turn, so "
        f"this size compounds across turns. The bot knows what the "
        f"file is for and can propose a refactor."
    )
    explanation = (
        f"Workspace files are part of the bot's context envelope — "
        f"every model turn ships them all. A 50 KB file open all day "
        f"quietly adds to per-turn cost, especially on automation "
        f"turns that don't reap the benefit interactively.\n\n"
        f"Diagnosis. `{filename}` is {size_kb:.0f} KB against a "
        f"target of {threshold_kb:.0f} KB. Common reasons: a heartbeat "
        f"or audit log appending without rotation, a fuller-than-"
        f"intended reference doc, or a workspace that absorbed "
        f"material it doesn't need at this size.\n\n"
        f"What to do. Paste the instruction below — the bot can "
        f"inspect the file, identify what it's currently used for, "
        f"and propose either rotation (for appending logs), splitting "
        f"into a core + reference doc (for documentation), or moving "
        f"content out of workspace (for non-context material).\n\n"
        f"What could go wrong. Trimming a file the bot's behavior "
        f"depends on can shift outputs. Let the bot identify the "
        f"load-bearing parts before cutting. Tune the threshold via "
        f"`cost_watchdog.bots.{bot_id}.context_bloat_kb` if the size "
        f"is intentional for this bot's role."
    )

    base_instruction = (
        f"Look at `{filename}` in your workspace "
        f"(`/Users/{bot_id}/.openclaw/workspace/{filename}`). It's "
        f"{size_kb:.0f} KB, over the {threshold_kb:.0f} KB target. "
    )
    if filename.lower().startswith("heartbeat"):
        manual_instruction = (
            base_instruction
            + f"Heartbeat logs grow forever unless rotated. Propose "
            f"trimming to the last N entries, moving older entries "
            f"to a dated archive (NOT loaded into context), or "
            f"reducing what each heartbeat writes. Explain the "
            f"trade-off of each."
        )
    else:
        manual_instruction = (
            base_instruction
            + f"Identify which sections you actually load into "
            f"context each turn versus reference material that "
            f"doesn't need to ship. Propose splitting it into a "
            f"compact core + a fuller reference, or moving the "
            f"reference out of workspace. Explain the trade-off."
        )
    context = (
        f"`{filename}` in {bot_name}'s workspace is {size_kb:.0f} KB, larger "
        f"than the {threshold_kb:.0f} KB target. This file loads into the "
        f"system context on every turn — at Sonnet rates "
        f"(~$3/MTok input) the cost compounds across automation turns.\n\n"
        f"Where this file lives: "
        f"`/Users/{bot_id}/.openclaw/workspace/{filename}`\n\n"
        f"Suggested fixes:\n"
    )
    if filename.lower().startswith("heartbeat"):
        context += (
            "  - Trim the rolling heartbeat log to the last N entries\n"
            "  - Move older entries to a dated archive that's NOT loaded into context\n"
            "  - Reduce heartbeat verbosity at write time\n"
        )
    else:
        context += (
            f"  - Split {filename} into a compact core + a fuller reference doc; "
            f"only the core needs to load each turn\n"
            "  - Remove sections no longer load-bearing for current behavior\n"
            "  - Move long examples to bot memory or a separate workflow doc\n"
        )
    context += (
        f"\nTune the threshold via "
        f"`cost_watchdog.bots.{bot_id}.context_bloat_kb`."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"context_bloat:{bot_id}/{filename}"],
        provenance=Provenance(
            technique="efficiency_hawk.context_bloat",
            signals={
                "filename": filename,
                "size_kb": size_kb,
                "threshold_kb": threshold_kb,
            },
            confidence=0.9,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase C-5 operator-first content (Tier 5 — paste-to-bot) ────
        summary=summary,
        explanation=explanation,
        manual_instruction=manual_instruction,
        dismiss_signature=dismiss_signature_for_context_bloat(filename),
        dismiss_scope="kind",
        # Per-finding surface override — context-bloat is hygiene
        # ("a memory file grew past the threshold"), not RSI. Routes
        # to Alerts. See internal/spec-rsi-proposal-eligibility-
        # 2026-06-05.md (audit row: make_context_bloat_proposal).
        surface="firing",
    )


def make_session_token_outlier_proposal(signal: Any) -> Proposal:
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    sid = details.get("session_id") or "?"
    cost = float(details.get("cost_usd") or 0.0)
    median = float(details.get("median_session_cost_usd") or 0.0)
    ratio = float(details.get("ratio") or 0.0)
    event_count = int(details.get("event_count") or 0)
    kinds = list(details.get("trigger_kinds") or [])
    kinds_str = "+".join(kinds) if kinds else "unknown"
    first_ts = details.get("first_ts") or ""
    last_ts = details.get("last_ts") or ""

    problem = (
        f"{bot_name}: session {sid[:8]} cost ${cost:.2f} "
        f"({ratio:.1f}× median ${median:.2f})"
    )
    headline = f"One {bot_name} session cost {ratio:.1f}× the usual amount"

    # ── Phase C-5 operator-first content (Tier 2 — UI manual) ───────────────
    summary = (
        f"A single {bot_name} session ({sid[:8]}) cost ${cost:.2f} — "
        f"{ratio:.1f}× the bot's recent median of ${median:.2f}. "
        f"That's usually a stuck loop, a runaway sub-agent, or a "
        f"retry storm. Worth inspecting before it repeats."
    )
    explanation = (
        f"Most of a bot's sessions cluster around a median cost. A "
        f"single session well above that median means something "
        f"unusual happened in that session — and if the unusual thing "
        f"recurs, the cost compounds.\n\n"
        f"Diagnosis. Session `{sid}` ran from {first_ts} to "
        f"{last_ts}, accumulating ${cost:.2f} across "
        f"{event_count} cost events on {kinds_str}. Median for this "
        f"bot is ${median:.2f}; this session is {ratio:.1f}× that.\n\n"
        f"Where to look. Open the Sessions page filtered to this "
        f"bot and click session {sid[:8]}. The trigger-kind + "
        f"tool-use breakdown is the fastest way to see whether the "
        f"session got stuck in a loop (same tool called repeatedly), "
        f"spawned a runaway sub-agent (look for subagent invocations "
        f"in the parent turn), or hit a retry storm (failing tool "
        f"called many times).\n\n"
        f"What could go wrong. If the session was a deliberate "
        f"long-running task (a deep research, a multi-step refactor), "
        f"the cost was intended — no action needed; the signal will "
        f"auto-resolve once it falls out of the recent-window median."
    )
    context = (
        f"Session `{sid}` on bot {bot_name} cost ${cost:.2f} on "
        f"{kinds_str}, {ratio:.1f}× the bot's recent median session cost "
        f"of ${median:.2f}. Spanned {event_count} cost events from "
        f"{first_ts} to {last_ts}.\n\n"
        f"Common causes worth checking:\n"
        f"  - **Stuck loop**: a heartbeat or background turn repeatedly "
        f"hitting the same tool with the same args; check the session "
        f"transcript for repeated tool_use blocks\n"
        f"  - **Runaway subagent**: a sub-agent task that exploded in "
        f"scope; look for subagent invocations in the parent turn\n"
        f"  - **Retry storm**: a failing tool retried many times "
        f"(check tool_result errors)\n"
        f"  - **Genuine large session**: a long deep-research or refactor "
        f"task — if this was intentional, no action needed; the signal "
        f"will auto-resolve once it falls out of the window.\n\n"
        f"To inspect: open the session in the admin UI's Cost > Sessions "
        f"view and look at the trigger_kind + tool_use breakdown."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"session_token_outlier:{bot_id}/{sid}"],
        provenance=Provenance(
            technique="efficiency_hawk.session_token_outlier",
            signals={
                "session_id": sid,
                "cost_usd": cost,
                "median_session_cost_usd": median,
                "ratio": ratio,
                "event_count": event_count,
                "trigger_kinds": kinds,
            },
            confidence=0.85,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase C-5 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Sessions page",
        manual_path=f"Cost → Sessions → {bot_name} → {sid[:8]}",
        dismiss_signature=DISMISS_SIG_SESSION_TOKEN_OUTLIER,
        dismiss_scope="kind",
        # Per-finding surface override — **the screenshot finding**.
        # A single session that cost N× the median is anomaly
        # triage, not RSI: no pattern, no objective, no material change
        # proposed. Routes to Alerts. See internal/spec-rsi-proposal-
        # eligibility-2026-06-05.md (audit row:
        # make_session_token_outlier_proposal).
        surface="firing",
    )


def _cheap_tier_model(bot_id: str) -> str | None:
    """Resolve the bot's cheap-tier (tier3) model for remediation copy.

    Routes through the pod's tier system (``models.resolve_tier``) so
    operator tier3 swaps — and non-Anthropic pods — get the right model
    in the suggested heartbeat override. Returns None when resolution is
    unavailable; callers degrade the copy to a placeholder rather than
    presume a provider (provider-agnostic principle).
    """
    try:
        from evolve_config import load_config  # noqa: E402
        from models import resolve_tier  # noqa: E402
        return resolve_tier("tier3", load_config(), bot_id=bot_id)
    except Exception:
        return None


def make_heartbeat_no_model_override_proposal(signal: Any) -> Proposal:
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    primary = details.get("primary_model") or "?"
    every = details.get("heartbeat_every") or "?"
    cheap_model = _cheap_tier_model(bot_id) or (
        "<your provider's cheap-tier model — see AI Optimization>"
    )

    problem = (
        f"{bot_name}: heartbeat (every {every}) runs on primary model "
        f"({primary})"
    )
    headline = f"Route {bot_name}'s heartbeat to a cheaper model"

    # ── Phase C-5 operator-first content (Tier 5 — paste-to-bot) ────────────
    summary = (
        f"{bot_name}'s heartbeat fires every {every} but has no model "
        f"override set, so each heartbeat runs on the bot's primary "
        f"model ({primary}). At primary-model rates that's around "
        f"$0.05 to $0.20 per heartbeat; on the cheap tier it's cents. "
        f"The work is mechanical — the cheap tier handles it fine."
    )
    explanation = (
        f"Heartbeats are the bot waking itself up on a fixed cadence "
        f"to do bookkeeping — context checks, file scans, exec status. "
        f"The work is short and structurally simple. By default the "
        f"heartbeat runs on the bot's primary model, which is the "
        f"model picked for user-facing conversation quality. That's "
        f"more model than a heartbeat needs.\n\n"
        f"Diagnosis. {bot_name}'s heartbeat block in openclaw.json "
        f"doesn't specify a model, so it inherits from "
        f"`agents.defaults.model.primary` ({primary}). Heartbeats "
        f"that should cost cents are instead costing what a user turn "
        f"costs.\n\n"
        f"What to do. Paste the instruction below to the bot. It can "
        f"add the override (cheap-tier model + `isolatedSession` + "
        f"`lightContext`) and redeploy itself via "
        f"`sudo evolve-admin deploy {bot_id}`. The primary model is "
        f"unchanged — only heartbeats route to the cheap tier.\n\n"
        f"What could go wrong. If the bot's heartbeat genuinely "
        f"needs the primary model for some reason on this bot, "
        f"setting the override to the same primary model explicitly "
        f"silences this signal (the engine sees the choice is "
        f"deliberate). Pick the cheapest tier that does the job."
    )

    manual_instruction = (
        f"Your heartbeat is running on the primary model ({primary}). "
        f"Add a cheap-tier override to your heartbeat block in "
        f"openclaw.json:\n\n"
        f"```json\n"
        f"\"heartbeat\": {{\n"
        f"  \"every\": \"{every}\",\n"
        f"  \"isolatedSession\": true,\n"
        f"  \"lightContext\": true,\n"
        f"  \"model\": \"{cheap_model}\"\n"
        f"}}\n"
        f"```\n\n"
        f"Then redeploy yourself (`sudo evolve-admin deploy {bot_id}`) "
        f"so OpenClaw picks up the new config. If the heartbeat "
        f"genuinely needs the primary model on this bot, explain why "
        f"and set the override to the same primary model explicitly "
        f"so the choice is visible."
    )
    context = (
        f"{bot_name}'s heartbeat fires every {every} but has no model "
        f"override in `agents.defaults.heartbeat`, so each heartbeat "
        f"session runs on the bot's primary model "
        f"(`{primary}`). At primary-model rates a heartbeat can easily "
        f"cost $0.05-$0.20; on the cheap tier it's cents.\n\n"
        f"Heartbeats are mechanical — context check, file scan, exec "
        f"status — and the cheap tier handles them fine.\n\n"
        f"**Fix:** edit `/Users/{bot_id}/.openclaw/openclaw.json`, "
        f"add to the heartbeat block:\n\n"
        f"```json\n"
        f"\"heartbeat\": {{\n"
        f"  \"every\": \"{every}\",\n"
        f"  \"isolatedSession\": true,\n"
        f"  \"lightContext\": true,\n"
        f"  \"model\": \"{cheap_model}\"\n"
        f"}}\n"
        f"```\n\n"
        f"Then redeploy the bot (`sudo evolve-admin deploy {bot_id}`) "
        f"so openclaw picks up the new config. The bot's primary model "
        f"is unchanged — only heartbeats route to the cheap tier.\n\n"
        f"If the heartbeat genuinely needs the primary model for some "
        f"reason on this bot, set the override explicitly to the same "
        f"primary model — making the choice visible silences this signal."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"heartbeat_no_model_override:{bot_id}"],
        provenance=Provenance(
            technique="efficiency_hawk.heartbeat_no_model_override",
            signals={
                "primary_model": primary,
                "heartbeat_every": every,
                "light_context": details.get("light_context"),
                "isolated_session": details.get("isolated_session"),
            },
            confidence=0.95,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase C-5 operator-first content (Tier 5 — paste-to-bot) ────
        summary=summary,
        explanation=explanation,
        manual_instruction=manual_instruction,
        dismiss_signature=DISMISS_SIG_HEARTBEAT_NO_OVERRIDE,
        dismiss_scope="kind",
        # Per-finding surface override — heartbeat-no-override is a
        # sys-admin config check ("set this knob"), not RSI. Routes to
        # Alerts. See internal/spec-rsi-proposal-eligibility-2026-06-05.md
        # (audit row: make_heartbeat_no_model_override_proposal).
        surface="drift",
    )


# ── Dispatch ──────────────────────────────────────────────────────────────────

# Map cost_watchdog signal type → proposal factory. Signal types not in
# this map are silently ignored — keeps the generator forward-compatible
# with new monitor types it doesn't yet know about.
SIGNAL_TYPE_TO_FACTORY: dict[str, Callable[[Any], Proposal]] = {
    "daily_spend_high": make_daily_spend_proposal,
    "automation_dominance": make_automation_dominance_proposal,
    "cron_wakes_agent": make_cron_wakes_agent_proposal,
    "cron_overactive": make_cron_overactive_proposal,
    "context_bloat": make_context_bloat_proposal,
    "session_token_outlier": make_session_token_outlier_proposal,
    "heartbeat_no_model_override": make_heartbeat_no_model_override_proposal,
}
