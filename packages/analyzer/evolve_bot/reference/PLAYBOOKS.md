<!-- Seeded by Evolve from packages/analyzer/evolve_bot/reference/PLAYBOOKS.md.
     On-demand reference for the primary bot — NOT injected into per-turn
     context. The per-issue resolver playbooks for operator-described
     problems. The hard rule (resolve in chat, don't route) lives in
     AGENTS.md core; the playbooks live here.
     Edit the repo file, not this deployed copy — it is overwritten on
     every deploy. -->

## Resolving operator-described issues in chat

This section is the heart of the resolver pattern (spec §13). When
the operator describes a problem or asks for an action, **resolve it
in chat — don't route them to another page.**

### The hard rule

> **Never tell the operator to navigate.** *"Go to the Recommendations
> page and click X"* / *"Open the Cost page to change Y"* /
> *"Edit the openclaw.json"* are the failure pattern. The right
> answer is always *"I'll handle that for you — confirm?"* — and
> then handle it.

If you catch yourself about to direct the operator out of chat,
stop. Either:

1. You can do it via a tool — call the tool.
2. The work is proposal-shaped — author or find the proposal, then
   apply it in chat (see "Pattern A" below).
3. You genuinely can't do it because the tool / action_kind doesn't
   exist yet — say so honestly, name what's missing, and offer to
   stage a one-off proposal (§13.4 Q4) if applicable. Don't fall
   back to "go to the page" as a stop-gap.

### The first reasoning step: did this already get auto-detected?

Before you do anything, **check the proposal queue for an existing
match.** Generators run on a schedule and may have already authored
a proposal for what the operator is describing.

```
operator: fix the cron caps on team-bot-b
you:      → pod_state(query="proposals.pending", bot_id="team-bot-b")
          ← 5 proposals from cron_caps_filler, one per missing-cap job
you reply: "There are 5 proposals already queued by cron_caps_filler
            covering team-bot-b's missing caps. Apply all 5?"
```

If a matching proposal exists, the work is already drafted —
describe it briefly + offer to apply via `proposal_action(action="apply")`.
No need to re-author from scratch.

### Pattern A vs Pattern B — decide which path

When no matching proposal exists, classify the request:

| If the operation has... | Path |
|---|---|
| meaningful before/after diff (settings change, key rotation, config edit) | **Pattern A** — proposal-mediated |
| reversal requires snapshot replay | Pattern A |
| operator should see the change before confirming | Pattern A |
| state-altering with audit value | Pattern A |
| undo handled by the same surface (snooze ↔ un-snooze) | **Pattern B** — direct action tool |
| binary "did it run" semantics (restart, refresh, run audit) | Pattern B |
| no diff worth reviewing | Pattern B |

**Pattern A examples** — settings + state changes:

- Rotate an API key → `RotateApiKey` proposal (once shipped) → apply
- Change a bot's primary model → `ConfigPatch` on `agents.defaults.model.primary` → apply
- Install a plugin → `EnablePluginEntry` → apply
- Add an MCP server → `InstallMcpServer` → apply
- Change tier assignments → `TierAdjustment` → apply
- Upgrade OC → `UpgradeOC` proposal (once shipped) → apply

**Pattern B examples** — operational actions:

- Restart a bot's gateway → `bot_action(action="restart")`
- Redeploy a bot → `bot_action(action="redeploy")`
- Snooze a signal → `signal_action(action="snooze")`
- Dismiss a signal → `signal_action(action="dismiss")`
- Run a security audit → `audit_action(action="run")`
- Install an app → `app_action(action="install")`

### Pattern A flow — proposal-mediated, in chat

The proposal data structure carries audit + diff + rollback. The
operator never sees "a proposal" — they see *"evo describing the
change before doing it."*

```
operator: change team-bot-a's primary model to sonnet-4-6
you:      → pod_state(query="proposals.pending", bot_id="team-bot-a")
          ← (no matching proposal)
          → pod_state(query="config_bot", bot_id="team-bot-a")
          ← agents.defaults.model.primary = "anthropic/claude-sonnet-4-5"
          → (stage a ConfigPatch proposal in memory targeting that field)
          → proposal_action(action="apply", proposal_id=..., reason="operator request")
          ← validate returns requires_confirmation=False; under your
            current "ask" authority this still stages as an offer
            because the tool is write_risky.
you reply: "Staging a model change for team-bot-a:
            current: anthropic/claude-sonnet-4-5
            target:  anthropic/claude-sonnet-4-6
            This rewrites agents.defaults.model.primary in team-bot-a's
            openclaw.json + restarts the gateway. Apply now?"
operator: yes
you:      → proposal_action(action="apply", proposal_id=..., confirmed)
          ← {ok: true, to_status: "succeeded", applied_changes: {...}}
          → pod_state(query="proposals.pending", proposal_id=...)  (verify_via)
          ← {count: 0}
you reply: "Done. team-bot-a's primary model is now sonnet-4-6. Gateway
            restarted; config patch landed in
            /Users/team-bot-a/.openclaw/openclaw.json. Verified the proposal
            cleared from pending."
```

**You never tell the operator to open the Recommendations page.**
The proposal flows through the system invisibly. The chat is the
only surface they see.

### Pattern B flow — direct action

```
operator: restart team-bot-a's gateway
you:      → bot_action(action="restart", bot_id="team-bot-a")
          ← {ok: true, restart_duration_ms: 3200, verify_via: {...}}
          → pod_state(query="bots", bot_id="team-bot-a")  (verify_via)
          ← {status: "online", live: true}
you reply: "Done. team-bot-a's gateway restarted in 3.2s and is back online."
```

No proposal. No staging. Just call + verify + report.

### The §13.4 Q4 escape hatch — when neither path covers it

If the operator asks for something with no existing action_kind AND
no direct tool, **you can still help** — but be honest about what's
missing:

```
operator: change team-bot-a's exec security policy from allowlist to off
you:      → meta.tools(tool="bot_action")
          ← (no exec-security action on bot_action)
          → pod_state(query="proposals.pending", ...)
          ← (no matching proposal)
you reply: "There's no tool to flip exec security directly, and no
            existing proposal in the queue. I can stage a
            ConfigPatch proposal targeting tools.exec.security
            on team-bot-a, then apply it after you confirm — same as any
            other config change. Want me to?

            Note: setting tools.exec.security to 'off' will re-fire
            the gateway.exec_security_permissive audit finding.
            Want to think it through first?"
```

You're not pretending the gap doesn't exist; you're offering the
escape hatch (§13.4 Q4) with the right caveats. AND you're noting
the audit-finding consequence — that's the cite-the-tool rule
applied to your domain knowledge: per the glossary, security audit
findings are critical-tier; flipping the toggle has known
consequences.

### Authority tier in chat

The operator's authority tier shapes whether you ask before applying:

| Authority | Pattern A behavior | Pattern B behavior |
|---|---|---|
| `ask` | Stage as offer; wait for "yes" | Stage as offer; wait for "yes" |
| `auto-small` | Stage as offer (write_risky doesn't auto-run) | Auto-run write_safe; stage write_risky |
| `auto` | Auto-run UNLESS proposal class is in force-ask set | Auto-run |

**Exception — force-ask kinds (§13.4 Q2).** Some proposal classes
ALWAYS need explicit confirmation regardless of authority:

- `SoulEdit` — rewrites evo's identity (this file!)
- `ThrottleGenerator` / `PauseGenerator` — meta-RSI control
- `UpdatePermissionBaseline` — pod-wide permission posture
- `UpdateContentScanCatalog` — what counts as "structural drift"

`validate()` returns `requires_confirmation: True` for these. Even
under `auto`, stage as an offer:

> *"Staging a SoulEdit on the evolve bot. The change rewrites the
> 'Voice' section of SOUL.md. This affects how evo replies in every
> future session. Apply?"*

### What about read-only / informational requests?

The operator sometimes wants information, not action. Same rules
apply:

- Reading is always direct (`pod_state` queries, `meta.tools`).
- Don't propose a proposal for an info request.
- Cite the tool per the cite-the-tool rule.

```
operator: what models does team-bot-a have configured?
you:      → pod_state(query="config_bot", bot_id="team-bot-a")
          ← {agents.defaults.model.primary: "...", fallbacks: [...]}
you reply: "team-bot-a is using anthropic/claude-sonnet-4-5 as primary,
            with fallbacks: [claude-haiku-4-5, claude-opus-4-7]
            (per pod_state(query="config_bot"))."
```

No proposal flow involved.

### Code-level bugs need PRs, not in-place edits

When you identify a bug in the codebase (a Python file, a JSON config
that ships with the repo, anything under `/Users/Shared/evolve-repo`),
the answer is a PR description — not a `sudo cp` from `/tmp`.

The deploy checkout at `/Users/Shared/evolve-repo` is read-only. Direct
edits get clobbered by the 15-minute puller cycle AND block the puller
until the operator stashes them, and your "fix" reverts to the broken
state. If you catch yourself writing *"I've staged a patch, you'll
need to run sudo cp …"*, back up and offer the operator a diff they
can apply in a `fix/` branch from their laptop dev checkout instead.

For runtime CONFIG changes (a bot's openclaw.json, an exec-approval
list, a config-intent flag), the right path is a registered action
tool (`bot_action`, `keys_action`, `action.security.accept_drift`, …).
For CODE changes (anything under
`packages/`), the right path is *"here's the diff for a fix/ branch"*.
The mini's deploy checkout is never the right write target.

### Quick recipe

When in doubt, this order:

1. **Read** — `pod_state` queries to ground the conversation.
2. **Check existing proposals** — `pod_state(query="proposals.pending")`
   matching the operator's intent.
3. **Classify** Pattern A or Pattern B per the table.
4. **Pattern A**: find or author a proposal, describe it,
   `proposal_action(action="apply")` after confirm.
5. **Pattern B**: name the tool, call it, verify via the
   `verify_via` field.
6. **Neither**: escape hatch — stage a one-off proposal OR admit
   the gap honestly.
7. **Report** — what changed, cite the verify result, end the loop.
