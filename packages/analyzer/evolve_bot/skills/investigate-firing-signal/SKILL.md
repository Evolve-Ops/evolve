---
name: investigate-firing-signal
description: >
  Recipe for triaging a single firing signal end-to-end — read the signal,
  pull related context (audit findings, bot state), decide what to do, and
  either propose / act / explain. Use when the operator asks "what's wrong
  with X?" or when you spot a firing signal that needs handling.
metadata:
  evolve:
    authored_by: evolve
    authored_at: "2026-05-19T00:00:00Z"
    # No obviated_by — this skill composes existing tools rather than
    # filling a tool gap. There's no single tool that obviates it.
---

# Investigate a firing signal end-to-end

When you encounter a firing signal in `pod_state(query="signals.firing")` and the
operator wants triage (or you're proactively offering one), follow this
recipe:

## Steps

1. **Read the signal.** `pod_state(query="signals.firing")` returns the signal's
   id, type, severity, bot_id, title, body, and any structured `details`.
   The body is operator-facing prose; the details are machine-readable
   fields the producer attached.

2. **Pull related context based on the signal's flavor:**

   - **`security` flavor** (permission_monitor, security_warden,
     mcp_admin) → call `pod_state(query="audit")` for the bot. Audit findings
     overlap with signals and add structured remediation prose.

   - **`platform` flavor** (sysadmin_watchdog, heal, watchdog) → call
     `pod_state(query="bots")` for the affected bot. Status, gateway probe,
     daemon health — same view the dashboard's bot tile shows.

   - **`cost` flavor** (budget_hawk, cost_watchdog) → call
     `pod_state(query="usage")` for the bot. Per-bot spend rollup + projected
     month-end.

   - **`reliability` flavor** (gateway_diagnostician, error_reporter)
     → call `pod_state(query="errors")` for the bot. Recent raw error log
     lines from heal.py's snapshot.

3. **Check for a matching proposal.** `pod_state(query="proposals.pending")`
   filtered by `bot_id` shows what's already in the queue. If the
   signal has a generator that handles it (cron_caps_filler,
   auth_drift_filler, etc.), a proposal may already exist. Mention
   the proposal_id; offer `proposal_action(action="apply")`.

4. **Decide the next step:**

   - **Proposal exists** → describe it and offer to apply it (per the
     resolver-pattern teaching in AGENTS.md).

   - **No proposal, direct tool exists** (e.g. `bot_action(action="restart")`,
     `signal_action(action="snooze")`) → describe what you'd run and offer to
     run it.

   - **Neither** → log a tool gap via `action.evo.log_tool_gap` so the
     gap is captured (per spec §14.3), and offer to stage a one-off
     proposal (per spec §13.4 Q4) if the change is well-scoped.

5. **Verify after action.** Every action tool you call returns a
   `verify_via` hint pointing at the read tool that confirms the
   change took effect. Call it. The cite-the-tool rule (AGENTS.md
   §3.7 lever #2) applies: claim "team-bot-a is back online" only after
   `pod_state(query="bots", bot_id='team-bot-a')` reports status=online.

## Common pitfalls

- **Don't paraphrase signal body verbatim.** The body is operator-
  facing already; your job is to add value — show related context,
  propose action, recommend confidence.

- **Don't route to the admin UI.** The §13.6 resolver pattern is
  explicit: never tell the operator to navigate. Act in chat.

- **Cite every claim.** "Looking at `pod_state(query="bots", bot_id='team-bot-a')`,
  the gateway is unreachable" is good; "team-bot-a's gateway is unreachable"
  with no citation is fragile.

## Example flow

> Operator: "evo, what's wrong with team-bot-a?"
>
> 1. `pod_state(query="signals.firing", bot_id='team-bot-a')` → one signal,
>    `perm_cron_uncapped_agent_turn` on the morning-brief job.
> 2. Signal flavor is `security` → `pod_state(query="audit", bot_id='team-bot-a')`
>    confirms no other findings; the cron-caps issue is the only one.
> 3. `pod_state(query="proposals.pending", bot_id='team-bot-a')` → one matching
>    proposal from `cron_caps_filler` (action_kind=UpsertCronJob,
>    sets maxTurns=20 + maxBudgetUsd=$1.00).
> 4. Reply: "team-bot-a's morning-brief cron is missing budget caps. There's
>    a proposal queued (p-abc12345) to set maxTurns=20 and
>    maxBudgetUsd=$1.00 — same defaults the admin UI uses. Want me
>    to apply it?"
> 5. On confirm: `proposal_action(action="apply", proposal_id='p-abc12345')` →
>    verify via `pod_state(query="signals.firing", bot_id='team-bot-a')` showing the
>    signal no longer appears.
